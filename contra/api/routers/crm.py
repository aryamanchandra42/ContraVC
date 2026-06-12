"""CRM leads API — list, promote, gate add, rank updates."""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from api.deps import get_db
import math
import re
from datetime import datetime

from contra.crm.models import CrmIcpQueueItem, CrmLead, CrmLeadUpdate, CrmManualAdd, CrmPromoteRequest, CrmProspect
from contra.crm.writer import (
    add_lead_from_gate,
    get_lead_by_id,
    preview_lead_from_gate,
    promote_prospect,
    upsert_manual_lead,
)

router = APIRouter()


class GateAddToCrmRequest(BaseModel):
    session_id: str
    preview_only: bool = False
    override: bool = False


class GateAddToCrmResponse(BaseModel):
    lead: Optional[CrmLead] = None
    preview: Optional[Dict[str, Any]] = None


def _parse_contacts(val: Any) -> Optional[dict]:
    if val is None:
        return None
    if isinstance(val, str):
        try:
            return json.loads(val)
        except json.JSONDecodeError:
            return None
    return val


def _safe_float(val: Any) -> Optional[float]:
    """Return None for NaN/inf/None — all of which are JSON-invalid."""
    if val is None:
        return None
    try:
        f = float(val)
        import math
        return None if (math.isnan(f) or math.isinf(f)) else f
    except (TypeError, ValueError):
        return None


def _workspace_row_to_lead(row: tuple, cols: List[str]) -> CrmLead:
    data = dict(zip(cols, row))
    for ts in ("created_at", "updated_at"):
        if data.get(ts) is not None:
            data[ts] = str(data[ts])
    return CrmLead(
        lead_id=str(data["lead_id"]),
        investor_name=data["investor_name"],
        name_key=data.get("name_key", ""),
        allocator_id=data.get("allocator_id"),
        source=data["source"],
        status=data["status"],
        investor_type=data.get("investor_type"),
        investor_location=data.get("investor_location"),
        investor_details=data.get("investor_details"),
        contacts_json=_parse_contacts(data.get("contacts_json")),
        pipeline_stage=data.get("pipeline_stage"),
        computed_score=_safe_float(data.get("computed_score")),
        manual_rank=int(data["manual_rank"]) if data.get("manual_rank") is not None else None,
        effective_rank=int(data["effective_rank"]) if data.get("effective_rank") is not None else None,
        gate_session_id=data.get("gate_session_id"),
        gate_verdict=data.get("gate_verdict"),
        gate_confidence=data.get("gate_confidence"),
        gate_summary=data.get("gate_summary"),
        icp_tier=data.get("icp_tier"),
        fit_score=_safe_float(data.get("fit_score")),
        contra_rank=int(data["contra_rank"]) if data.get("contra_rank") is not None else None,
        warm_path_count=int(data["warm_path_count"]) if data.get("warm_path_count") is not None else None,
        syndicate_score=_safe_float(data.get("syndicate_score")),
        needs_enrichment=bool(data.get("needs_enrichment")),
        created_at=data.get("created_at"),
        updated_at=data.get("updated_at"),
    )


@router.post("/gate/add-to-crm", response_model=GateAddToCrmResponse)
def gate_add_to_crm(req: GateAddToCrmRequest, con=Depends(get_db)) -> GateAddToCrmResponse:
    try:
        if req.preview_only:
            preview = preview_lead_from_gate(con, req.session_id, override=req.override)
            return GateAddToCrmResponse(preview=preview)
        lead = add_lead_from_gate(con, req.session_id, override=req.override)
        return GateAddToCrmResponse(lead=lead)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"{type(exc).__name__}: {exc}") from exc


@router.get("/crm/leads", response_model=List[CrmLead])
def list_leads(
    source: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    search: Optional[str] = Query(None),
    sort: str = Query("rank"),
    con=Depends(get_db),
) -> List[CrmLead]:
    sql = "SELECT * FROM v_crm_workspace WHERE 1=1"
    params: List[Any] = []
    if source:
        sql += " AND source = ?"
        params.append(source)
    if status:
        sql += " AND status = ?"
        params.append(status)
    if search:
        sql += " AND investor_name ILIKE ?"
        params.append(f"%{search}%")

    if sort == "fit_score":
        sql += " ORDER BY fit_score DESC NULLS LAST"
    elif sort == "computed_score":
        sql += " ORDER BY computed_score DESC NULLS LAST"
    elif sort == "name":
        sql += " ORDER BY investor_name ASC"
    else:
        sql += " ORDER BY COALESCE(manual_rank, 9999) ASC, computed_score DESC NULLS LAST"

    rows = con.execute(sql, params).fetchall()
    cols = [d[0] for d in con.description]
    return [_workspace_row_to_lead(r, cols) for r in rows]


@router.get("/crm/prospects", response_model=List[CrmProspect])
def list_prospects(
    source: Optional[str] = Query(None),
    top: int = Query(100, ge=1, le=500),
    con=Depends(get_db),
) -> List[CrmProspect]:
    # Load dismissed name_keys to filter them out
    dismissed_keys: set[str] = {
        r[0] for r in con.execute("SELECT name_key FROM crm_dismissed").fetchall()
    }

    sql = """
        SELECT allocator_id, investor_name, investor_type, investor_location,
               icp_tier, fit_score, contra_rank, warm_path_count,
               syndicate_score, suggested_source, prospect_score
        FROM v_crm_prospects
        WHERE 1=1
    """
    params: List[Any] = []
    if source:
        sql += " AND suggested_source = ?"
        params.append(source)
    sql += " ORDER BY prospect_score DESC NULLS LAST LIMIT ?"
    params.append(top)

    rows = con.execute(sql, params).fetchall()
    out: List[CrmProspect] = []
    seen: set[str] = set()
    for r in rows:
        name = r[1]
        if name in seen:
            continue
        if _make_name_key(name) in dismissed_keys:
            continue
        seen.add(name)
        out.append(CrmProspect(
            allocator_id=str(r[0]) if r[0] else None,
            investor_name=name,
            investor_type=r[2],
            investor_location=r[3],
            icp_tier=r[4],
            fit_score=_safe_float(r[5]),
            contra_rank=int(r[6]) if r[6] is not None else None,
            warm_path_count=int(r[7]) if r[7] is not None else None,
            syndicate_score=_safe_float(r[8]),
            suggested_source=r[9] or "icp",
            prospect_score=_safe_float(r[10]),
        ))
    return out


@router.get("/crm/enrichment", response_model=List[Dict[str, Any]])
def list_enrichment(con=Depends(get_db)) -> List[Dict[str, Any]]:
    rows = con.execute(
        """
        SELECT lead_id, investor_name, allocator_id, source, investor_type,
               investor_location, needs_enrichment, icp_tier, fit_score, record_type
        FROM v_crm_needs_enrichment
        ORDER BY fit_score DESC NULLS LAST
        LIMIT 200
        """
    ).fetchdf()
    return rows.to_dict(orient="records")


@router.get("/crm/icp-queue", response_model=List[CrmIcpQueueItem])
def list_icp_queue(
    readiness: Optional[str] = Query(None, description="READY | NEAR_READY | PENDING"),
    gate_status: str = Query("all", description="needs_gate | gated | all"),
    top: int = Query(150, ge=1, le=500),
    con=Depends(get_db),
) -> List[CrmIcpQueueItem]:
    """ICP-sourced LP discovery queue.

    Returns institutional prospects that passed ICP scoring but are not yet in
    CRM, with readiness labels and any prior Gate screening result.
    """
    sql = """
        SELECT allocator_id, investor_name, allocator_type, investor_location,
               icp_tier, fit_score, client_decision, client_status, core_pass,
               warm_path_count, readiness, gate_verdict, gate_session_id,
               gate_reviewed_at
        FROM v_crm_icp_queue
        WHERE 1=1
    """
    params: List[Any] = []

    if readiness:
        sql += " AND readiness = ?"
        params.append(readiness.upper())

    if gate_status == "needs_gate":
        sql += " AND gate_verdict IS NULL"
    elif gate_status == "gated":
        sql += " AND gate_verdict IS NOT NULL"

    sql += """
        ORDER BY
            CASE readiness
                WHEN 'READY'      THEN 1
                WHEN 'NEAR_READY' THEN 2
                ELSE 3
            END,
            fit_score DESC NULLS LAST
        LIMIT ?
    """
    params.append(top)

    rows = con.execute(sql, params).fetchall()
    out: List[CrmIcpQueueItem] = []
    for r in rows:
        fit = _safe_float(r[5])
        out.append(CrmIcpQueueItem(
            allocator_id=str(r[0]) if r[0] else None,
            investor_name=r[1],
            allocator_type=r[2],
            investor_location=r[3],
            icp_tier=r[4],
            fit_score=fit,
            client_decision=r[6],
            client_status=r[7],
            core_pass=bool(r[8]) if r[8] is not None else None,
            warm_path_count=int(r[9]) if r[9] is not None else None,
            readiness=r[10] or "PENDING",
            gate_verdict=r[11] or None,
            gate_session_id=r[12] or None,
            gate_reviewed_at=str(r[13]) if r[13] else None,
        ))
    return out


@router.post("/crm/leads", response_model=CrmLead)
def create_lead(body: CrmManualAdd, con=Depends(get_db)) -> CrmLead:
    try:
        return upsert_manual_lead(con, body)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/crm/leads/promote", response_model=CrmLead)
def promote_lead(body: CrmPromoteRequest, con=Depends(get_db)) -> CrmLead:
    try:
        return promote_prospect(
            con, body.allocator_id, source=body.source, investor_name=body.investor_name
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.delete("/crm/leads/{lead_id}", status_code=204)
def delete_lead(lead_id: str, con=Depends(get_db)) -> None:
    existing = con.execute(
        "SELECT lead_id FROM crm_leads WHERE lead_id = ?", [lead_id]
    ).fetchone()
    if not existing:
        raise HTTPException(status_code=404, detail="Lead not found")
    con.execute("DELETE FROM crm_leads WHERE lead_id = ?", [lead_id])


def _make_name_key(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower().strip())


class DismissRequest(BaseModel):
    investor_name: str
    reason: str = "dismissed"
    note: Optional[str] = None
    remove_from_crm: bool = True


class DismissedEntry(BaseModel):
    id: str
    investor_name: str
    reason: str
    note: Optional[str] = None
    dismissed_at: str


@router.post("/crm/dismiss", response_model=DismissedEntry, status_code=201)
def dismiss_lead(body: DismissRequest, con=Depends(get_db)) -> DismissedEntry:
    """Dismiss a prospect/lead: removes from CRM (if present) and hides from upgrade queue."""
    name_key = _make_name_key(body.investor_name)

    # Remove from crm_leads if present
    if body.remove_from_crm:
        con.execute("DELETE FROM crm_leads WHERE name_key = ?", [name_key])

    # Upsert into dismissed (replace if already dismissed)
    existing = con.execute(
        "SELECT id FROM crm_dismissed WHERE name_key = ?", [name_key]
    ).fetchone()
    if existing:
        con.execute(
            "UPDATE crm_dismissed SET reason=?, note=?, dismissed_at=NOW() WHERE name_key=?",
            [body.reason, body.note, name_key],
        )
        row = con.execute(
            "SELECT id, investor_name, reason, note, dismissed_at FROM crm_dismissed WHERE name_key=?",
            [name_key],
        ).fetchone()
    else:
        con.execute(
            "INSERT INTO crm_dismissed (investor_name, name_key, reason, note) VALUES (?,?,?,?)",
            [body.investor_name, name_key, body.reason, body.note],
        )
        row = con.execute(
            "SELECT id, investor_name, reason, note, dismissed_at FROM crm_dismissed WHERE name_key=?",
            [name_key],
        ).fetchone()

    return DismissedEntry(
        id=str(row[0]),
        investor_name=row[1],
        reason=row[2],
        note=row[3],
        dismissed_at=str(row[4]),
    )


@router.get("/crm/dismissed", response_model=List[DismissedEntry])
def list_dismissed(con=Depends(get_db)) -> List[DismissedEntry]:
    rows = con.execute(
        "SELECT id, investor_name, reason, note, dismissed_at FROM crm_dismissed ORDER BY dismissed_at DESC"
    ).fetchall()
    return [
        DismissedEntry(id=str(r[0]), investor_name=r[1], reason=r[2], note=r[3], dismissed_at=str(r[4]))
        for r in rows
    ]


@router.delete("/crm/dismissed/{investor_name}", status_code=204)
def restore_dismissed(investor_name: str, con=Depends(get_db)) -> None:
    """Restore a dismissed lead — removes from dismissed list so it reappears in prospects."""
    name_key = _make_name_key(investor_name)
    con.execute("DELETE FROM crm_dismissed WHERE name_key = ?", [name_key])


@router.patch("/crm/leads/{lead_id}", response_model=CrmLead)
def update_lead(lead_id: str, body: CrmLeadUpdate, con=Depends(get_db)) -> CrmLead:
    existing = con.execute(
        "SELECT lead_id FROM crm_leads WHERE lead_id = ?", [lead_id]
    ).fetchone()
    if not existing:
        raise HTTPException(status_code=404, detail="Lead not found")

    updates = []
    params: List[Any] = []
    if body.manual_rank is not None:
        updates.append("manual_rank = ?")
        params.append(body.manual_rank)
    if body.status is not None:
        updates.append("status = ?")
        params.append(body.status)
    if body.pipeline_stage is not None:
        updates.append("pipeline_stage = ?")
        params.append(body.pipeline_stage)
    if body.investor_details is not None:
        updates.append("investor_details = ?")
        params.append(body.investor_details)

    if updates:
        updates.append("updated_at = NOW()")
        params.append(lead_id)
        con.execute(
            f"UPDATE crm_leads SET {', '.join(updates)} WHERE lead_id = ?",
            params,
        )

    return get_lead_by_id(con, lead_id)


# ---------------------------------------------------------------------------
# LP dossier — durable institutional memory per LP
# ---------------------------------------------------------------------------

@router.get("/crm/dossier/{investor_name}")
def get_lp_dossier(investor_name: str, con=Depends(get_db)) -> Dict[str, Any]:
    from contra.crm.dossier import get_dossier

    dossier = get_dossier(con, investor_name)
    if not dossier:
        raise HTTPException(status_code=404, detail=f"No dossier for '{investor_name}'")
    return dossier


class DossierNotesUpdate(BaseModel):
    notes: str


@router.patch("/crm/dossier/{investor_name}/notes")
def update_dossier_notes(
    investor_name: str, body: DossierNotesUpdate, con=Depends(get_db)
) -> Dict[str, Any]:
    from contra.crm.dossier import get_dossier, set_analyst_notes

    set_analyst_notes(con, investor_name, body.notes)
    dossier = get_dossier(con, investor_name)
    if not dossier:
        raise HTTPException(status_code=404, detail=f"No dossier for '{investor_name}'")
    return dossier


# ---------------------------------------------------------------------------
# Outreach personalization agent
# ---------------------------------------------------------------------------

class OutreachGenerateRequest(BaseModel):
    tone: str = "warm"
    sender_name: str = ""
    extra_instructions: str = ""


class OutreachStatusUpdate(BaseModel):
    status: str  # draft | approved | sent | discarded


@router.post("/crm/leads/{lead_id}/outreach")
def generate_outreach(
    lead_id: str, body: OutreachGenerateRequest, con=Depends(get_db)
) -> Dict[str, Any]:
    from contra.crm.outreach import generate_outreach_draft

    try:
        return generate_outreach_draft(
            con, lead_id,
            tone=body.tone,
            sender_name=body.sender_name,
            extra_instructions=body.extra_instructions,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"{type(exc).__name__}: {exc}") from exc


@router.get("/crm/leads/{lead_id}/outreach")
def list_outreach(lead_id: str, con=Depends(get_db)) -> List[Dict[str, Any]]:
    from contra.crm.outreach import list_outreach_drafts

    return list_outreach_drafts(con, lead_id)


@router.patch("/crm/outreach/{draft_id}")
def update_outreach(
    draft_id: str, body: OutreachStatusUpdate, con=Depends(get_db)
) -> Dict[str, str]:
    from contra.crm.outreach import update_draft_status

    try:
        ok = update_draft_status(con, draft_id, body.status)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if not ok:
        raise HTTPException(status_code=404, detail="Draft not found")
    return {"draft_id": draft_id, "status": body.status}


# ---------------------------------------------------------------------------
# Next-best-action outreach queue
# ---------------------------------------------------------------------------

@router.get("/crm/outreach-queue")
def outreach_queue(top: int = Query(10, ge=1, le=50), con=Depends(get_db)) -> List[Dict[str, Any]]:
    """
    Ranked "contact these next" queue: active YES leads first, ordered by a
    blend of computed score, verified LP commitments, and warm paths.
    """
    rows = con.execute(
        """
        SELECT
            CAST(l.lead_id AS VARCHAR), l.investor_name, l.investor_type,
            l.investor_location, l.gate_verdict, l.gate_confidence,
            l.computed_score, l.warm_path_count, l.pipeline_stage,
            d.lp_commitments_json,
            (SELECT COUNT(*) FROM crm_outreach_drafts o
             WHERE CAST(o.lead_id AS VARCHAR) = CAST(l.lead_id AS VARCHAR)) AS draft_count
        FROM crm_leads l
        LEFT JOIN lp_dossiers d ON d.name_key = l.name_key
        WHERE l.status = 'active'
        """
    ).fetchall()

    scored: List[Dict[str, Any]] = []
    for r in rows:
        commitments = r[9]
        if isinstance(commitments, str):
            try:
                commitments = json.loads(commitments)
            except Exception:
                commitments = []
        commitments = commitments or []
        base = float(r[6] or 0)
        verdict_bonus = 30 if r[4] == "yes" else (10 if r[4] == "review" else 0)
        confidence_bonus = 10 if r[5] == "high" else 0
        commit_bonus = min(len(commitments), 3) * 15
        warm_bonus = min(int(r[7] or 0), 3) * 8
        priority = base + verdict_bonus + confidence_bonus + commit_bonus + warm_bonus

        why = []
        if commitments:
            why.append(f"{len(commitments)} verified LP commitment(s)")
        if r[4] == "yes":
            why.append(f"gate YES ({r[5]} confidence)")
        if int(r[7] or 0) > 0:
            why.append(f"{r[7]} warm path(s)")
        if not why:
            why.append("highest available score")

        scored.append({
            "lead_id": r[0],
            "investor_name": r[1],
            "investor_type": r[2],
            "investor_location": r[3],
            "gate_verdict": r[4],
            "priority_score": round(priority, 1),
            "why": "; ".join(why),
            "has_draft": int(r[10] or 0) > 0,
            "pipeline_stage": r[8],
        })

    scored.sort(key=lambda x: -x["priority_score"])
    return scored[:top]

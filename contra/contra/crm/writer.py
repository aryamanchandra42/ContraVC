"""CRM lead write operations — gate add, promote, manual insert."""

from __future__ import annotations

import json
import uuid
from typing import Any, Dict, List, Optional

from agents.normalization.crm_normalizer import norm_key
from contra.crm.extract import extract_crm_fields, merge_extraction
from contra.crm.models import CrmLead, CrmLeadExtraction, CrmManualAdd
from contra.crm.scoring import compute_score
from contra.gate.models import GateResult
from contra.gate.session import get_session
from contra.intelligence.brief import _crm_lookup


def _row_to_lead(row: tuple, cols: List[str]) -> CrmLead:
    data = dict(zip(cols, row))
    contacts = data.get("contacts_json")
    if isinstance(contacts, str):
        try:
            contacts = json.loads(contacts)
        except json.JSONDecodeError:
            contacts = None
    for ts in ("created_at", "updated_at"):
        if data.get(ts) is not None:
            data[ts] = str(data[ts])
    return CrmLead(
        lead_id=str(data["lead_id"]),
        investor_name=data["investor_name"],
        name_key=data["name_key"],
        allocator_id=data.get("allocator_id"),
        source=data["source"],
        status=data["status"],
        investor_type=data.get("investor_type"),
        investor_location=data.get("investor_location"),
        investor_details=data.get("investor_details"),
        contacts_json=contacts,
        pipeline_stage=data.get("pipeline_stage"),
        computed_score=data.get("computed_score"),
        manual_rank=data.get("manual_rank"),
        effective_rank=data.get("effective_rank"),
        gate_session_id=data.get("gate_session_id"),
        gate_verdict=data.get("gate_verdict"),
        gate_confidence=data.get("gate_confidence"),
        gate_summary=data.get("gate_summary"),
        icp_tier=data.get("icp_tier"),
        fit_score=data.get("fit_score"),
        contra_rank=data.get("contra_rank"),
        warm_path_count=data.get("warm_path_count"),
        syndicate_score=data.get("syndicate_score"),
        needs_enrichment=bool(data.get("needs_enrichment")),
        created_at=data.get("created_at"),
        updated_at=data.get("updated_at"),
    )


def _lead_exists(con, name: str) -> bool:
    """True if LP is in crm_leads or legacy crm_contacts."""
    in_crm, _ = _crm_lookup(con, name)
    return in_crm


def _in_crm_leads(con, name_key: str) -> bool:
    row = con.execute(
        "SELECT 1 FROM crm_leads WHERE name_key = ? AND status != 'passed' LIMIT 1",
        [name_key],
    ).fetchone()
    return row is not None


def _insert_lead(con, fields: Dict[str, Any], source: str) -> CrmLead:
    name = fields["investor_name"]
    key = norm_key(name)
    if _lead_exists(con, name):
        raise ValueError(f"Already in CRM: {name}")

    score = compute_score(
        fit_score=fields.get("fit_score"),
        gate_confidence=fields.get("gate_confidence"),
        warm_path_count=int(fields.get("warm_path_count") or 0),
        contra_rank=fields.get("contra_rank"),
        syndicate_score=fields.get("syndicate_score"),
    )

    lead_id = str(uuid.uuid4())
    contacts_json = fields.get("contacts_json")
    if contacts_json is not None and not isinstance(contacts_json, str):
        contacts_json = json.dumps(contacts_json)

    gate_reasons = fields.get("gate_reasons_json")
    if gate_reasons is not None and not isinstance(gate_reasons, str):
        gate_reasons = json.dumps(gate_reasons)

    appetite_json = fields.get("appetite_json")
    if appetite_json is not None and not isinstance(appetite_json, str):
        appetite_json = json.dumps(appetite_json)

    status = fields.get("status") or (
        "review" if fields.get("gate_verdict") == "review" else "active"
    )

    con.execute(
        """
        INSERT INTO crm_leads (
            lead_id, investor_name, name_key, allocator_id, source, status,
            investor_type, investor_location, investor_details, contacts_json,
            pipeline_stage, computed_score, gate_session_id, gate_verdict,
            gate_confidence, gate_summary, gate_reasons_json, appetite_json,
            icp_tier, fit_score, contra_rank, warm_path_count, syndicate_score,
            needs_enrichment, source_file, created_at, updated_at
        ) VALUES (
            ?, ?, ?, ?, ?, ?,
            ?, ?, ?, ?,
            ?, ?, ?, ?,
            ?, ?, ?, ?,
            ?, ?, ?, ?, ?,
            ?, ?, NOW(), NOW()
        )
        """,
        [
            lead_id, name, key,
            fields.get("allocator_id"), source, status,
            fields.get("investor_type"), fields.get("investor_location"),
            fields.get("investor_details"), contacts_json,
            fields.get("pipeline_stage"), score,
            fields.get("gate_session_id"), fields.get("gate_verdict"),
            fields.get("gate_confidence"), fields.get("gate_summary"),
            gate_reasons, appetite_json,
            fields.get("icp_tier"), fields.get("fit_score"),
            fields.get("contra_rank"), fields.get("warm_path_count"),
            fields.get("syndicate_score"), fields.get("needs_enrichment", False),
            fields.get("source_file"),
        ],
    )

    return get_lead_by_id(con, lead_id)


def get_lead_by_id(con, lead_id: str) -> CrmLead:
    row = con.execute(
        "SELECT * FROM v_crm_workspace WHERE lead_id = ?",
        [lead_id],
    ).fetchone()
    if not row:
        row = con.execute(
            "SELECT *, NULL AS effective_rank FROM crm_leads WHERE lead_id = ?",
            [lead_id],
        ).fetchone()
    if not row:
        raise ValueError(f"Lead not found: {lead_id}")
    cols = [d[0] for d in con.description]
    return _row_to_lead(row, cols)


def preview_lead_from_gate(con, session_id: str) -> Dict[str, Any]:
    """Extract and merge CRM fields without writing."""
    session, result, brief_dict = _load_gate_session(session_id)
    if not result.yes and not result.is_review:
        raise ValueError("Cannot add LP with NO verdict to CRM")
    if brief_dict.get("in_crm") or _lead_exists(con, result.lp_name):
        raise ValueError(f"Already in CRM: {result.lp_name}")

    extraction = extract_crm_fields(result, brief_dict)
    merged = merge_extraction(extraction, brief_dict, result)
    merged["computed_score"] = compute_score(
        fit_score=merged.get("fit_score"),
        gate_confidence=merged.get("gate_confidence"),
        warm_path_count=int(merged.get("warm_path_count") or 0),
        contra_rank=merged.get("contra_rank"),
        syndicate_score=merged.get("syndicate_score"),
    )
    return {"extraction": extraction.model_dump(), "lead": merged}


def add_lead_from_gate(con, session_id: str) -> CrmLead:
    """LLM-extract CRM fields from gate session and insert lead."""
    session = get_session(session_id)
    if session:
        from contra.gate.persist import persist_from_session
        new_id = persist_from_session(con, session)
        if new_id:
            session.brief_dict["allocator_id"] = new_id

    preview = preview_lead_from_gate(con, session_id)
    if not preview["lead"].get("allocator_id") and session:
        aid = session.brief_dict.get("allocator_id")
        if aid:
            preview["lead"]["allocator_id"] = aid
    return _insert_lead(con, preview["lead"], source="gate")


def _load_gate_session(session_id: str):
    session = get_session(session_id)
    if session is None:
        raise ValueError(f"Gate session expired or not found: {session_id}")

    result = GateResult.model_validate(session.result_dict)
    return session, result, session.brief_dict


def promote_prospect(
    con,
    allocator_id: str,
    source: str = "icp",
    investor_name: Optional[str] = None,
) -> CrmLead:
    """Promote a prospect (ICP/syndicate/benchmark) to CRM without full gate."""
    profile = con.execute(
        "SELECT * FROM v_lp_profile WHERE allocator_id = ? LIMIT 1",
        [allocator_id],
    ).fetchdf()
    syndicate = con.execute(
        "SELECT * FROM v_syndicate_profile WHERE allocator_id = ? LIMIT 1",
        [allocator_id],
    ).fetchdf()

    name = investor_name
    fields: Dict[str, Any] = {"allocator_id": allocator_id}

    if not profile.empty:
        p = profile.iloc[0]
        name = name or str(p["canonical_name"])
        fields.update({
            "investor_name": name,
            "investor_type": p.get("allocator_type"),
            "investor_location": p.get("geography"),
            "investor_details": (
                f"ICP {p.get('icp_tier') or '—'}, fit {p.get('fit_score') or '—'}. "
                f"Promoted from {source} prospects."
            ),
            "icp_tier": p.get("icp_tier"),
            "fit_score": float(p["fit_score"]) if p.get("fit_score") is not None else None,
            "contra_rank": int(p["contra_rank"]) if p.get("contra_rank") is not None else None,
            "warm_path_count": int(p["warm_path_count"]) if p.get("warm_path_count") is not None else 0,
            "needs_enrichment": not p.get("allocator_type") or not p.get("geography"),
            "pipeline_stage": "Prospect",
        })
    elif not syndicate.empty:
        s = syndicate.iloc[0]
        name = name or str(s["canonical_name"])
        fields.update({
            "investor_name": name,
            "investor_type": s.get("allocator_type"),
            "investor_location": s.get("geography"),
            "investor_details": f"Syndicate fund-LP. Promoted from {source} prospects.",
            "syndicate_score": float(s["fund_lp_behavior_score"])
                if s.get("fund_lp_behavior_score") is not None else None,
            "needs_enrichment": True,
            "pipeline_stage": "Prospect",
        })
    else:
        alloc = con.execute(
            "SELECT canonical_name, allocator_type, geography FROM allocators "
            "WHERE CAST(allocator_id AS VARCHAR) = ? LIMIT 1",
            [allocator_id],
        ).fetchone()
        if not alloc:
            raise ValueError(f"Allocator not found: {allocator_id}")
        name = name or alloc[0]
        fields.update({
            "investor_name": name,
            "investor_type": alloc[1],
            "investor_location": alloc[2],
            "investor_details": f"Promoted from {source} prospects.",
            "needs_enrichment": True,
            "pipeline_stage": "Prospect",
        })

    fields["investor_name"] = name
    return _insert_lead(con, fields, source=source)


def upsert_manual_lead(con, body: CrmManualAdd) -> CrmLead:
    """Manually add a lead by name."""
    fields = {
        "investor_name": body.investor_name,
        "investor_type": body.investor_type,
        "investor_location": body.investor_location,
        "investor_details": body.investor_details,
        "pipeline_stage": body.pipeline_stage or "Prospect",
        "needs_enrichment": not body.investor_type or not body.investor_location,
    }
    return _insert_lead(con, fields, source="manual")


def sync_import_to_leads(con) -> int:
    """Upsert crm_contacts rows into crm_leads (source=import). Returns rows added."""
    rows = con.execute(
        """
        SELECT investor_name, name_key, investor_type, investor_location,
               investor_details, contacts_json, crm_status, source_file
        FROM crm_contacts
        """
    ).fetchall()
    added = 0
    for row in rows:
        name, key, itype, loc, details, contacts, stage, src = row
        if _in_crm_leads(con, key):
            continue
        fields = {
            "investor_name": name,
            "investor_type": itype,
            "investor_location": loc,
            "investor_details": details,
            "contacts_json": contacts,
            "pipeline_stage": stage,
            "source_file": src,
            "needs_enrichment": False,
            "status": "active",
        }
        score = compute_score(fit_score=fields.get("fit_score"))
        lead_id = str(uuid.uuid4())
        contacts_json = fields.get("contacts_json")
        con.execute(
            """
            INSERT INTO crm_leads (
                lead_id, investor_name, name_key, source, status,
                investor_type, investor_location, investor_details, contacts_json,
                pipeline_stage, computed_score, needs_enrichment, source_file,
                created_at, updated_at
            ) VALUES (?, ?, ?, 'import', 'active', ?, ?, ?, ?, ?, ?, ?, ?, NOW(), NOW())
            """,
            [
                lead_id, name, key,
                itype, loc, details, contacts_json, stage, score,
                fields.get("needs_enrichment", False), src,
            ],
        )
        added += 1
    return added

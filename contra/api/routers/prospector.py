"""Prospector API — autonomous LP mining runs, candidate review queue, seeds."""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from api.deps import background_connection, get_db

logger = logging.getLogger(__name__)
router = APIRouter()


class RunRequest(BaseModel):
    max_seeds: Optional[int] = Field(default=None, ge=1, le=20)
    max_queries: Optional[int] = Field(default=None, ge=1, le=40)
    max_candidates: Optional[int] = Field(default=None, ge=1, le=40)
    promote: bool = True


class RunResponse(BaseModel):
    run_id: str
    started: bool
    detail: str = ""


@router.post("/prospector/run", response_model=RunResponse)
def start_run(req: RunRequest) -> RunResponse:
    """Kick off a mining run in the background; poll /prospector/runs for progress."""
    from contra.prospector.scheduler import try_start_run

    # The run outlives this request, so it needs the long-lived connection —
    # a get_db() cursor would be closed the moment this handler returns.
    run_id, busy = try_start_run(
        background_connection(),
        trigger="manual",
        max_seeds=req.max_seeds,
        max_queries=req.max_queries,
        max_candidates=req.max_candidates,
        promote=req.promote,
    )
    return RunResponse(run_id=run_id or "", started=not busy, detail=busy)


@router.get("/prospector/runs")
def list_runs(top: int = Query(20, ge=1, le=100), con=Depends(get_db)) -> List[Dict[str, Any]]:
    rows = con.execute(
        """
        SELECT run_id, status, trigger, seeds_json, queries_used, results_seen,
               docs_fetched, harvested, candidates_found, new_candidates,
               resolved, preranked, corroborated, gated,
               qualified, review, rejected, promoted, seeds_added, error,
               CAST(started_at AS VARCHAR) AS started_at,
               CAST(completed_at AS VARCHAR) AS completed_at
        FROM prospector_runs
        ORDER BY started_at DESC
        LIMIT ?
        """,
        [top],
    ).fetchall()
    from contra.prospector.scheduler import active_run_id

    cols = [d[0] for d in con.description]
    active = active_run_id()
    out = []
    for r in rows:
        d = dict(zip(cols, r))
        if isinstance(d.get("seeds_json"), str):
            try:
                d["seeds_json"] = json.loads(d["seeds_json"])
            except json.JSONDecodeError:
                pass
        d["active"] = d["run_id"] == active
        out.append(d)
    return out


@router.get("/prospector/candidates")
def list_candidates(
    status: Optional[str] = Query(None, description="qualified | review | rejected | promoted | dismissed"),
    top: int = Query(100, ge=1, le=500),
    con=Depends(get_db),
) -> List[Dict[str, Any]]:
    """Candidate queue with scorecards attached (batch join by name_key)."""
    from contra.scorecard import scorecards_for_keys

    sql = """
        SELECT CAST(candidate_id AS VARCHAR) AS candidate_id, investor_name, name_key,
               entity_type, geography, discovery_evidence, source_url, source_domain,
               run_id, status, verdict_reason,
               stage, prerank_score, prerank_checks_json, source_diversity,
               corroborated, corroboration_json, gate_verdict,
               CAST(revisit_date AS VARCHAR) AS revisit_date,
               CAST(created_at AS VARCHAR) AS created_at,
               CAST(updated_at AS VARCHAR) AS updated_at
        FROM prospector_candidates
        WHERE 1=1
    """
    params: List[Any] = []
    if status:
        sql += " AND status = ?"
        params.append(status)
    # Best evidence first: corroborated candidates, then by prerank score.
    sql += " ORDER BY corroborated DESC, prerank_score DESC NULLS LAST, updated_at DESC LIMIT ?"
    params.append(top)

    rows = con.execute(sql, params).fetchall()
    cols = [d[0] for d in con.description]
    items = [dict(zip(cols, r)) for r in rows]

    for item in items:
        for field in ("prerank_checks_json", "corroboration_json"):
            if isinstance(item.get(field), str):
                try:
                    item[field] = json.loads(item[field])
                except json.JSONDecodeError:
                    item[field] = None

    cards = scorecards_for_keys(con, [i["name_key"] for i in items])
    for item in items:
        sc = cards.get(item["name_key"])
        item["scorecard"] = sc.to_api_dict() if sc else None
    return items


class CandidateAction(BaseModel):
    note: Optional[str] = None


class CandidateApproval(BaseModel):
    override: bool = False  # proceed even when the gate returns NO


def _get_candidate(con, candidate_id: str) -> Dict[str, Any]:
    row = con.execute(
        "SELECT CAST(candidate_id AS VARCHAR), investor_name, name_key, entity_type, "
        "geography, discovery_evidence, source_url, status, corroboration_json "
        "FROM prospector_candidates WHERE CAST(candidate_id AS VARCHAR) = ?",
        [candidate_id],
    ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Candidate not found")
    keys = ["candidate_id", "investor_name", "name_key", "entity_type",
            "geography", "discovery_evidence", "source_url", "status",
            "corroboration_json"]
    return dict(zip(keys, row))


def _analyst_facts(cand: Dict[str, Any]) -> List[str]:
    """Stage 4 corroboration quotes, as the gate's analyst-fact channel."""
    raw = cand.get("corroboration_json")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            raw = None
    if not isinstance(raw, list):
        return []
    facts = []
    for entry in raw[:2]:
        if isinstance(entry, dict) and entry.get("quote"):
            facts.append(
                f"{cand['investor_name']}: {entry['quote']} "
                f"(source: {entry.get('url', '')})"
            )
    return facts


@router.post("/prospector/candidates/{candidate_id}/approve")
def approve_candidate(
    candidate_id: str,
    body: Optional[CandidateApproval] = None,
    con=Depends(get_db),
) -> Dict[str, Any]:
    """
    Promote a candidate into the CRM — through the LP gate, not around it.

    Previously this inserted straight into crm_leads, so a manually approved
    candidate arrived with no verdict, no confidence and no scorecard, and was
    indistinguishable in the CRM from a screened lead. Running the gate here is
    what makes "every CRM lead has a gate verdict" true rather than aspirational.

    A NO is reported back with the gate's reasoning and requires an explicit
    override, so the analyst keeps the final say but has to make it deliberately.
    """
    from contra.crm.writer import add_lead_from_gate
    from contra.gate.runner import run_gate
    from contra.scorecard import save_scorecard_from_gate

    body = body or CandidateApproval()
    cand = _get_candidate(con, candidate_id)

    try:
        result = run_gate(
            con, cand["investor_name"],
            analyst_facts=_analyst_facts(cand),
            compact_web=True,
            screening_mode="institutional",
        )
    except ValueError as exc:
        # Hard block — already in CRM, dismissed, or blacklisted.
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Gate failed while approving %s", cand["investor_name"])
        raise HTTPException(status_code=502, detail="Gate screening failed") from exc

    verdict = "yes" if result.yes else ("review" if result.is_review else "no")
    try:
        save_scorecard_from_gate(con, result)
    except Exception:
        logger.warning("Scorecard save failed for %s", cand["investor_name"], exc_info=True)

    if verdict == "no" and not body.override:
        con.execute(
            "UPDATE prospector_candidates SET gate_verdict = ?, verdict_reason = ?, "
            "stage = 'gate', updated_at = NOW() WHERE CAST(candidate_id AS VARCHAR) = ?",
            [verdict, (result.summary or "")[:800], candidate_id],
        )
        raise HTTPException(status_code=409, detail={
            "message": "Gate returned NO — re-send with override to promote anyway.",
            "verdict": verdict,
            "summary": result.summary,
            "reasons": result.reasons,
            "session_id": result.session_id,
        })

    try:
        lead = add_lead_from_gate(con, result.session_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    con.execute(
        "UPDATE prospector_candidates SET status = 'promoted', gate_verdict = ?, "
        "stage = 'gate', verdict_reason = ?, updated_at = NOW() "
        "WHERE CAST(candidate_id AS VARCHAR) = ?",
        [verdict, (result.summary or "")[:800], candidate_id],
    )
    return {
        "ok": True,
        "lead_id": lead.lead_id,
        "gate_verdict": verdict,
        "overridden": verdict == "no",
        "summary": result.summary,
    }


@router.post("/prospector/candidates/{candidate_id}/dismiss")
def dismiss_candidate(candidate_id: str, body: CandidateAction, con=Depends(get_db)) -> Dict[str, Any]:
    """Dismiss a candidate — recorded in crm_dismissed so it never resurfaces."""
    cand = _get_candidate(con, candidate_id)
    con.execute(
        "UPDATE prospector_candidates SET status = 'dismissed', updated_at = NOW() "
        "WHERE CAST(candidate_id AS VARCHAR) = ?",
        [candidate_id],
    )
    try:
        con.execute(
            "INSERT INTO crm_dismissed (investor_name, name_key, reason, note) VALUES (?, ?, 'prospector_dismiss', ?)",
            [cand["investor_name"], cand["name_key"], body.note],
        )
    except Exception:
        logger.warning("crm_dismissed insert failed for %s", cand["investor_name"], exc_info=True)
    return {"ok": True}


@router.post("/prospector/mine-syndicate")
def mine_syndicate(con=Depends(get_db)) -> Dict[str, Any]:
    """Auto-promote syndicate members with fund-deal history into the CRM."""
    from contra.prospector.owned_data import promote_syndicate_fund_lps

    try:
        return promote_syndicate_fund_lps(con)
    except Exception as exc:
        logger.exception("Syndicate mine failed")
        raise HTTPException(status_code=500, detail="Syndicate mining failed") from exc


@router.post("/prospector/backfill-outreach")
def backfill_outreach(con=Depends(get_db)) -> Dict[str, Any]:
    """Parse past_outreach/*.eml into outreach_log; mark matching leads contacted."""
    from contra.prospector.owned_data import backfill_past_outreach

    try:
        return backfill_past_outreach(con)
    except Exception as exc:
        logger.exception("Outreach backfill failed")
        raise HTTPException(status_code=500, detail="Outreach backfill failed") from exc


@router.get("/prospector/seeds")
def list_seeds(con=Depends(get_db)) -> List[Dict[str, Any]]:
    from contra.prospector.seeds import ensure_default_seeds

    ensure_default_seeds(con)
    rows = con.execute(
        """
        SELECT CAST(seed_id AS VARCHAR) AS seed_id, seed_type, value, geography,
               enabled, origin,
               CAST(last_mined_at AS VARCHAR) AS last_mined_at,
               CAST(created_at AS VARCHAR) AS created_at
        FROM prospector_seeds
        ORDER BY last_mined_at ASC NULLS FIRST, created_at ASC
        """
    ).fetchall()
    cols = [d[0] for d in con.description]
    return [dict(zip(cols, r)) for r in rows]


class SeedCreate(BaseModel):
    seed_type: str = Field(pattern="^(peer_fund|confirmed_lp|query_template)$")
    value: str = Field(min_length=3, max_length=300)
    geography: Optional[str] = None


@router.post("/prospector/seeds")
def create_seed(body: SeedCreate, con=Depends(get_db)) -> Dict[str, Any]:
    from contra.prospector.seeds import add_seed

    inserted = add_seed(con, body.seed_type, body.value,
                        geography=body.geography, origin="manual")
    if not inserted:
        raise HTTPException(status_code=409, detail="Seed already exists")
    return {"ok": True}


class SeedUpdate(BaseModel):
    enabled: bool


@router.patch("/prospector/seeds/{seed_id}")
def update_seed(seed_id: str, body: SeedUpdate, con=Depends(get_db)) -> Dict[str, Any]:
    con.execute(
        "UPDATE prospector_seeds SET enabled = ? WHERE CAST(seed_id AS VARCHAR) = ?",
        [body.enabled, seed_id],
    )
    return {"ok": True}

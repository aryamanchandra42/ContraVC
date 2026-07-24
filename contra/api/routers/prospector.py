"""Prospector API — autonomous LP mining runs, candidate review queue, seeds."""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from api.deps import get_db

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
def start_run(req: RunRequest, con=Depends(get_db)) -> RunResponse:
    """Kick off a mining run in the background; poll /prospector/runs for progress."""
    from contra.prospector.scheduler import try_start_run

    run_id, busy = try_start_run(
        con,
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
               candidates_found, new_candidates, qualified, review, rejected,
               promoted, seeds_added, error,
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
               entity_type, geography, discovery_evidence, source_url, run_id,
               status, verdict_reason,
               CAST(created_at AS VARCHAR) AS created_at,
               CAST(updated_at AS VARCHAR) AS updated_at
        FROM prospector_candidates
        WHERE 1=1
    """
    params: List[Any] = []
    if status:
        sql += " AND status = ?"
        params.append(status)
    sql += " ORDER BY updated_at DESC LIMIT ?"
    params.append(top)

    rows = con.execute(sql, params).fetchall()
    cols = [d[0] for d in con.description]
    items = [dict(zip(cols, r)) for r in rows]

    cards = scorecards_for_keys(con, [i["name_key"] for i in items])
    for item in items:
        sc = cards.get(item["name_key"])
        item["scorecard"] = sc.to_api_dict() if sc else None
    return items


class CandidateAction(BaseModel):
    note: Optional[str] = None


def _get_candidate(con, candidate_id: str) -> Dict[str, Any]:
    row = con.execute(
        "SELECT CAST(candidate_id AS VARCHAR), investor_name, name_key, entity_type, "
        "geography, discovery_evidence, source_url, status "
        "FROM prospector_candidates WHERE CAST(candidate_id AS VARCHAR) = ?",
        [candidate_id],
    ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Candidate not found")
    keys = ["candidate_id", "investor_name", "name_key", "entity_type",
            "geography", "discovery_evidence", "source_url", "status"]
    return dict(zip(keys, row))


@router.post("/prospector/candidates/{candidate_id}/approve")
def approve_candidate(candidate_id: str, con=Depends(get_db)) -> Dict[str, Any]:
    """Manually promote a review candidate into the CRM."""
    from contra.crm.writer import _insert_lead

    cand = _get_candidate(con, candidate_id)
    try:
        lead = _insert_lead(con, {
            "investor_name": cand["investor_name"],
            "investor_type": cand.get("entity_type"),
            "investor_location": cand.get("geography"),
            "investor_details": f"Approved from Prospector queue. {cand.get('discovery_evidence') or ''}"[:800],
            "pipeline_stage": "Prospect",
            "status": "active",
            "source_file": cand.get("source_url"),
        }, source="prospector")
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    con.execute(
        "UPDATE prospector_candidates SET status = 'promoted', updated_at = NOW() "
        "WHERE CAST(candidate_id AS VARCHAR) = ?",
        [candidate_id],
    )
    return {"ok": True, "lead_id": lead.lead_id}


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
        raise HTTPException(status_code=500, detail=f"{type(exc).__name__}: {exc}") from exc


@router.post("/prospector/backfill-outreach")
def backfill_outreach(con=Depends(get_db)) -> Dict[str, Any]:
    """Parse past_outreach/*.eml into outreach_log; mark matching leads contacted."""
    from contra.prospector.owned_data import backfill_past_outreach

    try:
        return backfill_past_outreach(con)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"{type(exc).__name__}: {exc}") from exc


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

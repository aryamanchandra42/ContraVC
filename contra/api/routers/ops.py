"""POST /api/enrich, POST /api/refresh, POST /api/phantombuster/run — pipeline ops."""

from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from api.deps import get_db, reset_shared_connection

router = APIRouter()


class EnrichRequest(BaseModel):
    population: str = "institutional"
    limit: int = 20
    unknown_only: bool = True
    research_fit: bool = False


@router.post("/enrich", response_model=Dict[str, Any])
def enrich(req: EnrichRequest, con=Depends(get_db)) -> Dict[str, Any]:
    from agents.research.enrichment_agent import run_enrichment

    pop_map = {"institutional": "institutional_prospect", "syndicate": "syndicate_lp"}
    pop = pop_map.get(req.population.lower(), req.population)
    try:
        return run_enrichment(
            con,
            population=pop,
            only_unknown_type=req.unknown_only,
            research_fit=req.research_fit,
            limit=req.limit,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"{type(exc).__name__}: {exc}") from exc


@router.post("/refresh", response_model=Dict[str, Any])
def refresh() -> Dict[str, Any]:
    from contra.orchestrator import run_refresh

    result = run_refresh()
    if result.success:
        reset_shared_connection()
    return {
        "success": result.success,
        "stages_completed": result.stages_completed,
        "failed_stage": result.failed_stage,
        "error": result.error,
        "started_at": result.started_at,
        "completed_at": result.completed_at,
    }


class PhantombusterRunRequest(BaseModel):
    agent_id: Optional[str] = None
    timeout_sec: int = 3600
    save_csv: bool = True


@router.post("/phantombuster/run", response_model=Dict[str, Any])
def phantombuster_run(req: PhantombusterRunRequest, con=Depends(get_db)) -> Dict[str, Any]:
    """
    Launch a Phantombuster phantom, ingest output, run LinkedIn contact matching.

    Requires PHANTOMBUSTER_API_KEY env var.
    agent_id defaults to PHANTOMBUSTER_AGENT_ID env var when not provided.
    """
    from agents.ingestion.phantombuster_client import PhantombusterError
    from agents.ingestion.phantombuster_sync import run_phantombuster_sync

    try:
        stats = run_phantombuster_sync(
            con,
            agent_id=req.agent_id or None,
            timeout_sec=req.timeout_sec,
            save_csv=req.save_csv,
        )
        reset_shared_connection()
        return stats
    except PhantombusterError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"{type(exc).__name__}: {exc}") from exc

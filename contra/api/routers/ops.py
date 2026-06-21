"""POST /api/enrich, POST /api/refresh — pipeline ops."""

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

"""POST /api/refresh — pipeline ops."""

from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter

from api.deps import reset_shared_connection

router = APIRouter()


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

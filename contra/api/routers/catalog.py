"""GET /api/catalog — data estate summary."""

from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Depends

from api.deps import get_db

router = APIRouter()


@router.get("/catalog", response_model=Dict[str, Any])
def catalog(con=Depends(get_db)) -> Dict[str, Any]:
    from contra.intelligence.catalog import get_catalog

    return get_catalog(con)

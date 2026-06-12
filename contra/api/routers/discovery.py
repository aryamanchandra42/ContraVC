"""LP Discovery API — autonomous mining of new LP candidates + auto-screening."""

from __future__ import annotations

import logging
import threading
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from api.deps import get_db
from agents.research.lp_prospector import DiscoveryResult, discover_lps, flag_known_candidates

logger = logging.getLogger(__name__)
router = APIRouter()


PRESET_THESES = [
    "Singapore and Southeast Asia family offices that commit to venture funds as LPs",
    "Family offices and fund-of-funds with emerging manager programs backing Fund I vehicles",
    "Middle East sovereign-adjacent family offices allocating to AI and deep tech VC funds",
    "US endowments and foundations backing first-time venture fund managers",
    "Asian corporate investors with LP positions in early-stage technology funds",
    "Individuals who anchored Fund I of seed-stage VC firms in the last 3 years",
]


class DiscoverRequest(BaseModel):
    query: str = Field(min_length=8, max_length=500)
    limit: int = Field(default=15, ge=3, le=30)


class ScreenRequest(BaseModel):
    names: List[str] = Field(min_length=1, max_length=50)
    screening_mode: str = "institutional"


class ScreenResponse(BaseModel):
    batch_id: str
    total: int


@router.get("/discovery/presets")
def discovery_presets() -> dict:
    return {"presets": PRESET_THESES}


@router.post("/discovery/search", response_model=DiscoveryResult)
def discovery_search(req: DiscoverRequest, con=Depends(get_db)) -> DiscoveryResult:
    """
    Run the LP prospector agent on a thesis query; return deduped candidates
    flagged against CRM / allocator DB / prior gate screens.
    """
    try:
        result = discover_lps(req.query, limit=req.limit, con=con)
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail=f"Discovery agent unavailable: {exc}. "
                   "Requires PULSE_SEARCH_PROVIDER=openai/auto and OPENAI_API_KEY.",
        ) from exc
    flag_known_candidates(con, result.candidates)
    return result


@router.post("/discovery/screen", response_model=ScreenResponse)
def discovery_screen(req: ScreenRequest, con=Depends(get_db)) -> ScreenResponse:
    """
    Push discovered candidates straight into the batch gate pipeline.

    Runs in a background thread (per-thread DuckDB cursors inside the batch
    runner); poll progress at /api/gate/batch/{batch_id}.
    """
    import uuid as _uuid

    from contra.gate.batch import batch_gate_run
    from contra.gate.batch_models import NfxInvestorRecord

    records = [
        NfxInvestorRecord(investor_name=n.strip())
        for n in req.names if n.strip()
    ]
    if not records:
        raise HTTPException(status_code=422, detail="No valid names provided.")

    batch_id = _uuid.uuid4().hex

    def _run() -> None:
        cur = con.cursor() if hasattr(con, "cursor") else con
        try:
            batch_gate_run(
                cur, records,
                source_type="discovery",
                delay_seconds=1.0,
                batch_id=batch_id,
            )
        except Exception as exc:
            logger.error("Discovery screen batch %s failed: %s", batch_id, exc)
        finally:
            if cur is not con:
                try:
                    cur.close()
                except Exception:
                    pass

    threading.Thread(target=_run, name=f"discovery-{batch_id[:8]}", daemon=True).start()
    return ScreenResponse(batch_id=batch_id, total=len(records))

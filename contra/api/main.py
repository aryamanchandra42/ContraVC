"""Contra FastAPI — HTTP layer over LP intelligence modules."""

from __future__ import annotations

import logging
import os
import pathlib
from contextlib import asynccontextmanager

from dotenv import load_dotenv

# Load .env before any module that reads os.environ at import/call time
# (e.g. MotherDuck token in agents.db).
ROOT = pathlib.Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api.auth import require_auth
from api.routers import auth, catalog, crm, discovery, gate, intel, ops, prospector

logger = logging.getLogger(__name__)


def _allowed_origins() -> list[str]:
    """Read ALLOWED_ORIGINS env var (comma-separated) with localhost fallback."""
    raw = os.getenv("ALLOWED_ORIGINS", "")
    origins = [o.strip() for o in raw.split(",") if o.strip()]
    defaults = [
        "http://localhost:3000",
        "http://127.0.0.1:3000",
        "http://localhost:3001",
        "http://127.0.0.1:3001",
    ]
    return list(dict.fromkeys(defaults + origins))


def _cors_origin_regex() -> str | None:
    """Permit any local dev port when CORS_DEV_REGEX is enabled (default on locally).

    On Render / when CORS_DEV_REGEX=0, only explicit ALLOWED_ORIGINS apply.
    """
    # Hosted platforms set RENDER=true — keep localhost regex off there by default.
    default = "0" if os.getenv("RENDER") else "1"
    if os.getenv("CORS_DEV_REGEX", default).lower() in ("0", "false", "no"):
        return None
    return r"http://(localhost|127\.0\.0\.1|\[::1\])(:\d+)?"


@asynccontextmanager
async def lifespan(app: FastAPI):
    load_dotenv(ROOT / ".env")

    from api.auth import auth_enabled

    if not auth_enabled():
        if os.getenv("RENDER") or os.getenv("CONTRA_REQUIRE_AUTH", "").lower() in (
            "1", "true", "yes",
        ):
            logger.error(
                "CONSOLE_PASSWORD is unset — API auth is DISABLED. "
                "Set CONSOLE_PASSWORD before exposing this service."
            )
        else:
            logger.warning("CONSOLE_PASSWORD unset — API auth disabled (dev mode)")

    # Bootstrap schema/views on the same shared connection the API uses
    from api.deps import _shared_connection, close_shared_connection

    _shared_connection()

    # Autonomous LP mining on an interval (PROSPECTOR_AUTORUN=false to disable)
    from contra.prospector.scheduler import start_scheduler, stop_scheduler

    start_scheduler(_shared_connection)

    yield

    stop_scheduler()
    close_shared_connection()


app = FastAPI(
    title="Contra LP Intelligence API",
    description="Backend intelligence layer for the FundingStack GP platform",
    version="0.1.0",
    lifespan=lifespan,
    dependencies=[Depends(require_auth)],
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins(),
    allow_origin_regex=_cors_origin_regex(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router, prefix="/api", tags=["auth"])
app.include_router(gate.router, prefix="/api", tags=["gate"])
app.include_router(crm.router, prefix="/api", tags=["crm"])
app.include_router(intel.router, prefix="/api", tags=["intel"])
app.include_router(ops.router, prefix="/api", tags=["ops"])
app.include_router(catalog.router, prefix="/api", tags=["catalog"])
app.include_router(discovery.router, prefix="/api", tags=["discovery"])
app.include_router(prospector.router, prefix="/api", tags=["prospector"])


@app.get("/api/health")
def health():
    from api.auth import auth_enabled

    return {"status": "ok", "service": "contra-api", "auth_required": auth_enabled()}

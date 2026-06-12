"""Contra FastAPI — HTTP layer over LP intelligence modules."""

from __future__ import annotations

import os
import pathlib
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api.routers import catalog, crm, discovery, gate, intel, ops

ROOT = pathlib.Path(__file__).resolve().parent.parent


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
    """Permit any local dev port when CORS_DEV_REGEX is not explicitly disabled."""
    if os.getenv("CORS_DEV_REGEX", "1").lower() in ("0", "false", "no"):
        return None
    return r"http://(localhost|127\.0\.0\.1|\[::1\])(:\d+)?"


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Load environment variables
    load_dotenv(ROOT / ".env")

    # Bootstrap schema/views on the same shared connection the API uses
    from api.deps import _shared_connection, close_shared_connection

    _shared_connection()

    yield

    close_shared_connection()


app = FastAPI(
    title="Contra LP Intelligence API",
    description="Backend intelligence layer for the FundingStack GP platform",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins(),
    allow_origin_regex=_cors_origin_regex(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(gate.router, prefix="/api", tags=["gate"])
app.include_router(crm.router, prefix="/api", tags=["crm"])
app.include_router(intel.router, prefix="/api", tags=["intel"])
app.include_router(ops.router, prefix="/api", tags=["ops"])
app.include_router(catalog.router, prefix="/api", tags=["catalog"])
app.include_router(discovery.router, prefix="/api", tags=["discovery"])


@app.get("/api/health")
def health():
    return {"status": "ok", "service": "contra-api"}

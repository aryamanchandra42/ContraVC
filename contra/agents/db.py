"""
Shared DuckDB connection + schema bootstrap for Contra.

Usage:
    from agents.db import get_conn
    con = get_conn()
    con.execute("SELECT * FROM v_lp_gate_context").fetchdf()
"""

from __future__ import annotations

from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "contra.duckdb"
SCHEMA_DIR = ROOT / "schema"


def get_conn(db_path: Path | None = None, read_only: bool = False) -> duckdb.DuckDBPyConnection:
    """Return a DuckDB connection. Creates and bootstraps the DB if it doesn't exist."""
    path = db_path or DB_PATH
    con = duckdb.connect(str(path), read_only=read_only)
    if not read_only:
        _bootstrap(con)
    return con


def _bootstrap(con: duckdb.DuckDBPyConnection) -> None:
    """Run DDL + views if tables don't exist yet. Idempotent."""
    ddl_path = SCHEMA_DIR / "duckdb.sql"
    views_path = SCHEMA_DIR / "views.sql"

    if ddl_path.exists():
        con.execute(ddl_path.read_text(encoding="utf-8"))
    from agents.db_migrations import (
        migrate_icp_scores_v41,
        migrate_signal_expansion,
        migrate_pipeline_runs_stage_check,
        migrate_contra_extension,
        migrate_crm_leads,
        migrate_crm_dismissed,
    )
    migrate_icp_scores_v41(con)
    migrate_signal_expansion(con)
    migrate_pipeline_runs_stage_check(con)
    migrate_contra_extension(con)
    migrate_crm_leads(con)
    migrate_crm_dismissed(con)
    if views_path.exists():
        try:
            con.execute(views_path.read_text(encoding="utf-8"))
        except Exception:
            # Views may fail on empty DB due to aggregate functions — acceptable at bootstrap
            pass


def ensure_views(con) -> None:
    """Apply LLM navigation views (safe to call on read-only if views already exist)."""
    views_path = SCHEMA_DIR / "views.sql"
    if views_path.exists() and not getattr(con, "read_only", False):
        try:
            con.execute(views_path.read_text(encoding="utf-8"))
        except Exception:
            pass

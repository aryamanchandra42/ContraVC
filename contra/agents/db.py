"""
Shared DuckDB connection + schema bootstrap for Contra.

Usage:
    from agents.db import get_conn
    con = get_conn()
    con.execute("SELECT * FROM v_lp_gate_context").fetchdf()

Cloud mode: set MOTHERDUCK_TOKEN env var to use MotherDuck instead of a local file.
The database name on MotherDuck is always "contra".
"""

from __future__ import annotations

import os
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "contra.duckdb"
SCHEMA_DIR = ROOT / "schema"

_MOTHERDUCK_DB = "md:contra"


def _is_cloud() -> bool:
    # Read at call time so .env loaded in api.main is visible.
    return bool(os.getenv("MOTHERDUCK_TOKEN", "").strip())


def _recover_corrupt_wal(path: Path) -> bool:
    """
    Move aside a WAL that fails replay (common after a crashed write).
    Returns True if a WAL was quarantined. Caller should retry the connect.
    """
    import logging
    import time

    wal = Path(str(path) + ".wal")
    if not wal.exists():
        return False
    quarantine = Path(str(wal) + f".corrupt-{int(time.time())}")
    try:
        wal.replace(quarantine)
        logging.getLogger(__name__).warning(
            "Quarantined corrupt DuckDB WAL %s → %s (uncommitted writes may be lost)",
            wal.name, quarantine.name,
        )
        return True
    except OSError as exc:
        logging.getLogger(__name__).error("Could not quarantine WAL %s: %s", wal, exc)
        return False


def get_conn(db_path: Path | None = None, read_only: bool = False) -> duckdb.DuckDBPyConnection:
    """Return a DuckDB connection. Uses MotherDuck when MOTHERDUCK_TOKEN is set."""
    if _is_cloud():
        # MotherDuck does not support read_only flag; all connections are writable.
        con = duckdb.connect(_MOTHERDUCK_DB)
    else:
        path = db_path or DB_PATH
        try:
            con = duckdb.connect(str(path), read_only=read_only)
        except duckdb.Error as exc:
            msg = str(exc)
            if "WAL" in msg or "replaying" in msg.lower():
                if _recover_corrupt_wal(path):
                    con = duckdb.connect(str(path), read_only=read_only)
                else:
                    raise
            else:
                raise
    if not read_only:
        _bootstrap(con)
    return con


def _bootstrap(con: duckdb.DuckDBPyConnection) -> None:
    """Run DDL + views if tables don't exist yet. Idempotent."""
    ddl_path = SCHEMA_DIR / "duckdb.sql"
    views_path = SCHEMA_DIR / "views.sql"

    if ddl_path.exists():
        con.execute(ddl_path.read_text(encoding="utf-8"))
    _run_migrations(con)
    if views_path.exists():
        try:
            con.execute(views_path.read_text(encoding="utf-8"))
        except Exception:
            # Views may fail on empty DB due to aggregate functions — acceptable at bootstrap
            pass


def _run_migrations(con: duckdb.DuckDBPyConnection) -> None:
    from agents.db_migrations import (
        migrate_icp_scores_v41,
        migrate_signal_expansion,
        migrate_pipeline_runs_stage_check,
        migrate_contra_extension,
        migrate_crm_leads,
        migrate_crm_dismissed,
        migrate_crm_gate_reviews,
        migrate_lp_dossiers,
        migrate_crm_outreach,
        migrate_crm_rejection_tracking,
        migrate_allocator_contacts_v2,
        migrate_lead_scorecards,
        migrate_prospector,
        migrate_prospector_cascade,
        migrate_prospector_cost,
        migrate_prospector_search_diag,
        migrate_web_search_log,
        migrate_outreach_log,
    )
    migrate_icp_scores_v41(con)
    migrate_signal_expansion(con)
    migrate_pipeline_runs_stage_check(con)
    migrate_contra_extension(con)
    migrate_crm_leads(con)
    migrate_crm_dismissed(con)
    migrate_crm_gate_reviews(con)
    migrate_lp_dossiers(con)
    migrate_crm_outreach(con)
    migrate_crm_rejection_tracking(con)
    migrate_allocator_contacts_v2(con)
    migrate_lead_scorecards(con)
    migrate_prospector(con)
    migrate_prospector_cascade(con)
    migrate_prospector_cost(con)
    migrate_prospector_search_diag(con)
    migrate_web_search_log(con)
    migrate_outreach_log(con)


def ensure_views(con) -> None:
    """Apply pending migrations + SQL views (idempotent; safe on API warm start)."""
    if getattr(con, "read_only", False):
        return
    views_path = SCHEMA_DIR / "views.sql"
    try:
        _run_migrations(con)
        if views_path.exists():
            con.execute(views_path.read_text(encoding="utf-8"))
    except Exception:
        import logging
        logging.getLogger(__name__).warning("ensure_views failed", exc_info=True)

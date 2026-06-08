"""Pipeline run tracking — writes pipeline_runs rows to DuckDB."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


def start_run(con, stage: str, params: Dict = None) -> str:
    """Insert a 'running' pipeline_run row. Returns run_id."""
    run_id = str(uuid.uuid4())
    con.execute(
        """
        INSERT INTO pipeline_runs
            (run_id, stage, status, params, started_at)
        VALUES (?, ?, 'running', ?, NOW())
        """,
        [run_id, stage, json.dumps(params or {})],
    )
    return run_id


def complete_run(
    con,
    run_id: str,
    rows_processed: int = 0,
    rows_written: int = 0,
    artifact_uris: List[str] = None,
    derivation_params_hash: Optional[str] = None,
) -> None:
    """Mark a pipeline_run as completed."""
    con.execute(
        """
        UPDATE pipeline_runs
        SET status = 'completed',
            completed_at = NOW(),
            rows_processed = ?,
            rows_written = ?,
            artifact_uris = ?,
            derivation_params_hash = ?
        WHERE CAST(run_id AS VARCHAR) = ?
        """,
        [
            rows_processed,
            rows_written,
            json.dumps(artifact_uris or []),
            derivation_params_hash,
            run_id,
        ],
    )


def fail_run(con, run_id: str, error: str) -> None:
    """Mark a pipeline_run as failed."""
    con.execute(
        """
        UPDATE pipeline_runs
        SET status = 'failed', completed_at = NOW(), error = ?
        WHERE CAST(run_id AS VARCHAR) = ?
        """,
        [error, run_id],
    )


def get_stage_status(con) -> List[Dict]:
    """Return the last run for each stage."""
    rows = con.execute(
        """
        SELECT stage, status, started_at, completed_at, rows_processed, rows_written, error
        FROM pipeline_runs
        WHERE (stage, started_at) IN (
            SELECT stage, MAX(started_at)
            FROM pipeline_runs
            GROUP BY stage
        )
        ORDER BY started_at DESC
        """
    ).fetchall()
    cols = ["stage", "status", "started_at", "completed_at", "rows_processed", "rows_written", "error"]
    return [dict(zip(cols, r)) for r in rows]

"""FastAPI dependencies — shared DuckDB connection for the API process.

DuckDB cannot mix read-only and read-write connections to the same file.
The API uses one writable shared connection (SELECT + INSERT both work).
"""

from __future__ import annotations

import threading
from typing import Generator, Optional

import duckdb

from agents.db import DB_PATH, _is_cloud, ensure_views, get_conn

_lock = threading.Lock()
_shared_con: Optional[duckdb.DuckDBPyConnection] = None


def _shared_connection() -> duckdb.DuckDBPyConnection:
    """Process-wide writable DuckDB handle (lazy init, thread-safe)."""
    global _shared_con
    with _lock:
        if _shared_con is None:
            # Cloud mode: MotherDuck connection; local mode: file-based DuckDB.
            _shared_con = get_conn(db_path=None if _is_cloud() else DB_PATH, read_only=False)
            ensure_views(_shared_con)
        return _shared_con


def get_db() -> Generator:
    """Yield the shared writable connection (do not close per request)."""
    yield _shared_connection()


def get_write_db() -> Generator:
    """Alias for get_db — same shared writable connection."""
    yield _shared_connection()


def close_shared_connection() -> None:
    """Close on API shutdown."""
    global _shared_con
    with _lock:
        if _shared_con is not None:
            _shared_con.close()
            _shared_con = None


def reset_shared_connection() -> None:
    """Drop cached connection so the next request reopens with fresh schema."""
    close_shared_connection()

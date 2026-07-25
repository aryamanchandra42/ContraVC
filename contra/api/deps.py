"""FastAPI dependencies — shared DuckDB database for the API process.

DuckDB cannot mix read-only and read-write connections to the same file, so the
API keeps ONE writable connection to the database and hands each request its own
cursor onto it (SELECT + INSERT both work).

The per-request cursor is not cosmetic. DuckDB stores the active result set —
and therefore `description` and the fetch* methods — on the connection object
rather than on a result handle. FastAPI runs sync endpoints in a threadpool, so
when every request shared one connection, two concurrent queries overwrote each
other's result between execute() and description. That surfaced as KeyError on a
column the query definitely selected, and, when the two result sets happened to
have the same column count, as rows silently zipped against another query's
column names — a wrong 200 rather than a 500. A cursor is an independent handle
onto the same database, so each request gets its own result state.

Do NOT wrap get_db()'s yield in a threading.Lock: FastAPI runs sync generators
via a threadpool, so acquire/release can land on different threads and RLock
raises "cannot release un-acquired lock". Background jobs that need
serialization should use db_locked() on a single thread for the whole job.
"""

from __future__ import annotations

import logging
import threading
from contextlib import contextmanager
from typing import Generator, Optional

import duckdb

from agents.db import DB_PATH, _is_cloud, ensure_views, get_conn

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_query_lock = threading.RLock()
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


@contextmanager
def db_locked():
    """Hold the query lock for a multi-statement critical section (same thread)."""
    with _query_lock:
        yield _shared_connection()


def get_db() -> Generator:
    """Yield a per-request cursor; the underlying connection stays open."""
    cur = _shared_connection().cursor()
    try:
        yield cur
    finally:
        try:
            cur.close()
        except Exception as exc:
            logger.warning("Error closing request cursor: %s", exc)


def get_write_db() -> Generator:
    """Alias for get_db — same shared database, independent result state."""
    yield from get_db()


def background_connection() -> duckdb.DuckDBPyConnection:
    """Root connection for work that outlives the request that started it.

    get_db() closes its cursor when the handler returns, so a thread that keeps
    running past that point must derive its own cursor from the process-wide
    connection instead of from the request's.
    """
    return _shared_connection()


def close_shared_connection() -> None:
    """Close on API shutdown."""
    global _shared_con
    with _lock:
        if _shared_con is not None:
            try:
                _shared_con.close()
            except Exception as exc:
                logger.warning("Error closing shared DuckDB connection: %s", exc)
            _shared_con = None


def reset_shared_connection() -> None:
    """Drop cached connection so the next request reopens with fresh schema."""
    close_shared_connection()

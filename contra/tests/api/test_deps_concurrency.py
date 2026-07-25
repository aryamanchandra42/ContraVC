"""Each request must get its own DuckDB result state.

DuckDB keeps the active result set — and therefore `description` and the fetch*
methods — on the connection object rather than on a result handle. FastAPI runs
sync endpoints in a threadpool, so while every request shared one connection two
concurrent queries clobbered each other between execute() and description. In
production that raised `KeyError: 'name_key'` from a SELECT that names the column
explicitly, and, whenever the colliding result sets happened to be the same
width, silently returned rows labelled with another query's columns.
"""

from __future__ import annotations

import threading

import duckdb
import pytest

from api import deps

# Deliberately the same width as each other: a column-count mismatch is what
# turned the race into a loud KeyError, so equal-width queries are the case that
# used to corrupt data quietly.
CANDIDATES_SQL = "SELECT candidate_id, name_key FROM candidates ORDER BY candidate_id"
RUNS_SQL = "SELECT run_id, status FROM runs ORDER BY run_id"

QUERIES = (
    (CANDIDATES_SQL, ["candidate_id", "name_key"]),
    (RUNS_SQL, ["run_id", "status"]),
)


@pytest.fixture
def shared_db(monkeypatch):
    con = duckdb.connect(":memory:")
    con.execute("CREATE TABLE candidates (candidate_id INTEGER, name_key VARCHAR)")
    con.execute("INSERT INTO candidates VALUES (1, 'alpha'), (2, 'beta')")
    con.execute("CREATE TABLE runs (run_id VARCHAR, status VARCHAR)")
    con.execute("INSERT INTO runs VALUES ('r1', 'done'), ('r2', 'error')")
    monkeypatch.setattr(deps, "_shared_con", con)
    yield con
    con.close()


def _query_via_get_db(sql):
    """Drive the dependency the way FastAPI drives it: next(), then close()."""
    gen = deps.get_db()
    cur = next(gen)
    try:
        rows = cur.execute(sql).fetchall()
        cols = [d[0] for d in cur.description]
        return cols, rows
    finally:
        gen.close()


def test_columns_never_come_from_another_concurrent_query(shared_db):
    observed = []
    errors = []
    threads_per_query = 4
    barrier = threading.Barrier(threads_per_query * len(QUERIES))

    def worker(sql, expected_cols):
        try:
            barrier.wait(timeout=30)
            for _ in range(40):
                cols, rows = _query_via_get_db(sql)
                observed.append((tuple(cols), tuple(expected_cols)))
                assert len(rows) == 2, f"{sql} returned {len(rows)} rows"
        except Exception as exc:  # noqa: BLE001 - reported via assert below
            errors.append(exc)

    threads = [
        threading.Thread(target=worker, args=QUERIES[i % len(QUERIES)], daemon=True)
        for i in range(threads_per_query * len(QUERIES))
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    assert not errors, errors
    mismatched = [pair for pair in observed if pair[0] != pair[1]]
    assert not mismatched, (
        f"{len(mismatched)}/{len(observed)} queries saw another query's columns"
    )


def test_request_cursor_is_closed_when_the_request_ends(shared_db):
    gen = deps.get_db()
    cur = next(gen)
    assert cur.execute("SELECT 1").fetchone() == (1,)
    gen.close()
    with pytest.raises(Exception):
        cur.execute("SELECT 1")


def test_closing_a_request_cursor_leaves_the_shared_connection_usable(shared_db):
    gen = deps.get_db()
    next(gen)
    gen.close()
    assert deps.background_connection().execute("SELECT 1").fetchone() == (1,)
    assert deps.background_connection() is shared_db

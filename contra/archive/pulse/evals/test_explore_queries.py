"""Smoke tests for pulse.explore.queries (no Streamlit required)."""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def con():
    from agents.db import get_conn

    db = ROOT / "pulse.duckdb"
    if not db.exists():
        pytest.skip("pulse.duckdb not present")
    conn = get_conn(read_only=True)
    yield conn
    conn.close()


def test_funnel_metrics_returns_counts(con):
    from pulse.explore.queries import funnel_metrics

    m = funnel_metrics(con)
    assert "icp_version" in m
    assert m.get("count_entities_raw") is not None


def test_outreach_queue_dataframe(con):
    from pulse.explore.queries import OutreachFilters, outreach_queue

    df = outreach_queue(con, OutreachFilters(institutional_only=True))
    assert hasattr(df, "columns")


def test_allocator_detail(con):
    from pulse.explore.queries import list_scored_allocators, allocator_detail

    alloc_df = list_scored_allocators(con, "institutional_prospect")
    if alloc_df.empty:
        pytest.skip("no scored allocators")
    aid = alloc_df.iloc[0]["allocator_id"]
    detail = allocator_detail(con, str(aid))
    assert "icp" in detail

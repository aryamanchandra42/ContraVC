"""Diagnosis copy for zero-yield mining runs."""

from __future__ import annotations

from contra.prospector.activity import diagnose_run


def test_diagnose_dies_at_corroborate():
    d = diagnose_run({
        "status": "completed",
        "queries_used": 10,
        "results_seen": 42,
        "docs_fetched": 6,
        "harvested": 18,
        "resolved": 12,
        "preranked": 8,
        "corroborated": 0,
        "gated": 0,
        "promoted": 0,
    })
    assert d["died_at"] == "corroborate"
    assert "independent" in d["detail"].lower() or "corroborat" in d["headline"].lower()


def test_diagnose_promoted():
    d = diagnose_run({
        "status": "completed",
        "queries_used": 8,
        "results_seen": 30,
        "docs_fetched": 4,
        "harvested": 10,
        "resolved": 6,
        "preranked": 4,
        "corroborated": 2,
        "gated": 2,
        "promoted": 1,
    })
    assert d["died_at"] is None
    assert "Promoted 1" in d["headline"]


def test_diagnose_running():
    d = diagnose_run({
        "status": "running",
        "current_stage": "corroborate",
        "queries_used": 8,
        "harvested": 5,
        "promoted": 0,
    })
    assert d["alive"] is True
    assert "corroborate" in d["headline"]


def test_diagnose_timeout():
    d = diagnose_run({
        "status": "failed",
        "error": "timeout: exceeded PROSPECTOR_MAX_RUNTIME_SEC=600 at stage gate",
        "queries_used": 12,
        "harvested": 4,
        "promoted": 0,
    })
    assert d["died_at"] == "timeout"


def test_diagnose_zero_search_results():
    d = diagnose_run({
        "status": "completed",
        "queries_used": 7,
        "results_seen": 0,
        "docs_fetched": 0,
        "harvested": 0,
        "promoted": 0,
    })
    assert d["died_at"] == "search"
    assert "TAVILY" in d["detail"].upper() or "0 results" in d["detail"]


def test_diagnose_search_errors_blames_credentials():
    """Every query raised — that is a provider/credentials problem."""
    d = diagnose_run({
        "status": "completed",
        "queries_used": 7,
        "results_seen": 0,
        "harvested": 0,
        "promoted": 0,
        "search_ok": 0,
        "search_empty": 0,
        "search_errors": 7,
        "search_provider": "anthropic",
    })
    assert d["died_at"] == "search"
    assert "raised an error" in d["headline"]
    assert "anthropic" in d["detail"]
    assert "API key" in d["detail"]


def test_diagnose_search_empty_blames_queries():
    """Provider answered every query and found nothing — a phrasing problem."""
    d = diagnose_run({
        "status": "completed",
        "queries_used": 7,
        "results_seen": 0,
        "harvested": 0,
        "promoted": 0,
        "search_ok": 0,
        "search_empty": 7,
        "search_errors": 0,
        "search_provider": "anthropic",
    })
    assert d["died_at"] == "search"
    assert "found nothing" in d["headline"]
    assert "too narrow" in d["detail"]
    # Must not send the operator chasing credentials that are working fine.
    assert "API key" not in d["detail"]


def test_results_seen_is_a_funnel_stage():
    """A zero-result run must not be reported as a document-fetch failure."""
    d = diagnose_run({
        "status": "completed",
        "queries_used": 7,
        "results_seen": 0,
        "docs_fetched": 0,
        "harvested": 0,
        "promoted": 0,
    })
    keys = [f["key"] for f in d["funnel"]]
    assert "results_seen" in keys
    assert keys.index("results_seen") < keys.index("docs_fetched")

"""Unit tests for prospector wall-clock + cost budgets."""

from __future__ import annotations

import time

import pytest

from contra.prospector.budget import (
    BudgetExceeded,
    CostMeter,
    RunDeadline,
    UnitCosts,
    check_deadline,
    check_run_cost,
    get_meter,
    run_budget,
)


def test_deadline_expires():
    deadline = RunDeadline(started_at=time.monotonic() - 10, max_seconds=5)
    with pytest.raises(BudgetExceeded, match="timeout"):
        deadline.check("harvest")


def test_run_budget_installs_meter():
    assert get_meter() is None
    with run_budget(max_seconds=60) as (deadline, meter):
        assert get_meter() is meter
        meter.add_searches(3)
        meter.add_llm(2)
        meter.add_gates(1)
        snap = meter.snapshot()
        assert snap["search_calls"] == 3
        assert snap["llm_calls"] == 2
        assert snap["gate_calls"] == 1
        assert snap["estimated_cost_usd"] > 0
        assert deadline.remaining() > 0
    assert get_meter() is None


def test_check_run_cost_cap(monkeypatch):
    monkeypatch.setenv("PROSPECTOR_MAX_RUN_COST_USD", "0.05")
    costs = UnitCosts(search_usd=0.05, llm_usd=0.05, fetch_usd=0.05, gate_usd=0.05)
    with run_budget(max_seconds=60) as (_d, meter):
        meter.unit_costs = costs
        meter.add_searches(2)  # $0.10 >= $0.05
        with pytest.raises(BudgetExceeded, match="cost_cap"):
            check_run_cost("harvest")


def test_check_deadline_via_context(monkeypatch):
    monkeypatch.setenv("PROSPECTOR_MAX_RUNTIME_SEC", "1")
    with run_budget(max_seconds=0.05):
        time.sleep(0.08)
        with pytest.raises(BudgetExceeded, match="timeout"):
            check_deadline("corroborate")


def test_cost_meter_thread_visible():
    """Worker threads must see the active meter (ContextVar may not propagate)."""
    from concurrent.futures import ThreadPoolExecutor

    with run_budget(max_seconds=60) as (_d, meter):
        def _inc():
            m = get_meter()
            assert m is not None
            m.add_searches(1)

        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(lambda _: _inc(), range(4)))
        assert meter.search_calls == 4

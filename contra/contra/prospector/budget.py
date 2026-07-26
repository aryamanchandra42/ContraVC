"""
Prospector run budgets — wall-clock deadline + API cost metering.

Stage count caps (MAX_QUERIES, MAX_GATE, …) already limit work per run. This
module adds the two controls that were missing:

  1. PROSPECTOR_MAX_RUNTIME_SEC  — abort between stages when the wall clock
                                   expires so a stuck cascade cannot burn credits
                                   for an unbounded period.
  2. CostMeter                   — counts search / LLM / fetch / gate units and
                                   estimates USD so runs (and a daily rollup)
                                   can be monitored via the API.

Unit prices are rough operator knobs, not invoices. Override via env when a
provider's real rates differ.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Dict, Generator, Optional

logger = logging.getLogger(__name__)

_meter_var: ContextVar[Optional["CostMeter"]] = ContextVar("prospector_cost_meter", default=None)
_deadline_var: ContextVar[Optional["RunDeadline"]] = ContextVar("prospector_deadline", default=None)
# ThreadPool workers (harvest/corroborate) may not inherit ContextVars on older
# Python; the module-level active meter is visible to every thread in the run.
_active_meter: Optional["CostMeter"] = None
_active_meter_lock = threading.Lock()


class BudgetExceeded(RuntimeError):
    """Raised when a prospector run hits its wall-clock or spend ceiling."""


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


def max_runtime_seconds() -> float:
    """Default 10 minutes — long enough for a budgeted cascade, short enough to cap burn."""
    return max(30.0, _env_float("PROSPECTOR_MAX_RUNTIME_SEC", 600.0))


def max_run_cost_usd() -> float:
    """Per-run soft ceiling. 0 disables. Default $3."""
    return max(0.0, _env_float("PROSPECTOR_MAX_RUN_COST_USD", 3.0))


def max_daily_cost_usd() -> float:
    """Daily soft ceiling across completed/failed runs. 0 disables. Default $8."""
    return max(0.0, _env_float("PROSPECTOR_MAX_DAILY_COST_USD", 8.0))


def max_runs_per_day() -> int:
    """Cap scheduled+manual starts in a UTC day. 0 disables. Default 4."""
    return max(0, _env_int("PROSPECTOR_MAX_RUNS_PER_DAY", 4))


def zero_yield_pause_runs() -> int:
    """
    Skip scheduled starts after this many consecutive completed runs with
    promoted=0 (and some paid work). 0 disables. Default 3.
    """
    return max(0, _env_int("PROSPECTOR_ZERO_YIELD_PAUSE", 3))


@dataclass
class UnitCosts:
    search_usd: float = 0.008
    llm_usd: float = 0.03
    fetch_usd: float = 0.001
    gate_usd: float = 0.25  # research + triage (+ typical escalation) per LP

    @classmethod
    def from_env(cls) -> "UnitCosts":
        return cls(
            search_usd=_env_float("PROSPECTOR_COST_SEARCH_USD", 0.008),
            llm_usd=_env_float("PROSPECTOR_COST_LLM_USD", 0.03),
            fetch_usd=_env_float("PROSPECTOR_COST_FETCH_USD", 0.001),
            gate_usd=_env_float("PROSPECTOR_COST_GATE_USD", 0.25),
        )


@dataclass
class CostMeter:
    """Thread-safe counter of billable units for one mining run."""

    search_calls: int = 0
    llm_calls: int = 0
    fetch_calls: int = 0
    gate_calls: int = 0
    unit_costs: UnitCosts = field(default_factory=UnitCosts.from_env)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def add_searches(self, n: int = 1) -> None:
        if n <= 0:
            return
        with self._lock:
            self.search_calls += n

    def add_llm(self, n: int = 1) -> None:
        if n <= 0:
            return
        with self._lock:
            self.llm_calls += n

    def add_fetches(self, n: int = 1) -> None:
        if n <= 0:
            return
        with self._lock:
            self.fetch_calls += n

    def add_gates(self, n: int = 1) -> None:
        if n <= 0:
            return
        with self._lock:
            self.gate_calls += n

    def estimated_usd(self) -> float:
        with self._lock:
            return round(
                self.search_calls * self.unit_costs.search_usd
                + self.llm_calls * self.unit_costs.llm_usd
                + self.fetch_calls * self.unit_costs.fetch_usd
                + self.gate_calls * self.unit_costs.gate_usd,
                4,
            )

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            est = round(
                self.search_calls * self.unit_costs.search_usd
                + self.llm_calls * self.unit_costs.llm_usd
                + self.fetch_calls * self.unit_costs.fetch_usd
                + self.gate_calls * self.unit_costs.gate_usd,
                4,
            )
            return {
                "search_calls": self.search_calls,
                "llm_calls": self.llm_calls,
                "fetch_calls": self.fetch_calls,
                "gate_calls": self.gate_calls,
                "estimated_cost_usd": est,
                "unit_costs": {
                    "search_usd": self.unit_costs.search_usd,
                    "llm_usd": self.unit_costs.llm_usd,
                    "fetch_usd": self.unit_costs.fetch_usd,
                    "gate_usd": self.unit_costs.gate_usd,
                },
            }


@dataclass
class RunDeadline:
    started_at: float
    max_seconds: float

    @classmethod
    def start(cls, max_seconds: Optional[float] = None) -> "RunDeadline":
        return cls(started_at=time.monotonic(), max_seconds=max_seconds or max_runtime_seconds())

    def elapsed(self) -> float:
        return time.monotonic() - self.started_at

    def remaining(self) -> float:
        return self.max_seconds - self.elapsed()

    def check(self, stage: str = "") -> None:
        """Raise BudgetExceeded when the wall-clock budget is exhausted."""
        if self.elapsed() >= self.max_seconds:
            label = f" at stage {stage}" if stage else ""
            raise BudgetExceeded(
                f"timeout: exceeded PROSPECTOR_MAX_RUNTIME_SEC="
                f"{int(self.max_seconds)}{label}"
            )


def get_meter() -> Optional[CostMeter]:
    meter = _meter_var.get()
    if meter is not None:
        return meter
    with _active_meter_lock:
        return _active_meter


def get_deadline() -> Optional[RunDeadline]:
    return _deadline_var.get()


def check_deadline(stage: str = "") -> None:
    deadline = _deadline_var.get()
    if deadline is not None:
        deadline.check(stage)


def check_run_cost(stage: str = "") -> None:
    """Abort the run early when the estimated spend ceiling is hit."""
    meter = get_meter()
    ceiling = max_run_cost_usd()
    if meter is None or ceiling <= 0:
        return
    spent = meter.estimated_usd()
    if spent >= ceiling:
        label = f" at stage {stage}" if stage else ""
        raise BudgetExceeded(
            f"cost_cap: estimated ${spent:.2f} >= PROSPECTOR_MAX_RUN_COST_USD="
            f"{ceiling:.2f}{label}"
        )


@contextmanager
def run_budget(
    *,
    max_seconds: Optional[float] = None,
) -> Generator[tuple[RunDeadline, CostMeter], None, None]:
    """Install deadline + cost meter for the duration of one prospector run."""
    global _active_meter
    deadline = RunDeadline.start(max_seconds)
    meter = CostMeter()
    t_deadline = _deadline_var.set(deadline)
    t_meter = _meter_var.set(meter)
    with _active_meter_lock:
        _active_meter = meter
    try:
        yield deadline, meter
    finally:
        with _active_meter_lock:
            if _active_meter is meter:
                _active_meter = None
        _deadline_var.reset(t_deadline)
        _meter_var.reset(t_meter)


def daily_spend_usd(con) -> float:
    """Sum estimated_cost_usd for runs started today (UTC calendar day)."""
    try:
        row = con.execute(
            """
            SELECT COALESCE(SUM(estimated_cost_usd), 0)
            FROM prospector_runs
            WHERE CAST(started_at AS DATE) = CURRENT_DATE
            """
        ).fetchone()
        return float(row[0] or 0) if row else 0.0
    except Exception as exc:
        logger.debug("daily_spend_usd unavailable: %s", exc)
        return 0.0


def runs_started_today(con) -> int:
    try:
        row = con.execute(
            """
            SELECT COUNT(*) FROM prospector_runs
            WHERE CAST(started_at AS DATE) = CURRENT_DATE
            """
        ).fetchone()
        return int(row[0] or 0) if row else 0
    except Exception as exc:
        logger.debug("runs_started_today unavailable: %s", exc)
        return 0


def consecutive_zero_yield_runs(con, limit: int = 10) -> int:
    """
    Count trailing completed runs with promoted=0 that still did paid work
    (queries_used > 0 or gated > 0). Stops at the first promoting run.
    """
    try:
        rows = con.execute(
            """
            SELECT promoted, queries_used, gated
            FROM prospector_runs
            WHERE status = 'completed'
            ORDER BY started_at DESC
            LIMIT ?
            """,
            [limit],
        ).fetchall()
    except Exception as exc:
        logger.debug("consecutive_zero_yield_runs unavailable: %s", exc)
        return 0

    streak = 0
    for promoted, queries_used, gated in rows:
        paid = (queries_used or 0) > 0 or (gated or 0) > 0
        if (promoted or 0) == 0 and paid:
            streak += 1
        else:
            break
    return streak


def autorun_block_reason(con) -> str:
    """
    Non-empty string when a scheduled start should be skipped for cost/yield
    guards. Manual runs are not blocked by these (operator intent).
    """
    run_cap = max_runs_per_day()
    if run_cap and runs_started_today(con) >= run_cap:
        return (
            f"Daily run cap reached ({run_cap}). "
            "Raise PROSPECTOR_MAX_RUNS_PER_DAY or wait until tomorrow."
        )

    day_cap = max_daily_cost_usd()
    if day_cap > 0:
        spent = daily_spend_usd(con)
        if spent >= day_cap:
            return (
                f"Daily cost cap reached (est. ${spent:.2f} >= ${day_cap:.2f}). "
                "Raise PROSPECTOR_MAX_DAILY_COST_USD or wait until tomorrow."
            )

    pause_after = zero_yield_pause_runs()
    if pause_after:
        streak = consecutive_zero_yield_runs(con, limit=max(pause_after, 5))
        if streak >= pause_after:
            return (
                f"Zero-yield pause: last {streak} completed runs promoted 0 LPs. "
                "Fix seeds/search provider or set PROSPECTOR_ZERO_YIELD_PAUSE=0."
            )
    return ""

"""
Prospector scheduling — one shared run-slot + an autonomous interval loop.

Only one mining run may execute at a time (search + LLM budgets are per-run).
Both the manual API trigger and the interval scheduler go through
try_start_run so they can never clash.

Env:
  PROSPECTOR_AUTORUN         "false" (default) — set "true" to run on a timer
  PROSPECTOR_INTERVAL_HOURS  24 (default)     — hours between scheduled runs
  PROSPECTOR_BOOT_DELAY_SEC  180 (default)    — wait after API start before
                                                the first scheduled run
"""

from __future__ import annotations

import logging
import os
import threading
import uuid
from typing import Any, Callable, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_active_run_id: Optional[str] = None
_stop_event = threading.Event()
_scheduler_thread: Optional[threading.Thread] = None


def active_run_id() -> Optional[str]:
    return _active_run_id


def try_start_run(
    con,
    *,
    trigger: str = "manual",
    max_seeds: Optional[int] = None,
    max_queries: Optional[int] = None,
    max_candidates: Optional[int] = None,
    promote: bool = True,
) -> Tuple[Optional[str], str]:
    """
    Start a mining run in a background thread if no run is active.
    Returns (run_id, "") on success or (active_run_id, reason) when busy.

    `con` must outlive this call — the run keeps using a cursor derived from it
    for minutes. Pass the process-shared connection, never a per-request cursor.
    """
    global _active_run_id
    from contra.prospector import run_prospector

    with _lock:
        if _active_run_id is not None:
            return _active_run_id, "A mining run is already in progress."
        run_id = uuid.uuid4().hex
        _active_run_id = run_id

    def _run() -> None:
        global _active_run_id
        # Use the shared connection on this one background thread.
        # (Do not take api.deps._query_lock across FastAPI request threads —
        #  RLock ownership is thread-local and breaks get_db.)
        cur = con.cursor() if hasattr(con, "cursor") else con
        try:
            run_prospector(
                cur,
                max_seeds=max_seeds,
                max_queries=max_queries,
                max_candidates=max_candidates,
                promote=promote,
                trigger=trigger,
                run_id=run_id,
            )
            # Scheduled runs also sweep owned data — free and idempotent.
            if trigger == "scheduled":
                try:
                    from contra.prospector.owned_data import promote_syndicate_fund_lps
                    stats = promote_syndicate_fund_lps(cur)
                    if stats.get("promoted"):
                        logger.info("Scheduled syndicate sweep promoted %s LPs", stats["promoted"])
                except Exception as exc:
                    logger.warning("Scheduled syndicate sweep failed: %s", exc)
        except Exception as exc:
            logger.error("Prospector run %s (%s) failed: %s", run_id, trigger, exc)
        finally:
            if cur is not con:
                try:
                    cur.close()
                except Exception:
                    pass
            with _lock:
                _active_run_id = None

    threading.Thread(target=_run, name=f"prospector-{run_id[:8]}", daemon=True).start()
    return run_id, ""


def _autorun_enabled() -> bool:
    # Default off — scheduled mining burns LLM/search credits; opt in explicitly.
    return os.environ.get("PROSPECTOR_AUTORUN", "false").lower().strip() in (
        "1", "true", "yes", "on",
    )


def _interval_seconds() -> float:
    try:
        hours = float(os.environ.get("PROSPECTOR_INTERVAL_HOURS", "24") or 24)
    except ValueError:
        hours = 24.0
    return max(0.25, hours) * 3600


def start_scheduler(get_connection: Callable[[], Any]) -> bool:
    """
    Start the interval scheduler (idempotent). get_connection must return the
    process-shared DuckDB connection.
    """
    global _scheduler_thread
    if not _autorun_enabled():
        logger.info("Prospector autorun disabled (PROSPECTOR_AUTORUN=false)")
        return False
    if _scheduler_thread is not None and _scheduler_thread.is_alive():
        return False

    boot_delay = float(os.environ.get("PROSPECTOR_BOOT_DELAY_SEC", "180") or 180)
    _stop_event.clear()

    def _loop() -> None:
        if _stop_event.wait(boot_delay):
            return
        while not _stop_event.is_set():
            try:
                con = get_connection()
                run_id, busy = try_start_run(con, trigger="scheduled")
                if busy:
                    logger.info("Scheduled mining skipped: %s", busy)
                else:
                    logger.info("Scheduled mining run started: %s", run_id)
            except Exception as exc:
                logger.error("Scheduled mining failed to start: %s", exc)
            if _stop_event.wait(_interval_seconds()):
                return

    _scheduler_thread = threading.Thread(
        target=_loop, name="prospector-scheduler", daemon=True,
    )
    _scheduler_thread.start()
    logger.info(
        "Prospector scheduler started (every %.1fh, first run in %.0fs)",
        _interval_seconds() / 3600, boot_delay,
    )
    return True


def stop_scheduler() -> None:
    _stop_event.set()

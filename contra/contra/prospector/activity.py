"""
Human-readable miner activity — why a run did (or did not) produce CRM leads.

The cascade already stores per-stage counts. This module turns those counts into
a plain-English diagnosis and a sample of dropped candidates so an operator can
see "lots of research, zero leads" without reading server logs.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Ordered funnel: each entry is (stat_key, stage_id, short label, death message).
_FUNNEL = (
    ("queries_used", "search", "Search",
     "No search queries were issued."),
    ("results_seen", "search", "Search results",
     "Every query came back empty — the web search returned nothing to read."),
    ("docs_fetched", "harvest", "Fetch docs",
     "Search returned results, but no usable documents were fetched."),
    ("harvested", "harvest", "Harvest names",
     "Documents were read, but no LP names were extracted."),
    ("resolved", "resolve", "Resolve",
     "Every harvested name was already known, a fund/GP, or otherwise dropped."),
    ("preranked", "prerank", "Prerank",
     "All new names failed structural ICP filters (PE-only, geo, etc.)."),
    ("corroborated", "corroborate", "Corroborate",
     "Names were found, but no independent source confirmed a fund commitment."),
    ("gated", "gate", "Gate",
     "Corroborated names never reached the LP gate (budget, timeout, or cost cap)."),
    ("promoted", "promote", "Promote",
     "The gate screened candidates but none returned YES — no CRM leads created."),
)


def diagnose_run(run: Dict[str, Any]) -> Dict[str, Any]:
    """
    Explain where the funnel died for one run row.

    Returns died_at, headline, detail, and the stage-by-stage funnel snapshot.
    """
    status = (run.get("status") or "").lower()
    error = (run.get("error") or "").strip()
    current = (run.get("current_stage") or "").strip()

    funnel = []
    for key, stage_id, label, _msg in _FUNNEL:
        funnel.append({
            "key": key,
            "stage": stage_id,
            "label": label,
            "count": int(run.get(key) or 0),
        })

    if status == "running":
        label = current or "starting"
        return {
            "died_at": None,
            "alive": True,
            "headline": f"Mining in progress — currently at {label}.",
            "detail": (
                "Stage counts update as each cascade step finishes. "
                "CRM leads only appear when Stage 5 (gate) returns YES."
            ),
            "funnel": funnel,
            "error": error or None,
        }

    if error:
        kind = "timeout" if "timeout" in error.lower() else (
            "cost_cap" if "cost_cap" in error.lower() else "error"
        )
        return {
            "died_at": kind,
            "alive": False,
            "headline": f"Run stopped early ({kind.replace('_', ' ')}).",
            "detail": error,
            "funnel": funnel,
            "error": error,
        }

    # Find the first stage that went to zero after a prior stage had survivors.
    prev_count: Optional[int] = None
    died_at = None
    detail = "Run completed."
    for key, stage_id, label, death_msg in _FUNNEL:
        count = int(run.get(key) or 0)
        if prev_count is not None and prev_count > 0 and count == 0:
            died_at = stage_id
            detail = death_msg
            break
        prev_count = count

    promoted = int(run.get("promoted") or 0)
    if promoted > 0:
        return {
            "died_at": None,
            "alive": False,
            "headline": f"Promoted {promoted} LP{'s' if promoted != 1 else ''} to CRM.",
            "detail": "Gate returned YES and leads were created.",
            "funnel": funnel,
            "error": None,
        }

    if died_at is None:
        # All zeros — never really started paid work.
        if int(run.get("queries_used") or 0) == 0:
            died_at = "search"
            detail = "No queries ran — check seeds and search provider credentials."
        else:
            died_at = "promote"
            detail = _FUNNEL[-1][3]

    # Special case: queries ran but the web returned nothing. The fix depends
    # entirely on WHY — a provider raising on every call is a credentials problem,
    # a provider answering "nothing found" is a query-phrasing problem, and those
    # two were indistinguishable until search_errors/search_empty were recorded.
    queries_used = int(run.get("queries_used") or 0)
    if (
        queries_used > 0
        and int(run.get("results_seen") or 0) == 0
        and int(run.get("harvested") or 0) == 0
    ):
        died_at = "search"
        errors = int(run.get("search_errors") or 0)
        empty = int(run.get("search_empty") or 0)
        provider = (run.get("search_provider") or "").strip() or "the search provider"
        if errors and not empty:
            detail = (
                f"All {errors} searches raised an error before returning. "
                f"Confirm PULSE_SEARCH_PROVIDER and the API key for {provider} "
                "on the host running the miner. See web_search_log for the error text."
            )
        elif empty:
            detail = (
                f"{provider} answered all {empty} searches but found nothing. "
                "The queries are too narrow — quoted phrases and OR operators are "
                "Google syntax, which a model-driven web search does not interpret."
            )
        else:
            detail = (
                f"Search returned 0 results for all {queries_used} queries. "
                "See web_search_log for what was sent and what came back."
            )

    # Friendly headline keyed by death stage.
    headlines = {
        "search": "Search is broken — queries ran but returned nothing.",
        "harvest": "Research ran, but harvested zero LP names.",
        "resolve": "Names found, but all were duplicates or non-LPs.",
        "prerank": "New names found, but all failed ICP structure filters.",
        "corroborate": "Research found names, but none got independent corroboration.",
        "gate": "Corroborated names never entered the gate.",
        "promote": "Gate screened candidates — none qualified as YES.",
    }
    if died_at == "search":
        if queries_used == 0:
            headlines["search"] = "Research never started — no queries issued."
        elif int(run.get("search_errors") or 0) and not int(run.get("search_empty") or 0):
            headlines["search"] = "Search is broken — every query raised an error."
        elif int(run.get("search_empty") or 0):
            headlines["search"] = "Searches ran, but the provider found nothing."
    return {
        "died_at": died_at,
        "alive": False,
        "headline": headlines.get(died_at, "No leads promoted."),
        "detail": detail,
        "funnel": funnel,
        "error": None,
    }


def _row_to_run(cols: List[str], row: tuple) -> Dict[str, Any]:
    d = dict(zip(cols, row))
    for field in ("seeds_json", "cost_json"):
        if isinstance(d.get(field), str):
            try:
                d[field] = json.loads(d[field])
            except json.JSONDecodeError:
                pass
    return d


def build_activity(con, *, drop_limit: int = 25) -> Dict[str, Any]:
    """
    Full activity payload for GET /api/prospector/activity.

    Includes last run diagnosis, cost rollup, and recent drop reasons so the UI
    can answer "what is the miner doing / why no leads?"
    """
    from contra.prospector.budget import (
        autorun_block_reason,
        consecutive_zero_yield_runs,
        daily_spend_usd,
        max_daily_cost_usd,
        max_run_cost_usd,
        max_runtime_seconds,
        max_runs_per_day,
        runs_started_today,
        zero_yield_pause_runs,
    )
    from contra.prospector.scheduler import active_run_id

    run_sql = """
        SELECT run_id, status, trigger, seeds_json, queries_used, results_seen,
               docs_fetched, harvested, candidates_found, new_candidates,
               resolved, preranked, corroborated, gated,
               qualified, review, rejected, promoted, seeds_added, error,
               search_calls, llm_calls, fetch_calls, gate_calls,
               estimated_cost_usd, duration_sec, cost_json, current_stage,
               search_ok, search_empty, search_errors, search_provider,
               CAST(started_at AS VARCHAR) AS started_at,
               CAST(completed_at AS VARCHAR) AS completed_at
        FROM prospector_runs
        ORDER BY started_at DESC
        LIMIT 1
    """
    legacy_sql = """
        SELECT run_id, status, trigger, seeds_json, queries_used, results_seen,
               docs_fetched, harvested, candidates_found, new_candidates,
               resolved, preranked, corroborated, gated,
               qualified, review, rejected, promoted, seeds_added, error,
               CAST(started_at AS VARCHAR) AS started_at,
               CAST(completed_at AS VARCHAR) AS completed_at
        FROM prospector_runs
        ORDER BY started_at DESC
        LIMIT 1
    """

    last_run = None
    try:
        row = con.execute(run_sql).fetchone()
        cols = [d[0] for d in con.description]
    except Exception:
        row = con.execute(legacy_sql).fetchone()
        cols = [d[0] for d in con.description]

    diagnosis = None
    if row:
        last_run = _row_to_run(cols, row)
        last_run["active"] = last_run["run_id"] == active_run_id()
        diagnosis = diagnose_run(last_run)

    drops: List[Dict[str, Any]] = []
    run_id = last_run["run_id"] if last_run else None
    if run_id:
        try:
            drows = con.execute(
                """
                SELECT investor_name, status, stage, verdict_reason, gate_verdict,
                       CAST(updated_at AS VARCHAR) AS updated_at
                FROM prospector_candidates
                WHERE run_id = ?
                  AND status IN ('review', 'rejected', 'dismissed')
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                [run_id, drop_limit],
            ).fetchall()
            dcols = [d[0] for d in con.description]
            drops = [dict(zip(dcols, r)) for r in drows]
        except Exception as exc:
            logger.debug("activity drops unavailable: %s", exc)

    # Stage breakdown across recent candidates (not just last run) — useful when
    # the review queue looks empty because everything is rejected/dismissed.
    stage_counts: Dict[str, int] = {}
    try:
        for stage, n in con.execute(
            """
            SELECT COALESCE(stage, 'unknown'), COUNT(*)
            FROM prospector_candidates
            WHERE updated_at >= CURRENT_DATE - INTERVAL 7 DAY
            GROUP BY 1
            """
        ).fetchall():
            stage_counts[str(stage)] = int(n)
    except Exception:
        pass

    block = autorun_block_reason(con)
    return {
        "active_run_id": active_run_id(),
        "last_run": last_run,
        "diagnosis": diagnosis,
        "drops": drops,
        "stage_counts_7d": stage_counts,
        "spend": {
            "today_usd": round(daily_spend_usd(con), 4),
            "today_runs": runs_started_today(con),
            "zero_yield_streak": consecutive_zero_yield_runs(con),
            "scheduled_blocked": bool(block),
            "scheduled_block_reason": block or None,
        },
        "caps": {
            "max_runtime_sec": max_runtime_seconds(),
            "max_run_cost_usd": max_run_cost_usd(),
            "max_daily_cost_usd": max_daily_cost_usd(),
            "max_runs_per_day": max_runs_per_day(),
            "zero_yield_pause": zero_yield_pause_runs(),
        },
    }

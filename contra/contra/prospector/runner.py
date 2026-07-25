"""
Prospector run loop — a 5-stage cascade.

Each stage is cheaper than the next and rejects on a DIFFERENT failure mode, so
the expensive LP gate only ever sees survivors. Correlated filters would buy
nothing; uncorrelated ones compound.

  1. HARVEST      (harvest.py)      find documents that name many LPs at once —
                                    fund-close announcements, directories,
                                    emerging-manager programs — fetch the full
                                    page, extract every LP with a VERBATIM span.
                                    Owns recall. ~15 searches + ~8 fetches.
  2. RESOLVE      (resolve.py)      identity errors: funds mistaken for LPs, GPs,
                                    portfolio companies, placeholders, and names
                                    we already hold or have already screened.
                                    Owns identity. Zero API cost.
  3. PRERANK      (prerank.py)      hard-kill structural disqualifiers from the
                                    ICP spec (PE-only, crypto-only, US/Europe-
                                    only, sanctioned) and rank the rest.
                                    Owns structural misfit. Zero API cost.
  4. CORROBORATE  (corroborate.py)  require a commitment quote from a DIFFERENT
                                    domain than the one that discovered the name.
                                    Owns single-source hallucination. ~2 searches
                                    each, no LLM.
  5. ADJUDICATE   (adjudicate.py)   the real LP gate, with corroborated quotes fed
                                    through its analyst-facts channel. YES is
                                    promoted to CRM with full gate provenance.
                                    Owns the verdict.

Every stage's survivor count is written to prospector_runs, so a run that yields
no leads reports exactly which stage the funnel died at instead of being a
mystery. Every candidate is persisted with the stage it reached and why it
stopped, so nothing disappears silently.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from typing import Any, Dict, List, Optional

from contra.prospector.models import Candidate

logger = logging.getLogger(__name__)


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


# ---------------------------------------------------------------------------
# Run bookkeeping
# ---------------------------------------------------------------------------

_STAT_COLUMNS = (
    "queries_used", "results_seen", "docs_fetched", "harvested",
    "candidates_found", "new_candidates", "resolved", "preranked",
    "corroborated", "gated", "qualified", "review", "rejected",
    "promoted", "seeds_added",
)


def _new_stats() -> Dict[str, int]:
    return {col: 0 for col in _STAT_COLUMNS}


def _start_run(con, run_id: str, trigger: str, seeds: List[Dict[str, Any]]) -> None:
    con.execute(
        "INSERT INTO prospector_runs (run_id, status, trigger, seeds_json) "
        "VALUES (?, 'running', ?, ?)",
        [
            run_id, trigger,
            json.dumps([{k: s[k] for k in ("seed_type", "value")} for s in seeds]),
        ],
    )


def _finish_run(con, run_id: str, stats: Dict[str, int], error: str = "") -> None:
    assignments = ", ".join(f"{col} = ?" for col in _STAT_COLUMNS)
    con.execute(
        f"""
        UPDATE prospector_runs SET
            status = ?, error = ?, completed_at = NOW(), {assignments}
        WHERE run_id = ?
        """,
        [
            "failed" if error else "completed", error or None,
            *[stats.get(col, 0) for col in _STAT_COLUMNS],
            run_id,
        ],
    )


# ---------------------------------------------------------------------------
# Candidate persistence
# ---------------------------------------------------------------------------

def _persist(con, cand: Candidate, run_id: str) -> None:
    """
    Upsert one candidate with everything the cascade learned about it.

    Includes the stage it reached and why it stopped, so the queue is a complete
    audit trail rather than only a list of survivors.
    """
    try:
        con.execute(
            """
            INSERT INTO prospector_candidates
                (investor_name, name_key, entity_type, geography,
                 discovery_evidence, source_url, source_domain, run_id, seed,
                 status, verdict_reason, stage, prerank_score, prerank_checks_json,
                 source_diversity, corroborated, corroboration_json,
                 gate_verdict, revisit_date)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (name_key) DO UPDATE SET
                status              = EXCLUDED.status,
                verdict_reason      = EXCLUDED.verdict_reason,
                stage               = EXCLUDED.stage,
                entity_type         = COALESCE(EXCLUDED.entity_type, prospector_candidates.entity_type),
                geography           = COALESCE(EXCLUDED.geography, prospector_candidates.geography),
                discovery_evidence  = COALESCE(EXCLUDED.discovery_evidence, prospector_candidates.discovery_evidence),
                source_url          = COALESCE(EXCLUDED.source_url, prospector_candidates.source_url),
                source_domain       = COALESCE(EXCLUDED.source_domain, prospector_candidates.source_domain),
                prerank_score       = EXCLUDED.prerank_score,
                prerank_checks_json = EXCLUDED.prerank_checks_json,
                source_diversity    = EXCLUDED.source_diversity,
                corroborated        = EXCLUDED.corroborated,
                corroboration_json  = EXCLUDED.corroboration_json,
                gate_verdict        = EXCLUDED.gate_verdict,
                revisit_date        = EXCLUDED.revisit_date,
                run_id              = EXCLUDED.run_id,
                updated_at          = NOW()
            """,
            [
                cand.name, cand.name_key,
                cand.entity_type or None, cand.geography or None,
                (cand.span or "")[:1000] or None,
                cand.source_url or None, cand.source_domain or None,
                run_id, cand.seed or cand.doc_type or "web",
                cand.status, (cand.verdict_reason or "")[:800],
                cand.stage, cand.prerank_score,
                json.dumps(cand.prerank_checks) if cand.prerank_checks else None,
                cand.source_diversity, cand.corroborated,
                json.dumps([
                    {"quote": c.quote, "url": c.url, "domain": c.domain}
                    for c in cand.corroborations
                ]) if cand.corroborations else None,
                cand.gate_verdict or None,
                cand.revisit_date,
            ],
        )
    except Exception as exc:
        logger.warning("Persist failed for %s: %s", cand.name, exc)


def _count_statuses(stats: Dict[str, int], candidates: List[Candidate]) -> None:
    for cand in candidates:
        if cand.status == "promoted":
            stats["qualified"] += 1
        elif cand.status in ("qualified", "review", "rejected"):
            stats[cand.status] += 1


# ---------------------------------------------------------------------------
# Seed expansion
# ---------------------------------------------------------------------------

def _expand_seeds(con, run_id: str, gated: List[Candidate], stats: Dict[str, int]) -> None:
    """
    Grow the frontier from verified results only.

    Confirmed LPs become `confirmed_lp` seeds so their co-LPs can be found. Fund
    names come from `lp_commitments_found` on the gate result rather than from an
    extraction pass, so a hallucinated fund name can never become a seed.
    """
    from contra.prospector.seeds import add_seed

    for cand in gated:
        if cand.gate_verdict != "yes":
            continue
        if add_seed(
            con, "confirmed_lp", cand.name,
            geography=cand.geography or None, origin=f"run:{run_id[:8]}",
        ):
            stats["seeds_added"] += 1


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def run_prospector(
    con,
    *,
    max_seeds: Optional[int] = None,
    max_queries: Optional[int] = None,
    max_candidates: Optional[int] = None,
    promote: bool = True,
    trigger: str = "manual",
    run_id: Optional[str] = None,
) -> Dict[str, Any]:
    """
    One budgeted cascade run. Returns the run stats dict.

    Budgets (env-overridable): PROSPECTOR_MAX_SEEDS=5, PROSPECTOR_MAX_QUERIES=12,
    PROSPECTOR_MAX_DOCS=8, PROSPECTOR_MAX_CANDIDATES=25 (into corroboration),
    PROSPECTOR_MAX_GATE=6 (into the gate).
    """
    from contra.prospector.adjudicate import adjudicate
    from contra.prospector.corroborate import corroborate
    from contra.prospector.harvest import harvest
    from contra.prospector.prerank import prerank
    from contra.prospector.resolve import resolve
    from contra.prospector.seeds import (
        ensure_default_seeds,
        geo_rotation,
        mark_seeds_mined,
        pick_seeds,
        queries_for_seed,
    )

    max_seeds = max_seeds or _env_int("PROSPECTOR_MAX_SEEDS", 5)
    max_queries = max_queries or _env_int("PROSPECTOR_MAX_QUERIES", 12)
    max_docs = _env_int("PROSPECTOR_MAX_DOCS", 8)
    max_candidates = max_candidates or _env_int("PROSPECTOR_MAX_CANDIDATES", 25)

    run_id = run_id or uuid.uuid4().hex
    stats = _new_stats()

    ensure_default_seeds(con)
    # Templates get the majority of seed slots because each peer_fund seed emits two
    # queries against one template's one, so an even split in seats is a 3:1 split in
    # searches. Templates are also the only seeds carrying the ICP geography rotation.
    seeds = pick_seeds(con, limit=max_seeds, min_templates=max(2, max_seeds * 3 // 5))
    rotation = geo_rotation(con)  # read before _start_run so it advances per run
    _start_run(con, run_id, trigger, seeds)

    try:
        # ----- 1. HARVEST -----------------------------------------------------
        queries: List[str] = []
        for seed in seeds:
            queries.extend(queries_for_seed(seed, rotation=rotation))
        queries = queries[:max_queries]
        stats["queries_used"] = len(queries)

        seed_label = ", ".join(s["value"][:40] for s in seeds[:3])
        harvested, harvest_stats = harvest(
            queries, seed_label=seed_label, max_docs=max_docs,
        )
        stats.update({k: harvest_stats.get(k, 0) for k in
                      ("results_seen", "docs_fetched", "harvested")})
        # candidates_found kept for backward compatibility with the old funnel.
        stats["candidates_found"] = stats["harvested"]

        if not harvested:
            mark_seeds_mined(con, [s["seed_id"] for s in seeds])
            _finish_run(con, run_id, stats)
            return {"run_id": run_id, **stats}

        # ----- 2. RESOLVE (free) ---------------------------------------------
        fresh, known, dropped = resolve(con, harvested)
        stats["resolved"] = len(fresh)
        stats["new_candidates"] = len(fresh)
        for cand in dropped + known:
            _persist(con, cand, run_id)
        _count_statuses(stats, dropped + known)

        # ----- 3. PRERANK (free) ---------------------------------------------
        ranked, pre_dropped = prerank(fresh)
        stats["preranked"] = len(ranked)
        for cand in pre_dropped:
            _persist(con, cand, run_id)
        _count_statuses(stats, pre_dropped)

        ranked = ranked[:max_candidates]

        # ----- 4. CORROBORATE -------------------------------------------------
        confirmed, unconfirmed = corroborate(ranked)
        stats["corroborated"] = len(confirmed)
        for cand in unconfirmed:
            _persist(con, cand, run_id)
        _count_statuses(stats, unconfirmed)

        # ----- 5. ADJUDICATE --------------------------------------------------
        gated, deferred, gate_stats = adjudicate(con, confirmed, promote=promote)
        stats["gated"] = gate_stats["gated"]
        stats["promoted"] = gate_stats["promoted"]
        for cand in gated + deferred:
            _persist(con, cand, run_id)
        _count_statuses(stats, gated + deferred)

        _expand_seeds(con, run_id, gated, stats)
        mark_seeds_mined(con, [s["seed_id"] for s in seeds])
        _finish_run(con, run_id, stats)

    except Exception as exc:
        logger.error("Prospector run %s failed: %s", run_id, exc, exc_info=True)
        _finish_run(con, run_id, stats, error=f"{type(exc).__name__}: {exc}")
        raise

    return {"run_id": run_id, **stats}

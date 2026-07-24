"""
Prospector run loop — mine, extract, dedupe, verify, promote, expand.

One run:
  1. SEEDS    pick the least-recently-mined seeds (peer funds, confirmed LPs,
              query templates) and turn them into concrete search queries.
  2. SEARCH   run the queries against Tavily in parallel (budgeted).
  3. EXTRACT  one structured LLM pass pulls named LP candidates out of the
              search results, each with an evidence quote + source URL.
  4. DEDUPE   drop names already in CRM / allocators / prior candidates /
              dismissed (exact name_key, then fuzzy >= 93).
  5. VERIFY   per new candidate: 2 targeted searches, then an LLM fills the
              5-check scorecard. Any check whose evidence is not literally
              quotable from the gathered snippets is downgraded to unknown —
              the agent cannot invent facts.
  6. PERSIST  scorecard + candidate row. Qualified candidates auto-promote to
              CRM (source='prospector'); review candidates wait in the queue.
  7. EXPAND   verified fund names become new peer_fund seeds; qualified LPs
              become confirmed_lp seeds. The frontier grows on its own.

Everything is recorded in prospector_runs so each run's funnel is auditable.
"""

from __future__ import annotations

import json
import logging
import os
import re
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional, Set, Tuple

from pydantic import BaseModel, ConfigDict, Field

from contra.prospector.seeds import (
    ensure_default_seeds,
    mark_seeds_mined,
    pick_seeds,
    add_seed,
    queries_for_seed,
)
from contra.scorecard import (
    LpScorecard,
    ScorecardExtraction,
    scorecard_from_extraction,
    upsert_scorecard,
)

logger = logging.getLogger(__name__)


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


# ---------------------------------------------------------------------------
# LLM schemas
# ---------------------------------------------------------------------------

class _MinedCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    entity_type: str = ""       # family office | fund of funds | individual | ...
    geography: str = ""
    evidence: str = Field(default="", max_length=300)  # verbatim-ish quote
    source_url: str = ""
    confidence: str = "medium"  # high | medium | low


class _MinedBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidates: List[_MinedCandidate] = Field(default_factory=list)


class _VerifyExtraction(ScorecardExtraction):
    """Scorecard + the peer funds named in evidence (fuel for seed expansion)."""
    funds_mentioned: List[str] = Field(default_factory=list, max_length=6)


_EXTRACT_SYSTEM = """You extract LP (limited partner) candidates from web search results.

INCLUDE only real, named entities or people with evidence of VC fund LP behavior:
committed capital to a venture fund as an LP, anchored a Fund I, runs an
emerging-manager program, disclosed as an LP in a fund close.

EXCLUDE: GPs and fund managers themselves, PE-buyout-only firms, secondaries
firms, pure direct/angel investors with no fund commitments, portfolio
companies, journalists, and anything you cannot point to in the provided text.

evidence MUST be a short quote or tight paraphrase of the provided snippet that
names the fund or program. source_url MUST be one of the provided URLs."""

_VERIFY_SYSTEM = """You are scoring one LP candidate against a 5-check scorecard using ONLY
the research snippets provided. For each check answer pass / fail / unknown:

1. fund_lp         — has committed to at least one VC fund as an LP.
2. new_managers    — has backed a Fund I / first-time or emerging manager,
                     or runs an emerging-manager program.
3. thesis_fit      — appetite for AI / deep tech / technology venture.
4. geography_fit   — invests in or from Asia, Southeast Asia, India,
                     Middle East, or explicitly global.
5. no_disqualifier — fail ONLY if the snippets show a structural blocker:
                     PE-buyout-only, direct-deals-only, crypto-only mandate,
                     explicit US/Europe-only mandate, or the entity is itself
                     a GP raising funds.

HARD RULES:
- evidence must be quotable from the snippets (short quote). If you cannot
  quote it, the status is unknown with empty evidence.
- source_url must be one of the snippet URLs.
- Never guess from the entity's name alone.
- funds_mentioned: names of VC funds the snippets say this LP committed to."""


# ---------------------------------------------------------------------------
# Search helpers
# ---------------------------------------------------------------------------

def _get_prospector_search_provider():
    """
    Prefer Claude web search (uses ANTHROPIC_API_KEY, adaptive multi-hop).
    Fall back to whatever PULSE_SEARCH_PROVIDER is set to, then Tavily.
    """
    from agents.research.web_search import SearchUnavailable, get_search_provider

    # Claude built-in web_search first — same key as the LLM, better for LP mining.
    for name in ("anthropic", None, "tavily"):
        try:
            return get_search_provider(name) if name else get_search_provider()
        except SearchUnavailable:
            continue
    raise SearchUnavailable(
        "No search provider for Prospector. Set ANTHROPIC_API_KEY (recommended) "
        "or TAVILY_API_KEY."
    )


def _parallel_search(queries: List[str], per_query: int = 5, max_workers: int = 4) -> List[Any]:
    """Run searches in parallel; returns flat SearchResult list."""
    from agents.research.web_search import FetchError, SearchUnavailable

    provider = _get_prospector_search_provider()
    # Claude web search is heavier per call — keep concurrency modest.
    provider_name = getattr(provider, "provider", "") or type(provider).__name__
    workers = 2 if "anthropic" in provider_name.lower() else max_workers

    results: List[Any] = []

    def _one(q: str) -> List[Any]:
        try:
            return provider.search(q, max_results=per_query).results
        except (SearchUnavailable, FetchError) as exc:
            logger.debug("Prospector search failed (%s): %s", q, exc)
            return []

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_one, q): q for q in queries}
        for fut in as_completed(futures):
            results.extend(fut.result())

    # Dedupe by URL, keep best score first
    seen: Set[str] = set()
    unique: List[Any] = []
    for r in sorted(results, key=lambda x: x.score, reverse=True):
        if r.url and r.url not in seen:
            seen.add(r.url)
            unique.append(r)
    return unique


def _results_to_corpus(results: List[Any], max_chars: int = 12000) -> Tuple[str, List[str]]:
    """Compile search results into a snippet corpus the LLM (and verifier) sees."""
    blocks: List[str] = []
    urls: List[str] = []
    total = 0
    for r in results:
        body = (r.raw_content or r.snippet or "")[:1200]
        block = f"URL: {r.url}\nTITLE: {r.title}\n{body}\n---"
        if total + len(block) > max_chars:
            break
        blocks.append(block)
        urls.append(r.url)
        total += len(block)
    return "\n".join(blocks), urls


# ---------------------------------------------------------------------------
# Evidence quotability check (same philosophy as gate evidence_verifier)
# ---------------------------------------------------------------------------

_GENERIC_TOKENS = {
    "fund", "funds", "capital", "ventures", "venture", "partners", "partner",
    "the", "and", "for", "with", "lp", "group", "global", "management",
    "family", "office", "investor", "investors", "limited", "committed",
    "emerging", "manager", "managers", "program", "invests", "backed",
}


def _quotable(evidence: str, corpus_low: str) -> bool:
    """Evidence counts only if its distinctive tokens all appear in the corpus."""
    if not evidence:
        return False
    tokens = [t for t in re.findall(r"[a-z0-9]{4,}", evidence.lower()) if t not in _GENERIC_TOKENS]
    if not tokens:
        return False
    hits = sum(1 for t in set(tokens) if t in corpus_low)
    return hits >= max(1, int(len(set(tokens)) * 0.6))


def _enforce_quotability(extraction: _VerifyExtraction, corpus: str, urls: List[str]) -> _VerifyExtraction:
    """Downgrade any check whose evidence is not quotable from the corpus."""
    corpus_low = corpus.lower()
    url_set = set(urls)
    update: Dict[str, Any] = {}
    for cid in ("fund_lp", "new_managers", "thesis_fit", "geography_fit", "no_disqualifier"):
        status = getattr(extraction, f"{cid}_status")
        evidence = getattr(extraction, f"{cid}_evidence")
        source_url = getattr(extraction, f"{cid}_source_url")
        if status != "unknown" and not _quotable(evidence, corpus_low):
            update[f"{cid}_status"] = "unknown"
            update[f"{cid}_evidence"] = ""
            update[f"{cid}_source_url"] = ""
        elif source_url and source_url not in url_set:
            update[f"{cid}_source_url"] = ""
    # Hallucinated fund names must not become seeds.
    kept_funds = [f for f in extraction.funds_mentioned if f and f.lower() in corpus_low]
    if kept_funds != extraction.funds_mentioned:
        update["funds_mentioned"] = kept_funds
    if update:
        extraction = extraction.model_copy(update=update)
    return extraction


# ---------------------------------------------------------------------------
# Dedupe
# ---------------------------------------------------------------------------

def _known_name_keys(con) -> Set[str]:
    keys: Set[str] = set()
    for sql in (
        "SELECT name_key FROM crm_leads",
        "SELECT name_key FROM crm_gate_reviews",
        "SELECT name_key FROM crm_dismissed",
        "SELECT name_key FROM prospector_candidates",
        "SELECT DISTINCT name_key FROM outreach_log WHERE name_key IS NOT NULL",
    ):
        try:
            keys.update(r[0] for r in con.execute(sql).fetchall() if r[0])
        except Exception:
            pass
    return keys


def _allocator_names(con) -> List[str]:
    try:
        return [
            r[0] for r in con.execute("SELECT canonical_name FROM allocators").fetchall()
            if r[0]
        ]
    except Exception:
        return []


def _is_duplicate(name: str, name_key: str, known_keys: Set[str], allocator_names: List[str]) -> bool:
    if name_key in known_keys:
        return True
    try:
        from rapidfuzz import fuzz, process

        match = process.extractOne(
            name, allocator_names, scorer=fuzz.token_sort_ratio, score_cutoff=93,
        )
        return match is not None
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Verification stage
# ---------------------------------------------------------------------------

def _verify_candidate(
    cand: _MinedCandidate,
    llm,
    per_query: int = 4,
) -> Tuple[Optional[_VerifyExtraction], str]:
    """Targeted research + LLM scorecard + quotability enforcement."""
    queries = [
        f'"{cand.name}" limited partner venture fund commitment',
        f'"{cand.name}" family office fund LP emerging manager investment',
    ]
    results = _parallel_search(queries, per_query=per_query, max_workers=2)
    corpus, urls = _results_to_corpus(results, max_chars=9000)

    # The discovery snippet is evidence too.
    if cand.evidence:
        corpus = f"URL: {cand.source_url}\nTITLE: discovery snippet\n{cand.evidence}\n---\n{corpus}"
        if cand.source_url:
            urls = [cand.source_url] + urls

    if len(corpus) < 100:
        return None, "no verification evidence found"

    prompt = (
        f"CANDIDATE: {cand.name}"
        + (f" ({cand.entity_type})" if cand.entity_type else "")
        + (f", {cand.geography}" if cand.geography else "")
        + f"\n\n=== RESEARCH SNIPPETS ===\n{corpus}\n\n"
        "Fill the 5-check scorecard. Quote evidence only from the snippets above."
    )
    try:
        extraction = llm.structured(
            prompt=prompt,
            response_model=_VerifyExtraction,
            system=_VERIFY_SYSTEM,
            max_tokens=1600,
        )
    except Exception as exc:
        return None, f"verification LLM failed: {exc}"

    extraction = _enforce_quotability(extraction, corpus, urls)
    return extraction, ""


# ---------------------------------------------------------------------------
# Promotion
# ---------------------------------------------------------------------------

def _promote_to_crm(con, cand: _MinedCandidate, sc: LpScorecard) -> Optional[str]:
    """Insert a qualified candidate as a CRM lead (source='prospector')."""
    from contra.crm.writer import _insert_lead

    checks = sc.check_map()
    fund_lp_ev = checks.get("fund_lp")
    details = (
        f"Prospector-verified LP. {sc.verdict_reason} "
        f"Hook ({sc.yes_reason}): {sc.yes_evidence}"
    )
    try:
        lead = _insert_lead(con, {
            "investor_name": cand.name,
            "investor_type": cand.entity_type or None,
            "investor_location": cand.geography or None,
            "investor_details": details[:800],
            "pipeline_stage": "Prospect",
            "status": "active",
            "source_file": (fund_lp_ev.source_url if fund_lp_ev else "") or cand.source_url,
        }, source="prospector")
        return lead.lead_id
    except ValueError:
        return None  # already in CRM — fine
    except Exception as exc:
        logger.warning("Prospector promote failed for %s: %s", cand.name, exc)
        return None


# ---------------------------------------------------------------------------
# Run bookkeeping
# ---------------------------------------------------------------------------

def _start_run(con, run_id: str, trigger: str, seeds: List[Dict[str, Any]]) -> None:
    con.execute(
        "INSERT INTO prospector_runs (run_id, status, trigger, seeds_json) VALUES (?, 'running', ?, ?)",
        [run_id, trigger, json.dumps([{k: s[k] for k in ("seed_type", "value")} for s in seeds])],
    )


def _finish_run(con, run_id: str, stats: Dict[str, int], error: str = "") -> None:
    con.execute(
        """
        UPDATE prospector_runs SET
            status = ?, error = ?, completed_at = NOW(),
            queries_used = ?, results_seen = ?, candidates_found = ?,
            new_candidates = ?, qualified = ?, review = ?, rejected = ?,
            promoted = ?, seeds_added = ?
        WHERE run_id = ?
        """,
        [
            "failed" if error else "completed", error or None,
            stats.get("queries_used", 0), stats.get("results_seen", 0),
            stats.get("candidates_found", 0), stats.get("new_candidates", 0),
            stats.get("qualified", 0), stats.get("review", 0),
            stats.get("rejected", 0), stats.get("promoted", 0),
            stats.get("seeds_added", 0),
            run_id,
        ],
    )


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
    One budgeted mining run. Returns the run stats dict.

    Budget defaults (env-overridable): PROSPECTOR_MAX_SEEDS=5,
    PROSPECTOR_MAX_QUERIES=10, PROSPECTOR_MAX_CANDIDATES=12.
    A default run costs roughly 10 + 2*12 = ~34 Tavily searches.
    """
    from agents.normalization.crm_normalizer import norm_key
    from agents.research.llm_client import get_llm_client

    max_seeds = max_seeds or _env_int("PROSPECTOR_MAX_SEEDS", 5)
    max_queries = max_queries or _env_int("PROSPECTOR_MAX_QUERIES", 10)
    max_candidates = max_candidates or _env_int("PROSPECTOR_MAX_CANDIDATES", 12)

    run_id = run_id or uuid.uuid4().hex
    stats: Dict[str, int] = {
        "queries_used": 0, "results_seen": 0, "candidates_found": 0,
        "new_candidates": 0, "qualified": 0, "review": 0, "rejected": 0,
        "promoted": 0, "seeds_added": 0,
    }

    ensure_default_seeds(con)
    seeds = pick_seeds(con, limit=max_seeds)
    _start_run(con, run_id, trigger, seeds)

    try:
        # ----- 1-2. Queries + parallel search --------------------------------
        queries: List[str] = []
        for seed in seeds:
            queries.extend(queries_for_seed(seed))
        queries = queries[:max_queries]
        stats["queries_used"] = len(queries)

        results = _parallel_search(queries, per_query=5)
        stats["results_seen"] = len(results)
        corpus, _urls = _results_to_corpus(results, max_chars=13000)

        # ----- 3. Extraction --------------------------------------------------
        mined: List[_MinedCandidate] = []
        if len(corpus) >= 200:
            llm = get_llm_client()
            prompt = (
                f"TARGET: up to {max_candidates * 2} named LP candidates.\n\n"
                f"=== SEARCH RESULTS ===\n{corpus}\n\n"
                "Extract LP candidates. Only entities with quotable fund-LP evidence."
            )
            try:
                batch = llm.structured(
                    prompt=prompt, response_model=_MinedBatch,
                    system=_EXTRACT_SYSTEM, max_tokens=2500,
                )
                mined = [c for c in batch.candidates if c.name and len(c.name.strip()) >= 3]
            except Exception as exc:
                logger.warning("Prospector extraction failed: %s", exc)
        stats["candidates_found"] = len(mined)

        # ----- 4. Dedupe ------------------------------------------------------
        known_keys = _known_name_keys(con)
        allocator_names = _allocator_names(con)
        fresh: List[_MinedCandidate] = []
        seen_in_run: Set[str] = set()
        for cand in mined:
            key = norm_key(cand.name)
            if not key or key in seen_in_run:
                continue
            seen_in_run.add(key)
            if _is_duplicate(cand.name, key, known_keys, allocator_names):
                continue
            fresh.append(cand)
        # High-confidence first so the verify budget goes to the best names.
        fresh.sort(key=lambda c: {"high": 0, "medium": 1}.get(c.confidence, 2))
        fresh = fresh[:max_candidates]
        stats["new_candidates"] = len(fresh)

        # ----- 5-7. Verify, persist, promote, expand --------------------------
        llm = get_llm_client()
        for cand in fresh:
            key = norm_key(cand.name)
            extraction, err = _verify_candidate(cand, llm)
            if extraction is None:
                sc = None
                status, reason = "review", f"Unverified: {err}"
            else:
                sc = scorecard_from_extraction(cand.name, extraction, source="prospector")
                status, reason = sc.verdict, sc.verdict_reason
                upsert_scorecard(con, sc)

            stats[status if status in ("qualified", "review", "rejected") else "review"] += 1

            promoted_lead: Optional[str] = None
            if promote and sc is not None and sc.verdict == "qualified":
                promoted_lead = _promote_to_crm(con, cand, sc)
                if promoted_lead:
                    stats["promoted"] += 1

            con.execute(
                """
                INSERT INTO prospector_candidates
                    (investor_name, name_key, entity_type, geography,
                     discovery_evidence, source_url, run_id, seed, status, verdict_reason)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (name_key) DO UPDATE SET
                    status = EXCLUDED.status,
                    verdict_reason = EXCLUDED.verdict_reason,
                    updated_at = NOW()
                """,
                [
                    cand.name, key, cand.entity_type or None, cand.geography or None,
                    cand.evidence[:500] or None, cand.source_url or None,
                    run_id, cand.entity_type or "web",
                    "promoted" if promoted_lead else status,
                    reason[:500],
                ],
            )

            # Seed expansion — verified funds and qualified LPs grow the frontier.
            if extraction is not None:
                for fund in extraction.funds_mentioned[:4]:
                    if add_seed(con, "peer_fund", fund, origin=f"expansion:{cand.name}"):
                        stats["seeds_added"] += 1
                if sc is not None and sc.verdict == "qualified":
                    if add_seed(con, "confirmed_lp", cand.name, geography=cand.geography or None,
                                origin=f"run:{run_id[:8]}"):
                        stats["seeds_added"] += 1

        mark_seeds_mined(con, [s["seed_id"] for s in seeds])
        _finish_run(con, run_id, stats)
    except Exception as exc:
        logger.error("Prospector run %s failed: %s", run_id, exc, exc_info=True)
        _finish_run(con, run_id, stats, error=f"{type(exc).__name__}: {exc}")
        raise

    return {"run_id": run_id, **stats}

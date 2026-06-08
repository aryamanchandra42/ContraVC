"""
PULSE Enrichment Agent — resolves unknown allocator attributes via web research.

Pipeline:
  1. Query DuckDB for target allocators (population=institutional_prospect,
     allocator_type='unknown' and/or NULL geography/hq_country/em_appetite).
  2. For each allocator: build a search query, fetch web results (cached), pass
     to LLM (instructor) → EnrichmentResult (strict Pydantic schema).
  3. Write a raw provenance record to entities_raw (source_type='api').
  4. COALESCE-only UPDATE on allocators — only fills NULL fields, never
     overwrites existing non-null values.
  5. If confidence < low_confidence_threshold → write_to_queue(allocator_types)
     for human review.

Graceful degradation:
  - No search provider? → skip web fetch; LLM gets only name + existing data.
  - No LLM? → skip LLM step; no enrichment writes happen (but provenance row
    is NOT written either — no LLM output = no fact to record).

Both cases are logged and counted; the agent never silently discards errors.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

ROOT = Path(__file__).resolve().parent.parent.parent
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Taxonomy helpers
# ---------------------------------------------------------------------------

def _normalize_allocator_type(raw: str) -> Optional[str]:
    """Map a raw LLM type string to a canonical AllocatorType value."""
    from agents.normalization.allocator_normalizer import normalize_lp_type_label
    try:
        return normalize_lp_type_label(raw)
    except Exception:
        return None


def _normalize_geography(raw: str) -> Optional[str]:
    """Map a raw geography string to a canonical Geography value."""
    if not raw:
        return None
    from agents.normalization.taxonomies import GEOGRAPHY_PATTERNS
    raw_lower = raw.lower().strip()
    for canon_val, patterns in GEOGRAPHY_PATTERNS.items():
        for pat in patterns:
            if pat.lower() in raw_lower or raw_lower in pat.lower():
                return str(canon_val)
    return raw.lower().replace(" ", "_")


def _normalize_appetite(raw: str) -> Optional[str]:
    """Map an appetite string to canonical: high | medium | low | none | unknown."""
    mapping = {
        "high": "high", "strong": "high", "very high": "high",
        "medium": "medium", "moderate": "medium", "some": "medium",
        "low": "low", "limited": "low", "minimal": "low",
        "none": "none", "no": "none", "not interested": "none",
    }
    return mapping.get(raw.lower().strip(), "unknown")


# ---------------------------------------------------------------------------
# Provenance helpers
# ---------------------------------------------------------------------------

def _write_research_raw_record(
    con,
    allocator_id: str,
    chunk_idx: int,
    payload: Dict[str, Any],
) -> str:
    """
    Insert one entities_raw row for an externally-researched enrichment fact.
    Returns the source_record_id.
    """
    from agents.ingestion.base import hash_content, make_source_record_id, persist_raw_records, RawRecord

    source_file = f"research/enrichment/{allocator_id}.json"
    source_offset = f"research:{allocator_id}:{chunk_idx}"
    content_hash = hash_content(payload)
    source_record_id = make_source_record_id(source_file, source_offset, content_hash)

    record = RawRecord(
        source_record_id=source_record_id,
        source_file=source_file,
        source_type="api",
        source_offset=source_offset,
        content_hash=content_hash,
        raw_content=payload,
    )
    persist_raw_records([record], con)
    return source_record_id


# ---------------------------------------------------------------------------
# Allocator update — COALESCE-only, preserves provenance
# ---------------------------------------------------------------------------

_ENRICHABLE_COLUMNS = (
    "allocator_type",
    "geography",
    "hq_country",
    "em_appetite",
    "ai_appetite",
    "stage_preference",
)


def _apply_enrichment_to_allocator(
    con,
    allocator_id: str,
    updates: Dict[str, str],
) -> int:
    """
    COALESCE-safe UPDATE: only sets columns that are currently NULL.
    Returns number of columns actually updated.
    """
    if not updates:
        return 0

    # Filter to only enrichable columns
    safe_updates = {
        k: v for k, v in updates.items()
        if k in _ENRICHABLE_COLUMNS and v not in (None, "", "unknown")
    }
    if not safe_updates:
        return 0

    set_clauses = [f"{col} = COALESCE({col}, ?)" for col in safe_updates]
    set_sql = ", ".join(set_clauses) + ", updated_at = NOW()"

    con.execute(
        f"UPDATE allocators SET {set_sql} WHERE CAST(allocator_id AS VARCHAR) = ?",
        list(safe_updates.values()) + [allocator_id],
    )
    return len(safe_updates)


# ---------------------------------------------------------------------------
# Core enrichment logic for a single allocator
# ---------------------------------------------------------------------------

def _enrich_single_allocator(
    con,
    allocator_id: str,
    canonical_name: str,
    existing: Dict[str, Any],
    llm_client,
    search_provider,
    low_conf_threshold: float,
) -> Dict[str, Any]:
    """
    Enrich one allocator. Returns a stats dict.
    """
    from agents.research.schemas import EnrichmentResult
    from agents.research.web_search import (
        build_lp_research_query,
        compile_search_context,
        SearchUnavailable,
        FetchError,
    )
    from agents.reviews.queue_writer import write_to_queue

    stats: Dict[str, Any] = {
        "allocator_id": allocator_id,
        "canonical_name": canonical_name,
        "searched": False,
        "llm_called": False,
        "columns_updated": 0,
        "queued": False,
        "error": None,
    }

    # --- Step 1: web search — run 3 targeted queries for full LP profile ---
    search_context = ""
    source_urls: List[str] = []
    try:
        from agents.research.web_search import build_lp_fit_queries
        queries = build_lp_fit_queries(canonical_name)
        all_results = []
        for query in queries:
            try:
                resp = search_provider.search(query, max_results=4)
                all_results.extend(resp.results)
            except (SearchUnavailable, FetchError):
                pass

        # Deduplicate by URL, keep top 10 by score
        seen_urls: set = set()
        unique_results = []
        for r in sorted(all_results, key=lambda x: x.score, reverse=True):
            if r.url not in seen_urls:
                seen_urls.add(r.url)
                unique_results.append(r)
            if len(unique_results) >= 10:
                break

        if unique_results:
            from agents.research.web_search import SearchResponse, compile_search_context
            merged = SearchResponse(
                query=f"[3-query research] {canonical_name}",
                results=unique_results,
            )
            search_context = compile_search_context(merged, max_chars=2500)
            source_urls = [r.url for r in unique_results]
            stats["searched"] = True

    except (SearchUnavailable, FetchError) as exc:
        logger.warning("Web search skipped for '%s': %s", canonical_name, exc)

    # --- Step 2: build LLM prompt ---
    existing_summary = "\n".join(
        f"  {k}: {v}" for k, v in existing.items() if v not in (None, "", "unknown")
    )
    prompt = f"""LP research for MyAsiaVC (AI-native VC, emerging markets, $30M raise).

LP: {canonical_name}
Known data: {existing_summary or "(none)"}

Web results:
{search_context or "(none — name-only research)"}

Fill the schema:
- Taxonomy: allocator_type, geography, hq_country, em_appetite, ai_appetite, stage_preference (canonical values only)
- Fit signals: em_track_record (yes/no/unknown), emerging_manager_history, ai_portfolio_evidence, check_size_evidence, venture_focus, recent_activity
- fit_assessment: strong=clear EM+AI+VC LP record | moderate=partial fit | weak=no EM/retail only | unknown=no evidence
- summary: 3 sentences (who, what they invest in, fit for MyAsiaVC)
- confidence per field [0-1]; null+0 if no evidence — never guess
"""

    # --- Step 3: call LLM ---
    try:
        result: EnrichmentResult = llm_client.structured(
            prompt=prompt,
            response_model=EnrichmentResult,
            system=(
                "You are a precise private-market analyst who classifies limited partners "
                "into canonical taxonomy categories. Be conservative: prefer null over guessing."
            ),
        )
        stats["llm_called"] = True
    except Exception as exc:
        logger.error("LLM call failed for '%s': %s", canonical_name, exc)
        stats["error"] = str(exc)
        return stats

    # --- Step 4: write provenance raw record ---
    enrichment_payload = {
        "allocator_id": allocator_id,
        "canonical_name": canonical_name,
        "enrichment_result": result.model_dump(),
        "source_urls": source_urls,
        "queries_run": 3,
    }
    source_record_id = _write_research_raw_record(con, allocator_id, 0, enrichment_payload)

    # --- Step 5: build canonical updates from EnrichmentResult ---
    updates: Dict[str, str] = {}
    min_confidence_to_write = 0.35  # below this we still queue but don't write

    def _maybe_add(col: str, enriched_field, normalizer=None):
        if enriched_field.value and enriched_field.confidence >= min_confidence_to_write:
            normalized = normalizer(enriched_field.value) if normalizer else enriched_field.value
            if normalized and normalized != "unknown":
                updates[col] = normalized

    _maybe_add("allocator_type", result.allocator_type, _normalize_allocator_type)
    _maybe_add("geography", result.geography, _normalize_geography)
    _maybe_add("hq_country", result.hq_country)
    _maybe_add("em_appetite", result.em_appetite, _normalize_appetite)
    _maybe_add("ai_appetite", result.ai_appetite, _normalize_appetite)
    _maybe_add("stage_preference", result.stage_preference)

    cols_written = _apply_enrichment_to_allocator(con, allocator_id, updates)
    stats["columns_updated"] = cols_written

    # --- Step 6: write fit intelligence to research notes ---
    _write_fit_note(allocator_id, canonical_name, result, source_urls)
    stats["fit_note_written"] = True

    # --- Step 7: review queue for low-confidence allocator_type ---
    type_field = result.allocator_type
    if type_field.value and type_field.confidence < low_conf_threshold:
        write_to_queue(
            target_type="allocator_types",
            entity_id=allocator_id,
            current_value={
                "proposed_type": type_field.value,
                "confidence": type_field.confidence,
                "reasoning": type_field.reasoning,
                "source_urls": type_field.source_urls,
            },
            evidence_pointers=[
                {"source_file": f"research/enrichment/{allocator_id}.json",
                 "source_record_id": source_record_id}
            ],
            confidence=type_field.confidence,
            reason=f"llm_research_low_confidence ({type_field.confidence:.2f} < {low_conf_threshold:.2f})",
            metadata={"canonical_name": canonical_name},
        )
        stats["queued"] = True

    logger.info(
        "Enriched '%s': %d cols updated, fit=%s, queued=%s",
        canonical_name, cols_written,
        (result.fit_assessment.value or "unknown"), stats["queued"]
    )
    return stats


# ---------------------------------------------------------------------------
# Fit intelligence output — research notes per LP
# ---------------------------------------------------------------------------

def _write_fit_note(
    allocator_id: str,
    canonical_name: str,
    result,
    source_urls: List[str],
) -> None:
    """
    Write a markdown research note per LP to processed_data/research_notes/.
    Also appends a row to the fit_summary.csv for quick review.
    """
    notes_dir = ROOT / "processed_data" / "research_notes"
    notes_dir.mkdir(parents=True, exist_ok=True)

    # --- Markdown note ---
    fit_val = result.fit_assessment.value or "unknown"
    fit_conf = result.fit_assessment.confidence
    fit_reason = result.fit_assessment.reasoning or ""
    summary = result.summary or ""

    def _field_line(label: str, field) -> str:
        val = field.value or "—"
        conf_str = f" (confidence: {field.confidence:.0%})" if field.confidence > 0 else ""
        reason_str = f"\n  > {field.reasoning}" if field.reasoning else ""
        return f"- **{label}:** {val}{conf_str}{reason_str}"

    md_lines = [
        f"# Research Note: {canonical_name}",
        f"",
        f"**Allocator ID:** `{allocator_id}`  ",
        f"**Research date:** {datetime.now(timezone.utc).strftime('%Y-%m-%d')}  ",
        f"**Fit verdict:** `{fit_val.upper()}` (confidence: {fit_conf:.0%})",
        f"",
        f"## Summary",
        summary or "_No summary generated._",
        f"",
        f"## ICP Fit Signals",
        _field_line("EM track record", result.em_track_record),
        _field_line("Emerging-manager history", result.emerging_manager_history),
        _field_line("AI/tech portfolio", result.ai_portfolio_evidence),
        _field_line("Check size", result.check_size_evidence),
        _field_line("Venture LP focus", result.venture_focus),
        _field_line("Recent activity", result.recent_activity),
        f"",
        f"## Taxonomy Classification",
        _field_line("Allocator type", result.allocator_type),
        _field_line("Geography", result.geography),
        _field_line("HQ country", result.hq_country),
        _field_line("EM appetite", result.em_appetite),
        _field_line("AI appetite", result.ai_appetite),
        _field_line("Stage preference", result.stage_preference),
        f"",
        f"## Source URLs",
    ] + (["- " + u for u in source_urls] if source_urls else ["_No sources (name-only research)._"])

    if fit_reason:
        md_lines.insert(
            md_lines.index("## ICP Fit Signals"),
            f"**Fit reasoning:** {fit_reason}\n",
        )

    note_path = notes_dir / f"{allocator_id}.md"
    note_path.write_text("\n".join(md_lines), encoding="utf-8")

    # --- Append to fit_summary.csv ---
    summary_path = notes_dir / "fit_summary.csv"
    write_header = not summary_path.exists()
    with open(summary_path, "a", encoding="utf-8", newline="") as f:
        import csv
        writer = csv.writer(f)
        if write_header:
            writer.writerow([
                "allocator_id", "canonical_name", "fit_verdict", "fit_confidence",
                "em_track_record", "ai_portfolio", "venture_focus",
                "check_size", "recent_activity", "allocator_type", "geography",
                "summary_short",
            ])
        writer.writerow([
            allocator_id,
            canonical_name,
            fit_val,
            f"{fit_conf:.2f}",
            result.em_track_record.value or "",
            result.ai_portfolio_evidence.value or "",
            result.venture_focus.value or "",
            result.check_size_evidence.value or "",
            result.recent_activity.value or "",
            result.allocator_type.value or "",
            result.geography.value or "",
            (summary[:120] + "...") if len(summary) > 120 else summary,
        ])


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def run_enrichment(
    con,
    population: str = "institutional_prospect",
    only_unknown_type: bool = True,
    research_fit: bool = False,
    limit: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Run the enrichment agent over target allocators.

    Parameters
    ----------
    con : DuckDB connection
    population : which allocator population to target
    only_unknown_type : if True, only process rows where allocator_type is 'unknown' or NULL.
                        Ignored when research_fit=True.
    research_fit : if True, run deep fit research over ALL allocators in population —
                   regardless of whether taxonomy fields are already filled.
                   Produces per-LP markdown notes and fit_summary.csv.
    limit : max number of allocators to process (None = all)

    Returns
    -------
    Dict with counts: targeted, searched, llm_called, columns_updated, queued, errors
    """
    from agents.research.llm_client import get_llm_client, LLMUnavailable
    from agents.research.web_search import get_search_provider, SearchUnavailable

    # Load thresholds
    uncertainty_path = ROOT / "prompts" / "uncertainty.yaml"
    with open(uncertainty_path, encoding="utf-8") as f:
        params = yaml.safe_load(f)
    low_conf_threshold = params.get("review_queue", {}).get("low_confidence_threshold", 0.40)

    # Get providers (graceful degradation)
    try:
        llm_client = get_llm_client()
    except LLMUnavailable as exc:
        logger.warning("LLM unavailable — enrichment agent will not run: %s", exc)
        return {
            "targeted": 0, "searched": 0, "llm_called": 0,
            "columns_updated": 0, "queued": 0, "errors": 0,
            "skipped_reason": str(exc),
        }

    try:
        search_provider = get_search_provider()
    except SearchUnavailable as exc:
        logger.warning("Search provider unavailable — will enrich without web context: %s", exc)
        search_provider = _NullSearchProvider()

    # Build target query
    if research_fit:
        # Deep fit research: all LPs, even those with taxonomy already filled
        type_filter = ""
        mode_label = "research_fit (ALL allocators)"
    else:
        type_filter = "AND (allocator_type = 'unknown' OR allocator_type IS NULL)" if only_unknown_type else ""
        mode_label = f"only_unknown={only_unknown_type}"

    limit_clause = f"LIMIT {limit}" if limit else ""

    rows = con.execute(
        f"""
        SELECT CAST(allocator_id AS VARCHAR), canonical_name,
               allocator_type, geography, hq_country, em_appetite, ai_appetite
        FROM allocators
        WHERE population = ?
        {type_filter}
        ORDER BY canonical_name
        {limit_clause}
        """,
        [population],
    ).fetchall()

    logger.info("Enrichment agent: %d allocators targeted (population=%s, mode=%s)",
                len(rows), population, mode_label)

    totals: Dict[str, int] = {
        "targeted": len(rows),
        "searched": 0,
        "llm_called": 0,
        "columns_updated": 0,
        "fit_notes_written": 0,
        "queued": 0,
        "errors": 0,
    }

    for allocator_id, canonical_name, alloc_type, geo, hq, em_app, ai_app in rows:
        existing = {
            "allocator_type": alloc_type,
            "geography": geo,
            "hq_country": hq,
            "em_appetite": em_app,
            "ai_appetite": ai_app,
        }
        try:
            stats = _enrich_single_allocator(
                con=con,
                allocator_id=allocator_id,
                canonical_name=canonical_name,
                existing=existing,
                llm_client=llm_client,
                search_provider=search_provider,
                low_conf_threshold=low_conf_threshold,
            )
        except Exception as exc:
            logger.error(
                "Unexpected error enriching '%s' (%s): %s",
                canonical_name, allocator_id, exc,
                exc_info=True,
            )
            totals["errors"] += 1
            continue

        if stats.get("error"):
            totals["errors"] += 1
        if stats.get("searched"):
            totals["searched"] += 1
        if stats.get("llm_called"):
            totals["llm_called"] += 1
        totals["columns_updated"] += stats.get("columns_updated", 0)
        if stats.get("fit_note_written"):
            totals["fit_notes_written"] += 1
        if stats.get("queued"):
            totals["queued"] += 1

    if totals["fit_notes_written"] > 0:
        summary_path = ROOT / "processed_data" / "research_notes" / "fit_summary.csv"
        logger.info(
            "Enrichment complete: %s | fit notes → %s",
            totals,
            summary_path,
        )
    else:
        logger.info("Enrichment complete: %s", totals)
    return totals


# ---------------------------------------------------------------------------
# Null search provider (fallback when no search configured)
# ---------------------------------------------------------------------------

class _NullSearchProvider:
    """Fallback when no search provider is configured. Returns empty results."""

    def search(self, query: str, max_results: int = 5):
        from agents.research.web_search import SearchResponse
        return SearchResponse(query=query, results=[], cached=False)

    def fetch(self, url: str) -> str:
        return ""

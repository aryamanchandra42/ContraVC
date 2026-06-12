"""
LP Prospector — hybrid discovery agent.

Quality comes from THREE channels merged and ranked — not one generic web search:

  1. INTERNAL MINING (highest signal, free)
     - v_crm_prospects (ICP tier 1/2, syndicate upgrades, benchmark top-50)
     - v_crm_icp_queue (READY / NEAR_READY institutional prospects)
     - Lookalikes from confirmed fund-LP allocators in investments table

  2. TARGETED WEB FAN-OUT (LP-specific queries, not one vague prompt)
     - 6–8 queries tuned to fund-close announcements, emerging-manager programs,
       family-office directories, LP-named press releases
     - Tavily + OpenAI search in parallel; compiled into one evidence block

  3. DEEP WEB SYNTHESIS (optional, when OpenAI configured)
     - One adaptive OpenAI web-search pass on the thesis
     - Extracted via structured LLM from compiled evidence (not raw JSON hope)

Candidates are scored, deduped, and ranked before return.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import List, Optional, Set, Tuple

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class LpCandidate(BaseModel):
    name: str
    entity_type: str = ""
    geography: str = ""
    rationale: str = ""
    source_url: str = ""
    confidence: str = "medium"
    source: str = "web"          # internal_db | icp_queue | syndicate | lookalike | web | deep_web
    fit_score: float = 0.0
    in_crm: bool = False
    in_database: bool = False
    already_screened: Optional[str] = None


class DiscoveryResult(BaseModel):
    query: str
    candidates: List[LpCandidate] = Field(default_factory=list)
    notes: str = ""
    sources_used: List[str] = Field(default_factory=list)
    internal_count: int = 0
    web_count: int = 0


class _ExtractedCandidate(BaseModel):
    name: str
    entity_type: str = ""
    geography: str = ""
    rationale: str = ""
    source_url: str = ""
    confidence: str = "medium"


class _ExtractionBatch(BaseModel):
    candidates: List[_ExtractedCandidate] = Field(default_factory=list)
    notes: str = ""


# ---------------------------------------------------------------------------
# Thesis → targeted query fan-out
# ---------------------------------------------------------------------------

_GEO_HINTS: List[Tuple[str, str]] = [
    ("southeast asia", "Southeast Asia"),
    ("singapore", "Singapore"),
    ("hong kong", "Hong Kong"),
    ("middle east", "Middle East"),
    ("mena", "Middle East"),
    ("dubai", "UAE"),
    ("uae", "UAE"),
    ("north america", "North America"),
    ("united states", "United States"),
    ("india", "India"),
    ("asia", "Asia"),
    ("europe", "Europe"),
    ("uk", "United Kingdom"),
    ("australia", "Australia"),
]

_ENTITY_HINTS: List[Tuple[str, str]] = [
    ("family office", "family office"),
    ("family offices", "family office"),
    ("fund of funds", "fund of funds"),
    ("fof", "fund of funds"),
    ("endowment", "endowment"),
    ("foundation", "foundation"),
    ("sovereign", "sovereign wealth"),
    ("corporate venture", "corporate"),
    ("institutional", "institution"),
    ("uhnw", "individual"),
    ("high net worth", "individual"),
]

_LP_SIGNAL_WORDS = (
    "limited partner", "lp in", "fund commitment", "anchor lp", "anchor investor",
    "emerging manager", "fund i", "first-time fund", "venture fund lp",
    "committed to", "backed fund", "fund-of-funds",
)


def _parse_thesis(thesis: str) -> Tuple[List[str], List[str], str]:
    """Return (geographies, entity_types, cleaned thesis)."""
    t = thesis.lower()
    geos: List[str] = []
    entities: List[str] = []
    for hint, label in _GEO_HINTS:
        if hint in t and label not in geos:
            geos.append(label)
    for hint, label in _ENTITY_HINTS:
        if hint in t and label not in entities:
            entities.append(label)
    return geos, entities, thesis.strip()


def build_discovery_query_fanout(thesis: str) -> List[str]:
    """
    LP-sourcing queries that surface fund-close LPs and allocator programs —
    not generic 'investor' hits.
    """
    geos, entities, clean = _parse_thesis(thesis)
    geo_phrase = " ".join(geos[:2]) if geos else ""
    entity_phrase = " ".join(entities[:2]) if entities else "limited partner"

    queries = [
        # Fund closes name LPs — highest-yield source on the open web
        f'"{geo_phrase}" venture fund close limited partners named anchor investor press release'.strip(),
        f'{entity_phrase} {geo_phrase} emerging manager program venture fund LP commitment'.strip(),
        f'{entity_phrase} {geo_phrase} "limited partner" venture capital fund commitment'.strip(),
        # Directories & lists
        f'{geo_phrase} family office venture capital fund investor directory list'.strip(),
        f'{geo_phrase} fund of funds venture emerging manager allocation'.strip(),
        # Fund I / first-time fund specific
        f'{geo_phrase} anchor LP first-time venture fund Fund I commitment'.strip(),
        # Sector overlap with our thesis
        f'{geo_phrase} {entity_phrase} AI technology venture fund limited partner'.strip(),
        # Thesis verbatim as fallback
        f'{clean} venture fund limited partner commitment evidence',
    ]
    # Deduplicate while preserving order
    seen: Set[str] = set()
    out: List[str] = []
    for q in queries:
        q = re.sub(r"\s+", " ", q).strip()
        if q and q not in seen:
            seen.add(q)
            out.append(q)
    return out[:8]


# ---------------------------------------------------------------------------
# Internal mining — your DB already has scored prospects
# ---------------------------------------------------------------------------

def _mine_internal_prospects(con, thesis: str, limit: int) -> List[LpCandidate]:
    """Pull high-scoring prospects from v_crm_prospects + ICP queue."""
    geos, entities, _ = _parse_thesis(thesis)
    candidates: List[LpCandidate] = []
    seen: Set[str] = set()

    def _add(
        name: str,
        entity_type: str,
        geography: str,
        rationale: str,
        source: str,
        score: float,
        confidence: str = "high",
    ) -> None:
        key = name.lower().strip()
        if not name or key in seen:
            return
        seen.add(key)
        candidates.append(LpCandidate(
            name=name,
            entity_type=entity_type or "",
            geography=geography or "",
            rationale=rationale,
            source_url="internal://pulse",
            confidence=confidence,
            source=source,
            fit_score=score,
        ))

    # 1. Ranked prospects view (ICP + syndicate + benchmark)
    try:
        rows = con.execute(
            """
            SELECT investor_name, investor_type, investor_location, icp_tier,
                   fit_score, warm_path_count, syndicate_score, suggested_source,
                   prospect_score
            FROM v_crm_prospects
            ORDER BY prospect_score DESC NULLS LAST
            LIMIT 80
            """
        ).fetchall()
        for r in rows:
            name, itype, loc, tier, fit, warm, synd, src, pscore = r
            # Soft-filter by thesis keywords
            blob = f"{name} {itype} {loc}".lower()
            thesis_l = thesis.lower()
            geo_match = not geos or any(g.lower() in blob or g.lower() in thesis_l for g in geos)
            entity_match = not entities or any(
                e.split()[0] in blob or e in (itype or "").lower() for e in entities
            )
            if not geo_match and not entity_match and pscore and float(pscore) < 40:
                continue
            rationale = (
                f"Internal prospect ({src}): ICP {tier or 'n/a'}, "
                f"fit {float(fit or 0):.2f}, {int(warm or 0)} warm paths, "
                f"syndicate score {float(synd or 0):.2f}"
            )
            _add(name, itype or "", loc or "", rationale, src or "internal_db", float(pscore or 0) + 20)
    except Exception as exc:
        logger.debug("Internal prospects query failed: %s", exc)

    # 2. ICP queue — READY / NEAR_READY not in CRM
    try:
        rows = con.execute(
            """
            SELECT investor_name, allocator_type, investor_location, icp_tier,
                   fit_score, warm_path_count, readiness, client_decision
            FROM v_crm_icp_queue
            WHERE readiness IN ('READY', 'NEAR_READY')
              AND gate_verdict IS NULL
            ORDER BY
                CASE readiness WHEN 'READY' THEN 1 ELSE 2 END,
                fit_score DESC NULLS LAST
            LIMIT 40
            """
        ).fetchall()
        for r in rows:
            name, atype, loc, tier, fit, warm, readiness, client_dec = r
            blob = f"{name} {atype} {loc}".lower()
            geo_match = not geos or any(g.lower() in blob for g in geos)
            if not geo_match and readiness != "READY":
                continue
            rationale = (
                f"ICP queue {readiness}: tier {tier}, fit {float(fit or 0):.2f}, "
                f"{int(warm or 0)} warm paths"
                + (f", client: {client_dec}" if client_dec else "")
            )
            score = float(fit or 0) * 100 + (30 if readiness == "READY" else 15)
            _add(name, atype or "", loc or "", rationale, "icp_queue", score)
    except Exception as exc:
        logger.debug("ICP queue query failed: %s", exc)

    candidates.sort(key=lambda c: -c.fit_score)
    return candidates[:limit]


def _mine_lookalikes(con, thesis: str, limit: int) -> List[LpCandidate]:
    """Find confirmed fund-LPs in DB with similar profiles to the thesis geography."""
    from contra.intelligence.brief import find_similar_confirmed_lps

    geos, _, _ = _parse_thesis(thesis)
    geo = geos[0] if geos else None
    try:
        similar = find_similar_confirmed_lps(con, geography=geo, limit=limit)
    except Exception as exc:
        logger.debug("Lookalike mining failed: %s", exc)
        return []

    out: List[LpCandidate] = []
    for lp in similar:
        dims = ", ".join(lp.get("match_dimensions") or [])
        rationale = (
            f"Lookalike to confirmed fund-LP in DB: {lp.get('fund_deal_count', 0)} fund deals, "
            f"similarity {lp.get('similarity_score', 0):.2f}"
            + (f" ({dims})" if dims else "")
        )
        out.append(LpCandidate(
            name=lp["name"],
            entity_type=lp.get("allocator_type") or lp.get("archetype") or "",
            geography=lp.get("geography") or "",
            rationale=rationale,
            source_url="internal://lookalike",
            confidence="high",
            source="lookalike",
            fit_score=50 + float(lp.get("similarity_score", 0)) * 30,
        ))
    return out


# ---------------------------------------------------------------------------
# Web mining — multi-query fan-out + structured extraction
# ---------------------------------------------------------------------------

_EXTRACTION_SYSTEM = """You extract LP sourcing candidates from web research evidence.

RULES:
- Only include REAL named entities/people with evidence of FUND LP behavior
  (committed capital to a VC fund as limited partner, runs emerging-manager program, etc.)
- EXCLUDE: GPs, PE buyout-only, secondaries firms, pure angel/direct investors,
  generic listicles without named LPs, made-up names.
- rationale must cite specific evidence from the research (fund name, program, press release).
- confidence: high = named LP in fund close or verified program page;
  medium = strong indirect evidence; low = weak/unclear.
- Return up to the requested count. Prefer quality over quantity."""


def _fanout_web_context(thesis: str, max_chars: int = 14000) -> Tuple[str, List[str]]:
    """Run LP-specific query fan-out; return compiled context + source URLs."""
    from agents.research.web_search import (
        FetchError,
        SearchResponse,
        SearchResult,
        SearchUnavailable,
        compile_search_context,
        get_search_provider,
        openai_search_configured,
    )

    queries = build_discovery_query_fanout(thesis)
    all_results: List[SearchResult] = []
    seen_urls: Set[str] = set()

    # Channel A: Tavily / configured provider fan-out
    try:
        provider = get_search_provider()
        for q in queries:
            try:
                resp = provider.search(q, max_results=4)
                for r in resp.results:
                    if r.url and r.url not in seen_urls and not r.url.startswith("openai://"):
                        seen_urls.add(r.url)
                        all_results.append(r)
            except (SearchUnavailable, FetchError):
                pass
    except Exception as exc:
        logger.debug("Provider fan-out skipped: %s", exc)

    # Channel B: OpenAI deep synthesis on the thesis (adaptive multi-search)
    if openai_search_configured():
        try:
            from agents.research.web_search import OpenAIWebSearchProvider
            oai = OpenAIWebSearchProvider()
            deep_prompt = (
                f"LP SOURCING RESEARCH for thesis: {thesis}\n\n"
                "Find SPECIFIC NAMED limited partners who commit to VC funds (not direct angels).\n"
                "Search: fund close press releases naming LPs, emerging manager programs, "
                "family office venture allocations, endowment VC fund commitments, "
                "fund-of-funds manager lists.\n\n"
                "For each person/entity found, report:\n"
                "- Name\n- Type (family office / FoF / endowment / etc.)\n"
                "- Evidence of fund LP behavior (name the fund or program)\n"
                "- Source URL\n"
                "Exclude GPs, PE-only, secondaries, pure angels."
            )
            text, citations = oai.research(deep_prompt)
            if text:
                all_results.insert(0, SearchResult(
                    title=f"Deep synthesis: {thesis[:60]}",
                    url="openai://deep-discovery",
                    snippet=text[:500],
                    score=1.0,
                    raw_content=text,
                ))
            for c in citations:
                url = c.get("url", "")
                if url and url not in seen_urls:
                    seen_urls.add(url)
                    all_results.append(SearchResult(
                        title=c.get("title", url),
                        url=url,
                        snippet="(deep web citation)",
                        score=0.85,
                    ))
        except Exception as exc:
            logger.debug("Deep web synthesis skipped: %s", exc)

    if not all_results:
        return "", []

    # Sort by score, compile
    all_results.sort(key=lambda r: r.score, reverse=True)
    resp = SearchResponse(query=f"[discovery] {thesis}", results=all_results[:20])
    context = compile_search_context(resp, max_chars=max_chars)
    urls = [r.url for r in all_results if r.url and not r.url.startswith("openai://")]
    return context, urls


def _extract_candidates_from_context(
    thesis: str, context: str, limit: int,
) -> Tuple[List[LpCandidate], str]:
    """Structured LLM extraction from compiled web evidence."""
    if not context or len(context) < 200:
        return [], "Web fan-out returned insufficient evidence."

    from agents.research.llm_client import get_llm_client

    prompt = (
        f"THESIS: {thesis}\n"
        f"TARGET: up to {limit} high-quality LP candidates.\n\n"
        f"=== WEB RESEARCH EVIDENCE ===\n{context[:12000]}\n\n"
        "Extract candidates with fund-LP evidence only. Return JSON matching the schema."
    )
    try:
        llm = get_llm_client()
        batch = llm.structured(
            prompt=prompt,
            response_model=_ExtractionBatch,
            system=_EXTRACTION_SYSTEM,
            max_tokens=2500,
        )
    except Exception as exc:
        logger.warning("Discovery extraction failed: %s", exc)
        return [], f"Extraction failed: {exc}"

    out: List[LpCandidate] = []
    for raw in batch.candidates[:limit * 2]:
        name = raw.name.strip()
        if not name or len(name) < 3:
            continue
        cand = LpCandidate(
            name=name,
            entity_type=raw.entity_type,
            geography=raw.geography,
            rationale=raw.rationale[:400],
            source_url=raw.source_url,
            confidence=raw.confidence.lower() or "medium",
            source="web",
            fit_score=_score_web_candidate(raw),
        )
        out.append(cand)
    out.sort(key=lambda c: -c.fit_score)
    return out[:limit], batch.notes


def _score_web_candidate(raw: _ExtractedCandidate) -> float:
    score = 10.0
    if raw.confidence == "high":
        score += 25
    elif raw.confidence == "medium":
        score += 12
    rationale = (raw.rationale or "").lower()
    score += sum(6 for w in _LP_SIGNAL_WORDS if w in rationale)
    if raw.source_url and raw.source_url.startswith("http"):
        score += 5
    return score


# ---------------------------------------------------------------------------
# Merge, dedupe, rank
# ---------------------------------------------------------------------------

def _merge_candidates(
    internal: List[LpCandidate],
    lookalikes: List[LpCandidate],
    web: List[LpCandidate],
    limit: int,
) -> List[LpCandidate]:
    """Merge channels; internal/lookalike names win on dedup (higher trust)."""
    by_key: dict[str, LpCandidate] = {}
    order: List[str] = []

    # Web first (lowest priority on dedup)
    for c in web:
        key = c.name.lower().strip()
        if key not in by_key:
            by_key[key] = c
            order.append(key)

    for c in lookalikes:
        key = c.name.lower().strip()
        if key in by_key:
            existing = by_key[key]
            if c.fit_score > existing.fit_score:
                by_key[key] = c
        else:
            by_key[key] = c
            order.append(key)

    for c in internal:
        key = c.name.lower().strip()
        if key in by_key:
            # Boost: keep internal rationale but merge scores
            existing = by_key[key]
            c.fit_score = max(c.fit_score, existing.fit_score) + 10
            c.rationale = c.rationale or existing.rationale
            c.source_url = existing.source_url or c.source_url
        by_key[key] = c
        if key not in order:
            order.append(key)

    merged = sorted(by_key.values(), key=lambda c: -c.fit_score)
    return merged[:limit]


def _extract_json(text: str) -> Optional[dict]:
    """Legacy fallback: pull JSON from raw web search text."""
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.MULTILINE)
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start: i + 1])
                except json.JSONDecodeError:
                    return None
    return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def discover_lps(query: str, limit: int = 15, con=None) -> DiscoveryResult:
    """
    Hybrid discovery: internal DB + lookalikes + targeted web fan-out.
    Pass `con` (DuckDB connection) to enable internal mining — strongly recommended.
    """
    limit = max(3, min(limit, 30))
    sources_used: List[str] = []
    internal: List[LpCandidate] = []
    lookalikes: List[LpCandidate] = []
    web: List[LpCandidate] = []
    notes_parts: List[str] = []

    # --- Channel 1: Internal DB (best leads, always first) ---
    if con is not None:
        try:
            internal = _mine_internal_prospects(con, query, limit=limit)
            if internal:
                sources_used.append(f"internal_db ({len(internal)})")
        except Exception as exc:
            logger.warning("Internal mining failed: %s", exc)

        try:
            lookalikes = _mine_lookalikes(con, query, limit=min(8, limit))
            if lookalikes:
                sources_used.append(f"lookalikes ({len(lookalikes)})")
        except Exception as exc:
            logger.warning("Lookalike mining failed: %s", exc)

    # --- Channel 2: Targeted web fan-out + structured extraction ---
    web_notes = ""
    try:
        context, urls = _fanout_web_context(query)
        if context:
            sources_used.append(f"web_fanout ({len(urls)} urls)")
            web, web_notes = _extract_candidates_from_context(query, context, limit=limit)
            if web:
                sources_used.append(f"web_extracted ({len(web)})")
    except Exception as exc:
        logger.warning("Web mining failed: %s", exc)
        web_notes = str(exc)

    # Fallback: legacy single-call if fan-out produced nothing
    if not web and not internal and not lookalikes:
        try:
            from agents.research.web_search import OpenAIWebSearchProvider
            provider = OpenAIWebSearchProvider()
            prompt = (
                f"Find up to {limit} named LP candidates for: {query}\n"
                "Return JSON: {\"candidates\": [{\"name\",\"entity_type\",\"geography\","
                "\"rationale\",\"source_url\",\"confidence\"}], \"notes\": \"...\"}"
            )
            text, _ = provider.research(prompt)
            data = _extract_json(text) or {}
            for raw in (data.get("candidates") or []):
                web.append(LpCandidate(
                    name=str(raw.get("name", "")).strip(),
                    entity_type=str(raw.get("entity_type", "")),
                    geography=str(raw.get("geography", "")),
                    rationale=str(raw.get("rationale", ""))[:400],
                    source_url=str(raw.get("source_url", "")),
                    confidence=str(raw.get("confidence", "low")),
                    source="web",
                    fit_score=5.0,
                ))
            web_notes = str(data.get("notes", ""))
            sources_used.append("web_fallback")
        except Exception as exc:
            web_notes = f"All discovery channels failed: {exc}"

    candidates = _merge_candidates(internal, lookalikes, web, limit)

    internal_count = sum(1 for c in candidates if c.source in ("internal_db", "icp_queue", "syndicate", "benchmark"))
    internal_count += sum(1 for c in candidates if c.source == "lookalike")
    web_count = sum(1 for c in candidates if c.source in ("web", "deep_web"))

    if internal_count:
        notes_parts.append(f"{internal_count} from your scored internal data (ICP/syndicate/lookalikes)")
    if web_count:
        notes_parts.append(f"{web_count} from targeted web research")
    if web_notes:
        notes_parts.append(web_notes)
    if not candidates:
        notes_parts.append(
            "No candidates found. Try a more specific thesis (geography + allocator type), "
            "or run PULSE refresh to populate internal prospects."
        )

    return DiscoveryResult(
        query=query,
        candidates=candidates,
        notes=" · ".join(notes_parts),
        sources_used=sources_used,
        internal_count=internal_count,
        web_count=web_count,
    )


def flag_known_candidates(con, candidates: List[LpCandidate]) -> None:
    """Mark candidates already in CRM / allocator DB / previously gate-screened."""
    from agents.normalization.crm_normalizer import norm_key

    for cand in candidates:
        key = norm_key(cand.name)
        try:
            row = con.execute(
                "SELECT 1 FROM crm_leads WHERE name_key = ? LIMIT 1", [key]
            ).fetchone()
            cand.in_crm = row is not None
        except Exception:
            pass
        try:
            row = con.execute(
                "SELECT gate_verdict FROM crm_gate_reviews WHERE name_key = ? LIMIT 1", [key]
            ).fetchone()
            if row:
                cand.already_screened = row[0]
        except Exception:
            pass
        try:
            row = con.execute(
                "SELECT 1 FROM allocators WHERE LOWER(canonical_name) = LOWER(?) LIMIT 1",
                [cand.name],
            ).fetchone()
            cand.in_database = row is not None
            if row:
                cand.in_database = True
        except Exception:
            pass

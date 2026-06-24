"""
Deep LP research via OpenAI web search — single adaptive call.

Replaces the fixed 7-query Tavily fan-out for gate research when
PULSE_SEARCH_PROVIDER is 'openai' or 'auto' (with OPENAI_API_KEY set).

Why this is better than the query fan-out:
  - The model runs MULTIPLE searches internally and adapts follow-up searches
    to what it finds (e.g. finds "trustee of X family office" → searches
    "X family office fund commitments").
  - Identity disambiguation happens inside the call — it is told who we think
    the person is and instructed to discard results about namesakes.
  - Output is already structured analyst notes with citations, so far less of
    the verdict model's context is wasted on boilerplate snippets.

Results are cached in the shared research cache (TTL applies).
"""

from __future__ import annotations

import logging
from typing import List, Optional, Tuple

from agents.research.web_search import (
    FetchError,
    OpenAIWebSearchProvider,
    SearchUnavailable,
    _cache_key,
    _load_cache,
    _save_cache,
)

logger = logging.getLogger(__name__)

_RESEARCH_INSTRUCTIONS = """You are a private-markets research analyst screening a potential
limited partner (LP) for an AI-native VC Fund I ($30M, pre-seed to Series A, geographies:
Southeast Asia / North America / Middle East).

Run as many web searches as needed (identity, fund commitments, family office / institution
profile, portfolio, press, regulatory filings) and then write structured analyst notes.

IDENTITY DISCIPLINE:
- First establish WHO this is (role, firm, location). If multiple people/entities share the
  name, state the ambiguity and only report findings you can tie to the SAME person/entity
  via employer, location, or cross-referenced sources. Discard namesake noise.

REPORT — use exactly these numbered sections:
1. IDENTITY: who they are (role, firm, location, links). Note namesake risk if any.
2. CONFIRMED LP FUND COMMITMENTS: external VC funds where THIS person/entity committed
   capital as an LP. Format: "LP in [Fund] ([year]) — [source URL]". Write "None found"
   if none. CRITICAL: a GP/Principal role at a fund is NOT an LP commitment, and an
   employer fund's portfolio is NOT this person's LP activity.
3. ALLOCATOR TYPE: family office / fund-of-funds / endowment / corporate / angel /
   GP-at-a-fund / unknown — with evidence.
4. DIRECT / ANGEL ACTIVITY: direct startup investments, syndicate activity (this is
   NOT fund LP evidence — list it separately here).
5. SECTOR & GEOGRAPHY APPETITE: AI/tech exposure, emerging-market / Asia / MENA exposure —
   from actual allocations, not job titles.
6. DISQUALIFIERS: PE-only, buyout-only, direct-only, wrong geography, very large minimum
   check sizes, inactive (no activity in 24+ months). Actively hunt for these.
7. SOURCES: list every URL used.

Be terse and factual. Cite a source for every claim. If the web has essentially nothing
on this person/entity, say so explicitly — do not pad."""


def build_deep_research_prompt(
    name: str,
    screening_mode: str = "institutional",
    nfx_url: Optional[str] = None,
    known_context: str = "",
    match_untrusted: bool = False,
) -> str:
    parts: List[str] = [_RESEARCH_INSTRUCTIONS, f"\nLP TO RESEARCH: {name}"]
    if known_context:
        parts.append(f"KNOWN CONTEXT (from our database/upload): {known_context}")
    if nfx_url:
        parts.append(
            f"They have an NFX Signal profile at {nfx_url} — read it. Remember NFX Signal "
            "lists ANGEL/direct investors; presence there is NOT evidence of fund LP behavior."
        )
    if match_untrusted:
        parts.append(
            "WARNING: our database matched this name to a likely DIFFERENT person. "
            "Be extra careful with identity disambiguation."
        )
    if screening_mode == "nfx_individual":
        parts.append(
            "CONTEXT: this name comes from an NFX Signal angel-network export; most such "
            "people are angels or micro-fund GPs, not fund LPs. Focus section 2 hard."
        )
    return "\n\n".join(parts)


def openai_lp_deep_research(
    name: str,
    screening_mode: str = "institutional",
    nfx_url: Optional[str] = None,
    known_context: str = "",
    match_untrusted: bool = False,
    max_chars: int = 12000,
) -> Tuple[str, List[str]]:
    """
    Run one adaptive deep-research call; return (analyst_notes, source_urls).

    Raises SearchUnavailable / FetchError on failure so callers can fall back
    to the Tavily query fan-out.
    """
    provider = OpenAIWebSearchProvider()  # raises SearchUnavailable if no key

    cache_key = _cache_key(
        f"deep-research:{name.lower().strip()}:{screening_mode}:{nfx_url or ''}:{match_untrusted}"
    )
    cached = _load_cache(cache_key)
    if cached:
        logger.debug("Deep research cache hit for '%s'", name)
        return cached["notes"][:max_chars], list(cached.get("urls") or [])

    prompt = build_deep_research_prompt(
        name,
        screening_mode=screening_mode,
        nfx_url=nfx_url,
        known_context=known_context,
        match_untrusted=match_untrusted,
    )
    notes, citations = provider.research(prompt)
    if not notes.strip():
        raise FetchError(f"OpenAI deep research returned empty notes for '{name}'")

    urls: List[str] = []
    seen: set = set()
    for c in citations:
        if c["url"] and c["url"] not in seen:
            seen.add(c["url"])
            urls.append(c["url"])

    header = f"=== DEEP WEB RESEARCH (OpenAI {provider.model} + web search) ===\n"
    notes = header + notes.strip()

    _save_cache(cache_key, {"name": name, "notes": notes, "urls": urls})
    return notes[:max_chars], urls


def build_outreach_research_prompt(
    name: str,
    archetype: str,
    known_context: str = "",
) -> str:
    instructions = """You are an elite private-markets research analyst preparing a dossier for cold outreach.
We need a HIGH-CONVERTING, SPECIFIC hook to open an email to this limited partner / investor.

Run web searches to find non-obvious, highly specific "trigger" events or deep context from the last 12-24 months.

WHAT WE ARE LOOKING FOR (find at least one of these):
1. A specific, recent podcast interview, blog post, or tweet thread they published, and their specific thesis/takeaway.
2. A specific, named startup they recently backed or joined the board of (and WHY it's interesting).
3. A specific, named VC fund they recently anchored or committed to as an LP.
4. A specific new program, initiative, or geographic push their firm just announced.

IDENTITY DISCIPLINE:
- First establish WHO this is (role, firm, location). Discard namesake noise.

REPORT — use exactly these numbered sections:
1. IDENTITY: Role, firm, location.
2. THE BEST OUTREACH HOOKS: List 3-5 highly specific facts, quotes, or investments that prove we did our homework. DO NOT list generic facts like "they invest in AI". List specifics like "They led the Series A for [Startup] last month, focusing on their unique approach to [Niche]." or "In a recent interview on [Podcast], they argued that [Specific insight]."
3. SOURCES: List every URL used.

Be terse. Cite sources. If they have no online footprint, say "No specific hooks found." Do not pad."""
    
    parts = [instructions, f"\nLP TO RESEARCH: {name}"]
    if archetype != "unknown" and archetype != "generalist":
        parts.append(f"KNOWN ARCHETYPE: {archetype} (Focus your search on this aspect of their activity)")
    if known_context:
        parts.append(f"KNOWN CONTEXT (from our database): {known_context}")
    return "\n\n".join(parts)


def openai_lp_outreach_research(
    name: str,
    archetype: str = "generalist",
    known_context: str = "",
    max_chars: int = 12000,
) -> Tuple[str, List[str]]:
    """
    Run one adaptive deep-research call specifically tuned for finding cold-email hooks.
    """
    provider = OpenAIWebSearchProvider()

    cache_key = _cache_key(f"outreach-research:{name.lower().strip()}:{archetype}")
    cached = _load_cache(cache_key)
    if cached:
        logger.debug("Outreach research cache hit for '%s'", name)
        return (cached.get("notes") or "")[:max_chars], list(cached.get("urls") or [])

    prompt = build_outreach_research_prompt(name, archetype, known_context)
    notes, citations = provider.research(prompt)
    if not notes or not notes.strip():
        raise FetchError(f"OpenAI outreach research returned empty notes for '{name}'")

    urls: List[str] = []
    seen: set = set()
    for c in citations:
        if c.get("url") and c["url"] not in seen:
            seen.add(c["url"])
            urls.append(c["url"])

    header = f"=== OUTREACH DEEP WEB RESEARCH (OpenAI {provider.model} + web search) ===\n"
    notes = header + notes.strip()

    _save_cache(cache_key, {"name": name, "notes": notes, "urls": urls})
    return notes[:max_chars], urls

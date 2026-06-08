"""Mandatory web research for gate."""

from __future__ import annotations

import logging
from typing import List, Optional, Tuple

from agents.research.web_search import (
    FetchError,
    SearchResponse,
    SearchResult,
    SearchUnavailable,
    build_lp_fit_queries,
    compile_search_context,
    get_search_provider,
)

logger = logging.getLogger(__name__)


def _inject_pitchbook(
    lp_name: str,
    results: List[SearchResult],
) -> List[SearchResult]:
    """
    If PitchBook session cookies are available, try to fetch the LP's profile.

    Strategy (in priority order):
    1. If a pitchbook.com URL already appeared in Tavily results, fetch it
       authenticated to get the full page (not just the login redirect).
    2. Otherwise search PitchBook by LP name for the profile.

    The fetched result is inserted at position 0 with score=2.0 so the LLM
    sees PitchBook's structured data (AUM, LP type, recent funds) first.
    """
    try:
        from agents.research.pitchbook_fetch import cookies_available, pb_inject_result

        if not cookies_available():
            return results

        # Check if any Tavily result already points to a PitchBook profile URL
        pb_url = None
        for r in results:
            if "pitchbook.com/profiles/" in r.url:
                pb_url = r.url
                break

        pb_result = pb_inject_result(lp_name, pb_url=pb_url)
        if pb_result:
            # Remove any existing (un-authed) PitchBook entry so we don't duplicate
            filtered = [r for r in results if "pitchbook.com" not in r.url]
            logger.debug("PitchBook profile injected for '%s'", lp_name)
            return [pb_result] + filtered

    except Exception as exc:
        logger.debug("PitchBook injection skipped for '%s': %s", lp_name, exc)

    return results


def search_lp(name: str, max_chars: int = 4000) -> Tuple[str, List[str]]:
    """Run the appetite-oriented fit queries; return (context_text, source_urls).

    Single-LP gate passes max_chars=4000 so all 10 searched URLs' snippets reach
    the LLM (~1000 tokens, well within Groq's 131K context window).
    Batch gate passes max_chars=1200 to stay under free-tier TPM limits.
    """
    queries = build_lp_fit_queries(name)
    try:
        provider = get_search_provider()
    except SearchUnavailable as exc:
        raise RuntimeError(
            "Web search required for contra gate. Set PULSE_SEARCH_PROVIDER=tavily and TAVILY_API_KEY."
        ) from exc

    all_results = []
    for query in queries:
        try:
            resp = provider.search(query, max_results=5)
            all_results.extend(resp.results)
        except (SearchUnavailable, FetchError):
            pass

    seen: set = set()
    unique = []
    for r in sorted(all_results, key=lambda x: x.score, reverse=True):
        if r.url not in seen:
            seen.add(r.url)
            unique.append(r)
        if len(unique) >= 10:
            break

    if not unique:
        return "(no web results retrieved)", []

    unique = _inject_pitchbook(name, unique)

    merged = SearchResponse(query=f"[gate] {name}", results=unique)
    urls = [r.url for r in unique]
    return compile_search_context(merged, max_chars=max_chars), urls


def search_lp_with_nfx(
    name: str,
    nfx_url: Optional[str] = None,
    max_chars: int = 4000,
) -> Tuple[str, List[str]]:
    """
    NFX-aware gate research.

    Runs the standard 6 appetite-oriented queries, then additionally fetches
    the investor's NFX Signal profile URL (if provided) as a priority source.
    The NFX profile is scored higher than generic search results so the LLM
    sees the investor's self-described mandate first.

    Falls back to search_lp() if the NFX fetch fails or no URL is given.
    """
    queries = build_lp_fit_queries(name)
    try:
        provider = get_search_provider()
    except SearchUnavailable as exc:
        raise RuntimeError(
            "Web search required for contra gate. Set PULSE_SEARCH_PROVIDER=tavily and TAVILY_API_KEY."
        ) from exc

    all_results: List[SearchResult] = []

    # Standard queries
    for query in queries:
        try:
            resp = provider.search(query, max_results=5)
            all_results.extend(resp.results)
        except (SearchUnavailable, FetchError):
            pass

    # NFX Signal profile — fetch the profile page directly for richer context.
    # Give it a boosted score so it ranks at the top of the merged list.
    if nfx_url:
        try:
            nfx_content = provider.fetch(nfx_url)
            if nfx_content:
                nfx_result = SearchResult(
                    title=f"NFX Signal profile: {name}",
                    url=nfx_url,
                    snippet=nfx_content[:400].strip(),
                    score=1.5,  # Above normal Tavily scores (0–1)
                    raw_content=nfx_content[:1200],
                )
                all_results.insert(0, nfx_result)
                logger.debug("NFX profile fetched for %s: %s", name, nfx_url)
        except (SearchUnavailable, FetchError) as exc:
            logger.debug("NFX profile fetch failed for %s (%s): %s", name, nfx_url, exc)

    seen: set = set()
    unique: List[SearchResult] = []
    for r in sorted(all_results, key=lambda x: x.score, reverse=True):
        if r.url not in seen:
            seen.add(r.url)
            unique.append(r)
        if len(unique) >= 10:
            break

    if not unique:
        return "(no web results retrieved)", []

    unique = _inject_pitchbook(name, unique)

    merged = SearchResponse(query=f"[gate/nfx] {name}", results=unique)
    urls = [r.url for r in unique]
    return compile_search_context(merged, max_chars=max_chars), urls

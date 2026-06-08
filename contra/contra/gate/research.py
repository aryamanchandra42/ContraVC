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


def search_lp(name: str, max_chars: int = 2800) -> Tuple[str, List[str]]:
    """Run the appetite-oriented fit queries; return (context_text, source_urls).

    Kept under ~3k chars so the full gate prompt fits Groq free-tier context limits
    (llama-3.1-8b-instant TPM cap ~6k tokens including system + schema).
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
            resp = provider.search(query, max_results=3)
            all_results.extend(resp.results)
        except (SearchUnavailable, FetchError):
            pass

    seen: set = set()
    unique = []
    for r in sorted(all_results, key=lambda x: x.score, reverse=True):
        if r.url not in seen:
            seen.add(r.url)
            unique.append(r)
        if len(unique) >= 7:
            break

    if not unique:
        return "(no web results retrieved)", []

    merged = SearchResponse(query=f"[gate] {name}", results=unique)
    urls = [r.url for r in unique]
    return compile_search_context(merged, max_chars=max_chars), urls


def search_lp_with_nfx(
    name: str,
    nfx_url: Optional[str] = None,
    max_chars: int = 2800,
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
            resp = provider.search(query, max_results=3)
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
        if len(unique) >= 7:
            break

    if not unique:
        return "(no web results retrieved)", []

    merged = SearchResponse(query=f"[gate/nfx] {name}", results=unique)
    urls = [r.url for r in unique]
    return compile_search_context(merged, max_chars=max_chars), urls

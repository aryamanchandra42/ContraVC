"""Mandatory web research for gate.

Two retrieval paths, in priority order:

1. Deep research (preferred): single adaptive OpenAI web-search call that runs
   multiple searches internally, disambiguates identity, and returns structured
   analyst notes with citations. Enabled when PULSE_SEARCH_PROVIDER is
   'openai'/'auto' and OPENAI_API_KEY is set.
2. Query fan-out (fallback): 6-7 fixed Tavily queries merged + deduped.

Both paths inject the authenticated PitchBook profile (structured commitments
parse) as the highest-priority source when session cookies are available.
"""

from __future__ import annotations

import logging
import os
import re
from typing import List, Optional, Tuple

from agents.research.web_search import (
    FetchError,
    SearchResponse,
    SearchResult,
    SearchUnavailable,
    build_lp_disambiguation_queries,
    build_lp_fit_queries,
    compile_search_context,
    get_search_provider,
    openai_search_configured,
)

logger = logging.getLogger(__name__)


def _inject_pitchbook(
    lp_name: str,
    results: List[SearchResult],
) -> List[SearchResult]:
    """
    If PitchBook session cookies are available, try to fetch the LP's profile.

    Strategy (in priority order):
    1. If a pitchbook.com URL already appeared in search results, fetch it
       authenticated to get the full page (not just the login redirect).
    2. Otherwise search PitchBook by LP name for the profile.

    The fetched result is inserted at position 0 with score=2.0 so the LLM
    sees PitchBook's structured data (AUM, LP type, recent funds) first.
    """
    try:
        from agents.research.pitchbook_fetch import cookies_available, pb_inject_result

        if not cookies_available():
            return results

        # Check if any search result already points to a PitchBook profile URL
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


def _apply_rerank(
    name: str,
    results: List[SearchResult],
    screening_mode: str = "institutional",
) -> List[SearchResult]:
    from agents.research.nim_rerank import build_lp_rerank_query, rerank_search_results

    query = build_lp_rerank_query(name, screening_mode=screening_mode)
    return rerank_search_results(results, query)


# ---------------------------------------------------------------------------
# Deep research path (OpenAI web search — preferred)
# ---------------------------------------------------------------------------

_URL_RE = re.compile(r"https?://[^\s\)\]\>\"']{8,200}")


def _urls_from_text(text: str) -> List[str]:
    """Harvest source URLs cited inline in research notes (SOURCES section etc.)."""
    urls: List[str] = []
    seen: set = set()
    for m in _URL_RE.finditer(text or ""):
        url = m.group(0).rstrip(".,;")
        if url not in seen and "openai.com" not in url:
            seen.add(url)
            urls.append(url)
    return urls


def _nfx_block(name: str, nfx_url: Optional[str]) -> Optional[Tuple[str, str]]:
    """
    Fetch the NFX Signal profile directly (it sits behind a JS app OpenAI's
    crawler often can't read). Returns (text_block, url) or None.
    """
    if not nfx_url:
        return None
    content = ""
    # 1. Plain HTTP fetch with a browser UA — NFX profile pages are public.
    try:
        import requests

        resp = requests.get(
            nfx_url, timeout=15,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
        )
        if resp.ok:
            raw = re.sub(r"<script[\s\S]*?</script>|<style[\s\S]*?</style>", " ", resp.text)
            raw = re.sub(r"<[^>]+>", " ", raw)
            content = re.sub(r"\s+", " ", raw).strip()
    except Exception as exc:
        logger.debug("NFX direct fetch failed for %s: %s", nfx_url, exc)
    # 2. Fall back to the configured search provider's fetch (Tavily extract / OpenAI).
    if len(content) < 200:
        try:
            content = get_search_provider().fetch(nfx_url) or ""
        except Exception as exc:
            logger.debug("NFX provider fetch failed for %s: %s", nfx_url, exc)
    if len(content) < 100:
        return None
    block = (
        f"=== NFX SIGNAL PROFILE (direct fetch) — {name} ===\n"
        f"URL: {nfx_url}\n"
        "NOTE: NFX Signal lists DIRECT/angel investing activity — NOT fund LP commitments.\n"
        f"{content[:2500]}"
    )
    return block, nfx_url


def _supplemental_fanout(
    name: str,
    existing_urls: List[str],
    match_untrusted: bool,
) -> Optional[Tuple[str, List[str]]]:
    """
    Slim Tavily fan-out to ADD independent sources alongside deep research
    (GATE_RESEARCH_MULTI=true, default). Two highest-yield queries only, so the
    free-tier Tavily quota lasts. Fails silently — deep research alone is fine.
    """
    if os.environ.get("GATE_RESEARCH_MULTI", "true").lower().strip() not in (
        "1", "true", "yes", "on",
    ):
        return None
    if not os.environ.get("TAVILY_API_KEY", "").strip():
        return None
    try:
        from agents.research.web_search import TavilyProvider

        provider = TavilyProvider()
        queries = [
            f'"{name}" limited partner fund commitment investor',
            f'"{name}" LinkedIn Crunchbase investor profile',
        ]
        if match_untrusted:
            queries.extend(build_lp_disambiguation_queries(name)[:2])
        results: List[SearchResult] = []
        for q in queries:
            try:
                results.extend(provider.search(q, max_results=4).results)
            except (SearchUnavailable, FetchError):
                pass
        seen = set(existing_urls)
        fresh = []
        for r in sorted(results, key=lambda x: x.score, reverse=True):
            if r.url and r.url not in seen:
                seen.add(r.url)
                fresh.append(r)
            if len(fresh) >= 6:
                break
        if not fresh:
            return None
        block = compile_search_context(
            SearchResponse(query=f"[supplemental] {name}", results=fresh),
            max_chars=3000,
        )
        return f"=== ADDITIONAL INDEPENDENT SOURCES (Tavily) ===\n{block}", [r.url for r in fresh]
    except Exception as exc:
        logger.debug("Supplemental fan-out skipped for '%s': %s", name, exc)
        return None


def _deep_research(
    name: str,
    max_chars: int,
    screening_mode: str,
    nfx_url: Optional[str],
    match_untrusted: bool,
    known_context: str = "",
) -> Optional[Tuple[str, List[str]]]:
    """Try the single-call deep research path. Returns None to trigger fallback."""
    if not openai_search_configured():
        return None
    try:
        from agents.research.openai_research import openai_lp_deep_research

        context, urls = openai_lp_deep_research(
            name,
            screening_mode=screening_mode,
            nfx_url=nfx_url,
            known_context=known_context,
            match_untrusted=match_untrusted,
            max_chars=max_chars,
        )
    except (SearchUnavailable, FetchError) as exc:
        logger.warning("Deep research unavailable for '%s' (%s) — falling back", name, exc)
        return None
    except Exception as exc:
        logger.warning("Deep research failed for '%s' (%s) — falling back", name, exc)
        return None

    # Harvest URLs cited inline in the notes (SOURCES section) that the API
    # didn't return as annotations — this is why runs showed "one source".
    seen = set(urls)
    for u in _urls_from_text(context):
        if u not in seen:
            seen.add(u)
            urls.append(u)

    # NFX Signal profile — fetched directly (auth-free page, JS-rendered).
    nfx = _nfx_block(name, nfx_url)
    if nfx:
        block, url = nfx
        context = f"{context}\n\n{block}"
        if url not in seen:
            seen.add(url)
            urls.append(url)

    # Independent second engine — Tavily slim fan-out for sources OpenAI missed.
    supplemental = _supplemental_fanout(name, urls, match_untrusted)
    if supplemental:
        block, extra_urls = supplemental
        context = f"{context}\n\n{block}"
        urls.extend(extra_urls)

    # Prepend the authenticated PitchBook block — highest-value ground truth.
    try:
        from agents.research.pitchbook_fetch import fetch_pb_structured, pb_structured_block

        pb_block = pb_structured_block(name)
        if pb_block:
            context = f"{pb_block}\n\n{context}"
            structured = fetch_pb_structured(name)
            if structured and structured.url and structured.url not in seen:
                urls = [structured.url] + urls
    except Exception as exc:
        logger.debug("PitchBook block skipped for '%s': %s", name, exc)

    return context[: max_chars + 6000], urls


# ---------------------------------------------------------------------------
# Query fan-out path (Tavily — fallback)
# ---------------------------------------------------------------------------

def _fanout_research(
    name: str,
    max_chars: int,
    screening_mode: str,
    nfx_url: Optional[str],
    match_untrusted: bool,
) -> Tuple[str, List[str]]:
    queries = build_lp_fit_queries(name)
    if match_untrusted:
        queries.extend(build_lp_disambiguation_queries(name))
    try:
        provider = get_search_provider()
    except SearchUnavailable as exc:
        raise RuntimeError(
            "Web search required for contra gate. Set PULSE_SEARCH_PROVIDER=auto "
            "with OPENAI_API_KEY, or PULSE_SEARCH_PROVIDER=tavily with TAVILY_API_KEY."
        ) from exc

    per_query = 6 if match_untrusted else 5
    max_urls = 14 if match_untrusted else 12

    all_results: List[SearchResult] = []
    for query in queries:
        try:
            resp = provider.search(query, max_results=per_query)
            all_results.extend(resp.results)
        except (SearchUnavailable, FetchError):
            pass

    # NFX Signal profile — fetch the profile page directly for richer context.
    # Give it a boosted score so it ranks at the top of the merged list.
    if nfx_url:
        try:
            nfx_content = provider.fetch(nfx_url)
            if nfx_content:
                all_results.insert(0, SearchResult(
                    title=f"NFX Signal profile: {name}",
                    url=nfx_url,
                    snippet=nfx_content[:400].strip(),
                    score=1.5,  # Above normal search scores (0-1)
                    raw_content=nfx_content[:2500],
                ))
                logger.debug("NFX profile fetched for %s: %s", name, nfx_url)
        except (SearchUnavailable, FetchError) as exc:
            logger.debug("NFX profile fetch failed for %s (%s): %s", name, nfx_url, exc)

    seen: set = set()
    unique: List[SearchResult] = []
    for r in sorted(all_results, key=lambda x: x.score, reverse=True):
        if r.url not in seen:
            seen.add(r.url)
            unique.append(r)
        if len(unique) >= max_urls:
            break

    if not unique:
        return "(no web results retrieved)", []

    unique = _inject_pitchbook(name, unique)
    unique = _apply_rerank(name, unique, screening_mode=screening_mode)

    merged = SearchResponse(query=f"[gate] {name}", results=unique)
    urls = [r.url for r in unique]
    return compile_search_context(merged, max_chars=max_chars), urls


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def search_lp(
    name: str,
    max_chars: int = 12000,
    screening_mode: str = "institutional",
    *,
    match_untrusted: bool = False,
) -> Tuple[str, List[str]]:
    """Research an LP; return (context_text, source_urls).

    When match_untrusted=True, identity disambiguation is emphasized
    (wrong-person DB match).
    """
    deep = _deep_research(
        name, max_chars, screening_mode,
        nfx_url=None, match_untrusted=match_untrusted,
    )
    if deep is not None:
        return deep
    return _fanout_research(
        name, max_chars, screening_mode,
        nfx_url=None, match_untrusted=match_untrusted,
    )


def search_lp_with_nfx(
    name: str,
    nfx_url: Optional[str] = None,
    max_chars: int = 12000,
    screening_mode: str = "institutional",
    *,
    match_untrusted: bool = False,
    known_context: str = "",
) -> Tuple[str, List[str]]:
    """
    NFX-aware gate research — the investor's NFX Signal profile (if provided)
    is read as a priority source so the LLM sees their self-described mandate.
    """
    deep = _deep_research(
        name, max_chars, screening_mode,
        nfx_url=nfx_url, match_untrusted=match_untrusted,
        known_context=known_context,
    )
    if deep is not None:
        return deep
    return _fanout_research(
        name, max_chars, screening_mode,
        nfx_url=nfx_url, match_untrusted=match_untrusted,
    )

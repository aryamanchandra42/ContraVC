"""
Web search and page-fetch layer for PULSE research agents.

Provider selection via PULSE_SEARCH_PROVIDER env var:
    openai  → OpenAI Responses API with built-in web_search tool (OPENAI_API_KEY)
    tavily  → Tavily AI Search API (TAVILY_API_KEY)
    auto    → openai if OPENAI_API_KEY is set, else tavily
    none    → raises SearchUnavailable; callers fall back to local-only mode

Cache contract (mirrors ontology cache):
    Every search/fetch result is cached at:
        processed_data/research_cache/{sha256(query|url)}.json
    Cache entries expire after RESEARCH_CACHE_TTL_DAYS (default 30; 0 = never),
    so "recency-weighted" appetite inference is not built on stale snippets.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Protocol, runtime_checkable

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent.parent
CACHE_DIR = ROOT / "processed_data" / "research_cache"


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class SearchUnavailable(RuntimeError):
    """Raised when no search provider is configured or credentials are missing."""


class FetchError(RuntimeError):
    """Raised when a URL fetch fails after retries."""


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass
class SearchResult:
    title: str
    url: str
    snippet: str
    score: float = 0.0
    raw_content: Optional[str] = None


@dataclass
class SearchResponse:
    query: str
    results: List[SearchResult] = field(default_factory=list)
    cached: bool = False
    fetched_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


# ---------------------------------------------------------------------------
# Provider protocol
# ---------------------------------------------------------------------------

@runtime_checkable
class WebSearchProvider(Protocol):
    def search(self, query: str, max_results: int = 5) -> SearchResponse: ...
    def fetch(self, url: str) -> str: ...


# ---------------------------------------------------------------------------
# Cache helpers
# ---------------------------------------------------------------------------

def _cache_key(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _cache_ttl_seconds() -> Optional[float]:
    """TTL for research cache entries. RESEARCH_CACHE_TTL_DAYS=0 disables expiry."""
    raw = os.environ.get("RESEARCH_CACHE_TTL_DAYS", "30").strip()
    try:
        days = float(raw)
    except ValueError:
        days = 30.0
    if days <= 0:
        return None
    return days * 86400.0


def _load_cache(key: str) -> Optional[dict]:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = CACHE_DIR / f"{key}.json"
    if path.exists():
        ttl = _cache_ttl_seconds()
        if ttl is not None:
            try:
                if (time.time() - path.stat().st_mtime) > ttl:
                    return None  # stale — force a re-fetch
            except OSError:
                pass
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None
    return None


def _save_cache(key: str, data: dict) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = CACHE_DIR / f"{key}.json"
    path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")


# ---------------------------------------------------------------------------
# Tavily provider
# ---------------------------------------------------------------------------

class TavilyProvider:
    """
    Tavily AI Search — retrieval-focused search API.

    Requires: pip install tavily-python; TAVILY_API_KEY env var.
    """

    def __init__(self) -> None:
        api_key = os.environ.get("TAVILY_API_KEY", "")
        if not api_key:
            raise SearchUnavailable(
                "TavilyProvider requires TAVILY_API_KEY to be set."
            )
        try:
            from tavily import TavilyClient  # type: ignore[import]
            self._client = TavilyClient(api_key=api_key)
        except ImportError as exc:
            raise SearchUnavailable(
                "tavily-python is not installed. Run: pip install tavily-python"
            ) from exc

    def search(self, query: str, max_results: int = 5) -> SearchResponse:
        cache_key = _cache_key(f"search:{query}:{max_results}")
        cached = _load_cache(cache_key)
        if cached:
            logger.debug("Research cache hit: search query '%s'", query)
            return SearchResponse(
                query=cached["query"],
                results=[SearchResult(**r) for r in cached["results"]],
                cached=True,
                fetched_at=cached["fetched_at"],
            )

        logger.debug("Tavily search: '%s'", query)
        try:
            raw = self._client.search(
                query=query,
                max_results=max_results,
                include_raw_content=True,
            )
        except Exception as exc:
            raise FetchError(f"Tavily search failed for '{query}': {exc}") from exc

        results = [
            SearchResult(
                title=r.get("title", ""),
                url=r.get("url", ""),
                snippet=r.get("content", ""),
                score=r.get("score", 0.0),
                raw_content=r.get("raw_content"),
            )
            for r in raw.get("results", [])
        ]

        response = SearchResponse(
            query=query,
            results=results,
            cached=False,
        )

        _save_cache(
            cache_key,
            {
                "query": query,
                "results": [
                    {
                        "title": r.title,
                        "url": r.url,
                        "snippet": r.snippet,
                        "score": r.score,
                        "raw_content": r.raw_content,
                    }
                    for r in results
                ],
                "fetched_at": response.fetched_at,
            },
        )
        return response

    def fetch(self, url: str) -> str:
        cache_key = _cache_key(f"fetch:{url}")
        cached = _load_cache(cache_key)
        if cached:
            logger.debug("Research cache hit: fetch '%s'", url)
            return cached.get("content", "")

        logger.debug("Fetching URL: '%s'", url)
        try:
            result = self._client.extract(urls=[url])
            content = ""
            if result and result.get("results"):
                content = result["results"][0].get("raw_content", "")
        except Exception as exc:
            raise FetchError(f"Tavily fetch failed for '{url}': {exc}") from exc

        _save_cache(cache_key, {"url": url, "content": content})
        return content


# ---------------------------------------------------------------------------
# OpenAI web-search provider (Responses API + built-in web_search tool)
# ---------------------------------------------------------------------------

class OpenAIWebSearchProvider:
    """
    OpenAI Responses API with the built-in ``web_search`` tool.

    One API call performs the search, reads the pages, and returns synthesized
    findings with URL citations — replacing the multi-query Tavily fan-out with
    a single richer retrieval. Billed against existing OpenAI API credits.

    Requires: pip install openai; OPENAI_API_KEY env var.
    Model via OPENAI_SEARCH_MODEL (default gpt-4o-mini — cheapest search-capable).
    """

    def __init__(self) -> None:
        api_key = os.environ.get("OPENAI_API_KEY", "").strip()
        if not api_key:
            raise SearchUnavailable(
                "OpenAIWebSearchProvider requires OPENAI_API_KEY to be set."
            )
        try:
            from openai import OpenAI  # type: ignore[import]
        except ImportError as exc:
            raise SearchUnavailable(
                "openai package is not installed. Run: pip install openai"
            ) from exc
        self._client = OpenAI(api_key=api_key)
        self.model = os.environ.get("OPENAI_SEARCH_MODEL", "").strip() or "gpt-4o-mini"

    # -- internal ----------------------------------------------------------

    def _responses_with_search(self, prompt: str) -> tuple[str, List[dict]]:
        """Run a Responses API call with web search; return (text, citations)."""
        last_exc: Optional[Exception] = None
        # Newer accounts use tool type "web_search"; older SDK/models need the
        # "web_search_preview" alias. Try both before failing.
        for tool_type in ("web_search", "web_search_preview"):
            try:
                resp = self._client.responses.create(
                    model=self.model,
                    tools=[{"type": tool_type}],
                    input=prompt,
                )
                text = (getattr(resp, "output_text", "") or "").strip()
                citations: List[dict] = []
                for item in getattr(resp, "output", None) or []:
                    if getattr(item, "type", "") != "message":
                        continue
                    for content in getattr(item, "content", None) or []:
                        for ann in getattr(content, "annotations", None) or []:
                            if getattr(ann, "type", "") == "url_citation":
                                url = getattr(ann, "url", "") or ""
                                if url:
                                    citations.append({
                                        "url": url,
                                        "title": getattr(ann, "title", "") or url,
                                    })
                return text, citations
            except Exception as exc:
                last_exc = exc
        raise FetchError(f"OpenAI web search failed: {last_exc}") from last_exc

    def research(self, prompt: str) -> tuple[str, List[dict]]:
        """Public single-call research: returns (synthesized text, url citations)."""
        return self._responses_with_search(prompt)

    # -- WebSearchProvider protocol -----------------------------------------

    def search(self, query: str, max_results: int = 5) -> SearchResponse:
        cache_key = _cache_key(f"openai-search:{query}:{max_results}")
        cached = _load_cache(cache_key)
        if cached:
            logger.debug("Research cache hit: openai search '%s'", query)
            return SearchResponse(
                query=cached["query"],
                results=[SearchResult(**r) for r in cached["results"]],
                cached=True,
                fetched_at=cached["fetched_at"],
            )

        logger.debug("OpenAI web search: '%s'", query)
        prompt = (
            f"Search the web for: {query}\n\n"
            "Report only concrete findings relevant to the query as terse bullet "
            "points, each with its source. Prefer primary sources (company sites, "
            "press releases, regulatory filings, LinkedIn, Crunchbase, PitchBook). "
            "If nothing relevant is found, reply exactly: No relevant results."
        )
        text, citations = self._responses_with_search(prompt)

        results: List[SearchResult] = []
        if text and not text.lower().startswith("no relevant results"):
            results.append(SearchResult(
                title=f"Web synthesis: {query}",
                url="openai://web-search",
                snippet=text[:400],
                score=1.0,
                raw_content=text,
            ))
        seen: set = set()
        for c in citations:
            if c["url"] in seen:
                continue
            seen.add(c["url"])
            results.append(SearchResult(
                title=c["title"], url=c["url"],
                snippet="(cited in web synthesis above)", score=0.7,
            ))
            if len(results) >= max_results + 1:
                break

        response = SearchResponse(query=query, results=results, cached=False)
        _save_cache(cache_key, {
            "query": query,
            "results": [
                {"title": r.title, "url": r.url, "snippet": r.snippet,
                 "score": r.score, "raw_content": r.raw_content}
                for r in results
            ],
            "fetched_at": response.fetched_at,
        })
        return response

    def fetch(self, url: str) -> str:
        cache_key = _cache_key(f"openai-fetch:{url}")
        cached = _load_cache(cache_key)
        if cached:
            return cached.get("content", "")

        prompt = (
            f"Open and read this page: {url}\n"
            "Reproduce the substantive content (investor bio, mandate, check sizes, "
            "portfolio, locations) as plain text. No commentary."
        )
        text, _ = self._responses_with_search(prompt)
        _save_cache(cache_key, {"url": url, "content": text})
        return text


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

_PROVIDERS: dict[str, type] = {
    "tavily": TavilyProvider,
    "openai": OpenAIWebSearchProvider,
}


def get_search_provider(provider: Optional[str] = None) -> WebSearchProvider:
    """
    Return a configured web search provider.

    'auto' prefers OpenAI web search (uses existing API credits) and falls back
    to Tavily. Raises SearchUnavailable if nothing is configured.
    """
    resolved = (provider or os.environ.get("PULSE_SEARCH_PROVIDER", "none")).lower().strip()

    if resolved in ("none", ""):
        raise SearchUnavailable(
            "No search provider configured. Set PULSE_SEARCH_PROVIDER=auto "
            "(or openai / tavily) with the matching API key."
        )

    if resolved == "auto":
        errors = []
        for name in ("openai", "tavily"):
            try:
                return _PROVIDERS[name]()
            except SearchUnavailable as exc:
                errors.append(f"{name}: {exc}")
        raise SearchUnavailable(
            "PULSE_SEARCH_PROVIDER=auto but no provider is usable. " + " | ".join(errors)
        )

    if resolved not in _PROVIDERS:
        raise SearchUnavailable(
            f"Unknown search provider '{resolved}'. Valid options: "
            f"{sorted(_PROVIDERS.keys()) + ['auto']}."
        )

    return _PROVIDERS[resolved]()


def openai_search_configured() -> bool:
    """True when the OpenAI web-search path can be used for deep LP research."""
    resolved = os.environ.get("PULSE_SEARCH_PROVIDER", "none").lower().strip()
    return (
        resolved in ("openai", "auto")
        and bool(os.environ.get("OPENAI_API_KEY", "").strip())
    )


def build_lp_research_query(canonical_name: str, extra_context: str = "") -> str:
    """Construct a targeted search query for LP research."""
    base = f'"{canonical_name}" limited partner investor fund'
    if extra_context:
        return f"{base} {extra_context}"
    return base


# Curated reference list of well-known emerging-manager / Fund-I-friendly VC
# vehicles. Passed into the gate prompt so the LLM can anchor appetite inference
# when one of these (or a similar vehicle) shows up in an allocator's backings.
# This is a reasoning aid, NOT a scoring table — order and membership are not ranked.
KNOWN_EMERGING_MANAGER_FUNDS: List[str] = [
    "Hustle Fund", "Weekend Fund", "Conviction", "Village Global",
    "Banana Capital", "Script Capital", "Chapter One", "Cocoa",
    "South Park Commons", "Pebblebed", "Bedrock", "Compound",
    "Afore Capital", "Precursor Ventures", "Backstage Capital",
    "January Capital", "Saison Capital", "Iterative",
]


def build_lp_disambiguation_queries(canonical_name: str) -> List[str]:
    """
    Extra searches when the DB match is unreliable or the LP is hard to resolve.
    Surfaces LinkedIn, regional press, and family-office profiles Tavily often misses.
    """
    name = canonical_name.strip()
    return [
        f'"{name}" limited partner venture capital fund commitment',
        f'"{name}" family office investor India Asia fund LP',
        f'"{name}" LinkedIn investor angel portfolio venture',
        f'"{name}" Crunchbase investor limited partner profile',
    ]


def build_lp_fit_queries(canonical_name: str) -> List[str]:
    """
    Build appetite-oriented search queries to research LP fit for an AI-native VC
    Fund I investing in emerging markets.

    The goal is to surface an allocator's HISTORICAL ALLOCATION DECISIONS (managers
    backed, portfolio companies, commitments) so appetite can be inferred from
    behavior rather than explicit statements. One query is deliberately NEGATIVE —
    it hunts for disqualifying evidence (PE-only, direct-only, large minimums).

    Returns 6 queries:
      1. General profile + investment mandate
      2. Allocation profile — fund commitments / managers backed (PitchBook-style)
      3. Emerging-manager / Fund I / anchor LP evidence
      4. Portfolio companies + AI / software / technology inference
      5. Venture fund LP + Asia / emerging-market geography
      6. NEGATIVE — PE-only / direct-only / large-minimum disqualifiers
    """
    name = canonical_name.strip()
    return [
        f'"{name}" investor portfolio venture capital fund LP',
        f'"{name}" PitchBook fund commitments managers backed limited partner',
        f'"{name}" anchor LP first-time fund emerging manager Fund I',
        f'"{name}" portfolio companies investments AI software technology',
        f'"{name}" venture capital fund LP Asia Southeast Asia India emerging markets',
        f'"{name}" private equity buyout direct-only minimum commitment',
        f'"{name}" Mumbai India investor family office venture fund',
    ]


def compile_search_context(response: SearchResponse, max_chars: int = 12000) -> str:
    """
    Flatten a SearchResponse into a text block for LLM prompting.

    Top-3 results get a larger content window (2000 chars); the rest get 800.
    Modern verdict models (Claude / GPT) have 128k+ context — evidence starvation
    causes more wrong verdicts than prompt length ever will, so budgets are generous
    and the final max_chars cap is the only hard limit.
    """
    lines: List[str] = [f"Search query: {response.query}\n"]
    for i, r in enumerate(response.results, 1):
        lines.append(f"[{i}] {r.title}")
        lines.append(f"    URL: {r.url}")
        lines.append(f"    {r.snippet}")
        if r.raw_content:
            content_limit = 2000 if i <= 3 else 800
            preview = r.raw_content[:content_limit].strip()
            if len(r.raw_content) > content_limit:
                preview += "..."
            lines.append(f"    Content: {preview}")
        lines.append("")

    text = "\n".join(lines)
    if len(text) > max_chars:
        text = text[:max_chars] + "\n[truncated]"
    return text

"""
Web search and page-fetch layer for PULSE research agents.

Provider selection via PULSE_SEARCH_PROVIDER env var:
    tavily  → Tavily AI Search API (TAVILY_API_KEY)
    none    → raises SearchUnavailable; callers fall back to local-only mode

Cache contract (mirrors ontology cache):
    Every search/fetch result is cached at:
        processed_data/research_cache/{sha256(query|url)}.json
    Cache files are NEVER auto-invalidated; delete them manually to re-fetch.
    This ensures that re-running the enrichment pipeline with the same LP
    queries produces identical inputs to the LLM extraction step.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
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


def _load_cache(key: str) -> Optional[dict]:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = CACHE_DIR / f"{key}.json"
    if path.exists():
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
# Factory
# ---------------------------------------------------------------------------

_PROVIDERS: dict[str, type] = {
    "tavily": TavilyProvider,
}


def get_search_provider(provider: Optional[str] = None) -> WebSearchProvider:
    """
    Return a configured web search provider.

    Raises SearchUnavailable if provider is 'none'/empty or credentials missing.
    """
    resolved = (provider or os.environ.get("PULSE_SEARCH_PROVIDER", "none")).lower().strip()

    if resolved in ("none", ""):
        raise SearchUnavailable(
            "No search provider configured. Set PULSE_SEARCH_PROVIDER=tavily "
            "and TAVILY_API_KEY."
        )

    if resolved not in _PROVIDERS:
        raise SearchUnavailable(
            f"Unknown search provider '{resolved}'. Valid options: {sorted(_PROVIDERS.keys())}."
        )

    return _PROVIDERS[resolved]()


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
        f'"{name}" venture capital fund LP Asia Southeast Asia emerging markets',
        f'"{name}" private equity buyout direct-only minimum commitment',
    ]


def compile_search_context(response: SearchResponse, max_chars: int = 4000) -> str:
    """
    Flatten a SearchResponse into a compact text block for LLM prompting.
    Truncates raw_content to avoid context-length issues.
    Top-3 results get a larger content window (600 chars); the rest get 350.
    """
    lines: List[str] = [f"Search query: {response.query}\n"]
    for i, r in enumerate(response.results, 1):
        lines.append(f"[{i}] {r.title}")
        lines.append(f"    URL: {r.url}")
        lines.append(f"    {r.snippet}")
        if r.raw_content:
            content_limit = 600 if i <= 3 else 350
            preview = r.raw_content[:content_limit].strip()
            if len(r.raw_content) > content_limit:
                preview += "..."
            lines.append(f"    Content: {preview}")
        lines.append("")

    text = "\n".join(lines)
    if len(text) > max_chars:
        text = text[:max_chars] + "\n[truncated]"
    return text

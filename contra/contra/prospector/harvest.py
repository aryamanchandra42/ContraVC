"""
Stage 1 of the cascade — HARVEST.

Owns RECALL, and nothing else. This stage is deliberately permissive: its job is
to maximise named entities per API call and hand everything downstream. Precision
is Stages 2-4's problem.

The design choice that matters is the unit of discovery. Asking "is X an LP?" one
name at a time is the expensive way to find LPs, because you must already know the
name. Instead this stage hunts DOCUMENTS that name many LPs at once — fund-close
announcements ("limited partners include..."), family-office directories,
emerging-manager program pages — then fetches the full page and extracts every LP
named in it. One close announcement typically yields 3-10 candidates.

Two rules make the output usable downstream:

  1. Extraction is one LLM call per document, never a merged corpus. With a single
     URL in the prompt there is no way to misattribute a span to the wrong source,
     and source attribution is what Stage 4's independence test rests on.
  2. Spans are stored verbatim. Stages 3 and 4 test against literal text, so a
     paraphrase silently breaks quotability.
"""

from __future__ import annotations

import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional, Set, Tuple

from pydantic import BaseModel, ConfigDict, Field

from contra.prospector.models import Candidate, domain_of

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Extraction schema
# ---------------------------------------------------------------------------

class _Mention(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    entity_type: str = ""       # family office | fund of funds | institution | individual
    geography: str = ""
    span: str = Field(default="", max_length=400)
    confidence: str = "medium"  # high | medium | low


class _MentionBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mentions: List[_Mention] = Field(default_factory=list)


_EXTRACT_SYSTEM = """You extract LP (limited partner) names from ONE web document.

An LP is an entity or person that COMMITS CAPITAL TO A VENTURE FUND — it appears
in the document as an investor IN a fund, not as the fund or its manager.

INCLUDE: family offices, fund-of-funds, institutions, foundations, endowments,
corporates and individuals that the document says committed to / backed / anchored
a venture fund, or that a directory lists as a fund investor.

EXCLUDE, no matter how prominent:
- the fund itself and its management company
- GPs, general partners, managing partners, founders of the fund
- portfolio companies the fund invested in
- placement agents, law firms, administrators, auditors
- journalists, publications, conference organisers
- anyone whose only described role is a direct/angel investment in a company

span MUST be copied VERBATIM from the document — the exact sentence or clause
that names this entity as a fund investor. Never paraphrase, never summarise,
never fix grammar. If you cannot copy a literal sentence naming the entity as a
fund investor, omit the entity entirely.

confidence: high = the document explicitly names them as an LP/investor in a
named fund. medium = listed as a fund investor without a named fund. low =
implied only."""


# ---------------------------------------------------------------------------
# Which search results are worth the cost of a full fetch
# ---------------------------------------------------------------------------

# Phrases that mark a document as likely to enumerate LPs.
_DOC_SIGNALS: Tuple[Tuple[str, int], ...] = (
    ("limited partners include", 10),
    ("with participation from", 8),
    ("limited partners", 6),
    ("anchor investor", 6),
    ("anchor lp", 6),
    ("backed by", 4),
    ("investors include", 8),
    ("first close", 5),
    ("final close", 5),
    ("fund close", 4),
    ("emerging manager program", 6),
    ("closes fund", 4),
    ("raises", 2),
    ("family office", 3),
    ("fund of funds", 3),
    ("lp base", 5),
)

# Evergreen "biggest LPs" listicles and LP-database pages are actively harmful to
# mine, which is not obvious until you look at what they yield. An observed run
# harvested the Teacher Retirement System of Texas ($225B), Greenspring ($17B FoF),
# Horsley Bridge ($22B), Baupost, and the Washington State Investment Board from
# pages like these. Every one is a real LP and every one is the wrong ICP: their
# minimum commitment alone exceeds a $30M Fund I. Such pages rank well precisely
# because they list famous institutions, whereas a specific fund-close
# announcement names the small family offices that actually back a Fund I.
_LISTICLE_SIGNALS: Tuple[Tuple[str, int], ...] = (
    ("top 10", 8), ("top 20", 8), ("top 50", 8), ("top 100", 8),
    ("largest", 6), ("biggest", 6), ("list of", 5), ("complete list", 8),
    ("ultimate guide", 8), ("database of", 8), ("directory of", 6),
    ("lp database", 8), ("investor database", 8), ("best ", 4),
    # Roundups of FUNDS are a distinct trap from roundups of LPs: they rank well on
    # venture vocabulary and name only GPs, so every name extracted is discarded at
    # Stage 2. "Oldest Venture Capital Firms with Offices in Indonesia" reached the
    # fetch set this way.
    ("oldest", 6), ("firms with offices", 8),
    ("venture capital firms in", 8), ("vc firms in", 8),
)

# How-to and explainer content is the worst thing this stage can fetch, because it
# scores high for exactly the wrong reason: an article ABOUT limited partnerships is
# dense in the vocabulary of _DOC_SIGNALS while naming no LP at all. Two Hustle Fund
# blog posts — "The Limited Partnership Agreement: The VC Contract Nobody Reads" (10)
# and "The Nuts and Bolts of Your First Close" (9) — outranked a real Indonesian fund
# close, taking two of eight fetch slots and two extraction calls to return nothing.
# The tell is second-person, definitional or advisory framing, which no close
# announcement uses: a press release says "the fund held its first close", never
# "your first close".
_EDUCATIONAL_SIGNALS: Tuple[Tuple[str, int], ...] = (
    ("nuts and bolts", 12), ("nobody reads", 12), ("your first", 10),
    ("how to", 8), ("how does", 8), ("what is a", 8), ("what is an", 8),
    ("explained", 8), ("need to understand", 10), ("need to know", 10),
    # Not a bare "101": that matches the "$101 million" in a real close announcement.
    ("a guide", 8), ("guide to", 8), (" 101:", 8), ("ing 101", 8), ("primer", 8),
    ("everything you need", 10), ("glossary", 10), ("template", 8),
    ("checklist", 8), ("faq", 8), ("tips for", 8), ("lessons from", 6),
    ("should you", 8), ("do you need", 8), ("beginner", 8),
)

# Domains that never repay a fetch.
_JUNK_DOMAINS = frozenset({
    "facebook.com", "twitter.com", "x.com", "instagram.com", "youtube.com",
    "reddit.com", "pinterest.com", "tiktok.com", "quora.com",
})

# Sentinel for "never fetch this", kept far below any listicle penalty so a
# heavily-penalised but legitimate page is merely deprioritised, not excluded.
# Otherwise a run where every result looks like a listicle harvests nothing at all.
_UNMINEABLE = -999


def _doc_type(text_low: str) -> str:
    if any(p in text_low for p in ("first close", "final close", "closes fund", "fund close")):
        return "fund_close"
    if "emerging manager program" in text_low:
        return "program"
    if "directory" in text_low or "investors list" in text_low:
        return "directory"
    return "profile"


def _doc_score(result: Any) -> int:
    """
    Deterministic estimate of how useful a document is to mine.

    Pure text scoring on the title and snippet we already have — no API cost — so
    the fetch budget goes to the pages most likely to name in-ICP LPs. Note that
    this estimates ICP-relevant density, not raw LP count: a "top 100 LPs" page
    scores badly despite naming a hundred of them, because they are all too large.
    """
    url = getattr(result, "url", "") or ""
    dom = domain_of(url)
    if dom in _JUNK_DOMAINS:
        return _UNMINEABLE
    text_low = " ".join(filter(None, (
        getattr(result, "title", "") or "",
        getattr(result, "snippet", "") or "",
    ))).lower()
    if not text_low:
        return 0

    score = sum(pts for phrase, pts in _DOC_SIGNALS if phrase in text_low)
    score -= sum(pts for phrase, pts in _LISTICLE_SIGNALS if phrase in text_low)
    score -= sum(pts for phrase, pts in _EDUCATIONAL_SIGNALS if phrase in text_low)

    # A provider synthesis has no page to fetch, but its text is already here.
    from agents.research.web_search import is_synthesis_url

    if is_synthesis_url(url):
        score += 2
    return score


# ---------------------------------------------------------------------------
# Search + fetch
# ---------------------------------------------------------------------------

def _get_provider():
    """
    Tavily first for mining, then Anthropic, then whatever is configured.

    This inverts the default preference used elsewhere in the codebase, for a
    reason specific to the cascade. Tavily returns many REAL urls per query with
    `raw_content` already populated, whereas the Anthropic and OpenAI providers
    return a single synthesized answer and only sometimes parse citations out of
    it. Three consequences, all of which matter here:

      - recall: an observed run issuing 4 queries against Anthropic produced
        exactly 4 results, one synthesis each, versus a page of real links
      - cost: raw_content arriving with the search result removes the separate
        fetch that Stage 1 would otherwise pay for
      - independence: a synthesis has no domain, and Stage 4's whole test is
        whether a DIFFERENT domain confirms the claim

    PROSPECTOR_SEARCH_PROVIDER overrides the order for a single provider.
    """
    from agents.research.web_search import SearchUnavailable, get_search_provider

    forced = os.environ.get("PROSPECTOR_SEARCH_PROVIDER", "").strip().lower()
    order = [forced] if forced else ["tavily", "anthropic", None]

    errors = []
    for name in order:
        try:
            return get_search_provider(name) if name else get_search_provider()
        except SearchUnavailable as exc:
            errors.append(f"{name or 'configured'}: {exc}")
    raise SearchUnavailable(
        "No search provider for Prospector. Set TAVILY_API_KEY (best for mining) "
        "or ANTHROPIC_API_KEY. " + " | ".join(errors)
    )


def _parallel_search(provider, queries: List[str], per_query: int, max_workers: int) -> List[Any]:
    """Run queries in parallel and merge, deduping by URL."""
    from agents.research.web_search import FetchError, SearchUnavailable

    provider_name = (getattr(provider, "provider", "") or type(provider).__name__).lower()
    workers = 2 if "anthropic" in provider_name else max_workers

    results: List[Any] = []

    def _one(q: str) -> List[Any]:
        try:
            return provider.search(q, max_results=per_query).results
        except (SearchUnavailable, FetchError) as exc:
            logger.debug("Harvest search failed (%s): %s", q, exc)
            return []
        except Exception as exc:
            logger.warning("Harvest search error (%s): %s", q, exc)
            return []

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = [pool.submit(_one, q) for q in queries]
        for fut in as_completed(futures):
            results.extend(fut.result())

    seen: Set[str] = set()
    unique: List[Any] = []
    for r in sorted(results, key=lambda x: getattr(x, "score", 0.0), reverse=True):
        url = getattr(r, "url", "") or ""
        if url and url not in seen:
            seen.add(url)
            unique.append(r)
    return unique


def _document_text(provider, result: Any, max_chars: int) -> str:
    """
    Full page text for a search result.

    Prefers what the provider already returned (`raw_content`) and only pays for a
    fetch when that is too thin to extract from. A provider synthesis has no page
    behind it, so its text is used as-is.
    """
    from agents.research.web_search import FetchError, SearchUnavailable, is_synthesis_url

    inline = (getattr(result, "raw_content", None) or getattr(result, "snippet", "") or "")
    url = getattr(result, "url", "") or ""

    if is_synthesis_url(url) or len(inline) >= 2500:
        return inline[:max_chars]

    try:
        fetched = provider.fetch(url) or ""
    except (SearchUnavailable, FetchError) as exc:
        logger.debug("Harvest fetch failed (%s): %s", url, exc)
        fetched = ""
    except Exception as exc:
        logger.debug("Harvest fetch error (%s): %s", url, exc)
        fetched = ""

    return (fetched if len(fetched) > len(inline) else inline)[:max_chars]


# ---------------------------------------------------------------------------
# Per-document extraction
# ---------------------------------------------------------------------------

def _normalize_span(span: str, document: str) -> str:
    """
    Keep the span only if it really is in the document.

    The prompt demands a verbatim copy; this verifies it rather than trusting it.
    Falls back to a whitespace-insensitive comparison, because collapsing runs of
    whitespace is the one rewrite that is safe and that models do constantly.
    """
    span = (span or "").strip()
    if not span:
        return ""
    if span in document:
        return span
    flat_doc = re.sub(r"\s+", " ", document)
    flat_span = re.sub(r"\s+", " ", span)
    return flat_span if flat_span in flat_doc else ""


def _extract_from_document(llm, result: Any, document: str, seed: str) -> List[Candidate]:
    """One LLM call against one document. Unambiguous source attribution."""
    if len(document) < 200:
        return []

    url = getattr(result, "url", "") or ""
    title = getattr(result, "title", "") or ""
    doc_type = _doc_type(f"{title} {document[:2000]}".lower())

    prompt = (
        f"DOCUMENT URL: {url}\n"
        f"DOCUMENT TITLE: {title}\n\n"
        f"=== DOCUMENT TEXT ===\n{document}\n=== END ===\n\n"
        "List every limited partner named in this document. Copy each span "
        "verbatim from the text above."
    )
    try:
        batch = llm.structured(
            prompt=prompt,
            response_model=_MentionBatch,
            system=_EXTRACT_SYSTEM,
            max_tokens=2500,
        )
    except Exception as exc:
        logger.warning("Harvest extraction failed for %s: %s", url or title, exc)
        return []

    out: List[Candidate] = []
    for m in batch.mentions:
        name = (m.name or "").strip()
        if len(name) < 3:
            continue
        span = _normalize_span(m.span, document)
        if not span:
            # No verbatim support means nothing downstream can verify it.
            continue
        out.append(Candidate(
            name=name,
            span=span,
            source_url=url,
            source_domain=domain_of(url),
            doc_type=doc_type,
            entity_type=(m.entity_type or "").strip(),
            geography=(m.geography or "").strip(),
            confidence=(m.confidence or "medium").strip().lower(),
            seed=seed,
            domains=[domain_of(url)] if domain_of(url) else [],
        ))
    return out


def _initials(name: str) -> str:
    """Initials of the significant words in a name. 'Japan Investment Corp' -> 'JIC'."""
    stop = {"of", "the", "and", "for", "de", "la", "el", "van", "von", "du", "da"}
    words = [w for w in re.findall(r"[A-Za-z][A-Za-z&']*", name) if w.lower() not in stop]
    return "".join(w[0].upper() for w in words) if len(words) >= 2 else ""


def _fold_acronyms(cands: List[Candidate]) -> List[Candidate]:
    """
    Fold acronym candidates into their expanded form.

    Extractors routinely emit both halves of "International Finance Corporation
    (IFC)" as two separate LPs, which then bill as two candidates, get screened
    twice, and can produce two CRM leads for one entity. `norm_key` cannot catch
    this because the strings genuinely differ.

    The acronym's domains are folded in rather than discarded — if two sources
    named the same entity, one by acronym, that is real source diversity.
    """
    by_initials: Dict[str, Candidate] = {}
    for cand in cands:
        init = _initials(cand.name)
        if init and len(init) >= 2:
            by_initials.setdefault(init, cand)

    kept: List[Candidate] = []
    for cand in cands:
        bare = re.sub(r"[^A-Za-z]", "", cand.name)
        # Only a short, fully-capitalised token is treated as an acronym, so
        # "Invesco" is never mistaken for one.
        is_acronym = 2 <= len(bare) <= 6 and bare.isupper() and bare == cand.name.strip()
        parent = by_initials.get(bare) if is_acronym else None
        if parent is not None and parent is not cand:
            for dom in cand.domains:
                if dom and dom not in parent.domains:
                    parent.domains.append(dom)
            if not parent.span:
                parent.span = cand.span
            parent.entity_type = parent.entity_type or cand.entity_type
            parent.geography = parent.geography or cand.geography
            logger.debug("Folded acronym %s into %s", cand.name, parent.name)
            continue
        kept.append(cand)
    return kept


def _merge_by_name(batches: List[List[Candidate]]) -> List[Candidate]:
    """
    Collapse mentions of the same name, accumulating the domains that named it.

    `source_diversity` — how many independent domains called this entity an LP —
    is the cheapest available proxy for the corroboration Stage 4 does properly,
    so it is worth carrying forward even though it costs nothing to compute.
    """
    from agents.normalization.crm_normalizer import norm_key

    merged: Dict[str, Candidate] = {}
    for batch in batches:
        for cand in batch:
            key = norm_key(cand.name)
            if not key:
                continue
            existing = merged.get(key)
            if existing is None:
                cand.name_key = key
                merged[key] = cand
                continue
            for dom in cand.domains:
                if dom and dom not in existing.domains:
                    existing.domains.append(dom)
            # Prefer the better-evidenced mention as the primary record.
            rank = {"high": 0, "medium": 1, "low": 2}
            if rank.get(cand.confidence, 3) < rank.get(existing.confidence, 3) or (
                len(cand.span) > len(existing.span) * 2
            ):
                for f in ("span", "source_url", "source_domain", "doc_type", "confidence"):
                    setattr(existing, f, getattr(cand, f))
            existing.entity_type = existing.entity_type or cand.entity_type
            existing.geography = existing.geography or cand.geography
    return _fold_acronyms(list(merged.values()))


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def harvest(
    queries: List[str],
    *,
    seed_label: str = "",
    max_docs: int = 8,
    per_query: int = 6,
    max_workers: int = 4,
    doc_max_chars: int = 20000,
) -> Tuple[List[Candidate], Dict[str, int]]:
    """
    Run discovery queries, fetch the densest documents, extract LP names.

    Returns (candidates, stats) where stats carries `results_seen`,
    `docs_fetched` and `harvested` for the run's audit row.
    """
    from agents.research.llm_client import get_llm_client

    stats = {"results_seen": 0, "docs_fetched": 0, "harvested": 0}
    if not queries:
        return [], stats

    provider = _get_provider()
    results = _parallel_search(provider, queries, per_query, max_workers)
    stats["results_seen"] = len(results)
    if not results:
        return [], stats

    # Spend the fetch budget on the pages most likely to enumerate LPs.
    ranked = sorted(
        ((r, _doc_score(r)) for r in results),
        key=lambda pair: pair[1],
        reverse=True,
    )
    chosen = [r for r, score in ranked if score > _UNMINEABLE][:max_docs]
    if not chosen:
        return [], stats

    llm = get_llm_client()

    def _one(result: Any) -> Tuple[bool, List[Candidate]]:
        """(document was readable, mentions found)."""
        document = _document_text(provider, result, doc_max_chars)
        if len(document) < 200:
            return False, []
        return True, _extract_from_document(llm, result, document, seed_label)

    batches: List[List[Candidate]] = []
    with ThreadPoolExecutor(max_workers=max(1, min(max_workers, len(chosen)))) as pool:
        futures = [pool.submit(_one, r) for r in chosen]
        for fut in as_completed(futures):
            try:
                readable, batch = fut.result()
            except Exception as exc:
                logger.warning("Harvest document task failed: %s", exc)
                continue
            # Counts documents we actually read, not only the ones that yielded a
            # name. Conflating the two hides the difference between "no documents
            # were readable" and "documents were read but named no LPs", which are
            # different problems with different fixes.
            if readable:
                stats["docs_fetched"] += 1
            if batch:
                batches.append(batch)

    candidates = _merge_by_name(batches)
    stats["harvested"] = len(candidates)
    return candidates, stats

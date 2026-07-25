"""
Stage 4 of the cascade — CORROBORATE.

Owns SINGLE-SOURCE HALLUCINATION. This is the stage that did not exist before, and
it is the reason the pipeline can promote automatically without promoting junk.

Every earlier stage trusts one document. That is the pipeline's structural
weakness: a model that misreads one press release, or a press release that is
itself wrong, produces a candidate with real-looking evidence and no way to catch
it. So this stage asks one question and answers it in isolation:

    Does an INDEPENDENT source — a different domain from the one that discovered
    this name — also say this entity committed capital to a venture fund?

It is a binary test, not a scorecard. That is deliberate: Stage 3 fails on keyword
presence, Stage 5 fails on holistic model judgment, and this stage fails on source
independence. Three uncorrelated failure modes compound into real precision;
three correlated ones would just be the same filter run three times.

On the quotability test
----------------------
The pre-existing `_quotable` helper in the old runner tested token-bag overlap,
with a `_GENERIC_TOKENS` stoplist that stripped `committed`, `backed`, `emerging`,
`manager`, `limited` and `investor` — precisely the vocabulary LP evidence is
written in — and then demanded 60% overlap on the proper nouns that remained. A
correct paraphrase failed it, which is how every check ended up `unknown`.

This module tests something narrower and far more robust: does the entity name
appear within a bounded window of an explicit commitment phrase, in text we
actually fetched? Co-occurrence in a real page, not similarity to a model's words.
"""

from __future__ import annotations

import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from contra.prospector.models import Candidate, Corroboration, domain_of

logger = logging.getLogger(__name__)

# How far apart the entity name and a commitment phrase may sit and still count
# as the same claim. Roughly a long sentence.
WINDOW_CHARS = 320

# Explicit commitment language. Deliberately narrow: these assert that capital
# went INTO a fund, which is the only claim this stage is willing to certify.
_COMMITMENT_PATTERNS = tuple(re.compile(p, re.I) for p in (
    r"\blimited partner\b",
    r"\bLPs?\b(?=[^a-z])",
    r"\bcommit(?:s|ted|ment|ments)?\s+(?:capital\s+)?(?:to|in)\b",
    r"\bback(?:s|ed|er|ers)\s+(?:the\s+|its\s+)?fund\b",
    r"\banchor(?:s|ed|\s+investor|\s+lp)\b",
    r"\binvested\s+in\s+(?:the\s+)?fund\b",
    r"\binvestor\s+in\s+(?:the\s+)?fund\b",
    r"\bparticipat(?:ed|ion)\s+(?:in|from)\b",
    r"\bfund\s+(?:i{1,3}|1|2|3|one|two|three)\b",
    r"\bemerging\s+manager\s+program\b",
    r"\bfund\s+investor\b",
))


def _fetch_budget() -> int:
    raw = os.environ.get("PROSPECTOR_CORROBORATE_FETCH", "").strip()
    try:
        return max(0, int(raw)) if raw else 3
    except ValueError:
        return 3


# ---------------------------------------------------------------------------
# The independence test
# ---------------------------------------------------------------------------

def _name_variants(name: str) -> List[str]:
    """
    Forms of the entity name worth matching.

    Corporate suffixes are dropped because sources are wildly inconsistent about
    them ("Acme Capital Partners LLC" vs "Acme Capital"), and requiring the full
    legal form would reject genuine corroboration.
    """
    base = re.sub(r"\s+", " ", (name or "").strip())
    if not base:
        return []
    variants = {base}
    stripped = re.sub(
        r"\b(?:llc|l\.l\.c\.|ltd|limited|inc|incorporated|llp|lp|plc|pte|pvt|"
        r"private|holdings?|group|corporation|corp|co|company|sa|nv|bv|gmbh|ag)\b\.?",
        "", base, flags=re.I,
    )
    stripped = re.sub(r"[,\s]+$", "", re.sub(r"\s+", " ", stripped)).strip()
    if len(stripped) >= 4:
        variants.add(stripped)
    # Longest first so the most specific match wins.
    return sorted((v for v in variants if len(v) >= 4), key=len, reverse=True)


def find_commitment_span(name: str, text: str) -> str:
    """
    Verbatim span where `name` co-occurs with commitment language, or "".

    Returns the actual window from `text` so the quote stored downstream is real
    page content and can be shown to a human.
    """
    if not text:
        return ""
    variants = _name_variants(name)
    if not variants:
        return ""

    flat = re.sub(r"\s+", " ", text)
    low = flat.lower()

    for variant in variants:
        vlow = variant.lower()
        start = 0
        while True:
            idx = low.find(vlow, start)
            if idx < 0:
                break
            lo = max(0, idx - WINDOW_CHARS)
            hi = min(len(flat), idx + len(variant) + WINDOW_CHARS)
            window = flat[lo:hi]
            if any(p.search(window) for p in _COMMITMENT_PATTERNS):
                # Trim to sentence-ish boundaries for a readable quote.
                return _trim_to_sentence(window, idx - lo, len(variant))
            start = idx + len(variant)
    return ""


def _trim_to_sentence(window: str, name_at: int, name_len: int) -> str:
    """Trim a window to the sentence containing the entity name."""
    left = window.rfind(". ", 0, name_at)
    start = left + 2 if left >= 0 else 0
    right = window.find(". ", name_at + name_len)
    end = right + 1 if right >= 0 else len(window)
    span = window[start:end].strip()
    return span if len(span) >= 30 else window.strip()


# ---------------------------------------------------------------------------
# Independent research for one candidate
# ---------------------------------------------------------------------------

def _queries(name: str) -> List[str]:
    return [
        f'"{name}" limited partner venture fund commitment',
        f'"{name}" LP investor "venture fund" backed emerging manager',
    ]


def _corroborate_one(
    provider,
    cand: Candidate,
    *,
    per_query: int,
    fetch_budget: int,
) -> Candidate:
    """
    Search independently, then look for a commitment span on a NEW domain.

    Domains already seen during harvest are excluded outright — re-reading the
    discovery source cannot corroborate anything.
    """
    from agents.research.web_search import FetchError, SearchUnavailable, is_synthesis_url

    excluded: Set[str] = {d for d in cand.domains if d}
    if cand.source_domain:
        excluded.add(cand.source_domain)

    results: List[Any] = []
    for q in _queries(cand.name):
        try:
            results.extend(provider.search(q, max_results=per_query).results)
        except (SearchUnavailable, FetchError) as exc:
            logger.debug("Corroborate search failed (%s): %s", q, exc)
        except Exception as exc:
            logger.warning("Corroborate search error (%s): %s", q, exc)

    # Independent sources only, best-scoring first.
    fresh: List[Any] = []
    seen_urls: Set[str] = set()
    for r in sorted(results, key=lambda x: getattr(x, "score", 0.0), reverse=True):
        url = getattr(r, "url", "") or ""
        dom = domain_of(url)
        if not url or url in seen_urls:
            continue
        # A provider synthesis has no verifiable domain of its own, so it cannot
        # serve as an independent source however plausible its text is.
        if is_synthesis_url(url) or not dom or dom in excluded:
            continue
        seen_urls.add(url)
        fresh.append(r)

    found: List[Corroboration] = []
    fetches = 0
    for r in fresh:
        url = getattr(r, "url", "") or ""
        dom = domain_of(url)
        if any(c.domain == dom for c in found):
            continue  # one corroboration per domain — two pages on one site is one source

        text = (getattr(r, "raw_content", None) or getattr(r, "snippet", "") or "")
        span = find_commitment_span(cand.name, text)

        # Snippet was too thin; pay for the page.
        if not span and fetches < fetch_budget:
            fetches += 1
            try:
                page = provider.fetch(url) or ""
            except (SearchUnavailable, FetchError):
                page = ""
            except Exception:
                page = ""
            if page:
                span = find_commitment_span(cand.name, page)

        if span:
            found.append(Corroboration(quote=span[:600], url=url, domain=dom))
        if len(found) >= 2:  # the gate counts at most 2 analyst facts
            break

    cand.corroborations = found
    if found:
        domains = ", ".join(c.domain for c in found)
        cand.advance("corroborate")
        cand.verdict_reason = f"Corroborated by {len(found)} independent source(s): {domains}"
    else:
        # Not a rejection of the entity — a rejection of the *evidence*. These are
        # plausible but unproven, which is exactly what a human queue is for.
        cand.advance("corroborate")
        cand.status = "review"
        cand.verdict_reason = (
            "No independent source confirms a fund commitment — "
            f"only {cand.source_domain or 'the discovery source'} makes the claim"
        )
    return cand


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def corroborate(
    candidates: List[Candidate],
    *,
    per_query: int = 5,
    max_workers: int = 3,
) -> Tuple[List[Candidate], List[Candidate]]:
    """
    Split candidates into (corroborated, uncorroborated).

    Costs ~2 searches per candidate plus up to `PROSPECTOR_CORROBORATE_FETCH`
    page fetches, and no LLM calls at all — the test is deterministic.
    """
    if not candidates:
        return [], []

    from contra.prospector.harvest import _get_provider

    provider = _get_provider()
    provider_name = (getattr(provider, "provider", "") or type(provider).__name__).lower()
    workers = 2 if "anthropic" in provider_name else max_workers
    fetch_budget = _fetch_budget()

    done: List[Candidate] = []
    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(candidates)))) as pool:
        futures = [
            pool.submit(
                _corroborate_one, provider, c,
                per_query=per_query, fetch_budget=fetch_budget,
            )
            for c in candidates
        ]
        for fut in as_completed(futures):
            try:
                done.append(fut.result())
            except Exception as exc:
                logger.warning("Corroboration task failed: %s", exc)

    corroborated = [c for c in done if c.corroborated]
    uncorroborated = [c for c in done if not c.corroborated]
    corroborated.sort(key=_gate_priority, reverse=True)
    return corroborated, uncorroborated


# Org words too generic to identify an entity in a domain — "capital.com" must not
# read as first-party evidence for every firm with "Capital" in its name.
_GENERIC_ORG_WORDS = frozenset({
    "the", "and", "for", "group", "holdings", "holding", "capital", "ventures",
    "venture", "partners", "partner", "trust", "foundation", "fund", "funds",
    "family", "office", "investments", "investment", "management", "advisors",
    "associates", "company", "corporation", "international", "global", "limited",
    "systems", "system", "board", "retirement", "university", "endowment",
})


def _first_party(cand: Candidate) -> bool:
    """
    True when the entity's OWN site corroborates the commitment.

    rockefellerfoundation.org confirming a Rockefeller Foundation fund commitment
    is the strongest evidence available short of a regulatory filing, and it is
    essentially impossible to produce by accident.

    Matches on the distinctive words of the name rather than a fixed prefix, since
    a leading article defeats prefix matching ("The Rockefeller Foundation" starts
    with "thero...", not "rockef...").
    """
    words = [
        w for w in re.findall(r"[a-z]+", cand.name.lower())
        if len(w) >= 5 and w not in _GENERIC_ORG_WORDS
    ]
    if not words:
        return False
    return any(
        word in c.domain.replace("-", "")
        for c in cand.corroborations
        for word in words
    )


def _gate_priority(cand: Candidate):
    """
    Ordering for the gate queue — deliberately NOT by prerank score.

    Prerank score measures how talkative the discovery document was, and it is
    measurably anti-correlated with LP fitness (see the note in prerank.py). Using
    it to order the most expensive stage was actively harmful: in a live run it
    handed all six gate slots to corporate VCs and fund-of-funds GPs scoring 60,
    while the Rockefeller Foundation, the W.K. Kellogg Foundation, the Baupost
    Group and the Washington State Investment Board — each corroborated by two
    independent domains — were deferred on a score of 5.

    So priority follows the evidence this stage actually gathered: how many
    independent domains confirmed the commitment, and whether one of them was the
    entity itself.
    """
    return (len(cand.corroborations), _first_party(cand), cand.source_diversity)

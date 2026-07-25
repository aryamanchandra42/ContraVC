"""
Stage 3 of the cascade — PRERANK.

Owns STRUCTURAL MISFIT, and costs nothing. Two jobs:

  1. Hard-kill entities that are disqualified by construction — PE-buyout-only,
     secondaries-only, crypto-only, direct-deals-only, US/Europe-only mandates,
     sanctioned jurisdictions. These can never become LPs for an Asia-focused AI
     Fund I no matter how much evidence we gather, so paying to research them is
     pure waste.
  2. Rank the survivors, so the paid stages spend their budget top-down.

The keyword lists are not invented here. They are the same C1-C4 core filters and
E1-E14 exclusion phrases the offline ICP scorer already uses
(`agents/scoring/icp_spec.py`), which means the miner and the ICP pipeline agree
on what disqualifies an LP instead of drifting apart.

Scoring runs against the VERBATIM span from Stage 1, never a paraphrase, so a hit
is always traceable to text a human can read.

WEIGHTS are deliberately exposed as a dict: `contra/scripts/eval_prerank.py`
grid-searches them against the 158 labelled client decisions in `icp_scores`, so
the cutoff comes off a precision/recall curve rather than out of the air.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from contra.prospector.models import Candidate

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Weights (calibratable)
# ---------------------------------------------------------------------------

WEIGHTS: Dict[str, int] = {
    # Core filters. C1 dominates: "does this entity commit to VC funds as an LP"
    # is the whole question, and the rest are only appetite refinements.
    "c1_fund_lp":        35,
    "c2_new_managers":   20,
    "c3_thesis":         15,
    "c4_geography":      10,
    # Independence and provenance.
    "diversity_2":        8,   # named as an LP by 2 distinct domains
    "diversity_3plus":   14,   # ...by 3 or more
    "doc_fund_close":     6,   # found in a close announcement, not a profile page
    "confidence_high":    5,
}

# Prerank RANKS; it does not filter on score.
#
# This default was 40, and the measured consequence was bad. In a live run the
# Rockefeller Foundation, the Kresge Foundation, the W.K. Kellogg Foundation, the
# Baupost Group and the Washington State Investment Board — all real institutional
# LPs — scored 5 and were discarded, because they surfaced in a directory-style
# document whose span was little more than the name. Intel Capital, a corporate VC
# that commits to no external funds at all, scored 60 off a rich paragraph. The
# score tracks how talkative the source was, not how good the LP is — the same
# conclusion `scripts/eval_prerank.py` reaches against the 158 labelled client
# decisions (AUC 0.406, slightly worse than a coin toss).
#
# So the score orders the queue, the hard exclusions still kill outright, and cost
# is controlled by PROSPECTOR_MAX_CANDIDATES taking the top N — not by a threshold
# that throws away foundations. Raise PROSPECTOR_PRERANK_MIN above 0 only with a
# measurement that justifies it.
DEFAULT_MIN_SCORE = 0


def min_score() -> int:
    raw = os.environ.get("PROSPECTOR_PRERANK_MIN", "").strip()
    try:
        return int(raw) if raw else DEFAULT_MIN_SCORE
    except ValueError:
        return DEFAULT_MIN_SCORE


# ---------------------------------------------------------------------------
# Text matching
# ---------------------------------------------------------------------------

def _normalize(text: str) -> str:
    """
    Lowercase with collapsed whitespace, space-padded.

    Padding matters: several icp_spec patterns are written with surrounding
    spaces (" ai ", " us ") specifically to avoid matching inside other words.
    """
    return " " + re.sub(r"\s+", " ", (text or "").lower()) + " "


def _hits(text_norm: str, phrases: Any) -> List[str]:
    return [p for p in phrases if p and p.lower() in text_norm]


# ---------------------------------------------------------------------------
# Assertive matching for exclusions
# ---------------------------------------------------------------------------

# Words that, following a matched phrase, mark it as commentary ABOUT evidence
# rather than a statement about the LP. Without this guard the E8 phrase
# "no emerging manager" fires on "No emerging manager EVIDENCE in scoring text",
# which records that we failed to find something — the opposite of a
# disqualifier. Measured on the 158 labelled allocators, that single collision
# hard-excluded 78 of the 112 client-APPROVED LPs.
_META_SUFFIXES = (
    "evidence", "signal", "signals", "in scoring text", "found", "detected",
    "mention", "mentioned", "data", "information", "indication", "record",
    "recorded", "listed", "available", "identified", "visible", "history",
)

# Negators that invert a phrase when they immediately precede it, so that
# "no private equity focus" does not match the E1 phrase "private equity focus".
_NEGATORS = frozenset({
    "no", "not", "non", "without", "never", "neither", "nor", "lacks", "lacking",
    "isn't", "isnt", "aren't", "arent", "doesn't", "doesnt", "don't", "dont",
    "excludes", "excluding", "besides", "unlike",
})

# Phrases that already carry their own negative polarity — the E8/E10 families.
_SELF_NEGATED_PREFIXES = (
    "no ", "not ", "does not", "doesnt", "don't", "dont", "only ",
)

# Where the clause containing a match begins. Negation does not reach across a
# clause boundary: in "does not invest in real estate; it backs emerging
# managers" the "not" belongs to the first clause and must not suppress the
# second, whereas in "commits to funds but does not back emerging managers" it
# must.
_CLAUSE_BREAK = re.compile(
    r"(?:[,;:.]|\b(?:but|however|although|though|while|whereas|yet|instead)\b)"
)


def _clause_before(text_norm: str, at: int) -> str:
    """The text from the start of the current clause up to `at`."""
    head = text_norm[:at]
    last = None
    for m in _CLAUSE_BREAK.finditer(head):
        last = m
    return head[last.end():] if last else head


def _is_assertive(text_norm: str, phrase: str, at: int) -> bool:
    """
    True when a matched phrase actually asserts what it appears to assert.

    Two ways a raw substring match lies:
      1. Meta context — the phrase is followed by "evidence" / "signals" /
         "in scoring text", so the sentence is about our data, not the LP.
      2. Negation — a negator appears earlier in the SAME clause, so the sentence
         denies what the phrase would otherwise assert. Adjacency is not enough:
         "does not back emerging managers" puts two words between the negator and
         the phrase. Skipped for phrases that are themselves negative, since
         those carry their own polarity.
    """
    end = at + len(phrase)
    if any(m in text_norm[end:end + 40] for m in _META_SUFFIXES):
        return False

    if not phrase.lstrip().startswith(_SELF_NEGATED_PREFIXES):
        clause_words = re.findall(r"[a-z']+", _clause_before(text_norm, at))
        # Only the few words leading up to the phrase can scope over it.
        if any(w in _NEGATORS for w in clause_words[-5:]):
            return False

    return True


def _assertive_hits(text_norm: str, phrases: Any) -> List[str]:
    """Exclusion-phrase hits that survive the meta and negation guards."""
    out: List[str] = []
    for phrase in phrases:
        if not phrase:
            continue
        low = phrase.lower()
        start = 0
        while True:
            at = text_norm.find(low, start)
            if at < 0:
                break
            if _is_assertive(text_norm, low, at):
                out.append(phrase)
                break
            start = at + len(low)
    return out


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------

@dataclass
class PrerankResult:
    score: int = 0
    checks: Dict[str, Any] = field(default_factory=dict)
    hard_exclusion: str = ""

    @property
    def excluded(self) -> bool:
        return bool(self.hard_exclusion)


def score_candidate(
    name: str,
    span: str,
    *,
    entity_type: str = "",
    geography: str = "",
    doc_type: str = "",
    confidence: str = "medium",
    source_diversity: int = 1,
    weights: Optional[Dict[str, int]] = None,
) -> PrerankResult:
    """
    Score one candidate on text alone. No API calls, no database.

    Kept independent of `Candidate` so the calibration script can feed it rows
    from `icp_scores` and measure it against real client decisions.
    """
    from agents.scoring.icp_spec import (
        ALL_HARD_EXCLUSION_PHRASES,
        C1_KEYWORDS,
        C1_REQUIRED_ANY,
        C2_EMERGING_MANAGER_POSITIVE,
        C3_SECTORS,
        C4_REGIONS,
        SANCTIONED_COUNTRIES,
    )

    w = {**WEIGHTS, **(weights or {})}
    # Entity type and geography are descriptive metadata about this candidate, so
    # they are legitimate scoring surface alongside the span.
    text = _normalize(" ".join(filter(None, (span, entity_type, geography))))
    result = PrerankResult()

    # --- Hard exclusions: any hit ends it ---------------------------------
    excl = _assertive_hits(text, ALL_HARD_EXCLUSION_PHRASES)
    if excl:
        result.hard_exclusion = f"hard exclusion: '{excl[0]}'"
        result.checks = {"hard_exclusions": excl[:3]}
        return result

    sanctioned = [c for c in SANCTIONED_COUNTRIES if f" {c} " in text]
    if sanctioned:
        result.hard_exclusion = f"sanctioned jurisdiction: {sanctioned[0]}"
        result.checks = {"sanctioned": sanctioned[:3]}
        return result

    # --- Core filters -----------------------------------------------------
    checks: Dict[str, Any] = {}
    score = 0

    # The same assertive matching applies to the POSITIVE checks. A raw substring
    # test counts "does not back emerging managers" as emerging-manager appetite,
    # and "no qualifying region (Asia/NA/ME)" as an Asia hit via the word "asia" —
    # scoring an LP up on sentences that say the opposite.
    c1_hits = _assertive_hits(text, C1_KEYWORDS)
    # C1_REQUIRED_ANY guards against "fund manager" style text that mentions
    # funds without describing a commitment.
    c1_ok = bool(c1_hits) and bool(_assertive_hits(text, C1_REQUIRED_ANY))
    if c1_ok:
        score += w["c1_fund_lp"]
    checks["c1_fund_lp"] = {"met": c1_ok, "hits": c1_hits[:4]}

    c2_hits = _assertive_hits(text, C2_EMERGING_MANAGER_POSITIVE)
    if c2_hits:
        score += w["c2_new_managers"]
    checks["c2_new_managers"] = {"met": bool(c2_hits), "hits": c2_hits[:4]}

    c3_hits = _assertive_hits(text, C3_SECTORS)
    if c3_hits:
        score += w["c3_thesis"]
    checks["c3_thesis"] = {"met": bool(c3_hits), "hits": c3_hits[:4]}

    c4_hits = _assertive_hits(text, C4_REGIONS)
    if c4_hits:
        score += w["c4_geography"]
    checks["c4_geography"] = {"met": bool(c4_hits), "hits": c4_hits[:4]}

    # --- Independence and provenance --------------------------------------
    if source_diversity >= 3:
        score += w["diversity_3plus"]
    elif source_diversity == 2:
        score += w["diversity_2"]
    checks["source_diversity"] = source_diversity

    if doc_type == "fund_close":
        score += w["doc_fund_close"]
    checks["doc_type"] = doc_type

    if (confidence or "").lower() == "high":
        score += w["confidence_high"]
    checks["confidence"] = confidence

    result.score = min(score, 100)
    result.checks = checks
    return result


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def prerank(
    candidates: List[Candidate],
    *,
    cutoff: Optional[int] = None,
) -> Tuple[List[Candidate], List[Candidate]]:
    """
    Score and split candidates into (survivors, dropped), survivors ranked best-first.

    Zero API calls.
    """
    threshold = min_score() if cutoff is None else cutoff
    survivors: List[Candidate] = []
    dropped: List[Candidate] = []

    for cand in candidates:
        res = score_candidate(
            cand.name,
            cand.span,
            entity_type=cand.entity_type,
            geography=cand.geography,
            doc_type=cand.doc_type,
            confidence=cand.confidence,
            source_diversity=max(1, cand.source_diversity),
        )
        cand.prerank_score = res.score
        cand.prerank_checks = res.checks

        if res.excluded:
            dropped.append(cand.drop("prerank", res.hard_exclusion))
            continue
        if res.score < threshold:
            dropped.append(cand.drop(
                "prerank", f"prerank {res.score} below cutoff {threshold}"
            ))
            continue
        survivors.append(cand.advance("prerank"))

    survivors.sort(key=lambda c: c.prerank_score, reverse=True)
    return survivors, dropped

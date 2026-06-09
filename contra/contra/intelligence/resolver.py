"""Name normalization and allocator resolution."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from rapidfuzz import fuzz, process

_LEGAL_SUFFIX_RE = re.compile(
    r"\s+(ltd|limited|llc|inc|corp|plc|pte|sa|bv|gmbh|lp|llp|co)\.?$", re.IGNORECASE
)

FUZZY_AUTO = 92      # trust fully, load all DB data
FUZZY_REVIEW = 85    # load with match_untrusted=True caveat
FUZZY_LOW = 60       # load ONLY investment summary (no ICP/syndicate) with caveat


def norm_key(name: str) -> str:
    key = (name or "").strip().lower()
    key = _LEGAL_SUFFIX_RE.sub("", key).strip()
    key = re.sub(r"[^a-z0-9]", "", key)
    return key


@dataclass
class MatchResult:
    input_name: str
    matched_name: Optional[str]
    allocator_id: Optional[str]
    confidence: float
    method: str  # exact | alias | fuzzy | none


def resolve(con, name: str) -> MatchResult:
    key = norm_key(name)
    if not key:
        return MatchResult(name, None, None, 0.0, "none")

    row = con.execute(
        """
        SELECT CAST(allocator_id AS VARCHAR), canonical_name
        FROM allocators_effective
        WHERE lower(regexp_replace(canonical_name, '[^a-zA-Z0-9]', '', 'g')) = ?
        LIMIT 1
        """,
        [key],
    ).fetchone()
    if row:
        return MatchResult(name, row[1], row[0], 1.0, "exact")

    alias = con.execute(
        """
        SELECT ea.canonical_id, a.canonical_name
        FROM entity_aliases ea
        JOIN allocators_effective a ON CAST(a.allocator_id AS VARCHAR) = ea.canonical_id
        WHERE lower(regexp_replace(ea.alias_text, '[^a-zA-Z0-9]', '', 'g')) = ?
        LIMIT 1
        """,
        [key],
    ).fetchone()
    if alias:
        return MatchResult(name, alias[1], alias[0], 0.95, "alias")

    names: List[Tuple[str, str]] = con.execute(
        "SELECT CAST(allocator_id AS VARCHAR), canonical_name FROM allocators_effective"
    ).fetchall()
    choices = [n for _, n in names]
    if not choices:
        return MatchResult(name, None, None, 0.0, "none")

    hit = process.extractOne(name, choices, scorer=fuzz.token_sort_ratio)
    if not hit:
        return MatchResult(name, None, None, 0.0, "none")
    matched, score, idx = hit
    aid = names[idx][0]
    conf = score / 100.0
    if score >= FUZZY_AUTO:
        return MatchResult(name, matched, aid, conf, "fuzzy")
    if score >= FUZZY_REVIEW:
        return MatchResult(name, matched, aid, conf, "fuzzy_review")
    if score >= FUZZY_LOW:
        # Return the candidate ID so we can pull investment history only.
        # All other DB signals (ICP, syndicate) are suppressed in brief.py.
        return MatchResult(name, matched, aid, conf, "fuzzy_low")
    return MatchResult(name, None, None, conf, "none")

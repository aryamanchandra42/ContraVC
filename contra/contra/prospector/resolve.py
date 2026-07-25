"""
Stage 2 of the cascade — RESOLVE.

Owns IDENTITY errors, and costs nothing. Every rejection here is free, which is
the entire reason this stage sits before the paid ones.

The extractor is instructed to return only LPs, but in practice a document about a
fund close names the fund, its GPs, its portfolio companies, its placement agent
and the reporter who wrote it. Those are false positives of a kind that no amount
of evidence-scoring downstream can fix, because the evidence is real — it is the
*entity* that is wrong. So they are removed on structural grounds before a single
API call is spent:

    - the name is a fund in our own `funds` table (a fund is not an LP)
    - the span describes them as the manager/GP, not the committer
    - the name is not an organisation or person at all
    - we already know them (in CRM, already screened, already emailed)

The "already known" case is deliberately not a plain drop. A high fuzzy match
against `allocators` means the name is already an asset, so it is reported
separately as `known` rather than being silently discarded.
"""

from __future__ import annotations

import logging
import re
from typing import Dict, List, Optional, Sequence, Set, Tuple

from contra.prospector.models import Candidate

logger = logging.getLogger(__name__)

# Fuzzy threshold for "this is the same entity we already have".
FUZZY_MATCH_CUTOFF = 93


# ---------------------------------------------------------------------------
# Span-role tests — is the entity the committer, or the manager?
# ---------------------------------------------------------------------------

# The span named this entity, but in a role that is not "LP".
_GP_ROLE_PATTERNS = tuple(re.compile(p, re.I) for p in (
    r"\b(?:is|as|its|their|the)\s+(?:the\s+)?(?:founding|managing|general)\s+partner\b",
    r"\bfound(?:ed|er|ers)\s+(?:the\s+)?fund\b",
    r"\b(?:launch|launche[sd]|raising|raised|closes?|closed)\s+(?:its|their|the|a)\b",
    r"\bmanaging\s+director\s+of\s+the\s+fund\b",
    r"\bgeneral\s+partner\s+(?:at|of)\b",
    # Real client rejection wording: "runs own fund and doesnt invest as an LP".
    # An entity running its own fund is a GP, which the gate treats as a
    # structural disqualifier too.
    r"\bruns?\s+(?:his|her|its|their|)\s*own\s+fund\b",
    r"\bplacement\s+agent\b",
    r"\blegal\s+(?:counsel|advis)",
    r"\bfund\s+administrator\b",
    r"\bauditor\b",
    # Corporate venture arms (Intel Capital, Salesforce Ventures, Qualcomm
    # Ventures) invest DIRECTLY in startups off a parent's balance sheet and
    # commit to no external funds. In a live run these reached the gate and were
    # rejected there as "wrong entity type" — a correct verdict, but one this
    # stage can reach for free. Naming alone cannot identify them, since plenty
    # of real LPs are called "X Capital"; the span saying "corporate venture arm"
    # can.
    r"\bcorporate\s+(?:venture|vc)\b",
    r"\b(?:venture|investment)\s+arm\s+of\b",
    r"\bcvc\s+(?:arm|unit|fund)\b",
    r"\bstrategic\s+investment\s+arm\b",
))

# NOT tested here: "raises capital from" and "manages assets on behalf of".
#
# Those read like GP behaviour but describe a fund-of-funds exactly — an FoF raises
# from its own LPs and then commits that capital INTO venture funds. Since a
# fund-of-funds is the highest-priority LP type in the ICP spec
# (`LP_TYPE_PRIORITY["fund_of_funds"] = 1.00`), matching on fundraising language
# would silently kill the best leads the miner can find, for free and with no gate
# verdict to show for it. Raising capital and committing capital are not mutually
# exclusive, so neither phrase can settle identity on its own.

# Portfolio-company language: the fund invested IN them, they are not an LP.
_PORTFOLIO_PATTERNS = tuple(re.compile(p, re.I) for p in (
    r"\bportfolio\s+compan(?:y|ies)\b",
    r"\b(?:invested|investment)\s+in\s+\w+.{0,40}\b(?:seed|series\s+[a-d])\b",
    r"\b(?:seed|series\s+[a-d])\s+round\b",
    r"\bacquired\s+by\b",
))

# Words that mean this is not a nameable investing entity at all.
_NON_ENTITY_TOKENS = frozenset({
    "unknown", "undisclosed", "anonymous", "confidential", "various",
    "several", "multiple", "others", "n/a", "na", "none", "tbd",
    "family offices", "family office", "limited partners", "institutional investors",
    "high net worth individuals", "hnwis", "angel investors", "investors",
    "lps", "lp", "gps", "gp", "the fund", "the company", "the firm",
})


def _is_non_entity(name: str) -> bool:
    """True for placeholders and unnamed collective nouns."""
    low = name.strip().lower().strip(".,;:")
    if low in _NON_ENTITY_TOKENS:
        return True
    if len(low) < 3:
        return True
    # "a number of family offices", "undisclosed investors", etc.
    if not re.search(r"[a-z]", low):
        return True
    # A name that is entirely generic role words carries no identity.
    words = [w for w in re.findall(r"[a-z]+", low) if len(w) > 2]
    return bool(words) and all(
        w in {"family", "office", "offices", "limited", "partner", "partners",
              "investor", "investors", "institutional", "individual", "individuals",
              "undisclosed", "various", "other", "others", "anonymous"}
        for w in words
    )


def _span_role_conflict(span: str) -> str:
    """Non-empty reason when the span casts this entity as something other than an LP."""
    for pat in _GP_ROLE_PATTERNS:
        if pat.search(span):
            return "span describes a manager/GP or service provider, not an LP"
    for pat in _PORTFOLIO_PATTERNS:
        if pat.search(span):
            return "span describes a portfolio company, not an LP"
    return ""


# ---------------------------------------------------------------------------
# Known-universe lookups
# ---------------------------------------------------------------------------

def _fetch_keys(con, sql: str) -> Set[str]:
    try:
        return {r[0] for r in con.execute(sql).fetchall() if r[0]}
    except Exception as exc:
        logger.debug("Resolve lookup failed (%s): %s", sql[:60], exc)
        return set()


def _fund_name_keys(con) -> Set[str]:
    """name_keys of funds we know about — these are never LPs."""
    from agents.normalization.crm_normalizer import norm_key

    try:
        rows = con.execute("SELECT canonical_name FROM funds").fetchall()
    except Exception:
        return set()
    keys = set()
    for (name,) in rows:
        if name:
            key = norm_key(name)
            if key:
                keys.add(key)
    return keys


def _blocked_keys(con) -> Dict[str, str]:
    """
    name_key -> reason, for entities that must not be re-mined.

    A candidate previously screened to `no` is blocked only until its
    `revisit_date`. The gate itself distinguishes a confirmed misfit from a mere
    absence of evidence (`_CONFIRMED_MISFIT_FLAGS` vs `_ABSENCE_FLAGS` in
    gate/evaluator.py); honouring that here is what stops a thin-evidence NO from
    banning a good LP forever.
    """
    blocked: Dict[str, str] = {}

    for key in _fetch_keys(con, "SELECT name_key FROM crm_leads"):
        blocked[key] = "already a CRM lead"
    for key in _fetch_keys(con, "SELECT name_key FROM crm_dismissed"):
        blocked[key] = "previously dismissed"
    for key in _fetch_keys(
        con, "SELECT DISTINCT name_key FROM outreach_log WHERE name_key IS NOT NULL"
    ):
        blocked.setdefault(key, "already contacted")

    # Previously screened, and not yet due for another look.
    try:
        rows = con.execute(
            """
            SELECT name_key, status, revisit_date
            FROM prospector_candidates
            WHERE status IN ('rejected', 'dismissed', 'promoted', 'review')
            """
        ).fetchall()
    except Exception:
        rows = []
    for name_key, status, revisit in rows:
        if not name_key or name_key in blocked:
            continue
        if status in ("promoted", "review"):
            blocked[name_key] = f"already in prospector queue ({status})"
        elif revisit is None:
            blocked[name_key] = "previously rejected"
        # revisit_date set and in the past → deliberately left unblocked.
        elif str(revisit) > _today():
            blocked[name_key] = f"rejected, revisit after {revisit}"

    # Screened by the gate outside the miner, with no revisit policy recorded.
    for key in _fetch_keys(
        con, "SELECT name_key FROM crm_gate_reviews WHERE gate_verdict = 'no'"
    ):
        blocked.setdefault(key, "gate already returned no")

    return blocked


def _today() -> str:
    from datetime import date

    return date.today().isoformat()


def _allocator_names(con) -> List[str]:
    try:
        return [
            r[0] for r in con.execute("SELECT canonical_name FROM allocators").fetchall()
            if r[0]
        ]
    except Exception:
        return []


def _fuzzy_known(name: str, allocator_names: Sequence[str]) -> Optional[str]:
    """Matched allocator name when this is the same entity we already hold."""
    if not allocator_names:
        return None
    try:
        from rapidfuzz import fuzz, process

        match = process.extractOne(
            name, allocator_names, scorer=fuzz.token_sort_ratio,
            score_cutoff=FUZZY_MATCH_CUTOFF,
        )
        return match[0] if match else None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def resolve(
    con,
    candidates: List[Candidate],
) -> Tuple[List[Candidate], List[Candidate], List[Candidate]]:
    """
    Split harvested candidates into (fresh, known, dropped).

    fresh   — genuinely new entities worth spending money on
    known   — already in `allocators`; an enrichment opportunity, not a new lead
    dropped — identity failures, with `drop_reason` set

    Zero API calls.
    """
    from agents.normalization.crm_normalizer import norm_key

    fund_keys = _fund_name_keys(con)
    blocked = _blocked_keys(con)
    allocator_names = _allocator_names(con)

    fresh: List[Candidate] = []
    known: List[Candidate] = []
    dropped: List[Candidate] = []
    seen: Set[str] = set()

    for cand in candidates:
        key = cand.name_key or norm_key(cand.name)
        cand.name_key = key

        if not key:
            dropped.append(cand.drop("resolve", "name does not normalise"))
            continue
        if key in seen:
            dropped.append(cand.drop("resolve", "duplicate within run"))
            continue
        seen.add(key)

        if _is_non_entity(cand.name):
            dropped.append(cand.drop("resolve", "not a named entity"))
            continue
        if key in fund_keys:
            dropped.append(cand.drop("resolve", "name is a fund, not an LP"))
            continue

        role_conflict = _span_role_conflict(cand.span)
        if role_conflict:
            dropped.append(cand.drop("resolve", role_conflict))
            continue

        reason = blocked.get(key)
        if reason:
            dropped.append(cand.drop("resolve", reason))
            continue

        matched = _fuzzy_known(cand.name, allocator_names)
        if matched:
            cand.advance("resolve")
            cand.status = "review"
            cand.verdict_reason = f"Already in database as '{matched}' — enrichment candidate"
            known.append(cand)
            continue

        fresh.append(cand.advance("resolve"))

    return fresh, known, dropped

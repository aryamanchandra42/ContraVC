"""
LP archetype similarity — multi-dimensional scoring for similar-LP calibration.

Replaces the old fund-deal-count ranking with an explainable, weighted scorer
that takes geography, allocator type, EM/AI appetite, fund thesis overlap, and
behavioral archetype into account.

Two-pass design:
  Pre-LLM  — build_similarity_target() from brief + NFX context; coarser match.
  Post-LLM — rebuild target with appetite fields filled; tighter archetype match.

Thresholds (tunable here, consumed by evaluator.py and brief.py):
  MIN_DISPLAY_SCORE  — include in UI (weak anchors still shown for context)
  MIN_SIGNAL_SCORE   — count toward the strict "Similar Confirmed LP Precedent" signal
  MIN_SIGNAL_COUNT   — ≥ N qualifying anchors fires the signal
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set

from agents.normalization.taxonomies import Geography, normalize_geography

# ---------------------------------------------------------------------------
# Thresholds
# ---------------------------------------------------------------------------

MIN_DISPLAY_SCORE: int = 35
MIN_SIGNAL_SCORE: int = 45
MIN_SIGNAL_COUNT: int = 2


# ---------------------------------------------------------------------------
# Geography adjacency — partial credit for nearby/overlapping regions
# ---------------------------------------------------------------------------

_GEO_ADJACENT: Dict[str, Set[str]] = {
    Geography.SOUTHEAST_ASIA: {Geography.ASIA_PACIFIC, Geography.EAST_ASIA, Geography.EMERGING_MARKETS},
    Geography.SOUTH_ASIA:     {Geography.ASIA_PACIFIC, Geography.EMERGING_MARKETS},
    Geography.EAST_ASIA:      {Geography.ASIA_PACIFIC, Geography.SOUTHEAST_ASIA},
    Geography.ASIA_PACIFIC:   {Geography.SOUTHEAST_ASIA, Geography.SOUTH_ASIA, Geography.EAST_ASIA},
    Geography.MIDDLE_EAST:    {Geography.EMERGING_MARKETS, Geography.GLOBAL},
    Geography.AFRICA:         {Geography.EMERGING_MARKETS},
    Geography.NORTH_AMERICA:  {Geography.GLOBAL},
    Geography.EUROPE:         {Geography.GLOBAL},
    Geography.EMERGING_MARKETS: {
        Geography.SOUTHEAST_ASIA, Geography.SOUTH_ASIA, Geography.MIDDLE_EAST, Geography.AFRICA,
    },
    Geography.GLOBAL:         set(),
}

# Contra ICP geographies — full credit if target geography belongs to one of these clusters.
_CONTRA_ICP_GEOS: Set[str] = {
    Geography.SOUTHEAST_ASIA, Geography.ASIA_PACIFIC, Geography.EAST_ASIA, Geography.SOUTH_ASIA,
    Geography.NORTH_AMERICA,
    Geography.MIDDLE_EAST,
}

# ---------------------------------------------------------------------------
# Allocator type groupings for partial match
# ---------------------------------------------------------------------------

_FAMILY_OFFICE_TYPES: Set[str] = {"family_office", "family_office_single", "family_office_multi"}
_FOF_TYPES:           Set[str] = {"fund_of_funds"}
_INSTITUTION_TYPES:   Set[str] = {"pension_fund", "sovereign_wealth", "endowment", "insurance",
                                   "asset_manager", "foundation", "development_finance", "institution"}
_INDIVIDUAL_TYPES:    Set[str] = {"high_net_worth", "angel", "hnwi"}


def _type_group(raw: Optional[str]) -> str:
    """Collapse DB allocator_type into a broad group for scoring."""
    t = (raw or "").lower().replace(" ", "_").replace("-", "_")
    if t in _FAMILY_OFFICE_TYPES or "family" in t:
        return "family_office"
    if t in _FOF_TYPES or "fund_of_funds" in t or "fof" in t:
        return "fund_of_funds"
    if any(k in t for k in ("institution", "pension", "endowment", "sovereign", "asset_manag", "insurance")):
        return "institution"
    if any(k in t for k in ("hnwi", "high_net", "angel", "individual")):
        return "individual"
    return "other"


# ---------------------------------------------------------------------------
# Appetite ordinal
# ---------------------------------------------------------------------------

_APPETITE_ORD: Dict[str, int] = {
    "strong":   3,
    "moderate": 2,
    "weak":     1,
    "none":     0,
    "unknown":  -1,
}


def _appetite_score(target_level: str, candidate_level: str, weight: int) -> int:
    """
    Award 0–weight points for appetite similarity.

    Both unknown → half credit (we don't know → neutral, not negative).
    Target unknown → half credit (give benefit of doubt).
    Otherwise linear on ordinal distance.
    """
    t = _APPETITE_ORD.get((target_level or "").lower(), -1)
    c = _APPETITE_ORD.get((candidate_level or "").lower(), -1)
    if t == -1 and c == -1:
        return weight // 2
    if t == -1:
        return weight // 2
    if c == -1:
        return 0
    max_dist = 3
    dist = abs(t - c)
    return round(weight * (1 - dist / max_dist))


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class LpSimilarityTarget:
    """Normalized profile of the LP being screened — used to score candidates."""
    geography: str = Geography.UNKNOWN
    allocator_type: str = ""
    em_appetite: str = "unknown"
    ai_appetite: str = "unknown"
    archetype: str = "unknown"
    check_size_bucket: str = "unknown"
    fund_focus_geos: Set[str] = field(default_factory=set)
    # Exclude this LP from its own result set
    exclude_id: Optional[str] = None
    exclude_name: Optional[str] = None


@dataclass
class SimilarityResult:
    """Score + explanation for a single candidate anchor LP."""
    score: int                              # 0–100
    match_dimensions: List[str]            # e.g. ["geography", "em_appetite"]
    archetype: str                         # inferred archetype of the candidate


@dataclass
class ArchetypeFit:
    """Post-LLM summary comparing screened LP to matched anchors."""
    fit_level: str        # "strong" | "partial" | "weak" | "none"
    avg_similarity_score: int
    rationale: str


# ---------------------------------------------------------------------------
# Archetype inference from DB fields (no LLM)
# ---------------------------------------------------------------------------

def infer_db_archetype(
    allocator_type: Optional[str],
    geography: Optional[str],
    em_appetite: Optional[str],
    fund_deal_count: int,
    fund_focus_geos: Optional[Set[str]] = None,
) -> str:
    """Map raw DB fields to a behavioral Archetype without using the LLM."""
    geo_norm = normalize_geography(geography)
    type_grp = _type_group(allocator_type)
    em_lvl = (em_appetite or "").lower()
    focs = fund_focus_geos or set()

    if type_grp == "fund_of_funds":
        return "fund_of_funds"

    # Emerging-manager specialist: strong EM appetite + repeat fund LP behavior
    if em_lvl in ("strong", "moderate") and fund_deal_count >= 2:
        return "emerging_manager_specialist"

    if type_grp == "family_office":
        return "family_office"

    # Asia specialist: geography in Asia cluster + funded Asia-focused funds
    asia_geos = {Geography.SOUTHEAST_ASIA, Geography.EAST_ASIA, Geography.SOUTH_ASIA, Geography.ASIA_PACIFIC}
    if geo_norm in asia_geos or any(g in asia_geos for g in focs):
        return "asia_specialist"

    if type_grp == "institution":
        return "institutional_lp"

    if type_grp == "individual" and fund_deal_count >= 1:
        return "founder_lp"

    return "generalist"


# ---------------------------------------------------------------------------
# Target profile builder
# ---------------------------------------------------------------------------

def build_similarity_target(
    brief,                          # IntelligenceBrief (avoid circular import)
    *,
    nfx_context: Optional[str] = None,
    web_context: Optional[str] = None,
    appetite=None,                  # Optional[AppetiteProfile] — post-LLM only
) -> LpSimilarityTarget:
    """
    Build a normalized target profile for similarity scoring.

    Priority order per field:
      geography   → brief.allocator_profile > NFX Location: line > normalize from web keywords
      type        → brief.allocator_profile > NFX firm keywords
      em/ai       → brief.allocator_profile > post-LLM AppetiteProfile
      archetype   → post-LLM AppetiteProfile.archetype
    """
    profile = brief.allocator_profile or {}

    # --- Geography ---
    geo_raw = str(profile.get("geography") or "")
    if not geo_raw and nfx_context:
        for line in nfx_context.splitlines():
            low = line.strip().lower()
            if low.startswith("location") or low.startswith("locations"):
                geo_raw = line.split(":", 1)[-1].strip()
                break
    if not geo_raw and web_context:
        # Light heuristic: pick the first geography keyword found
        from agents.normalization.taxonomies import GEOGRAPHY_PATTERNS
        for geo_val, pats in GEOGRAPHY_PATTERNS.items():
            if any(p.lower() in web_context.lower() for p in pats if len(p) > 3):
                geo_raw = geo_val
                break
    geography = _canonicalize_geo(geo_raw) if geo_raw else Geography.UNKNOWN

    # --- Allocator type ---
    alloc_type = str(profile.get("allocator_type") or "")

    # --- EM / AI appetite ---
    em_ap = str(profile.get("em_appetite") or "unknown").lower()
    ai_ap = str(profile.get("ai_appetite") or "unknown").lower()

    # --- Post-LLM overrides ---
    archetype = "unknown"
    if appetite is not None:
        if appetite.archetype and appetite.archetype != "unknown":
            archetype = appetite.archetype
        if appetite.em_appetite and appetite.em_appetite != "unknown":
            em_ap = appetite.em_appetite
        if appetite.ai_tech_appetite and appetite.ai_tech_appetite != "unknown":
            ai_ap = appetite.ai_tech_appetite

    # --- Fund focus geographies (from backed funds when allocator is known) ---
    fund_focus_geos: Set[str] = set()
    inv_summary = brief.investment_summary or {}
    for geo_key in ("geography_focus", "fund_geographies", "fund_focus"):
        raw = inv_summary.get(geo_key)
        if isinstance(raw, (list, set)):
            for g in raw:
                n = normalize_geography(str(g))
                if n != Geography.UNKNOWN:
                    fund_focus_geos.add(n)
        elif isinstance(raw, str) and raw:
            n = normalize_geography(raw)
            if n != Geography.UNKNOWN:
                fund_focus_geos.add(n)

    return LpSimilarityTarget(
        geography=geography,
        allocator_type=alloc_type,
        em_appetite=em_ap,
        ai_appetite=ai_ap,
        archetype=archetype,
        check_size_bucket=str(profile.get("check_size_bucket") or "unknown"),
        fund_focus_geos=fund_focus_geos,
        exclude_id=brief.allocator_id,
        exclude_name=brief.input_name,
    )


# ---------------------------------------------------------------------------
# Geography scorer
# ---------------------------------------------------------------------------

_CANONICAL_GEOS: frozenset = frozenset(g.value for g in Geography)


def _canonicalize_geo(raw: str) -> str:
    """
    Convert raw geography text to a canonical Geography value.

    Handles both human-readable strings ("Singapore", "North America") and
    already-canonical values ("north_america", "southeast_asia") so the scorer
    is idempotent when called with pre-normalized DB/target values.
    """
    if not raw:
        return Geography.UNKNOWN
    low = raw.strip().lower()
    # Already canonical (e.g. stored as "north_america" by build_similarity_target)
    if low in _CANONICAL_GEOS:
        return low
    return normalize_geography(raw)


def _geo_score(target_geo: str, candidate_geo: str) -> int:
    """Return 0–25 geography similarity points."""
    if not candidate_geo or candidate_geo == Geography.UNKNOWN:
        return 0
    cg = _canonicalize_geo(candidate_geo)
    tg = target_geo

    if tg == Geography.UNKNOWN:
        return 0
    if cg == tg:
        return 25
    # Candidate is Global → some credit
    if cg == Geography.GLOBAL:
        return 10
    # Target or candidate is one of the Contra ICP regions → adjacency credit
    adj = _GEO_ADJACENT.get(tg, set())
    if cg in adj:
        return 15
    # Both within Contra ICP → small credit even without direct adjacency
    if tg in _CONTRA_ICP_GEOS and cg in _CONTRA_ICP_GEOS:
        return 8
    return 0


# ---------------------------------------------------------------------------
# Main scorer
# ---------------------------------------------------------------------------

def score_lp_similarity(
    target: LpSimilarityTarget,
    candidate: Dict[str, Any],
) -> SimilarityResult:
    """
    Score a candidate LP against the target profile (0–100).

    Dimension weights (total 100):
      geography          25
      allocator_type     20
      em_appetite        20
      ai_appetite        15
      fund_thesis_overlap 10
      archetype          10
    """
    match_dims: List[str] = []
    score = 0

    # 1. Geography (25)
    geo_pts = _geo_score(target.geography, candidate.get("geography", ""))
    score += geo_pts
    if geo_pts >= 15:
        match_dims.append("geography")

    # 2. Allocator type (20)
    tg = _type_group(target.allocator_type)
    cg = _type_group(candidate.get("allocator_type", ""))
    if tg == cg and tg != "other":
        score += 20
        match_dims.append("type")
    elif tg != "other" and cg != "other":
        # Different groups but both known → small partial
        score += 5

    # 3. EM appetite (20)
    em_pts = _appetite_score(target.em_appetite, candidate.get("em_appetite", "unknown"), 20)
    score += em_pts
    if em_pts >= 14:
        match_dims.append("em_appetite")

    # 4. AI/tech appetite (15)
    ai_pts = _appetite_score(target.ai_appetite, candidate.get("ai_appetite", "unknown"), 15)
    score += ai_pts
    if ai_pts >= 10:
        match_dims.append("ai_appetite")

    # 5. Fund thesis overlap (10) — Jaccard on geography_focus sets
    c_fund_geos: Set[str] = set()
    raw_fg = candidate.get("fund_focus_geos")
    if isinstance(raw_fg, set):
        c_fund_geos = raw_fg
    elif isinstance(raw_fg, (list, str)):
        for g in (raw_fg if isinstance(raw_fg, list) else [raw_fg]):
            n = normalize_geography(str(g))
            if n != Geography.UNKNOWN:
                c_fund_geos.add(n)

    if target.fund_focus_geos and c_fund_geos:
        intersection = target.fund_focus_geos & c_fund_geos
        union = target.fund_focus_geos | c_fund_geos
        jaccard = len(intersection) / len(union) if union else 0.0
        thesis_pts = round(jaccard * 10)
        score += thesis_pts
        if thesis_pts >= 7:
            match_dims.append("fund_thesis")
    elif not target.fund_focus_geos and not c_fund_geos:
        # Both unknown → neutral half credit
        score += 5

    # 6. Behavioral archetype (10)
    c_arch = candidate.get("archetype", "unknown")
    if not c_arch or c_arch == "unknown":
        c_arch = infer_db_archetype(
            candidate.get("allocator_type"),
            candidate.get("geography"),
            candidate.get("em_appetite"),
            int(candidate.get("fund_deal_count", 0)),
        )
    if target.archetype != "unknown" and c_arch != "unknown":
        if target.archetype == c_arch:
            score += 10
            match_dims.append("archetype")
        elif _archetypes_related(target.archetype, c_arch):
            score += 5
    else:
        score += 5  # both unknown → neutral

    return SimilarityResult(
        score=min(score, 100),
        match_dimensions=match_dims,
        archetype=c_arch,
    )


def _archetypes_related(a: str, b: str) -> bool:
    """True when two archetypes are close enough for partial credit."""
    _RELATED: List[Set[str]] = [
        {"family_office", "generalist"},
        {"fund_of_funds", "emerging_manager_specialist"},
        {"asia_specialist", "institutional_lp"},
        {"institutional_lp", "generalist"},
    ]
    for group in _RELATED:
        if a in group and b in group:
            return True
    return False


# ---------------------------------------------------------------------------
# Post-LLM archetype fit summary
# ---------------------------------------------------------------------------

def compute_archetype_fit(
    target_archetype: str,
    matches: List[Dict[str, Any]],
) -> ArchetypeFit:
    """
    Summarise how well a screened LP's archetype fits the anchor set.

    matches: the final list of similar_confirmed_lps dicts (with similarity_score).
    """
    if not matches:
        return ArchetypeFit(fit_level="none", avg_similarity_score=0, rationale="No comparable LP anchors found in database.")

    scores = [int(m.get("similarity_score", 0)) for m in matches]
    avg = round(sum(scores) / len(scores))
    qualifying = [m for m in matches if m.get("similarity_score", 0) >= MIN_SIGNAL_SCORE]

    if target_archetype == "unknown":
        fit_level = "partial" if qualifying else "weak"
        rationale = (
            f"Archetype not yet determined; {len(qualifying)} of {len(matches)} anchor(s) "
            f"score ≥{MIN_SIGNAL_SCORE}. Average similarity: {avg}."
        )
        return ArchetypeFit(fit_level=fit_level, avg_similarity_score=avg, rationale=rationale)

    archetype_matches = [m for m in qualifying if m.get("archetype") == target_archetype]
    best = sorted(matches, key=lambda m: -m.get("similarity_score", 0))

    if len(qualifying) >= 2 and len(archetype_matches) >= 1:
        fit_level = "strong"
        top_names = ", ".join(m["name"] for m in best[:2])
        rationale = (
            f"{len(qualifying)} anchor(s) at or above threshold — "
            f"{len(archetype_matches)} share the {target_archetype.replace('_', ' ')} archetype. "
            f"Strongest: {top_names} (avg score {avg})."
        )
    elif len(qualifying) >= 1:
        fit_level = "partial"
        top_names = ", ".join(m["name"] for m in best[:2])
        rationale = (
            f"{len(qualifying)} qualifying anchor(s) but archetype overlap partial. "
            f"Closest: {top_names} (avg score {avg})."
        )
    else:
        fit_level = "weak"
        top_name = best[0]["name"] if best else "none"
        rationale = (
            f"No anchors reach the {MIN_SIGNAL_SCORE}-point threshold. "
            f"Best match: {top_name} ({scores[0] if scores else 0}). "
            "Archetype calibration may be inconclusive."
        )

    return ArchetypeFit(fit_level=fit_level, avg_similarity_score=avg, rationale=rationale)

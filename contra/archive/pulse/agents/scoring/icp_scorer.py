"""
ICP Scorer v4.1 — scores every allocator against the full LP Scoping spec.

Sources used:
  - MyAsiaVC LP Scoping.xlsx: Core Filters (C1–C4), Exclusion Rules (E1–E12),
    Soft Signals (S1–S7), LP Type Priority tiers
  - ICP 4.0 Prospect List: scored AQVC rows as ground truth

Key improvements over v4.0:
  - Emerging Manager Appetite promoted to core filter C2
  - LP type scoring now uses the scoping doc's priority tiers
    (FoF/MFO = top tier; Pension/Endowment = de-prioritize)
  - Decision speed added as soft signal S4 (first-close urgency)
  - All 12 exclusion rules from scoping doc implemented
  - Proxy fund overlap signal added (S7)
  - Tier thresholds recalibrated

Idempotent: clears icp_scores for this ICP_VERSION before writing.
"""

from __future__ import annotations

import json
import re
import uuid
from typing import Any, Dict, List, Optional, Tuple

# Anonymized placeholder names ("Name A", "Investor B", "LP 1") must be skipped
# so they don't create junk allocator rows or inflate unmatched_rows counts.
_ANON_RE = re.compile(
    r'^(name|investor|lp|entity|company|contact|prospect)\s+[a-z0-9]{1,3}$',
    re.IGNORECASE,
)

from agents.scoring.icp_spec import (
    ICP_VERSION,
    # Core filters
    C1_KEYWORDS, C1_REQUIRED_ANY,
    C2_EMERGING_MANAGER_POSITIVE,
    C3_SECTORS,
    C4_REGIONS,
    # Exclusions
    ALL_HARD_EXCLUSION_PHRASES,
    SANCTIONED_COUNTRIES,
    CLIENT_STATUS_BLACKLIST, CLIENT_STATUS_CONFLICT,
    E9_OVERLARGE_PHRASES,
    # Soft signals
    S1_AI_PHRASES, S1_WEIGHT,
    S2_EM_PHRASES, S2_WEIGHT,
    LP_TYPE_PRIORITY,
    S3_WEIGHT, S4_WEIGHT,
    S5_STAGE_PHRASES, S5_WEIGHT,
    S6_CONFLICT_PHRASES, S6_WEIGHT,
    S7_PROXY_FUNDS, S7_WEIGHT,
    get_tier_thresholds,
    # Column constants
    PROSPECTS_HEADER_ROW,
    COL_NAME, COL_COUNTRY, COL_SCORING,
    COL_CLIENT_STATUS, COL_CLIENT_COMMENTS, COL_MINER_COMMENTS,
)


# ---------------------------------------------------------------------------
# Name resolution
# ---------------------------------------------------------------------------

def _build_name_index(con) -> Dict[str, str]:
    index: Dict[str, str] = {}
    for alias_text, canonical_id in con.execute(
        "SELECT alias_text, canonical_id FROM entity_aliases WHERE entity_type = 'allocator'"
    ).fetchall():
        if alias_text:
            index[alias_text.lower().strip()] = canonical_id
    for allocator_id, canonical_name in con.execute(
        "SELECT CAST(allocator_id AS VARCHAR), canonical_name FROM allocators"
    ).fetchall():
        if canonical_name:
            index[canonical_name.lower().strip()] = allocator_id
    return index


def _resolve_name(name: str, index: Dict[str, str]) -> Optional[str]:
    if not name:
        return None
    key = name.lower().strip()
    if key in index:
        return index[key]
    cleaned = re.sub(r'\s+(ltd|limited|llc|inc|corp|plc|pte|sa|bv|gmbh)\.?$', '', key, flags=re.I).strip()
    return index.get(cleaned)


# ---------------------------------------------------------------------------
# Core filter scoring (C1–C4)
# ---------------------------------------------------------------------------

def _score_c1_vc_fund(text: str) -> Tuple[bool, str]:
    """C1: LP must invest in VC funds as a primary commitment."""
    t = text.lower()
    hits = [kw for kw in C1_KEYWORDS if kw in t]
    has_required = any(kw in t for kw in C1_REQUIRED_ANY)
    if hits and has_required:
        return True, f"VC fund evidence: {', '.join(hits[:4])}"
    return False, "No explicit VC fund investment evidence in scoring text"


def _score_c2_emerging_manager(text: str, client_comments: str) -> Tuple[bool, str]:
    """C2: Emerging manager appetite — positive signals anywhere in text."""
    combined = (text + " " + client_comments).lower()
    hits = [p for p in C2_EMERGING_MANAGER_POSITIVE if p in combined]
    if hits:
        return True, f"Emerging manager signals: {', '.join(hits[:3])}"
    return False, "No emerging manager evidence in scoring text or client comments"


def _score_c3_ai_tech(text: str) -> Tuple[bool, str]:
    """C3: AI/tech thesis alignment."""
    t = text.lower()
    hits = [s for s in C3_SECTORS if s in t]
    if hits:
        return True, f"AI/tech: {', '.join(hits[:5])}"
    return False, "No AI/tech sector signals in scoring text"


def _score_c4_geography(text: str) -> Tuple[bool, str]:
    """C4: Geographic fit — Asia, North America, or Middle East."""
    t = text.lower()
    hits = [r for r in C4_REGIONS if r in t]
    if hits:
        return True, f"Regions: {', '.join(hits[:4])}"
    return False, "No qualifying region (Asia/NA/ME) in scoring text"


# ---------------------------------------------------------------------------
# Exclusion scoring
# ---------------------------------------------------------------------------

def _score_exclusions(
    client_status: str,
    scoring_text: str,
    client_comments: str,
    country: str,
) -> Tuple[bool, Optional[str]]:
    """
    Check all 12 exclusion rules.
    Returns (excluded, reason_string).
    """
    cs = (client_status or "").lower()

    # E11: Blacklist
    if CLIENT_STATUS_BLACKLIST in cs:
        return True, "E11: Blacklisted by client"

    # E11: Stated IIP conflict
    if CLIENT_STATUS_CONFLICT in cs:
        return True, "E11: Client stated conflict with IIP"

    # E6 / Sanctioned country
    c = (country or "").lower()
    for sc in SANCTIONED_COUNTRIES:
        if sc in c:
            return True, f"E6 (C6): Sanctioned jurisdiction: {country}"

    # Scan combined text for hard exclusion phrases
    combined = (scoring_text + " " + client_comments).lower()

    # E9: Check size mismatch
    for phrase in E9_OVERLARGE_PHRASES:
        if phrase in combined:
            return True, f"E9: Check size mismatch — '{phrase}'"

    # All other hard exclusion phrases (E1–E8, E10, E12)
    for phrase in ALL_HARD_EXCLUSION_PHRASES:
        if phrase in combined:
            return True, f"Hard exclusion pattern: '{phrase}'"

    return False, None


# ---------------------------------------------------------------------------
# Soft signal scoring (S1–S7)
# ---------------------------------------------------------------------------

def _score_s1_ai_signal(text: str) -> float:
    """S1: AI investment signal (portfolio mention > thesis mention > generalism)."""
    t = text.lower()
    # Count direct portfolio company mentions (strong signal)
    portfolio_hits = sum(1 for p in ["openai", "anthropic", "cohere", "xai", "gemini"] if p in t)
    # Count thesis/keyword mentions
    thesis_hits = sum(1 for p in S1_AI_PHRASES if p in t)

    if portfolio_hits >= 2:
        return 1.0
    if portfolio_hits == 1:
        return 0.90
    if thesis_hits >= 4:
        return 0.85
    if thesis_hits >= 2:
        return 0.70
    if thesis_hits == 1:
        return 0.50
    return 0.0


def _score_s2_emerging_manager(text: str, client_comments: str) -> float:
    """S2: Emerging manager depth (quality of EM appetite signal)."""
    combined = (text + " " + client_comments).lower()
    # High confidence: explicit EM program or multiple fund I/II mentions
    high_confidence = [
        "emerging manager program", "dedicated emerging", "ilp program",
        "backs emerging", "fund i and fund ii", "first-time fund",
    ]
    medium_confidence = [
        "emerging manager", "emerging managers", "fund i", "fund 1",
        "fund ii", "fund 2", "emerging fund", "new manager",
    ]
    high_hits = sum(1 for p in high_confidence if p in combined)
    med_hits  = sum(1 for p in medium_confidence if p in combined)

    if high_hits >= 1:
        return 1.0
    if med_hits >= 3:
        return 0.85
    if med_hits >= 2:
        return 0.70
    if med_hits == 1:
        return 0.55
    return 0.20  # no signal but not denied — neutral-low


def _score_s3_lp_type(allocator_type: str) -> float:
    """S3: LP type priority score from the scoping doc tier table."""
    entry = LP_TYPE_PRIORITY.get(allocator_type or "unknown", LP_TYPE_PRIORITY["unknown"])
    return entry[1]  # lp_type_score


def _score_s4_decision_speed(allocator_type: str) -> float:
    """S4: Decision speed (urgency for first close). Fastest = 1.0."""
    entry = LP_TYPE_PRIORITY.get(allocator_type or "unknown", LP_TYPE_PRIORITY["unknown"])
    return entry[2]  # decision_speed_score


def _score_s5_stage(text: str) -> float:
    """S5: Stage alignment (PreSeed/Seed/Series A focus)."""
    t = text.lower()
    hits = sum(1 for p in S5_STAGE_PHRASES if p in t)
    if hits >= 3:
        return 1.0
    if hits == 2:
        return 0.75
    if hits == 1:
        return 0.50
    return 0.10


def _score_s6_clean_profile(text: str, client_comments: str) -> float:
    """S6: Absence of conflict/mismatch signals. Clean = 1.0."""
    combined = (text + " " + client_comments).lower()
    hits = [p for p in S6_CONFLICT_PHRASES if p in combined]
    if not hits:
        return 1.0
    if len(hits) == 1:
        return 0.50
    return 0.10


def _score_s7_proxy_fund(text: str) -> float:
    """S7: Portfolio overlap with MyAsiaVC proxy funds."""
    t = text.lower()
    hits = [f for f in S7_PROXY_FUNDS if f in t]
    if len(hits) >= 2:
        return 1.0
    if len(hits) == 1:
        return 0.70
    return 0.0


def _compute_fit_score(
    s1: float, s2: float, s3: float, s4: float,
    s5: float, s6: float, s7: float,
) -> float:
    return round(
        s1 * S1_WEIGHT
        + s2 * S2_WEIGHT
        + s3 * S3_WEIGHT
        + s4 * S4_WEIGHT
        + s5 * S5_WEIGHT
        + s6 * S6_WEIGHT
        + s7 * S7_WEIGHT,
        4,
    )


def _compute_tier(core_pass: bool, excluded: bool, fit_score: float, client_decision: str) -> str:
    tier_1_min, tier_2_min = get_tier_thresholds()
    if excluded or not core_pass:
        return "tier_4"
    if fit_score >= tier_1_min and client_decision == "approved":
        return "tier_1"
    if fit_score >= tier_1_min:
        return "tier_2"  # strong fit but not yet approved
    if fit_score >= tier_2_min:
        return "tier_2"
    return "tier_3"


def _parse_client_decision(client_status: str) -> str:
    s = (client_status or "").lower()
    if "blacklist" in s or "conflict" in s:
        return "rejected"
    if "don't campaign" in s or "dont campaign" in s:
        return "approved_no_campaign"
    if "approved" in s:
        return "approved"
    return "pending"


# ---------------------------------------------------------------------------
# Main scoring entry point
# ---------------------------------------------------------------------------

def run_icp_scoring(con) -> Dict[str, int]:
    """
    Score all allocators against ICP v4.1.
    Idempotent: deletes existing icp_scores for this version before writing.
    Returns: {scored, unmatched_rows, tier_1..4}.
    """
    name_index = _build_name_index(con)

    rows = con.execute(
        """
        SELECT source_record_id, source_file, raw_content
        FROM entities_raw
        WHERE source_type = 'xlsx'
          AND (
            json_extract_string(raw_content, '$._sheet') LIKE 'Prospects_m%'
            OR json_extract_string(raw_content, '$._sheet') = 'Prospects_Hong Kong'
            OR json_extract_string(raw_content, '$._sheet') = 'Prospects_London'
          )
          AND CAST(json_extract_string(raw_content, '$._row_number') AS INTEGER) > ?
          AND json_extract_string(raw_content, '$."Unnamed: 1"') IS NOT NULL
          AND json_extract_string(raw_content, '$."Unnamed: 1"') != ''
        """,
        [PROSPECTS_HEADER_ROW],
    ).fetchall()

    alloc_meta: Dict[str, Dict[str, Any]] = {}
    for aid, atype, geo, hq in con.execute(
        "SELECT CAST(allocator_id AS VARCHAR), allocator_type, geography, hq_country FROM allocators"
    ).fetchall():
        alloc_meta[aid] = {
            "allocator_type": atype or "unknown",
            "geography": geo or "unknown",
            "hq_country": hq or "",
        }

    con.execute("DELETE FROM icp_scores WHERE icp_version = ?", [ICP_VERSION])

    scored_ids: set[str] = set()
    batch: List[tuple] = []
    unmatched = 0

    for source_record_id, source_file, raw_content in rows:
        if isinstance(raw_content, str):
            raw_content = json.loads(raw_content)

        investor_name = (raw_content.get(COL_NAME) or "").strip()
        if not investor_name:
            continue

        # Skip anonymized placeholders ("Name A", "Investor B", "LP 1" etc.)
        if _ANON_RE.match(investor_name):
            continue

        allocator_id = _resolve_name(investor_name, name_index)
        if not allocator_id:
            unmatched += 1
            continue

        if allocator_id in scored_ids:
            continue
        scored_ids.add(allocator_id)

        sheet       = raw_content.get("_sheet", "")
        row_number  = raw_content.get("_row_number", 0)
        scoring     = str(raw_content.get(COL_SCORING) or "")
        client_st   = str(raw_content.get(COL_CLIENT_STATUS) or "")
        comments    = str(raw_content.get(COL_CLIENT_COMMENTS) or "")
        miner       = str(raw_content.get(COL_MINER_COMMENTS) or "")
        country     = str(raw_content.get(COL_COUNTRY) or "")

        meta         = alloc_meta.get(allocator_id, {})
        alloc_type   = meta.get("allocator_type", "unknown")
        geography    = meta.get("geography", "unknown")
        hq_country   = meta.get("hq_country", "")

        # --- Core filters ---
        c1, c1_ev = _score_c1_vc_fund(scoring)
        c2, c2_ev = _score_c2_emerging_manager(scoring, comments)
        c3, c3_ev = _score_c3_ai_tech(scoring)
        c4, c4_ev = _score_c4_geography(scoring)
        core_pass = c1 and c2 and c3 and c4

        # --- Exclusions ---
        excluded, excl_reason = _score_exclusions(client_st, scoring, comments, country)

        # --- Soft signals ---
        s1 = _score_s1_ai_signal(scoring)
        s2 = _score_s2_emerging_manager(scoring, comments)
        s3 = _score_s3_lp_type(alloc_type)
        s4 = _score_s4_decision_speed(alloc_type)
        s5 = _score_s5_stage(scoring)
        s6 = _score_s6_clean_profile(scoring, comments)
        s7 = _score_s7_proxy_fund(scoring)

        fit_score       = _compute_fit_score(s1, s2, s3, s4, s5, s6, s7)
        client_decision = _parse_client_decision(client_st)
        tier            = _compute_tier(core_pass, excluded, fit_score, client_decision)

        stated_reason = comments[:500] if comments else None
        miner_note    = miner[:500] if miner else None

        batch.append((
            str(uuid.uuid4()),
            allocator_id,
            ICP_VERSION,
            c1, c1_ev[:300],
            c2, c2_ev[:300],
            c3, c3_ev[:300],
            c4, c4_ev[:300],
            core_pass,
            excluded, excl_reason,
            s1, s2, s3, s4, s5, s6, s7,
            fit_score, tier,
            client_st[:200],
            client_decision,
            stated_reason,
            miner_note,
            sheet,
            int(row_number),
            source_file,
        ))

    if batch:
        con.executemany(
            """
            INSERT INTO icp_scores (
                score_id, allocator_id, icp_version,
                c1_asset_class_pass, c1_evidence,
                c2_emerging_manager_pass, c2_evidence,
                c3_ai_tech_pass, c3_evidence,
                c4_geography_pass, c4_evidence,
                core_pass,
                excluded, exclusion_reason,
                s1_ai_signal, s2_emerging_manager, s3_lp_type,
                s4_decision_speed, s5_stage, s6_clean_profile, s7_proxy_fund,
                fit_score, tier,
                client_status, client_decision,
                stated_reason, data_miner_comment,
                source_sheet, source_row, source_file
            ) VALUES (
                ?,?,?,
                ?,?,
                ?,?,
                ?,?,
                ?,?,
                ?,
                ?,?,
                ?,?,?,?,?,?,?,
                ?,?,
                ?,?,
                ?,?,
                ?,?,?
            )
            """,
            batch,
        )

    tier_counts = {"tier_1": 0, "tier_2": 0, "tier_3": 0, "tier_4": 0}
    for row in batch:
        t = row[22]  # tier column index
        if t in tier_counts:
            tier_counts[t] += 1

    return {
        "scored": len(batch),
        "scored_xlsx": len(batch),
        "unmatched_rows": unmatched,
        **tier_counts,
    }

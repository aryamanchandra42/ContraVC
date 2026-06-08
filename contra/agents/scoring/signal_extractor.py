"""
Signal Extractor — populates the `signals` table for every allocator.

Derives 6 signal types from prospect sheet rows via signal_evidence (derive-only uncertainty).

Idempotent: cascade-deletes prior prospect signals + evidence, then rewrites.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from typing import Any, Dict, List, Optional, Tuple

from agents.scoring.icp_spec import (
    LP_TYPE_PRIORITY,
    S2_EM_PHRASES,
    PROSPECTS_HEADER_ROW,
    COL_NAME, COL_SCORING, COL_CLIENT_STATUS, COL_QA_STATUS,
    COL_DATA_SOURCES, COL_CLIENT_COMMENTS,
    COL_WEBSITE, COL_EMAIL, COL_LINKEDIN, COL_CONTACT_TITLE, COL_CONTACT_NAME,
)
from agents.scoring.signal_evidence_writer import (
    delete_signals_cascade,
    insert_signals_batch,
    make_evidence_row,
)

# Titles that suggest operator / founder / tech background
_OPERATOR_TITLE_PATTERNS = [
    "cio", "cto", "coo", "ceo", "founder", "co-founder", "managing partner",
    "partner", "principal", "managing director", "director", "chief investment",
    "chief executive", "managing", "entrepreneur", "exited",
    "investment director", "vp investments", "head of investments",
]

# Titles that are admin / IR / support — reduce operator signal
_NON_OPERATOR_TITLES = [
    "assistant", "associate", "analyst", "admin", "coordinator", "secretary",
    "ir", "investor relations", "operations", "compliance",
]

# Geography → overlap score for our fund's target regions
_GEO_OVERLAP: Dict[str, float] = {
    "southeast_asia":   1.0,
    "south_asia":       1.0,
    "east_asia":        1.0,
    "asia_pacific":     1.0,
    "middle_east":      1.0,
    "north_america":    0.90,
    "global":           0.75,
    "emerging_markets": 0.70,
    "europe":           0.55,
    "latin_america":    0.40,
    "africa":           0.40,
    "unknown":          0.40,
}

# Keywords in scoring text indicating recent deployment activity
_ACTIVE_DEPLOYMENT_PHRASES = [
    "actively investing", "actively deploying", "currently deploying",
    "recently invested", "new fund close", "recently closed",
    "actively seeking", "looking to invest", "open to new", "actively looking",
    "deploying capital", "fund is open", "new commitments",
    "fresh capital", "latest vintage", "recently launched",
]

_INACTIVE_PHRASES = [
    "not currently investing", "paused", "fund closed", "fully deployed",
    "no new commitments", "hiatus", "wind down",
]


def _stable_hash(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:32]


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
# Individual signal scorers — return (normalized_value, evidence_strength)
# ---------------------------------------------------------------------------

def _signal_em_participation(scoring: str, comments: str, em_score_from_icp: float) -> Tuple[float, float]:
    combined = (scoring + " " + comments).lower()
    hits = sum(1 for p in S2_EM_PHRASES if p in combined)

    if hits >= 3:
        return 1.0, 0.90
    if hits == 2:
        return 0.85, 0.80
    if hits == 1:
        return 0.60, 0.70
    if em_score_from_icp > 0.5:
        return em_score_from_icp, 0.55
    return 0.20, 0.50


def _signal_geography_overlap(geography: str, hq_country: str) -> Tuple[float, float]:
    score = _GEO_OVERLAP.get(geography or "unknown", 0.40)
    confidence = 0.85 if geography and geography != "unknown" else 0.50
    return score, confidence


def _signal_deployment_velocity(
    client_status: str, qa_status: str, scoring: str, client_comments: str
) -> Tuple[float, float]:
    cs = (client_status or "").lower()
    qs = (qa_status or "").lower()
    combined = (scoring + " " + client_comments).lower()

    if "approved - campaign" in cs:
        base = 0.80
    elif "approved" in cs:
        base = 0.60
    elif "rejected" in cs:
        base = 0.20
    else:
        base = 0.40

    if "validated" in qs:
        base = min(base + 0.10, 1.0)

    active_hits = sum(1 for p in _ACTIVE_DEPLOYMENT_PHRASES if p in combined)
    inactive_hits = sum(1 for p in _INACTIVE_PHRASES if p in combined)

    if active_hits >= 2:
        base = min(base + 0.15, 1.0)
    elif active_hits == 1:
        base = min(base + 0.08, 1.0)
    if inactive_hits >= 1:
        base = max(base - 0.20, 0.0)

    confidence = 0.75 if "validated" in qs else 0.60
    return round(base, 3), confidence


def _signal_exploratory_check(
    qa_status: str, email: str, linkedin: str, website: str, data_sources: str
) -> Tuple[float, float]:
    score = 0.0
    if (qa_status or "").lower() == "validated":
        score += 0.40
    if email and "@" in email and email not in ("", "nan"):
        score += 0.30
    if linkedin and "linkedin.com" in linkedin.lower():
        score += 0.15
    if website and ("http" in website.lower() or "www" in website.lower()):
        score += 0.10
    if data_sources:
        sources = [s.strip() for s in data_sources.split(",")]
        if len(sources) >= 3:
            score += 0.05

    return round(min(score, 1.0), 3), 0.80


def _signal_response_speed(allocator_type: str) -> Tuple[float, float]:
    entry = LP_TYPE_PRIORITY.get(allocator_type or "unknown", LP_TYPE_PRIORITY["unknown"])
    speed = entry[2]
    confidence = 0.85 if allocator_type and allocator_type != "unknown" else 0.50
    return speed, confidence


def _signal_operator_background(contact_title: str, contact_name: str) -> Tuple[float, float]:
    title_lower = (contact_title or "").lower()
    if not title_lower:
        return 0.30, 0.40

    if any(p in title_lower for p in _NON_OPERATOR_TITLES):
        return 0.20, 0.70

    if any(p in title_lower for p in ["ceo", "cio", "cto", "founder", "co-founder", "managing partner"]):
        return 1.0, 0.85
    if any(p in title_lower for p in ["managing director", "chief investment", "chief executive"]):
        return 0.85, 0.80
    if any(p in title_lower for p in ["partner", "principal", "director", "head of"]):
        return 0.70, 0.75

    return 0.50, 0.60


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def run_signal_extraction(con) -> Dict[str, int]:
    """
    Extract signals for institutional prospects from Prospect sheet rows.
    Idempotent: cascade-deletes prior prospect signals + evidence.
    """
    name_index = _build_name_index(con)

    alloc_meta: Dict[str, Dict[str, Any]] = {}
    for aid, atype, geo, hq in con.execute(
        "SELECT CAST(allocator_id AS VARCHAR), allocator_type, geography, hq_country FROM allocators"
    ).fetchall():
        alloc_meta[aid] = {"allocator_type": atype or "unknown", "geography": geo or "unknown", "hq_country": hq or ""}

    icp_scores: Dict[str, Dict[str, Any]] = {}
    for aid, s2, s4, fit in con.execute(
        "SELECT CAST(allocator_id AS VARCHAR), s2_emerging_manager, s4_decision_speed, fit_score "
        "FROM icp_scores WHERE icp_version = '4.1'"
    ).fetchall():
        icp_scores[aid] = {"s2_em": s2 or 0.20, "s4_speed": s4 or 0.40, "fit_score": fit or 0.0}

    delete_signals_cascade(
        con,
        "source_file LIKE '%ICP%' OR source_file LIKE '%Prospect%'",
    )

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

    counts = {
        "em_participation": 0, "geography_overlap": 0, "deployment_velocity": 0,
        "exploratory_check": 0, "response_speed": 0, "operator_background": 0,
    }
    seen_ids: set[str] = set()
    signal_rows: List[Tuple] = []
    evidence_rows: List[Tuple] = []

    for src_id, src_file, raw_content in rows:
        if isinstance(raw_content, str):
            raw_content = json.loads(raw_content)

        investor_name = (raw_content.get(COL_NAME) or "").strip()
        if not investor_name:
            continue

        allocator_id = _resolve_name(investor_name, name_index)
        if not allocator_id or allocator_id in seen_ids:
            continue
        seen_ids.add(allocator_id)

        scoring       = str(raw_content.get(COL_SCORING) or "")
        client_status = str(raw_content.get(COL_CLIENT_STATUS) or "")
        client_cmts   = str(raw_content.get(COL_CLIENT_COMMENTS) or "")
        qa_status     = str(raw_content.get(COL_QA_STATUS) or "")
        data_sources  = str(raw_content.get(COL_DATA_SOURCES) or "")
        email         = str(raw_content.get(COL_EMAIL) or "")
        linkedin      = str(raw_content.get(COL_LINKEDIN) or "")
        website       = str(raw_content.get(COL_WEBSITE) or "")
        contact_title = str(raw_content.get(COL_CONTACT_TITLE) or "")
        contact_name  = str(raw_content.get(COL_CONTACT_NAME) or "")
        row_offset    = f"row:{raw_content.get('_row_number', '')}"

        meta      = alloc_meta.get(allocator_id, {})
        atype     = meta.get("allocator_type", "unknown")
        geography = meta.get("geography", "unknown")
        icp       = icp_scores.get(allocator_id, {})
        em_icp    = icp.get("s2_em", 0.20)

        signal_defs = [
            ("em_participation",    *_signal_em_participation(scoring, client_cmts, em_icp)),
            ("geography_overlap",   *_signal_geography_overlap(geography, meta.get("hq_country", ""))),
            ("deployment_velocity", *_signal_deployment_velocity(client_status, qa_status, scoring, client_cmts)),
            ("exploratory_check",   *_signal_exploratory_check(qa_status, email, linkedin, website, data_sources)),
            ("response_speed",      *_signal_response_speed(atype)),
            ("operator_background", *_signal_operator_background(contact_title, contact_name)),
        ]

        for sig_type, norm_val, strength in signal_defs:
            sig_id = str(uuid.uuid4())
            signal_rows.append((
                sig_id, allocator_id, sig_type, None, norm_val,
                src_id, src_file, _stable_hash(allocator_id, sig_type, src_id),
            ))
            evidence_rows.append(make_evidence_row(
                sig_id, src_id, "signal_heuristic", strength, src_file,
                notes=f"normalized={norm_val:.3f}",
                source_offset=row_offset,
            ))
            counts[sig_type] += 1

    insert_signals_batch(con, signal_rows, evidence_rows)
    return counts

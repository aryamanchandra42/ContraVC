"""
Rejection Extractor — populates the `rejections` table from Prospect sheet data.

Reads all Prospect sheet rows in entities_raw.
Identifies rows where the client has stated a rejection decision.
Maps rejection type:
  - "Rejected - Blacklist"         → rejection_type='structural'
  - "Rejected - Seems to conflict" → rejection_type='stated'
  - Hard structural exclusions     → rejection_type='structural'

Also reads the scoring text for inferred rejection signals even for approved LPs
(e.g., mentions of constraint language → rejection_type='inferred').

Idempotent: clears existing rejections sourced from Prospect sheets before re-running.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from typing import Any, Dict, List, Optional, Tuple

from agents.scoring.icp_spec import (
    CLIENT_STATUS_BLACKLIST as E1_BLACKLIST_STATUS,
    CLIENT_STATUS_CONFLICT  as E2_CONFLICT_STATUS,
    ALL_HARD_EXCLUSION_PHRASES as HARD_EXCLUSION_PHRASES,
    COL_NAME, COL_SCORING, COL_CLIENT_STATUS, COL_CLIENT_COMMENTS, COL_MINER_COMMENTS,
    PROSPECTS_HEADER_ROW,
)


# Rejection reason tag → pattern mapping for inferred rejections
_INFERRED_REASON_TAGS: Dict[str, List[str]] = {
    "geography_constraint": [
        "does not invest in asia", "no asia focus", "us only", "us-only",
        "north america only", "europe only", "domestic only",
    ],
    "mandate_mismatch": [
        "does not invest in funds", "direct investments only", "no fund investments",
        "exclusively direct", "no venture", "not investing in vc",
        "public markets only", "fixed income only", "real estate only",
        "direct investments vs fund", "direct investment focus",
        "vc secondaries", "secondaries only", "secondary focus",
        "pe focus", "private equity focus", "pe only",
        "fund of one", "not a fit",
    ],
    "sector_mismatch": [
        "blockchain focus", "web3 focus", "crypto focus", "nft focus",
        "healthcare only", "biotech only", "climate only",
        "real estate focus", "real estate only",
        "climate focus", "healthcare focus", "life sciences focus",
        "lifesciences focus", "life science focus",
        "energy focus", "cleantech focus", "infrastructure focus",
        "consumer focus", "fintech only", "edtech only",
    ],
    "size_constraint": [
        "ticket too small", "minimum ticket", "below minimum",
        "check size", "fund size too small", "fund too small",
        "write larger checks", "larger checks", "larger ticket",
        "we do not fit bucket", "not fit bucket",
        "minimum check", "too small for them",
    ],
    "timing_constraint": [
        "not currently investing", "fundraising paused", "fund closed",
        "deployed capital", "no new commitments", "currently deployed",
        "currently raising", "already committed",
    ],
    "committee_constraint": [
        "committee approval", "board approval", "investment committee",
        "long approval", "takes 12", "takes 18", "takes 24",
        "emerging managers", "no emerging managers", "not emerging managers",
        "established track record", "proven track record required",
    ],
    "ai_conflict": [
        "steering clear of ai", "avoiding ai", "avoid artificial intelligence",
        "not investing in ai", "no ai",
    ],
    "asset_manager_conflict": [
        "asset manager", "fund of funds only", "institutional only",
        "no direct lp", "not taking direct lp",
    ],
}

# Future conversion probability estimates by rejection type + reason tag
_CONVERSION_PROB: Dict[str, float] = {
    "stated":      0.15,  # client explicitly rejected; might revisit
    "inferred":    0.25,  # soft signal; might still work
    "structural":  0.02,  # hard exclusion; very unlikely
}
_REASON_OVERRIDE_PROB: Dict[str, float] = {
    "timing_constraint":  0.35,  # temporal — can re-engage later
    "size_constraint":    0.20,  # might become viable as fund grows
    "committee_constraint": 0.20,
    "geography_constraint": 0.08,
    "mandate_mismatch":   0.04,
    "ai_conflict":        0.05,
    "sector_mismatch":    0.12,
}


def _extract_reason_tags(text: str) -> List[str]:
    t = text.lower()
    tags = []
    for tag, patterns in _INFERRED_REASON_TAGS.items():
        if any(p in t for p in patterns):
            tags.append(tag)
    return tags


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


def run_rejection_extraction(con) -> Dict[str, int]:
    """
    Extract rejections from Prospect sheets into the rejections table.

    Returns counts: {stated, inferred, structural, skipped_unmatched}.
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

    # Clear existing Prospect-sourced rejections (idempotent)
    con.execute(
        "DELETE FROM rejections WHERE source_file LIKE '%ICP%' OR source_file LIKE '%Prospect%'"
    )

    counts = {"stated": 0, "inferred": 0, "structural": 0, "skipped_unmatched": 0}
    batch: List[tuple] = []
    seen_ids: set[str] = set()

    for source_record_id, source_file, raw_content in rows:
        if isinstance(raw_content, str):
            raw_content = json.loads(raw_content)

        investor_name = (raw_content.get(COL_NAME) or "").strip()
        if not investor_name:
            continue

        allocator_id = _resolve_name(investor_name, name_index)
        if not allocator_id:
            counts["skipped_unmatched"] += 1
            continue

        client_status = str(raw_content.get(COL_CLIENT_STATUS) or "")
        client_comments = str(raw_content.get(COL_CLIENT_COMMENTS) or "")
        scoring_text = str(raw_content.get(COL_SCORING) or "")
        miner_comments = str(raw_content.get(COL_MINER_COMMENTS) or "")

        cs_lower = client_status.lower()

        # ----------------------------------------------------------------
        # Case 1: Structural rejection (blacklist)
        # ----------------------------------------------------------------
        def _make_hash(*parts: str) -> str:
            return hashlib.sha256("|".join(parts).encode()).hexdigest()[:32]

        if E1_BLACKLIST_STATUS in cs_lower:
            key = f"{allocator_id}:structural"
            if key in seen_ids:
                continue
            seen_ids.add(key)
            counts["structural"] += 1
            reason_tags = ["blacklist"] + _extract_reason_tags(client_comments + " " + scoring_text)
            batch.append((
                str(uuid.uuid4()),
                allocator_id,
                "structural",
                json.dumps(reason_tags),
                client_status[:200],
                None,
                "blacklisted_by_client",
                _CONVERSION_PROB["structural"],
                0.95,
                1,
                0.0,
                source_record_id,
                source_file,
                _make_hash(allocator_id, "structural", source_record_id),
            ))
            continue

        # ----------------------------------------------------------------
        # Case 2: Stated rejection (IIP conflict)
        # ----------------------------------------------------------------
        if E2_CONFLICT_STATUS in cs_lower:
            key = f"{allocator_id}:stated"
            if key in seen_ids:
                continue
            seen_ids.add(key)
            counts["stated"] += 1
            reason_tags = _extract_reason_tags(client_comments + " " + scoring_text)
            if not reason_tags:
                reason_tags = ["iip_conflict_unspecified"]
            stated_reason = client_comments[:500] if client_comments else client_status
            conv_prob = max(
                (_REASON_OVERRIDE_PROB.get(t, _CONVERSION_PROB["stated"]) for t in reason_tags),
                default=_CONVERSION_PROB["stated"],
            )
            batch.append((
                str(uuid.uuid4()),
                allocator_id,
                "stated",
                json.dumps(reason_tags),
                stated_reason[:500],
                None,
                None,
                conv_prob,
                0.90,
                1,
                0.0,
                source_record_id,
                source_file,
                _make_hash(allocator_id, "stated", source_record_id),
            ))
            continue

        # ----------------------------------------------------------------
        # Case 3: Approved LP — check for inferred/latent rejection signals
        # ----------------------------------------------------------------
        combined = (scoring_text + " " + client_comments + " " + miner_comments).lower()
        reason_tags = _extract_reason_tags(combined)
        hard_hits = [p for p in HARD_EXCLUSION_PHRASES if p in combined]
        if hard_hits:
            reason_tags = list(set(reason_tags + ["mandate_mismatch"]))

        if not reason_tags:
            continue

        key = f"{allocator_id}:inferred"
        if key in seen_ids:
            continue
        seen_ids.add(key)
        counts["inferred"] += 1

        inferred_reason = f"Inferred from scoring text: {', '.join(reason_tags)}"
        conv_prob = max(
            (_REASON_OVERRIDE_PROB.get(t, _CONVERSION_PROB["inferred"]) for t in reason_tags),
            default=_CONVERSION_PROB["inferred"],
        )
        batch.append((
            str(uuid.uuid4()),
            allocator_id,
            "inferred",
            json.dumps(reason_tags),
            None,
            inferred_reason[:500],
            None,
            conv_prob,
            0.50,
            1,
            0.0,
            source_record_id,
            source_file,
            _make_hash(allocator_id, "inferred", source_record_id),
        ))

    if batch:
        con.executemany(
            """
            INSERT INTO rejections (
                rejection_id, allocator_id, rejection_type,
                reason_tags, stated_reason, inferred_reason, structural_constraint,
                future_conversion_prob, confidence, evidence_count, contradiction_score,
                source_record_id, source_file, content_hash
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            batch,
        )

    return counts

"""
Allocator normalizer — applies taxonomies and provenance preservation to allocator rows.
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from agents.normalization.taxonomies import (
    normalize_allocator_type, normalize_lp_type_label, infer_type_from_name,
    normalize_geography, parse_usd, classify_check_size,
    AllocatorType, Geography, CheckSizeBucket, Appetite,
)


def enrich_all_allocators(con) -> Dict[str, int]:
    """
    Two-pass enrichment for all allocators:
    Pass 1 — re-run column-level enrichment for every allocator against all its
             matching raw rows (fills in type, geography, check size, etc.).
    Pass 2 — for still-unknown types, scan scoring rationale text in entities_raw
             for LP type mentions adjacent to the allocator name.

    Returns counts: {enriched_pass1, enriched_pass2}.
    """
    pass1 = _enrich_from_raw_columns(con)
    pass2 = _enrich_from_scoring_text(con)
    return {"enriched_pass1": pass1, "enriched_pass2": pass2}


def _enrich_from_raw_columns(con) -> int:
    """
    For every allocator, find all entities_raw rows whose content contains
    the canonical name (or any alias) and call enrich_allocator_from_raw.
    """
    # Restrict to institutional prospects. Syndicate-roster LPs are enriched at
    # creation time in syndicate_normalizer; running the per-name LIKE scans below
    # over thousands of them would be prohibitively slow.
    allocators = con.execute(
        "SELECT CAST(allocator_id AS VARCHAR), canonical_name FROM allocators "
        "WHERE COALESCE(population, '') <> 'syndicate_lp'"
    ).fetchall()

    updated = 0
    for allocator_id, canonical_name in allocators:
        # Gather aliases
        alias_rows = con.execute(
            "SELECT alias_text FROM entity_aliases WHERE canonical_id = ?",
            [allocator_id],
        ).fetchall()
        names = {canonical_name} | {r[0] for r in alias_rows}

        for name in names:
            raw_rows = con.execute(
                """
                SELECT source_record_id, raw_content, source_file
                FROM entities_raw
                WHERE source_type IN ('xlsx', 'csv') AND raw_content LIKE ?
                LIMIT 10
                """,
                [f"%{name}%"],
            ).fetchall()
            for src_id, raw_content, src_file in raw_rows:
                if isinstance(raw_content, str):
                    try:
                        raw_content = json.loads(raw_content)
                    except Exception:
                        continue
                if not isinstance(raw_content, dict):
                    continue
                enrich_allocator_from_raw(allocator_id, raw_content, src_file, con)
                updated += 1

    return updated


# LP type mention patterns for text scanning (order matters: most specific first)
_TEXT_TYPE_PATTERNS: List[tuple] = [
    (r"single[- ]family office", AllocatorType.FAMILY_OFFICE_SINGLE),
    (r"multi[- ]family office", AllocatorType.FAMILY_OFFICE_MULTI),
    (r"family office", AllocatorType.FAMILY_OFFICE_SINGLE),
    (r"fund[- ]of[- ]funds", AllocatorType.FUND_OF_FUNDS),
    (r"fund of funds", AllocatorType.FUND_OF_FUNDS),
    (r"pension fund", AllocatorType.PENSION_FUND),
    (r"endowment fund", AllocatorType.ENDOWMENT),
    (r"\bendowment\b", AllocatorType.ENDOWMENT),
    (r"asset manager", AllocatorType.ASSET_MANAGER),
    (r"investment manager", AllocatorType.ASSET_MANAGER),
    (r"hnwi|high net worth individual", AllocatorType.HIGH_NET_WORTH),
    (r"sovereign wealth", AllocatorType.SOVEREIGN_WEALTH),
    (r"development finance institution", AllocatorType.DEVELOPMENT_FINANCE),
    (r"corporate venture", AllocatorType.CORPORATE),
]


def _enrich_from_scoring_text(con) -> int:
    """
    For allocators still marked 'unknown', scan prose text in entities_raw
    for LP type mentions. Also tries name-based inference as a final fallback.
    """
    unknowns = con.execute(
        "SELECT CAST(allocator_id AS VARCHAR), canonical_name FROM allocators "
        "WHERE allocator_type = 'unknown' AND COALESCE(population, '') <> 'syndicate_lp'"
    ).fetchall()

    updated = 0
    for allocator_id, canonical_name in unknowns:
        # Try name-based inference first (free, no DB scan needed)
        inferred = infer_type_from_name(canonical_name)
        if inferred != AllocatorType.UNKNOWN:
            con.execute(
                "UPDATE allocators SET allocator_type = ? WHERE CAST(allocator_id AS VARCHAR) = ?",
                [inferred, allocator_id],
            )
            updated += 1
            continue

        # Gather aliases for a wider text search
        alias_rows = con.execute(
            "SELECT alias_text FROM entity_aliases WHERE canonical_id = ?",
            [allocator_id],
        ).fetchall()
        names = [canonical_name] + [r[0] for r in alias_rows]

        found_type = AllocatorType.UNKNOWN
        for name in names:
            text_rows = con.execute(
                """
                SELECT raw_content FROM entities_raw
                WHERE raw_content LIKE ?
                LIMIT 5
                """,
                [f"%{name}%"],
            ).fetchall()

            for (raw_content,) in text_rows:
                if isinstance(raw_content, str):
                    try:
                        rc = json.loads(raw_content)
                    except Exception:
                        rc = {"text": raw_content}
                elif isinstance(raw_content, dict):
                    rc = raw_content
                else:
                    continue
                text = " ".join(str(v) for v in rc.values() if isinstance(v, str) and v.strip())

                for pattern, atype in _TEXT_TYPE_PATTERNS:
                    if re.search(pattern, text, re.IGNORECASE):
                        found_type = atype
                        break
                if found_type != AllocatorType.UNKNOWN:
                    break
            if found_type != AllocatorType.UNKNOWN:
                break

        if found_type != AllocatorType.UNKNOWN:
            con.execute(
                "UPDATE allocators SET allocator_type = ? WHERE CAST(allocator_id AS VARCHAR) = ?",
                [found_type, allocator_id],
            )
            updated += 1

    return updated


def enrich_allocator_from_raw(allocator_id: str, raw_row: Dict, source_file: str, con) -> None:
    """
    Enrich an existing allocator row with fields parsed from a raw xlsx row.
    Only updates null fields; does not overwrite existing values.
    """
    raw_lower = {k.strip().lower(): v for k, v in raw_row.items() if isinstance(v, str)}
    sheet = str(raw_row.get("_sheet", ""))
    is_prospects_sheet = "Prospects" in sheet or "prospect" in sheet.lower()

    updates = {}

    # Allocator type — use the more precise LP-type-label mapper first
    raw_type = (
        raw_lower.get("investor type")
        or raw_lower.get("type")
        or raw_lower.get("allocator type")
        or raw_lower.get("lp type")
        or raw_lower.get("lp type priority")
        or raw_lower.get("investor class")
        # Prospects sheets store Investor Type in "Unnamed: 2"
        or (raw_lower.get("unnamed: 2") if is_prospects_sheet else None)
    )
    if raw_type:
        candidate = normalize_lp_type_label(raw_type)
        if candidate != AllocatorType.UNKNOWN:
            updates["allocator_type"] = candidate

    # Geography
    raw_geo = (
        raw_lower.get("country of headquarter")
        or raw_lower.get("geography")
        or raw_lower.get("location")
        or raw_lower.get("region")
        or raw_lower.get("country")
        or raw_lower.get("hq country")
        or raw_lower.get("hq")
    )
    if raw_geo:
        updates["geography"] = normalize_geography(raw_geo)
        # Extract country if a comma-separated "City, Country" format
        if "," in raw_geo:
            parts = [p.strip() for p in raw_geo.split(",")]
            if parts:
                updates["hq_country"] = parts[-1]

    # Check size
    raw_check = (
        raw_lower.get("check size") or raw_lower.get("ticket size")
        or raw_lower.get("commitment size") or raw_lower.get("investment size")
        or raw_lower.get("typical commitment")
    )
    if raw_check:
        check_usd = parse_usd(raw_check)
        if check_usd:
            updates["check_size_min_usd"] = check_usd
            updates["check_size_max_usd"] = check_usd
            updates["check_size_bucket"] = classify_check_size(check_usd)

    # EM appetite
    raw_em = raw_lower.get("em appetite") or raw_lower.get("em exposure") or raw_lower.get("emerging market")
    if raw_em:
        updates["em_appetite"] = _normalize_appetite(raw_em)

    # AI appetite
    raw_ai = raw_lower.get("ai appetite") or raw_lower.get("ai focus") or raw_lower.get("tech focus")
    if raw_ai:
        updates["ai_appetite"] = _normalize_appetite(raw_ai)

    # Stage preference
    raw_stage = raw_lower.get("stage") or raw_lower.get("investment stage") or raw_lower.get("stage preference")
    if raw_stage:
        updates["stage_preference"] = _normalize_stage(raw_stage)

    if not updates:
        return

    # Build SET clause — only update null fields
    set_clauses = [f"{k} = COALESCE({k}, ?)" for k in updates.keys()]
    set_sql = ", ".join(set_clauses) + ", updated_at = NOW()"
    con.execute(
        f"UPDATE allocators SET {set_sql} WHERE CAST(allocator_id AS VARCHAR) = ?",
        list(updates.values()) + [allocator_id],
    )


def _normalize_appetite(raw: str) -> str:
    raw_lower = raw.lower().strip()
    if any(w in raw_lower for w in ["high", "strong", "significant", "active", "yes", "y"]):
        return Appetite.HIGH
    if any(w in raw_lower for w in ["medium", "moderate", "some", "limited"]):
        return Appetite.MEDIUM
    if any(w in raw_lower for w in ["low", "minimal", "little"]):
        return Appetite.LOW
    if any(w in raw_lower for w in ["none", "no", "n", "not"]):
        return Appetite.NONE
    return Appetite.UNKNOWN


def _normalize_stage(raw: str) -> str:
    raw_lower = raw.lower()
    if "seed" in raw_lower:
        return "seed"
    if "series a" in raw_lower or "series-a" in raw_lower:
        return "series_a"
    if "series b" in raw_lower:
        return "series_b"
    if "growth" in raw_lower:
        return "growth"
    if "late" in raw_lower:
        return "late"
    if "buyout" in raw_lower:
        return "buyout"
    if "multi" in raw_lower or "all" in raw_lower:
        return "multi_stage"
    if "fund" in raw_lower:
        return "fund_level"
    return "unknown"

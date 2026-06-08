"""
Entity resolver — rapidfuzz-based fuzzy matching across source files.

Matches LP/fund names across the 6 source files to build entity_aliases.
Conservative threshold: ambiguous matches go to the review queue, not forced.
Cross-file matches emit relationship_evidence rows so resolution is auditable.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from agents.normalization.taxonomies import (
    normalize_allocator_type, normalize_lp_type_label, infer_type_from_name,
    normalize_geography, parse_usd, classify_check_size,
)
from agents.reviews.queue_writer import write_to_queue, should_queue, _load_thresholds


try:
    from rapidfuzz import fuzz, process as rfprocess
    _RAPIDFUZZ_AVAILABLE = True
except ImportError:
    _RAPIDFUZZ_AVAILABLE = False

ROOT = Path(__file__).resolve().parent.parent.parent

# Confidence thresholds
AUTO_MATCH_THRESHOLD = 0.90       # >= this → automatic match, no review
REVIEW_MATCH_THRESHOLD = 0.70     # [review_threshold, auto_threshold) → queue for review
# Below REVIEW_MATCH_THRESHOLD → not a match


def resolve_entities_from_raw(con) -> Dict[str, int]:
    """
    Main entry point for normalization.
    Reads entities_raw, groups by source_type, resolves allocator names, writes:
    - allocators table (canonical rows)
    - entity_aliases table (alias mappings)
    - relationship_evidence (cross-file match evidence)
    - review_queues/aliases.jsonl (ambiguous candidates)

    Returns counts: {allocators_created, aliases_created, evidence_rows, queued_for_review}
    """
    # Fetch all xlsx rows (primary source for structured allocator data).
    # Exclude the AngelList syndicate roster + ContraVC ranking: those are large
    # individual-angel populations handled by syndicate_normalizer via exact-name
    # matching. Routing thousands of person names through O(n^2) fuzzy clustering
    # would both explode runtime and badly over-cluster on shared first/last names.
    rows = con.execute(
        """
        SELECT source_record_id, source_file, raw_content
        FROM entities_raw
        WHERE source_type = 'xlsx'
          AND source_file NOT LIKE '%Syndicate LPs%'
          AND source_file NOT LIKE '%ContraVC%'
        ORDER BY source_file, source_record_id
        """
    ).fetchall()

    parsed_rows = []
    for src_id, src_file, raw_content in rows:
        if isinstance(raw_content, str):
            try:
                raw_content = json.loads(raw_content)
            except json.JSONDecodeError:
                continue

        parsed_rows.append((src_id, src_file, raw_content))

    # Extract candidate entity names
    candidates = _extract_name_candidates(parsed_rows)

    # Cluster candidates into canonical entities
    clusters = _cluster_by_name(candidates)

    allocators_created = 0
    aliases_created = 0
    evidence_rows = 0
    queued_for_review = 0

    for canonical_name, members in clusters.items():
        # Create or find the canonical allocator
        allocator_id = _upsert_allocator(con, canonical_name, members)

        # Write aliases for all members
        for member in members:
            alias_conf = member["match_confidence"]
            _write_alias(con, str(allocator_id), "allocator", member["raw_name"],
                         member["source_file"], alias_conf)
            aliases_created += 1

            # Write corroboration evidence when the same entity appears in a different
            # file OR a different sheet within the same file.  "Different sheet" counts
            # as cross-context evidence because it means two independent data collection
            # processes (e.g., m3 Blurbs profiling vs Prospects_m3 campaign scoring)
            # both observed the entity.
            primary = members[0]
            primary_sheet = str(primary.get("source_offset", "")).split(":")[0]
            member_sheet = str(member.get("source_offset", "")).split(":")[0]
            is_different_context = (
                member["source_file"] != primary["source_file"]
                or primary_sheet != member_sheet
            )
            if is_different_context:
                _write_cross_file_evidence(
                    con,
                    allocator_id=str(allocator_id),
                    source_record_id=member["source_record_id"],
                    canonical_source_record_id=primary["source_record_id"],
                    evidence_strength=member["match_confidence"],
                    provenance_pointer={
                        "source_file": member["source_file"],
                        "source_offset": member.get("source_offset", ""),
                        "row_id": member["source_record_id"],
                        "canonical_source_file": primary["source_file"],
                        "canonical_row_id": primary["source_record_id"],
                    },
                )
                evidence_rows += 1

            # Queue ambiguous matches for review
            if member["match_confidence"] < AUTO_MATCH_THRESHOLD:
                write_to_queue(
                    target_type="aliases",
                    entity_id=str(allocator_id),
                    current_value={
                        "canonical_name": canonical_name,
                        "alias_text": member["raw_name"],
                        "confidence": member["match_confidence"],
                    },
                    evidence_pointers=[{
                        "source_file": member["source_file"],
                        "source_record_id": member["source_record_id"],
                    }],
                    confidence=member["match_confidence"],
                    reason=f"fuzzy_match_below_auto_threshold ({member['match_confidence']:.2f})",
                )
                queued_for_review += 1

        allocators_created += 1

    return {
        "allocators_created": allocators_created,
        "aliases_created": aliases_created,
        "evidence_rows": evidence_rows,
        "queued_for_review": queued_for_review,
    }


def _extract_name_candidates(rows: List[Tuple]) -> List[Dict]:
    """Extract LP/entity name candidates from raw xlsx rows."""
    candidates = []
    name_fields = [
        "LP Name", "Name", "Investor Name", "Allocator", "Institution",
        "Organization", "Entity", "Company", "Fund Name", "LP",
        "Investor", "Contact Name", "Organisation",
        "Firm Name",
    ]
    # Values that look like column headers or placeholder text (skip them)
    _SKIP_VALUES = frozenset({
        "nan", "none", "", "investor name", "lp name", "name", "nr", "nr.",
        "company", "organisation", "organization", "entity", "fund", "fund name",
        "investor", "lp", "total", "rejected", "rejection rate", "healthiness score",
        "approved", "pending", "data sources", "scoring details", "qa status",
        "client status", "month", "prospects",
    })

    import re as _re
    # Anonymized prospect placeholders: "Name A", "Name B", "Name 1", "Investor A" etc.
    _ANON_PATTERN = _re.compile(
        r'^(name|investor|lp|entity|company|contact|prospect)\s+[a-z0-9]{1,3}$',
        _re.IGNORECASE,
    )

    for src_id, src_file, raw in rows:
        if not isinstance(raw, dict):
            continue

        raw_lower = {k.strip().lower(): v for k, v in raw.items()}
        name = None

        # Standard named columns first
        for field in name_fields:
            val = raw_lower.get(field.lower(), "")
            if val and str(val).strip().lower() not in _SKIP_VALUES:
                name = str(val).strip()
                break

        # Fallback: handle Prospects_m1/m2/m3 sheets where the Excel header row is
        # buried deep — pandas reads the title as header, leaving LP Name in "Unnamed: 1"
        if not name:
            sheet = str(raw.get("_sheet", ""))
            if "Prospects" in sheet or "prospect" in sheet.lower():
                for col in ("Unnamed: 1", "Unnamed:1", "unnamed: 1", "unnamed:1"):
                    raw_col = raw.get(col) or raw.get(col.lower()) or raw_lower.get(col.lower(), "")
                    raw_str = str(raw_col).strip()
                    if raw_str.lower() not in _SKIP_VALUES and len(raw_str) > 2 and len(raw_str) < 80:
                        # Must look like a proper name: starts with uppercase or contains space+uppercase
                        if raw_str[0].isupper() or (len(raw_str) > 4 and not raw_str.startswith("*")):
                            name = raw_str
                            break

        if not name:
            continue

        # Drop anonymized placeholders: "Name A", "Investor B", "LP 1" etc.
        if _ANON_PATTERN.match(name.strip()):
            continue

        # Build source_offset: use sheet:row for xlsx, row:N for csv/api
        sheet = raw.get("_sheet", "")
        row_num = str(raw.get("_row_number", ""))
        if sheet:
            source_offset = f"{sheet}:{row_num}"
        else:
            source_offset = f"row:{row_num}"

        candidates.append({
            "raw_name": name,
            "source_record_id": src_id,
            "source_file": src_file,
            "source_offset": source_offset,
            "raw_row": raw,
            "match_confidence": 1.0,  # will be updated during clustering
        })

    return candidates


def _cluster_by_name(candidates: List[Dict]) -> Dict[str, List[Dict]]:
    """
    Group candidates into canonical entity clusters using rapidfuzz.
    Returns {canonical_name: [member_dicts]}.
    """
    if not candidates:
        return {}

    if not _RAPIDFUZZ_AVAILABLE:
        # Fallback: exact match only
        clusters: Dict[str, List[Dict]] = {}
        for c in candidates:
            clusters.setdefault(c["raw_name"], []).append(c)
        return clusters

    clusters: Dict[str, List[Dict]] = {}
    assigned: set = set()

    names = [c["raw_name"] for c in candidates]

    for i, candidate in enumerate(candidates):
        if i in assigned:
            continue

        cluster_name = candidate["raw_name"]
        cluster_members = [candidate]
        assigned.add(i)

        # Find similar names
        results = rfprocess.extract(
            candidate["raw_name"],
            names,
            scorer=fuzz.WRatio,
            score_cutoff=REVIEW_MATCH_THRESHOLD * 100,
            limit=None,
        )

        for matched_name, score, matched_idx in results:
            if matched_idx == i or matched_idx in assigned:
                continue
            normalized_score = score / 100.0
            member = dict(candidates[matched_idx])
            member["match_confidence"] = normalized_score
            cluster_members.append(member)
            assigned.add(matched_idx)

        clusters[cluster_name] = cluster_members

    return clusters


def _upsert_allocator(con, canonical_name: str, members: List[Dict]) -> uuid.UUID:
    """Create allocator if it doesn't exist. Returns allocator_id."""
    existing = con.execute(
        "SELECT allocator_id FROM allocators WHERE canonical_name = ?",
        [canonical_name],
    ).fetchone()

    if existing:
        return uuid.UUID(str(existing[0]))

    # Build from the first (primary) member's raw row
    primary = members[0]
    raw = primary.get("raw_row", {})
    raw_lower = {k.strip().lower(): v for k, v in raw.items() if isinstance(v, str)}

    allocator_id = uuid.uuid4()

    sheet = str(raw.get("_sheet", ""))
    is_prospects_sheet = "Prospects" in sheet or "prospect" in sheet.lower()

    # Resolve allocator type: try structured column first, then Unnamed: 2 for Prospects,
    # then name-based inference as final fallback
    raw_type_str = (
        raw_lower.get("investor type")
        or raw_lower.get("type")
        or raw_lower.get("allocator type")
        or raw_lower.get("lp type")
        or raw_lower.get("investor class")
        or raw_lower.get("lp type priority")
        # Prospects_m1/m2/m3: Investor Type in "Unnamed: 2" column
        or (raw_lower.get("unnamed: 2") if is_prospects_sheet else None)
    )
    allocator_type = normalize_lp_type_label(raw_type_str)
    if allocator_type == "unknown":
        allocator_type = infer_type_from_name(canonical_name)

    geography = normalize_geography(
        raw_lower.get("country of headquarter")
        or raw_lower.get("geography")
        or raw_lower.get("location")
        or raw_lower.get("region")
        or raw_lower.get("country")
        or raw_lower.get("hq country")
        # Prospects_m1/m2/m3: Country in "Unnamed: 4" column
        or (raw_lower.get("unnamed: 4") if is_prospects_sheet else None)
    )
    check_raw = raw_lower.get("check size") or raw_lower.get("ticket size") or raw_lower.get("commitment size")
    check_usd = parse_usd(check_raw)
    check_bucket = classify_check_size(check_usd)

    content_hash = primary["source_record_id"][:64]  # reuse source_record_id as proxy

    con.execute(
        """
        INSERT INTO allocators (
            allocator_id, canonical_name, allocator_type, geography,
            check_size_min_usd, check_size_max_usd, check_size_bucket,
            source_record_id, source_file, content_hash
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            str(allocator_id), canonical_name, allocator_type, geography,
            check_usd, check_usd, check_bucket,
            primary["source_record_id"], primary["source_file"], content_hash,
        ],
    )
    return allocator_id


def _write_alias(con, canonical_id: str, entity_type: str, alias_text: str,
                 source_file: str, confidence: float) -> None:
    existing = con.execute(
        """
        SELECT 1 FROM entity_aliases
        WHERE canonical_id = ? AND alias_text = ? AND source_file = ?
        """,
        [canonical_id, alias_text, source_file],
    ).fetchone()
    if existing:
        return
    con.execute(
        """
        INSERT INTO entity_aliases
            (alias_id, canonical_id, entity_type, alias_text, source_file, confidence)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        [str(uuid.uuid4()), canonical_id, entity_type, alias_text, source_file, confidence],
    )


def _write_cross_file_evidence(
    con,
    allocator_id: str,
    source_record_id: str,
    canonical_source_record_id: str,
    evidence_strength: float,
    provenance_pointer: Dict,
) -> None:
    """
    Write a cross_file_match evidence row.

    A cross-file entity match confirms the same allocator appears in multiple source
    files.  We model this as a self-referential 'cross_file_corroboration' relationship
    edge on the canonical allocator, then attach the evidence row to that edge.
    This ensures every relationship_evidence row has a valid edge_id anchor.
    """
    now = datetime.now(timezone.utc).isoformat()

    # Find or create the placeholder corroboration edge (self-loop on the allocator)
    existing_edge = con.execute(
        """
        SELECT CAST(edge_id AS VARCHAR) FROM relationships
        WHERE source_node_id = ? AND target_node_id = ?
          AND edge_type = 'cross_file_corroboration'
        LIMIT 1
        """,
        [allocator_id, allocator_id],
    ).fetchone()

    if existing_edge:
        edge_id = existing_edge[0]
        # Update last_seen and bump weight to reflect additional file corroboration
        con.execute(
            "UPDATE relationships SET last_seen = ?, weight = weight + 1.0 WHERE CAST(edge_id AS VARCHAR) = ?",
            [now, edge_id],
        )
    else:
        edge_id = str(uuid.uuid4())
        con.execute(
            """
            INSERT INTO relationships
                (edge_id, source_node_id, source_node_type,
                 target_node_id, target_node_type,
                 edge_type, weight, confidence, first_seen, last_seen)
            VALUES (?, ?, 'lp', ?, 'lp', 'cross_file_corroboration', 1.0, ?, ?, ?)
            """,
            [edge_id, allocator_id, allocator_id, evidence_strength, now, now],
        )

    con.execute(
        """
        INSERT INTO relationship_evidence
            (evidence_id, edge_id, source_record_id, evidence_type,
             evidence_strength, confidence, provenance_pointer)
        VALUES (?, ?, ?, 'cross_file_match', ?, ?, ?)
        """,
        [
            str(uuid.uuid4()), edge_id, source_record_id,
            evidence_strength, evidence_strength,
            json.dumps(provenance_pointer),
        ],
    )

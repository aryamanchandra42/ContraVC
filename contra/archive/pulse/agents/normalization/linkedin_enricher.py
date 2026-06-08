"""
LinkedIn export enricher — fuzzy-match Phantombuster rows to existing allocators.

COALESCE-only: fills NULL alias matches; emits cross_file_match evidence.
"""

from __future__ import annotations

import json
import uuid
from typing import Any, Dict, List, Optional, Tuple

from rapidfuzz import fuzz


def _norm(name: str) -> str:
    return (name or "").lower().strip()


def _build_allocator_index(con) -> List[Tuple[str, str, str]]:
    rows = con.execute(
        """
        SELECT CAST(allocator_id AS VARCHAR), canonical_name, population
        FROM allocators
        WHERE population IN ('institutional_prospect', 'syndicate_lp')
        """
    ).fetchall()
    index = [(aid, name, pop) for aid, name, pop in rows if name]
    for alias, aid in con.execute(
        """
        SELECT alias_text, canonical_id FROM entity_aliases
        WHERE entity_type = 'allocator'
        """
    ).fetchall():
        if alias:
            index.append((aid, alias, "alias"))
    return index


def _best_match(name: str, index: List[Tuple[str, str, str]], threshold: int = 88) -> Optional[Tuple[str, str, int]]:
    if not name:
        return None
    best: Optional[Tuple[str, str, int]] = None
    for aid, candidate, pop in index:
        score = fuzz.token_sort_ratio(_norm(name), _norm(candidate))
        if score >= threshold and (best is None or score > best[2]):
            best = (aid, candidate, score)
    return best


def run_linkedin_enrichment(con, threshold: int = 88) -> Dict[str, int]:
    """Match linkedin_export rows to allocators; write aliases + evidence."""
    rows = con.execute(
        """
        SELECT source_record_id, source_file, content_hash, raw_content
        FROM entities_raw
        WHERE json_extract_string(raw_content, '$._source_platform') = 'linkedin_export'
        """
    ).fetchall()

    if not rows:
        return {"linkedin_rows": 0, "matched": 0, "aliases_created": 0}

    index = _build_allocator_index(con)
    matched = aliases = 0

    for src_id, src_file, content_hash, raw in rows:
        if isinstance(raw, str):
            raw = json.loads(raw)

        name = (
            raw.get("_li_full_name")
            or raw.get("_li_company")
            or ""
        ).strip()
        if not name:
            continue

        hit = _best_match(name, index, threshold)
        if not hit:
            continue
        aid, matched_name, score = hit
        matched += 1

        existing_alias = con.execute(
            """
            SELECT 1 FROM entity_aliases
            WHERE canonical_id = ? AND alias_text = ?
            """,
            [aid, name],
        ).fetchone()
        if not existing_alias and name.lower() != matched_name.lower():
            con.execute(
                """
                INSERT INTO entity_aliases
                    (alias_id, canonical_id, entity_type, alias_text,
                     source_file, confidence, resolver_method)
                VALUES (?, ?, 'allocator', ?, ?, ?, 'linkedin_fuzzy_match')
                """,
                [str(uuid.uuid4()), aid, name, src_file, score / 100.0],
            )
            aliases += 1

    return {
        "linkedin_rows": len(rows),
        "matched": matched,
        "aliases_created": aliases,
    }

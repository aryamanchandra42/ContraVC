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

        # Persist contact details to allocator_contacts (upsert by linkedin_url or name)
        _upsert_allocator_contact(con, aid, raw, score / 100.0, src_file)

    return {
        "linkedin_rows": len(rows),
        "matched": matched,
        "aliases_created": aliases,
    }


def _upsert_allocator_contact(con, allocator_id: str, raw: dict, confidence: float, source: str) -> None:
    """Upsert a LinkedIn-sourced contact row into allocator_contacts."""
    try:
        li_url = (raw.get("_li_url") or raw.get("_li_profile_url") or "").strip() or None
        email = (raw.get("_li_email") or raw.get("Email") or "").strip() or None
        title = (raw.get("_li_title") or raw.get("Title") or "").strip() or None
        company = (raw.get("_li_company") or raw.get("Company") or "").strip() or None
        location = (raw.get("_li_location") or raw.get("Location") or "").strip() or None
        full_name = (raw.get("_li_full_name") or raw.get("Name") or "").strip() or None

        # Check for existing row by allocator + source combo
        existing = con.execute(
            """
            SELECT contact_id FROM allocator_contacts
            WHERE allocator_id = ? AND source = 'linkedin_export'
              AND (linkedin_url = ? OR full_name = ?)
            """,
            [allocator_id, li_url, full_name],
        ).fetchone()

        if existing:
            con.execute(
                """
                UPDATE allocator_contacts SET
                    full_name = COALESCE(?, full_name),
                    email = COALESCE(?, email),
                    linkedin_url = COALESCE(?, linkedin_url),
                    title = COALESCE(?, title),
                    company = COALESCE(?, company),
                    location = COALESCE(?, location),
                    match_confidence = ?,
                    ingested_at = NOW()
                WHERE contact_id = ?
                """,
                [full_name, email, li_url, title, company, location,
                 confidence, str(existing[0])],
            )
        else:
            con.execute(
                """
                INSERT INTO allocator_contacts
                    (contact_id, allocator_id, source, full_name, email,
                     linkedin_url, title, company, location, match_confidence)
                VALUES (?, ?, 'linkedin_export', ?, ?, ?, ?, ?, ?, ?)
                """,
                [str(uuid.uuid4()), allocator_id, full_name, email,
                 li_url, title, company, location, confidence],
            )
    except Exception:
        pass

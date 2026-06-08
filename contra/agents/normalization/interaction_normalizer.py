"""Interaction normalizer — maps raw interaction rows to the interactions table."""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Dict, Optional


def ingest_interaction_rows(con) -> int:
    """Scan entities_raw for rows that look like interaction/meeting records."""
    rows = con.execute(
        """
        SELECT source_record_id, source_file, content_hash, raw_content
        FROM entities_raw
        WHERE source_type IN ('xlsx', 'pdf', 'docx')
          AND source_file NOT LIKE '%Syndicate LPs%'
          AND source_file NOT LIKE '%ContraVC%'
        """
    ).fetchall()

    created = 0
    for src_id, src_file, ch, raw_content in rows:
        if isinstance(raw_content, str):
            try:
                raw_content = json.loads(raw_content)
            except Exception:
                continue
        if not isinstance(raw_content, dict):
            continue

        raw_lower = {k.strip().lower(): v for k, v in raw_content.items() if isinstance(v, str)}

        # Heuristic: row has meeting/call/interaction indicators
        interaction_type = _detect_interaction_type(raw_lower)
        if not interaction_type:
            continue

        # Try to resolve allocator
        allocator_name = (
            raw_lower.get("lp name") or raw_lower.get("name") or raw_lower.get("investor name")
            or raw_lower.get("allocator") or raw_lower.get("contact")
        )
        if not allocator_name:
            continue

        allocator_row = con.execute(
            "SELECT CAST(allocator_id AS VARCHAR) FROM allocators WHERE canonical_name = ?",
            [allocator_name.strip()],
        ).fetchone()
        if not allocator_row:
            # Try alias match
            alias_row = con.execute(
                """
                SELECT canonical_id FROM entity_aliases
                WHERE alias_text = ? AND entity_type = 'allocator'
                LIMIT 1
                """,
                [allocator_name.strip()],
            ).fetchone()
            if not alias_row:
                continue
            allocator_id = alias_row[0]
        else:
            allocator_id = allocator_row[0]

        # Parse date
        raw_date = raw_lower.get("date") or raw_lower.get("meeting date") or raw_lower.get("call date")
        occurred_at = _parse_date(raw_date)

        # Sentiment
        notes_text = raw_lower.get("notes") or raw_lower.get("comments") or raw_lower.get("summary") or ""
        sentiment = _infer_sentiment(notes_text)

        existing = con.execute(
            "SELECT 1 FROM interactions WHERE source_record_id = ?", [src_id]
        ).fetchone()
        if existing:
            continue

        con.execute(
            """
            INSERT INTO interactions
                (interaction_id, allocator_id, interaction_type, occurred_at, notes,
                 sentiment, source_record_id, source_file, content_hash)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                str(uuid.uuid4()), allocator_id, interaction_type,
                occurred_at.isoformat() if occurred_at else None,
                notes_text or None, sentiment, src_id, src_file, ch,
            ],
        )
        created += 1

    return created


def _detect_interaction_type(raw_lower: Dict) -> Optional[str]:
    """Return interaction type if this row looks like an interaction record."""
    keys_str = " ".join(raw_lower.keys())
    values_str = " ".join(str(v) for v in raw_lower.values())
    combined = (keys_str + " " + values_str).lower()

    if any(w in combined for w in ["meeting", "call", "intro", "outreach", "follow up", "conference"]):
        if "meeting" in combined:
            return "meeting"
        if "call" in combined:
            return "call"
        if "intro" in combined:
            return "intro"
        if "conference" in combined:
            return "conference"
        return "meeting"
    return None


def _parse_date(raw: Optional[str]) -> Optional[datetime]:
    if not raw or str(raw).strip() in ("", "nan", "None"):
        return None
    from dateutil import parser as dparser
    try:
        return dparser.parse(str(raw), dayfirst=False)
    except Exception:
        return None


def _infer_sentiment(text: str) -> str:
    text_lower = text.lower()
    positive = ["interested", "positive", "excited", "bullish", "strong interest", "want to proceed"]
    negative = ["not interested", "passed", "decline", "reject", "no interest", "not a fit"]
    if any(w in text_lower for w in positive):
        return "positive"
    if any(w in text_lower for w in negative):
        return "negative"
    if text_lower:
        return "neutral"
    return "unknown"

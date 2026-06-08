"""
Override applier — reads human_reviews rows and appends them to the DB.

Overrides are NEVER applied by mutating canonical rows.
They are applied at QUERY TIME via _effective SQL views.

This module handles:
1. Validating and ingesting reviewer decisions (jsonl → human_reviews table)
2. Listing pending review queue items
3. Computing queue status statistics
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from schema.models import HumanReview, VALID_REVIEW_TARGET_TYPES, VALID_REVIEW_DECISIONS

ROOT = Path(__file__).resolve().parent.parent.parent


def ingest_decisions(
    decisions_path: Path,
    con,
    reviewer: str = "human",
) -> int:
    """
    Read a jsonl file of reviewer decisions and append them to human_reviews.

    Each line must be a JSON object with:
    - entity_id: str (UUID of the target entity)
    - target_type: str (alias|allocator_archetype|ontology_term|signal|relationship_edge|rejection)
    - decision: str (confirm|reject|revise|defer)
    - override_payload: dict | null (required when decision='revise')
    - confidence_adjustment: float | null
    - override_reason: str | null
    - notes: str | null
    - supersedes: str | null (UUID of prior review to supersede)

    Returns number of rows inserted.
    """
    inserted = 0
    with open(decisions_path, encoding="utf-8") as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError as e:
                raise ValueError(f"Invalid JSON on line {lineno} of {decisions_path}: {e}")

            # Validate via Pydantic
            review = HumanReview(
                entity_id=raw["entity_id"],
                target_type=raw["target_type"],
                reviewer=raw.get("reviewer", reviewer),
                decision=raw["decision"],
                override_payload=raw.get("override_payload"),
                confidence_adjustment=raw.get("confidence_adjustment"),
                override_reason=raw.get("override_reason"),
                notes=raw.get("notes"),
                supersedes=uuid.UUID(raw["supersedes"]) if raw.get("supersedes") else None,
            )

            _insert_review(con, review)
            inserted += 1

    return inserted


def _insert_review(con, review: HumanReview) -> None:
    """Insert a single HumanReview row. Never updates existing rows."""
    con.execute(
        """
        INSERT INTO human_reviews (
            review_id, target_type, entity_id, reviewer, decision,
            override_payload, confidence_adjustment, override_reason,
            notes, reviewed_at, supersedes
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            str(review.review_id),
            review.target_type,
            review.entity_id,
            review.reviewer,
            review.decision,
            json.dumps(review.override_payload) if review.override_payload else None,
            review.confidence_adjustment,
            review.override_reason,
            review.notes,
            review.reviewed_at.isoformat(),
            str(review.supersedes) if review.supersedes else None,
        ],
    )


def get_review_status(con) -> Dict[str, Dict[str, int]]:
    """
    Return counts of decisions per target_type from the human_reviews table.
    """
    rows = con.execute(
        """
        SELECT target_type, decision, COUNT(*) as cnt
        FROM human_reviews
        GROUP BY target_type, decision
        ORDER BY target_type, decision
        """
    ).fetchall()

    status: Dict[str, Dict[str, int]] = {}
    for target_type, decision, cnt in rows:
        status.setdefault(target_type, {})[decision] = cnt
    return status


def list_latest_reviews(con, target_type: Optional[str] = None) -> List[Dict]:
    """Return the latest non-superseded review for each entity."""
    where = f"AND hr.target_type = '{target_type}'" if target_type else ""
    rows = con.execute(
        f"""
        SELECT hr.*
        FROM human_reviews hr
        WHERE NOT EXISTS (
            SELECT 1 FROM human_reviews hr2
            WHERE hr2.supersedes = hr.review_id
        )
        {where}
        ORDER BY hr.reviewed_at DESC
        """
    ).fetchall()

    cols = [
        "review_id", "target_type", "entity_id", "reviewer", "decision",
        "override_payload", "confidence_adjustment", "override_reason",
        "notes", "reviewed_at", "supersedes",
    ]
    return [dict(zip(cols, row)) for row in rows]

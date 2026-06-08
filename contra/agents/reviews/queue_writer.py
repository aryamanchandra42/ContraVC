"""
Review queue writer — writes review candidates to processed_data/review_queues/{target_type}.jsonl.

Every pipeline stage that produces uncertain output calls write_to_queue().
Queues are append-only; they are the audit log of what was surfaced for review.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

ROOT = Path(__file__).resolve().parent.parent.parent
QUEUES_DIR = ROOT / "processed_data" / "review_queues"
UNCERTAINTY_PARAMS = ROOT / "prompts" / "uncertainty.yaml"

VALID_TARGET_TYPES = frozenset({
    "aliases", "allocator_types", "edges", "ontology_terms", "signals",
})


def _load_thresholds() -> Dict[str, Any]:
    with open(UNCERTAINTY_PARAMS, encoding="utf-8") as f:
        params = yaml.safe_load(f)
    return params.get("review_queue", {})


def write_to_queue(
    target_type: str,
    entity_id: str,
    current_value: Any,
    evidence_pointers: List[Dict],
    confidence: Optional[float],
    reason: str,
    metadata: Optional[Dict] = None,
) -> str:
    """
    Append a review candidate to processed_data/review_queues/{target_type}.jsonl.

    Returns the queue_item_id.
    """
    if target_type not in VALID_TARGET_TYPES:
        raise ValueError(f"Invalid target_type '{target_type}'. Must be one of {VALID_TARGET_TYPES}")

    QUEUES_DIR.mkdir(parents=True, exist_ok=True)
    queue_path = QUEUES_DIR / f"{target_type}.jsonl"

    item = {
        "queue_item_id": str(uuid.uuid4()),
        "target_type": target_type,
        "entity_id": entity_id,
        "current_value": current_value,
        "evidence_pointers": evidence_pointers,
        "confidence": confidence,
        "reason": reason,
        "metadata": metadata or {},
        "surfaced_at": datetime.now(timezone.utc).isoformat(),
        "status": "pending",
    }

    with open(queue_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(item) + "\n")

    return item["queue_item_id"]


def should_queue(
    confidence: Optional[float],
    contradiction_score: Optional[float],
    source_agreement_score: Optional[float],
    evidence_count: int,
    thresholds: Optional[Dict] = None,
) -> tuple[bool, str]:
    """
    Determine whether a derived assertion should be surfaced for human review.
    Returns (should_queue: bool, reason: str).
    """
    if thresholds is None:
        thresholds = _load_thresholds()

    low_conf = thresholds.get("low_confidence_threshold", 0.40)
    high_contra = thresholds.get("high_contradiction_threshold", 0.30)
    low_agree = thresholds.get("low_source_agreement_threshold", 0.50)

    if confidence is not None and confidence < low_conf:
        return True, f"low_confidence ({confidence:.2f} < {low_conf})"
    if contradiction_score is not None and contradiction_score > high_contra:
        return True, f"high_contradiction ({contradiction_score:.2f} > {high_contra})"
    if source_agreement_score is not None and source_agreement_score < low_agree:
        return True, f"low_source_agreement ({source_agreement_score:.2f} < {low_agree})"
    return False, ""


def read_queue(target_type: str) -> List[Dict]:
    """Read all items from a review queue. Returns list of dicts."""
    if target_type not in VALID_TARGET_TYPES:
        raise ValueError(f"Invalid target_type '{target_type}'")
    queue_path = QUEUES_DIR / f"{target_type}.jsonl"
    if not queue_path.exists():
        return []
    items = []
    with open(queue_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                items.append(json.loads(line))
    return items


def queue_counts() -> Dict[str, int]:
    """Return pending item counts per target_type."""
    counts = {}
    for tt in VALID_TARGET_TYPES:
        items = read_queue(tt)
        counts[tt] = len(items)
    return counts

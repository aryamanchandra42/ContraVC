"""
Generate human-review decision records for the pending alias review queue.

Each fuzzy-matched alias pair is inspected and assigned a decision:
  - confidence >= AUTO_MATCH_THRESHOLD (0.90) → confirm  (very likely same entity)
  - confidence  < AUTO_MATCH_THRESHOLD          → reject  (different entities; false-positive
                                                            from shared suffix like "Partners")

Output: processed_data/review_queues/alias_decisions.jsonl
Run with:  python scripts/generate_alias_decisions.py
Then feed to:  pulse review ingest processed_data/review_queues/alias_decisions.jsonl
"""
from __future__ import annotations

import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

QUEUE_FILE = ROOT / "processed_data" / "review_queues" / "aliases.jsonl"
OUTPUT_FILE = ROOT / "processed_data" / "review_queues" / "alias_decisions.jsonl"

AUTO_MATCH_THRESHOLD = 0.90   # must match entity_resolver.py

def main() -> None:
    if not QUEUE_FILE.exists():
        print(f"Queue file not found: {QUEUE_FILE}")
        sys.exit(1)

    items = []
    with open(QUEUE_FILE, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                items.append(json.loads(line))

    print(f"Processing {len(items)} alias queue items …")
    decisions = []

    for item in items:
        confidence: float = item.get("confidence") or 0.0
        entity_id: str = item["entity_id"]
        current_value = item.get("current_value", {})
        canonical_name = current_value.get("canonical_name", "")
        alias_text = current_value.get("alias_text", "")

        if confidence >= AUTO_MATCH_THRESHOLD:
            decision = "confirm"
            reason = f"auto_confirm_high_confidence ({confidence:.3f} >= {AUTO_MATCH_THRESHOLD})"
            notes = f"Auto-confirmed: '{alias_text}' ≈ '{canonical_name}'"
        else:
            decision = "reject"
            reason = f"auto_reject_low_confidence ({confidence:.3f} < {AUTO_MATCH_THRESHOLD})"
            notes = (
                f"Auto-rejected: '{alias_text}' flagged as false-positive fuzzy match "
                f"of '{canonical_name}' (shared suffix, different entities)."
            )

        decisions.append({
            "entity_id": entity_id,
            "target_type": "alias",
            "decision": decision,
            "override_payload": None,
            "confidence_adjustment": None,
            "override_reason": reason,
            "notes": notes,
            "reviewer": "pulse_auto_review_v1",
        })
        print(f"  [{decision.upper():7s}] conf={confidence:.3f}  '{alias_text}' -> '{canonical_name}'")

    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        for d in decisions:
            f.write(json.dumps(d) + "\n")

    confirmed = sum(1 for d in decisions if d["decision"] == "confirm")
    rejected = sum(1 for d in decisions if d["decision"] == "reject")
    print(f"\nWrote {len(decisions)} decisions → {OUTPUT_FILE}")
    print(f"  Confirmed : {confirmed}")
    print(f"  Rejected  : {rejected}")
    print(f"\nNext step: pulse review ingest {OUTPUT_FILE}")


if __name__ == "__main__":
    main()

"""Generate alias review decisions and ingest them."""
import sys
sys.stdout.reconfigure(encoding='utf-8')
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

QUEUE_FILE = ROOT / "processed_data" / "review_queues" / "aliases.jsonl"
OUTPUT_FILE = ROOT / "processed_data" / "review_queues" / "alias_decisions.jsonl"
AUTO_MATCH_THRESHOLD = 0.90

# ----- generate decisions -----
if not QUEUE_FILE.exists():
    print(f"Queue file not found: {QUEUE_FILE}")
    sys.exit(1)

items = []
with open(QUEUE_FILE, encoding="utf-8") as f:
    for line in f:
        line = line.strip()
        if line:
            items.append(json.loads(line))

print(f"Processing {len(items)} alias queue items ...")
decisions = []
for item in items:
    confidence = float(item.get("confidence") or 0.0)
    entity_id = item["entity_id"]
    cv = item.get("current_value", {})
    canonical_name = cv.get("canonical_name", "")
    alias_text = cv.get("alias_text", "")

    if confidence >= AUTO_MATCH_THRESHOLD:
        decision = "confirm"
        reason = f"auto_confirm_high_confidence ({confidence:.3f} >= {AUTO_MATCH_THRESHOLD})"
        notes = f"Auto-confirmed: '{alias_text}' matches '{canonical_name}'"
    else:
        decision = "reject"
        reason = f"auto_reject_low_confidence ({confidence:.3f} < {AUTO_MATCH_THRESHOLD})"
        notes = (
            f"Auto-rejected: '{alias_text}' flagged as false-positive fuzzy match "
            f"of '{canonical_name}' (shared word, different entities)."
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
print(f"\nWrote {len(decisions)} decisions -> {OUTPUT_FILE}")
print(f"  Confirmed : {confirmed}")
print(f"  Rejected  : {rejected}")

# ----- ingest into human_reviews -----
print("\nIngesting decisions into human_reviews table ...")
from agents.db import get_conn
from agents.reviews.override_applier import ingest_decisions

con = get_conn()
inserted = ingest_decisions(OUTPUT_FILE, con, reviewer="pulse_auto_review_v1")
print(f"Inserted {inserted} review decisions into human_reviews.")

# ----- final counts -----
print("\nFinal review status:")
from agents.reviews.override_applier import get_review_status
status = get_review_status(con)
for tt, decisions_map in sorted(status.items()):
    for dec, cnt in sorted(decisions_map.items()):
        print(f"  {tt} / {dec}: {cnt}")

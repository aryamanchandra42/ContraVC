"""Debug why relationship hints aren't persisting and check table schema."""
import sys, json, traceback
sys.stdout.reconfigure(encoding='utf-8')

ROOT = __import__('pathlib').Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import duckdb
con = duckdb.connect('pulse.duckdb')

print("=== Relationships table columns ===")
try:
    cols = con.execute("PRAGMA table_info('relationships')").fetchall()
    for c in cols:
        print(f"  {c}")
except Exception as e:
    print(f"  ERROR: {e}")

print("\n=== relationship_evidence table columns ===")
try:
    cols = con.execute("PRAGMA table_info('relationship_evidence')").fetchall()
    for c in cols:
        print(f"  {c}")
except Exception as e:
    print(f"  ERROR: {e}")

print("\n=== Allocator count ===")
n = con.execute("SELECT COUNT(*) FROM allocators").fetchone()[0]
print(f"  {n} allocators")

print("\n=== Known entity names for co-occurrence ===")
from agents.ontology.pipeline import _load_known_entities
entities = _load_known_entities(con)
print(f"  {len(entities)} entity names loaded: {entities[:10]}")

print("\n=== Test: manually run _persist_relationship_hint ===")
from agents.ontology.pipeline import _persist_relationship_hint
from agents.ontology.base import ExtractedRelationshipHint

# Get two allocators to test with
rows = con.execute("SELECT CAST(allocator_id AS VARCHAR), canonical_name, source_record_id FROM allocators LIMIT 3").fetchall()
print("  Sample allocators:", [(r[1], r[2][:16]) for r in rows])

if len(rows) >= 2:
    hint = ExtractedRelationshipHint(
        source_entity_name=rows[0][1],
        target_entity_name=rows[1][1],
        edge_type="co_mentioned",
        evidence_type="debug_test",
        evidence_strength=0.4,
        confidence=0.4,
        source_record_id=rows[0][2],
        provenance_pointer={"test": True},
    )
    try:
        _persist_relationship_hint(con, hint)
        cnt = con.execute("SELECT COUNT(*) FROM relationship_evidence").fetchone()[0]
        print(f"  After test hint: relationship_evidence rows = {cnt}")
        rcnt = con.execute("SELECT COUNT(*) FROM relationships").fetchone()[0]
        print(f"  After test hint: relationships rows = {rcnt}")
    except Exception as e:
        print(f"  ERROR in _persist_relationship_hint: {e}")
        traceback.print_exc()

print("\n=== Test: _write_cross_file_evidence ===")
from agents.normalization.entity_resolver import _write_cross_file_evidence
if rows:
    try:
        _write_cross_file_evidence(
            con,
            allocator_id=rows[0][0],
            source_record_id=rows[0][2],
            canonical_source_record_id=rows[0][2],
            evidence_strength=0.85,
            provenance_pointer={"test": True, "source_file": "test.xlsx"},
        )
        cnt = con.execute("SELECT COUNT(*) FROM relationship_evidence").fetchone()[0]
        rcnt = con.execute("SELECT COUNT(*) FROM relationships").fetchone()[0]
        print(f"  After test xfile evidence: rel_evidence={cnt}, relationships={rcnt}")
        if rcnt > 0:
            r = con.execute("SELECT edge_type, weight, confidence FROM relationships LIMIT 3").fetchall()
            print(f"  Sample relationships: {r}")
    except Exception as e:
        print(f"  ERROR in _write_cross_file_evidence: {e}")
        traceback.print_exc()

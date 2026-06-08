"""
DEPRECATED — Use `pulse refresh` or click "Refresh PULSE" in `pulse explore`.

This script is kept for backward compatibility only. It is out of date:
it does not run syndicate integration, the signal layer, calibration, or the
exports step. The canonical full-pipeline entry point is now:

    pulse refresh          # headless / CI
    pulse explore          # UI with Refresh button (partner workflow)
    Launch_PULSE.bat       # Windows double-click shortcut

This file will be removed in a future cleanup once all callers are migrated.
"""
import sys
sys.stdout.reconfigure(encoding='utf-8')
sys.stderr.reconfigure(encoding='utf-8')

print(
    "\n⚠  scripts/run_pipeline.py is deprecated.\n"
    "   Run `pulse refresh` or click 'Refresh PULSE' inside `pulse explore`.\n",
    file=sys.stderr,
)
sys.exit(0)


import duckdb
from agents.db import get_conn
from pulse.run_tracker import start_run, complete_run, fail_run

def banner(msg):
    print(f"\n{'='*60}")
    print(f"  {msg}")
    print(f"{'='*60}")


banner("STAGE 1: INGEST")
con = get_conn()
from agents.ingestion.registry import ingest_all
manifests = ingest_all(ROOT / "raw_data", con)
total = sum(m.record_count for m in manifests.values())
print(f"  Files ingested : {len(manifests)}")
print(f"  Total records  : {total}")
for path, m in sorted(manifests.items()):
    print(f"    {path}: {m.record_count} rows, {len(m.warnings)} warnings")


banner("STAGE 2: NORMALIZE")
from agents.normalization.entity_resolver import resolve_entities_from_raw
from agents.normalization.fund_normalizer import ingest_fund_rows
from agents.normalization.interaction_normalizer import ingest_interaction_rows
from agents.normalization.allocator_normalizer import enrich_all_allocators

counts = resolve_entities_from_raw(con)
print(f"  Allocators created   : {counts['allocators_created']}")
print(f"  Aliases created      : {counts['aliases_created']}")
print(f"  Cross-file evidence  : {counts['evidence_rows']}")
print(f"  Queued for review    : {counts['queued_for_review']}")

enrich_counts = enrich_all_allocators(con)
print(f"  Enriched (col pass)  : {enrich_counts['enriched_pass1']}")
print(f"  Enriched (text pass) : {enrich_counts['enriched_pass2']}")

fund_count = ingest_fund_rows(con)
print(f"  Funds created        : {fund_count}")

interaction_count = ingest_interaction_rows(con)
print(f"  Interactions created : {interaction_count}")


banner("STAGE 3: EXTRACT")
import uuid as _uuid
run_id = str(_uuid.uuid4())
from agents.ontology.pipeline import run_extraction_pipeline
oc = run_extraction_pipeline(con, run_id)
print(f"  Docs processed       : {oc['documents_processed']}")
print(f"  Terms extracted      : {oc['terms_extracted']}")
print(f"  Relationship hints   : {oc['relationships_hinted']}")
print(f"  Cache hits           : {oc['cached_hits']}")


banner("STAGE 4: DERIVE")
from agents.uncertainty.aggregator import derive_all
from agents.uncertainty.temporal import derive_temporal
agg = derive_all(con)
tc = derive_temporal(con)
print(f"  Relationships updated: {agg.get('relationships_updated', 0)}")
print(f"  Temporal updated     : {tc}")


banner("STAGE 5: GRAPH")
from agents.graph.builder import build_graph
from agents.graph.persist import persist_graph
from agents.graph.metrics import compute_all_metrics
G = build_graph(con)
paths = persist_graph(G, run_id)
metrics = compute_all_metrics(G)
print(f"  Nodes                : {metrics['nodes']}")
print(f"  Edges                : {metrics['edges']}")
print(f"  Density              : {metrics.get('density', 0):.4f}")
print(f"  Components           : {metrics.get('connected_components', 'n/a')}")


banner("ALLOCATOR TYPE DISTRIBUTION")
rows = con.execute(
    "SELECT allocator_type, COUNT(*) as cnt FROM allocators GROUP BY allocator_type ORDER BY cnt DESC"
).fetchall()
for atype, cnt in rows:
    print(f"  {atype or 'NULL'!r:35s}: {cnt}")


banner("RELATIONSHIP EVIDENCE SUMMARY")
rev = con.execute("SELECT COUNT(*) FROM relationship_evidence").fetchone()[0]
rel = con.execute("SELECT COUNT(*) FROM relationships").fetchone()[0]
print(f"  relationship_evidence rows : {rev}")
print(f"  relationships rows         : {rel}")

rt = con.execute(
    "SELECT edge_type, COUNT(*) FROM relationships GROUP BY edge_type ORDER BY COUNT(*) DESC"
).fetchall()
for etype, cnt in rt:
    print(f"    {etype or 'NULL'!r:35s}: {cnt}")

print("\nDone.")

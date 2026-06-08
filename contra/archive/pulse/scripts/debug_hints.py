"""Debug which hints are produced and why they fail to persist."""
import sys, json, traceback
sys.stdout.reconfigure(encoding='utf-8')
ROOT = __import__('pathlib').Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agents.db import get_conn
from agents.ontology.pipeline import _load_known_entities, _load_documents, _build_extractor_chain
from agents.ontology.base import ExtractionContext

con = get_conn()

print("=== Known entities ===")
entities = _load_known_entities(con)
print(f"  {len(entities)} names")
for e in entities:
    print(f"    {e!r}")

print("\n=== Running extractor on first 50 docs ===")
docs = list(_load_documents(con))
print(f"  {len(docs)} total docs")

extractors = _build_extractor_chain()
import uuid as _uuid
run_id = str(_uuid.uuid4())

all_hints = []
for doc in docs:  # Process all docs
    for extractor in extractors:
        ctx = ExtractionContext(
            run_id=run_id,
            extractor_name=extractor.name,
            extractor_version=extractor.version,
            extra={"known_entities": entities},
        )
        try:
            result = extractor.extract(doc, ctx)
        except Exception:
            continue
        if result.relationship_hints:
            for hint in result.relationship_hints:
                all_hints.append({
                    "source_file": doc.source_file,
                    "source_type": doc.source_type,
                    "source_entity_name": hint.source_entity_name,
                    "target_entity_name": hint.target_entity_name,
                    "edge_type": hint.edge_type,
                    "confidence": hint.confidence,
                    "snippet": str(hint.provenance_pointer.get("matched_text", hint.provenance_pointer.get("sentence_snippet", "")))[:100],
                })

print(f"\n=== Found {len(all_hints)} hints ===")
for h in all_hints:
    print(f"  [{h['edge_type']}] {h['source_entity_name']!r} -> {h['target_entity_name']!r}")
    print(f"    conf={h['confidence']:.2f}  file={h['source_file']}  snippet={h['snippet']!r}")
    print()

print("\n=== Why each hint might fail to persist ===")
from agents.ontology.pipeline import _resolve_entity_id
for h in all_hints:
    src_id = _resolve_entity_id(con, h['source_entity_name']) if h['source_entity_name'] else None
    tgt_id = _resolve_entity_id(con, h['target_entity_name']) if h['target_entity_name'] else None
    print(f"  [{h['edge_type']}] src={h['source_entity_name']!r} -> src_id={src_id}")
    print(f"             tgt={h['target_entity_name']!r} -> tgt_id={tgt_id}")
    if not src_id:
        print(f"    FAIL: source entity could not be resolved")
    if not tgt_id:
        print(f"    FAIL: target entity could not be resolved")
    if src_id and tgt_id and src_id == tgt_id:
        print(f"    FAIL: self-loop (same entity)")
    print()

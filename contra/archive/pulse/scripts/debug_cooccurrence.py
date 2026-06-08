"""Debug: which source docs contain known LP names, and do any mention 2+ LPs?"""
import sys, json, re
sys.stdout.reconfigure(encoding='utf-8')
ROOT = __import__('pathlib').Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agents.db import get_conn
from agents.ontology.pipeline import _load_known_entities, _load_documents

con = get_conn()
entities = _load_known_entities(con)
docs = list(_load_documents(con))

print(f"Known entities: {len(entities)}")
print(f"Total docs: {len(docs)}")

# For each doc, find which known entities appear in its text
hits_by_source = {}
for doc in docs:
    text = doc.text or ""
    if not text.strip():
        continue
    found = [e for e in entities if re.search(r'\b' + re.escape(e) + r'\b', text, re.IGNORECASE)]
    if found:
        k = doc.source_file
        hits_by_source.setdefault(k, []).append({
            "offset": doc.source_offset,
            "found": found,
            "snippet": text[:200],
        })

print("\n=== Documents containing known LP names ===")
for src_file, hits in sorted(hits_by_source.items()):
    print(f"\n{src_file}: {len(hits)} docs with LP mentions")
    for h in hits[:5]:
        print(f"  offset={h['offset']}  found={h['found']}")
        print(f"  snippet={h['snippet']!r:.120}")

# Find docs with 2+ known entities = potential co-occurrence
print("\n=== Docs with 2+ entity co-mentions ===")
multi_count = 0
for doc in docs:
    text = doc.text or ""
    if not text.strip():
        continue
    found = [e for e in entities if re.search(r'\b' + re.escape(e) + r'\b', text, re.IGNORECASE)]
    if len(found) >= 2:
        multi_count += 1
        print(f"  {doc.source_file} @ {doc.source_offset}: {found}")
        print(f"  Text: {text[:200]!r}")
        print()

print(f"\nTotal docs with 2+ co-mentions: {multi_count}")

# Check campaign sheets - LPs in same sheet = co-campaign context
print("\n=== ICP campaign sheet LP groupings ===")
import duckdb
sheets = con.execute("""
    SELECT raw_content->>'_sheet' as sheet, COUNT(*) as cnt
    FROM entities_raw
    WHERE source_file = 'MyAsiaVC_ICP_4.0_Prospect_List_External.xlsx'
    GROUP BY sheet ORDER BY cnt DESC
""").fetchall()
for sheet, cnt in sheets:
    print(f"  sheet={sheet!r}: {cnt} rows")

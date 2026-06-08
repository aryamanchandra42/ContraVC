import sys, json
sys.stdout.reconfigure(encoding='utf-8')
import duckdb
con = duckdb.connect('pulse.duckdb')

print("=== Sample raw_content keys from entities_raw (xlsx) ===")
rows = con.execute(
    "SELECT source_file, raw_content FROM entities_raw WHERE source_type = 'xlsx' LIMIT 20"
).fetchall()
seen_files = set()
for sf, rc in rows:
    if isinstance(rc, str):
        try:
            rc = json.loads(rc)
        except Exception:
            rc = {}
    keys = [k for k in rc.keys() if not str(k).startswith('Unnamed')]
    if sf not in seen_files:
        print(f'\nFILE: {sf}')
        print(f'  KEYS: {keys}')
        # Show a row that might have type info
        t = rc.get('Type') or rc.get('type') or rc.get('Investor Type') or rc.get('investor type')
        n = rc.get('Investor Name') or rc.get('LP Name') or rc.get('Name')
        if t or n:
            print(f'  SAMPLE: name={n!r}, type={t!r}')
        seen_files.add(sf)

print("\n=== Allocator type distribution ===")
rows2 = con.execute(
    "SELECT allocator_type, COUNT(*) as cnt FROM allocators GROUP BY allocator_type ORDER BY cnt DESC"
).fetchall()
for r in rows2:
    print(f'  {r[0]!r}: {r[1]}')

print("\n=== entities_raw source_file distribution ===")
rows3 = con.execute(
    "SELECT source_file, source_type, COUNT(*) FROM entities_raw GROUP BY source_file, source_type ORDER BY source_file"
).fetchall()
for r in rows3:
    print(f'  {r[0]} ({r[1]}): {r[2]} rows')

print("\n=== Review queue aliases.jsonl ===")
from pathlib import Path
p = Path('processed_data/review_queues/aliases.jsonl')
if p.exists():
    lines = p.read_text(encoding='utf-8').strip().split('\n')
    print(f'  {len(lines)} items')
    for line in lines[:5]:
        item = json.loads(line)
        print(f'  {item}')
else:
    print('  File not found')

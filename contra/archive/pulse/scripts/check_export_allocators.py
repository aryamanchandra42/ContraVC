"""Check which allocators were created from export.csv and their ICP scores."""
import duckdb, json
from pathlib import Path

con = duckdb.connect(str(Path(__file__).parent.parent / "pulse.duckdb"))

# Find allocators whose canonical_name matches an investor from the CSV
csv_names = []
import csv
with open("raw_data/export.csv", newline="", encoding="utf-8-sig") as f:
    for row in csv.DictReader(f):
        name = row.get("Investor Name", "").strip()
        if name:
            csv_names.append(name)

print(f"Investors in CSV: {len(csv_names)}")
print()

# Check which ones became allocators
found = []
for name in csv_names:
    rows = con.execute(
        "SELECT CAST(allocator_id AS VARCHAR), canonical_name, allocator_type, population, geography FROM allocators WHERE canonical_name ILIKE ? OR canonical_name ILIKE ?",
        [name, f"%{name[:15]}%"]
    ).fetchall()
    for r in rows:
        found.append({"name": name, "canonical": r[1], "type": r[2], "pop": r[3], "geo": r[4]})

print(f"Matched to allocators: {len(found)}")
pops = {}
for f in found:
    pops[f['pop']] = pops.get(f['pop'], 0) + 1
print(f"Population breakdown: {pops}")
print()

# Check ICP scores for these allocators
print("=== ICP Scores for CSV investors ===")
for item in found:
    score_row = con.execute(
        "SELECT fit_score, tier, core_pass, excluded FROM icp_scores WHERE allocator_id IN (SELECT allocator_id FROM allocators WHERE canonical_name ILIKE ?) ORDER BY fit_score DESC LIMIT 1",
        [item["canonical"]]
    ).fetchone()
    if score_row:
        print(f"  [{score_row[1]}] {score_row[0]:.2f}  {item['canonical']} ({item['type']}) pop={item['pop']}")
    else:
        print(f"  [NOT SCORED] {item['canonical']} ({item['type']}) pop={item['pop']}")

con.close()

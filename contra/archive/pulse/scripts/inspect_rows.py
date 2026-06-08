import csv
from pathlib import Path

path = Path("raw_data/export.csv")
rows = {}
with open(path, newline="", encoding="utf-8-sig", errors="replace") as f:
    for row in csv.DictReader(f):
        rows[row["Investor Name"].strip()] = row

targets = ["VTS", "Acrew Capital", "Siddharth Mehta", "Alvin Tse",
           "Steve Mixtacki", "Aniruddha Nazre", "Krusen Capital"]
for name in targets:
    r = rows.get(name, {})
    t = r.get("Investor Type", "")
    loc = r.get("Investor Location", "")[:80]
    ind = r.get("Industry Focus", "")[:100]
    stage = r.get("Stage Focus", "")
    detail = r.get("Investor Details", "")[:250]
    inv = r.get("Relevant Investments", "")[:150]
    print(f"=== {name} ===")
    print(f"  Type: {t}")
    print(f"  Location: {loc}")
    print(f"  Industry: {ind}")
    print(f"  Stage: {stage}")
    print(f"  Investments: {inv}")
    print(f"  Details: {detail}")
    print()

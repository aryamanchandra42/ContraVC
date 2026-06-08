import csv
from pathlib import Path

path = Path("processed_data/First_LPs_Ready.csv")
rows = list(csv.DictReader(path.open(encoding="utf-8")))

ready = [r for r in rows if r["readiness"].startswith("READY")]
near = [r for r in rows if "NEAR-READY" in r["readiness"]]
inst_t1 = [r for r in ready if r["icp_tier"] == "tier_1" and "ICP" in r["data_source"]]

print("READY total:", len(ready))
print("READY Tier 1 institutional:", len(inst_t1))
print("NEAR-READY total:", len(near))
print()
print("--- TOP 20 READY Tier 1 (by fit) ---")
for r in sorted(inst_t1, key=lambda x: -float(x["fit_score"]))[:20]:
    conn = r.get("connectivity_score") or "-"
    print(f"  {float(r['fit_score']):.3f} | {r['allocator_type']:22} | {r['geography']:16} | conn={conn} | {r['lp_name']}")

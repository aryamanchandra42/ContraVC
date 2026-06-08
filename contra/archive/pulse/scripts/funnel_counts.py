import csv
from collections import Counter

import duckdb

con = duckdb.connect("pulse.duckdb")

print("=== RAW DATA VOLUME ===")
queries = [
    ("entities_raw rows", "SELECT COUNT(*) FROM entities_raw"),
    ("allocators total", "SELECT COUNT(*) FROM allocators"),
    ("institutional_prospect allocators", "SELECT COUNT(*) FROM allocators WHERE population = 'institutional_prospect'"),
    ("syndicate_lp allocators", "SELECT COUNT(*) FROM allocators WHERE population = 'syndicate_lp'"),
    ("icp_scores rows", "SELECT COUNT(*) FROM icp_scores"),
    ("relationships edges", "SELECT COUNT(*) FROM relationships"),
]
for label, sql in queries:
    print(f"  {label}: {con.execute(sql).fetchone()[0]}")

print("\n=== INSTITUTIONAL ICP — BY TIER (xlsx prospects, scored once each) ===")
for tier, n in con.execute("""
    SELECT i.tier, COUNT(*)
    FROM icp_scores i
    JOIN allocators a ON CAST(a.allocator_id AS VARCHAR) = i.allocator_id
    WHERE COALESCE(a.population, '') = 'institutional_prospect'
    GROUP BY i.tier ORDER BY i.tier
""").fetchall():
    print(f"  {tier}: {n}")

print("\n=== INSTITUTIONAL — CLIENT DECISION ===")
for dec, n in con.execute("""
    SELECT COALESCE(i.client_decision, '(none)'), COUNT(*)
    FROM icp_scores i
    JOIN allocators a ON CAST(a.allocator_id AS VARCHAR) = i.allocator_id
    WHERE COALESCE(a.population, '') = 'institutional_prospect'
    GROUP BY 1 ORDER BY 2 DESC
""").fetchall():
    print(f"  {dec}: {n}")

print("\n=== OUTREACH PACK ELIGIBLE ===")
t1_approved = con.execute("""
    SELECT COUNT(*) FROM icp_scores i
    JOIN allocators a ON CAST(a.allocator_id AS VARCHAR) = i.allocator_id
    WHERE i.tier = 'tier_1'
      AND i.client_decision = 'approved'
      AND COALESCE(a.population, '') = 'institutional_prospect'
""").fetchone()[0]
t1_all = con.execute("""
    SELECT COUNT(*) FROM icp_scores i
    JOIN allocators a ON CAST(a.allocator_id AS VARCHAR) = i.allocator_id
    WHERE i.tier = 'tier_1'
      AND COALESCE(a.population, '') = 'institutional_prospect'
""").fetchone()[0]
print(f"  Tier 1 total (institutional): {t1_all}")
print(f"  Tier 1 + client approved: {t1_approved}")

print("\n=== EXCLUDED / TIER 4 (institutional) ===")
excl = con.execute("""
    SELECT COUNT(*) FROM icp_scores i
    JOIN allocators a ON CAST(a.allocator_id AS VARCHAR) = i.allocator_id
    WHERE i.excluded = true
      AND COALESCE(a.population, '') = 'institutional_prospect'
""").fetchone()[0]
t4 = con.execute("""
    SELECT COUNT(*) FROM icp_scores i
    JOIN allocators a ON CAST(a.allocator_id AS VARCHAR) = i.allocator_id
    WHERE i.tier = 'tier_4'
      AND COALESCE(a.population, '') = 'institutional_prospect'
""").fetchone()[0]
print(f"  excluded flag: {excl}")
print(f"  tier_4: {t4}")

print("\n=== SYNDICATE LPs (not in ICP outreach list) ===")
n_syn = con.execute("SELECT COUNT(*) FROM allocators WHERE population = 'syndicate_lp'").fetchone()[0]
print(f"  syndicate_lp allocators: {n_syn}")

c = Counter()
with open("processed_data/LP_Ranked_List.csv", encoding="utf-8") as f:
    for row in csv.DictReader(f):
        c[row["Tier"]] += 1
        if row["Tier"] == "tier_1" and "approved" in (row.get("Decision") or "").lower():
            c["tier_1_approved_campaign"] += 1
print("\n=== LP_Ranked_List.csv (institutional prospects ranked) ===")
for k in ["tier_1", "tier_2", "tier_3", "tier_4", "tier_1_approved_campaign"]:
    if k in c:
        print(f"  {k}: {c[k]}")

print("\n=== icp_scores dedup check ===")
print(f"  total icp_scores rows: {con.execute('SELECT COUNT(*) FROM icp_scores').fetchone()[0]}")
print(f"  unique allocators scored: {con.execute('SELECT COUNT(DISTINCT allocator_id) FROM icp_scores').fetchone()[0]}")
t1u = con.execute("""
    SELECT COUNT(DISTINCT i.allocator_id) FROM icp_scores i
    JOIN allocators a ON CAST(a.allocator_id AS VARCHAR) = i.allocator_id
    WHERE i.tier = 'tier_1' AND a.population = 'institutional_prospect'
""").fetchone()[0]
print(f"  tier_1 unique (institutional): {t1u}")

con.close()

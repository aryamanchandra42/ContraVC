"""
One-time migration: copy all tables from local contra.duckdb → MotherDuck.

Usage:
    set MOTHERDUCK_TOKEN=<your token>
    python scripts/migrate_to_motherduck.py

Requires the motherduck extension (auto-loaded by duckdb >= 0.10 when connecting to md:).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parent.parent
LOCAL_DB = ROOT / "contra.duckdb"

token = os.getenv("MOTHERDUCK_TOKEN", "").strip()
if not token:
    print("ERROR: MOTHERDUCK_TOKEN env var is not set.")
    sys.exit(1)

if not LOCAL_DB.exists():
    print(f"ERROR: Local DB not found at {LOCAL_DB}")
    sys.exit(1)

print(f"Connecting to local DB: {LOCAL_DB}")
local = duckdb.connect(str(LOCAL_DB), read_only=True)

tables = [r[0] for r in local.execute(
    "SELECT table_name FROM information_schema.tables WHERE table_schema = 'main' AND table_type = 'BASE TABLE'"
).fetchall()]

print(f"Found {len(tables)} tables: {tables}")

print("Connecting to MotherDuck …")
cloud = duckdb.connect("md:")

print("Creating database 'contra' on MotherDuck (if not exists) …")
cloud.execute("CREATE DATABASE IF NOT EXISTS contra")
cloud.execute("USE contra")

print("Attaching local DB inside cloud connection …")
cloud.execute(f"ATTACH '{LOCAL_DB}' AS local_db (READ_ONLY)")

for table in tables:
    print(f"  Copying {table} …", end=" ", flush=True)
    cloud.execute(f"CREATE OR REPLACE TABLE main.{table} AS SELECT * FROM local_db.main.{table}")
    count = cloud.execute(f"SELECT COUNT(*) FROM main.{table}").fetchone()[0]
    print(f"{count} rows")

cloud.execute("DETACH local_db")
local.close()
cloud.close()

print("\nDone. All tables copied to MotherDuck (md:contra).")
print("Set MOTHERDUCK_TOKEN on Render and redeploy.")

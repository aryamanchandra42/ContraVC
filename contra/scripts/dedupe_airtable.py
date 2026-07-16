"""
Remove duplicate rows from Contra CRM Airtable tables.

Keeps the newest record (by createdTime) for each unique key:
  - LP Leads         -> Investor Name
  - Outreach Drafts  -> Investor Name
  - LP Dossier       -> Name Key (fallback: Investor Name)

Usage:
    cd contra
    python scripts/dedupe_airtable.py --dry-run   # preview only
    python scripts/dedupe_airtable.py             # delete duplicates
"""

from __future__ import annotations

import argparse
import os
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except ImportError:
    pass

from pyairtable import Api


def _table(api: Api, env_var: str, default: str):
    return api.table(os.environ["AIRTABLE_BASE_ID"], os.environ.get(env_var, default))


def dedupe_table(table, key_field: str, dry_run: bool) -> tuple[int, int]:
    """Return (kept, deleted) counts."""
    records = table.all()
    groups: dict[str, list] = defaultdict(list)
    for rec in records:
        key = (rec.get("fields") or {}).get(key_field, "")
        if not key:
            key = f"__missing__:{rec['id']}"
        groups[str(key)].append(rec)

    kept = 0
    deleted = 0
    for key, recs in groups.items():
        if len(recs) == 1:
            kept += 1
            continue
        recs.sort(key=lambda r: r.get("createdTime", ""), reverse=True)
        keep = recs[0]
        dupes = recs[1:]
        kept += 1
        print(f"  {key_field}={key!r}: keep 1, delete {len(dupes)}")
        for rec in dupes:
            if dry_run:
                print(f"    [dry-run] would delete {rec['id']}")
            else:
                table.delete(rec["id"])
            deleted += 1
    return kept, deleted


def main() -> None:
    parser = argparse.ArgumentParser(description="Remove duplicate Airtable CRM rows")
    parser.add_argument("--dry-run", action="store_true", help="Preview without deleting")
    args = parser.parse_args()

    if not os.environ.get("AIRTABLE_API_KEY") or not os.environ.get("AIRTABLE_BASE_ID"):
        sys.exit("Set AIRTABLE_API_KEY and AIRTABLE_BASE_ID in .env")

    api = Api(os.environ["AIRTABLE_API_KEY"])
    mode = "DRY RUN" if args.dry_run else "DELETE"
    print(f"Airtable dedupe ({mode})\n")

    specs = [
        ("LP Leads", "AIRTABLE_LEADS_TABLE", "LP Leads", "Investor Name"),
        ("Outreach Drafts", "AIRTABLE_DRAFTS_TABLE", "Outreach Drafts", "Investor Name"),
        ("LP Dossier", "AIRTABLE_DOSSIERS_TABLE", "LP Dossier", "Name Key"),
    ]

    total_deleted = 0
    for label, env_var, default, key_field in specs:
        print(f"{label} (key={key_field}):")
        table = _table(api, env_var, default)
        kept, deleted = dedupe_table(table, key_field, args.dry_run)
        print(f"  -> {kept} unique, {deleted} duplicates\n")
        total_deleted += deleted

    print(f"Done. {'Would delete' if args.dry_run else 'Deleted'} {total_deleted} duplicate rows.")


if __name__ == "__main__":
    main()

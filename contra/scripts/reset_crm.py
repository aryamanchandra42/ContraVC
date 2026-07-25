"""
Wipe the CRM and the prospector queue so the cascade starts from a clean slate.

Run:  python scripts/reset_crm.py --yes
      python scripts/reset_crm.py --yes --keep-outreach-log
      python scripts/reset_crm.py            # dry run: shows counts, changes nothing

WHAT IS CLEARED
    crm_leads             the leads themselves
    crm_gate_reviews      cached gate verdicts (would otherwise suppress re-screening)
    crm_dismissed         dismissals (would otherwise block re-mining)
    lead_scorecards       per-lead scorecards
    lp_dossiers           per-lead research dossiers
    crm_outreach_drafts   drafted emails, which reference lead_ids that will not exist
    prospector_candidates the mining queue, including its rejection history
    prospector_runs       the mining audit log

WHAT IS PRESERVED, AND WHY
    allocators, funds, signals, icp_scores, interactions
        The owned dataset. This is the asset the whole system is built on and it
        is not regenerable from the web — deleting it would be unrecoverable.
    prospector_seeds
        The mining frontier, including confirmed_lp and peer_fund seeds learned
        from earlier runs. Wiping these would throw away accumulated discovery
        surface and make the next run re-tread ground it already covered.
    outreach_log
        The record of who we have already emailed. Clearing it risks contacting
        someone twice, which is a real-world cost that cannot be undone. Pass
        --clear-outreach-log if you truly want it gone.

Every table is TRUNCATEd inside a single transaction, so a failure part-way
leaves the database as it was rather than half-wiped.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Dict, List

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Order matters only for readability; these have no enforced FKs between them.
CLEAR_TABLES: List[str] = [
    "crm_outreach_drafts",
    "lead_scorecards",
    "lp_dossiers",
    "crm_gate_reviews",
    "crm_dismissed",
    "crm_leads",
    "prospector_candidates",
    "prospector_runs",
]

PRESERVE_TABLES: List[str] = [
    "allocators",
    "funds",
    "signals",
    "icp_scores",
    "interactions",
    "prospector_seeds",
    "outreach_log",
]


def _table_exists(con, name: str) -> bool:
    row = con.execute(
        "SELECT 1 FROM information_schema.tables WHERE table_name = ?", [name]
    ).fetchone()
    return row is not None


def _count(con, name: str) -> int:
    if not _table_exists(con, name):
        return -1
    try:
        return int(con.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0])
    except Exception:
        return -1


def _report(con, tables: List[str], heading: str) -> Dict[str, int]:
    counts = {t: _count(con, t) for t in tables}
    print(f"  {heading}")
    for table, n in counts.items():
        label = "missing" if n < 0 else f"{n:>7,} rows"
        print(f"    {table:24} {label}")
    return counts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=os.environ.get("PULSE_DB", "contra.duckdb"))
    parser.add_argument("--yes", action="store_true",
                        help="actually perform the reset (otherwise dry run)")
    parser.add_argument("--clear-outreach-log", action="store_true",
                        help="also wipe outreach_log (risks contacting people twice)")
    args = parser.parse_args()

    db_path = args.db if os.path.isabs(args.db) else str(ROOT / args.db)
    if not os.path.exists(db_path):
        print(f"Database not found: {db_path}", file=sys.stderr)
        return 1

    import duckdb

    to_clear = list(CLEAR_TABLES)
    preserve = list(PRESERVE_TABLES)
    if args.clear_outreach_log:
        to_clear.append("outreach_log")
        preserve.remove("outreach_log")

    con = duckdb.connect(db_path)
    try:
        print(f"Database: {db_path}")
        print()
        before = _report(con, to_clear, "TO BE CLEARED:")
        print()
        _report(con, preserve, "PRESERVED:")
        print()

        total = sum(n for n in before.values() if n > 0)
        if not args.yes:
            print(f"DRY RUN — {total:,} rows would be deleted. Re-run with --yes.")
            return 0

        con.execute("BEGIN TRANSACTION")
        try:
            cleared = 0
            for table in to_clear:
                if not _table_exists(con, table):
                    print(f"  skip     {table} (missing)")
                    continue
                con.execute(f"TRUNCATE {table}")
                cleared += max(0, before.get(table, 0))
                print(f"  cleared  {table}")
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise

        print()
        print(f"Reset complete — {cleared:,} rows deleted.")
        remaining = {t: _count(con, t) for t in to_clear if _table_exists(con, t)}
        stragglers = {t: n for t, n in remaining.items() if n > 0}
        if stragglers:
            print(f"WARNING: rows remain in {stragglers}", file=sys.stderr)
            return 1
        print("Next: run the miner (POST /prospector/run) to repopulate from scratch.")
        return 0
    finally:
        con.close()


if __name__ == "__main__":
    raise SystemExit(main())

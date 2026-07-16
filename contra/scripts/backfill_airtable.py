"""
Backfill Airtable with all existing CRM data.

For every lead in the database:
  1. Push the lead row to Airtable "LP Leads"
  2. Push their dossier to Airtable "LP Dossier"
  3. Generate an outreach email (if none exists yet) and push to "Outreach Drafts"

Usage:
    cd contra
    python scripts/backfill_airtable.py              # generate emails for all leads
    python scripts/backfill_airtable.py --leads-only # push leads + dossiers, skip email gen
    python scripts/backfill_airtable.py --limit 10   # process first 10 leads only

Leads that already have a draft in the DB are skipped for email generation.
Leads with no gate research are skipped (email cannot be personalized).
All Airtable pushes are non-blocking — failures are logged, not raised.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from pathlib import Path

# ── bootstrap ────────────────────────────────────────────────────────────────
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except ImportError:
    pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)


# ── helpers ───────────────────────────────────────────────────────────────────

def _row_to_dict(cursor, row) -> dict:
    cols = [d[0].lower() for d in cursor.description]
    return dict(zip(cols, row))


def fetch_all_leads(con) -> list[dict]:
    cursor = con.execute(
        """
        SELECT
            CAST(lead_id AS VARCHAR)      AS lead_id,
            investor_name,
            investor_type,
            investor_location,
            investor_details,
            CAST(allocator_id AS VARCHAR) AS allocator_id,
            pipeline_stage,
            status,
            gate_verdict,
            gate_confidence,
            gate_summary,
            icp_tier,
            fit_score,
            computed_score,
            needs_enrichment
        FROM crm_leads
        ORDER BY created_at DESC
        """
    )
    rows = cursor.fetchall()
    return [_row_to_dict(cursor, r) for r in rows]


def fetch_dossier(con, investor_name: str) -> dict | None:
    from contra.crm.dossier import get_dossier
    try:
        return get_dossier(con, investor_name)
    except Exception:
        return None


# ── main ─────────────────────────────────────────────────────────────────────

def run(leads_only: bool = False, limit: int | None = None) -> None:
    from agents.db import get_conn
    from contra.crm import airtable_sync
    from contra.crm.outreach import generate_outreach_draft

    con = get_conn()

    logger.info("Fetching all CRM leads...")
    leads = fetch_all_leads(con)
    if not leads:
        logger.info("No leads found in crm_leads. Nothing to backfill.")
        return

    if limit:
        leads = leads[:limit]

    total = len(leads)
    logger.info(f"Found {total} leads. Starting backfill...")
    print()

    skipped_intel = 0
    generated      = 0
    failed         = 0
    pushed_leads   = 0
    pushed_dossiers = 0

    for idx, lead in enumerate(leads, 1):
        name = lead.get("investor_name", "?")
        lead_id = lead.get("lead_id", "")
        prefix = f"[{idx:>3}/{total}]  {name}"

        # ── 1. Push lead row ────────────────────────────────────────────────
        airtable_sync.push_lead(lead)
        pushed_leads += 1

        # ── 2. Push dossier ────────────────────────────────────────────────
        dossier = fetch_dossier(con, name)
        if dossier:
            airtable_sync.push_dossier(dossier)
            pushed_dossiers += 1

        if leads_only:
            logger.info(f"{prefix}  [pushed]")
            continue

        # ── 3. Generate a fresh email (sync pushes latest draft to Airtable) ──
        logger.info(f"{prefix}  generating email...")
        try:
            result = generate_outreach_draft(con, lead_id)
            if result.get("error") == "insufficient_intel":
                logger.info(f"{prefix}  [skip — {result['message'][:80]}]")
                skipped_intel += 1
            else:
                logger.info(
                    f"{prefix}  [OK]  subject: {result.get('subject', '')[:60]}"
                )
                generated += 1
            # Small pause to avoid hammering the LLM
            time.sleep(1.5)
        except Exception as exc:
            logger.warning(f"{prefix}  [ERROR]  {exc}")
            failed += 1

    # Wait for background Airtable pushes to settle
    logger.info("\nWaiting for Airtable sync threads to finish...")
    time.sleep(5)

    print()
    print("=" * 60)
    print(f"  Leads pushed to Airtable    : {pushed_leads}")
    print(f"  Dossiers pushed             : {pushed_dossiers}")
    if not leads_only:
        print(f"  Emails generated            : {generated}")
        print(f"  Skipped (no intel)          : {skipped_intel}")
        print(f"  Failed                      : {failed}")
    print("=" * 60)
    print()


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Backfill Airtable from existing CRM data")
    parser.add_argument(
        "--leads-only",
        action="store_true",
        help="Only push leads + dossiers; skip email generation",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        metavar="N",
        help="Process only the first N leads (useful for testing)",
    )
    args = parser.parse_args()

    if not os.environ.get("AIRTABLE_API_KEY") or not os.environ.get("AIRTABLE_BASE_ID"):
        sys.exit(
            "ERROR: AIRTABLE_API_KEY and AIRTABLE_BASE_ID must be set in .env\n"
            "Run scripts/setup_airtable.py first if you haven't already."
        )

    run(leads_only=args.leads_only, limit=args.limit)

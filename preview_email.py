"""
Quick email preview — generates ONE draft for the highest-ranked active lead
and prints the subject + opening paragraph so you can judge quality without
running the full server.

Usage:
  python preview_email.py                   # top-ranked lead
  python preview_email.py --name "Acme"     # partial name match
  python preview_email.py --id <lead_id>    # specific lead
  python preview_email.py --all             # regenerate + print first 5 leads
"""

import os
import sys
import argparse
import json
from pathlib import Path
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).parent / "contra"))
load_dotenv(Path(__file__).parent / "contra" / ".env")

from agents.db import get_conn
from contra.crm.outreach import generate_outreach_draft


DIVIDER = "-" * 70


def print_draft(draft: dict) -> None:
    if not draft:
        print("Draft is None.")
        return
        
    fmt_letter = draft.get("subject_format") or "?"
    points = draft.get("personalization_points") or []
    print(f"\n{DIVIDER}")
    print(f"  LP:            {draft['investor_name']}")
    print(f"  Subject fmt:   {fmt_letter}")
    print(f"  Subject:       {draft['subject']}")
    print(f"{DIVIDER}")
    # Print only the opening paragraph (up to the factsheet line) for a quick sanity check
    body: str = draft.get("body", "")
    if not body:
        print(f"\n  BODY IS EMPTY OR NOT FOUND IN DRAFT.")
        return
        
    factsheet_marker = "Our Fund I factsheet is here"
    split_idx = body.find(factsheet_marker)
    if split_idx != -1:
        opening = body[:split_idx].strip()
        newline_idx = body.find("\n", split_idx)
        factsheet_line = body[split_idx:newline_idx if newline_idx != -1 else len(body)].strip()
        print(f"\n  BODY:\n{opening}\n")
        print(f"  CTA: {factsheet_line}")
    else:
        print(f"\n  BODY (first 600 chars):\n{body[:600]}")
    print(f"\n  PERSONALIZATION HOOKS:")
    for p in points:
        print(f"    • {p}")
    print(f"{DIVIDER}\n")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--id", help="Specific lead_id")
    parser.add_argument("--name", help="Partial name match (case-insensitive)")
    parser.add_argument("--all", action="store_true", help="Generate for top 5 leads")
    parser.add_argument("--sender", default="Alex", help="Sender name (default: Alex)")
    parser.add_argument("--tone", default="warm", help="Tone of the email (default: warm)")
    args = parser.parse_args()

    con = get_conn(read_only=False)

    if args.id:
        leads = [(args.id, None)]
    elif args.name:
        rows = con.execute(
            "SELECT lead_id, investor_name FROM crm_leads "
            "WHERE status='active' AND investor_name ILIKE ? LIMIT 5",
            [f"%{args.name}%"],
        ).fetchall()
        if not rows:
            print(f"No active leads matching '{args.name}'")
            return
        leads = [(str(r[0]), r[1]) for r in rows]
    else:
        n = 5 if args.all else 1
        rows = con.execute(
            "SELECT lead_id, investor_name FROM crm_leads "
            "WHERE status='active' "
            "ORDER BY COALESCE(manual_rank, 9999) ASC, computed_score DESC NULLS LAST "
            f"LIMIT {n}"
        ).fetchall()
        if not rows:
            print("No active leads found.")
            return
        leads = [(str(r[0]), r[1]) for r in rows]

    print(f"\nGenerating {'draft' if len(leads)==1 else f'{len(leads)} drafts'} with new prompt...\n")
    for lead_id, name in leads:
        label = name or lead_id
        print(f"  Generating for {label}...", end=" ", flush=True)
    try:
        draft = generate_outreach_draft(
            con, lead_id=lead_id, tone=args.tone, sender_name=args.sender
        )
        print("done.")
        print_draft(draft)
    except Exception as e:
        import traceback
        print(f"FAILED: {e}")
        traceback.print_exc()


if __name__ == "__main__":
    main()

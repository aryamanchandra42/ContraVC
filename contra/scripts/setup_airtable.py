"""
One-time Airtable schema setup script.

Creates all required fields in the three Contra CRM tables:
  - LP Leads
  - Outreach Drafts
  - LP Dossiers

Run once after creating the base:
    python contra/scripts/setup_airtable.py

Requires:
    AIRTABLE_API_KEY=pat...
    AIRTABLE_BASE_ID=app...

Add schema:bases:write to your Personal Access Token scope if you haven't already.
The script skips fields that already exist, so it's safe to re-run.
"""

from __future__ import annotations

import os
import sys
import json
from pathlib import Path

# Load .env if present
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except ImportError:
    pass

try:
    import requests
except ImportError:
    sys.exit("requests is not installed. Run: pip install requests")


API_KEY  = os.environ.get("AIRTABLE_API_KEY", "").strip()
BASE_ID  = os.environ.get("AIRTABLE_BASE_ID", "").strip()

LEADS_TABLE    = os.environ.get("AIRTABLE_LEADS_TABLE",    "LP Leads")
DRAFTS_TABLE   = os.environ.get("AIRTABLE_DRAFTS_TABLE",   "Outreach Drafts")
DOSSIERS_TABLE = os.environ.get("AIRTABLE_DOSSIERS_TABLE", "LP Dossiers")

if not API_KEY or not BASE_ID:
    sys.exit(
        "ERROR: Set AIRTABLE_API_KEY and AIRTABLE_BASE_ID in your .env or environment.\n"
        "  AIRTABLE_API_KEY=pat...\n"
        "  AIRTABLE_BASE_ID=app..."
    )

HEADERS = {
    "Authorization": f"Bearer {API_KEY}",
    "Content-Type": "application/json",
}


# ─────────────────────────────────────────────────────────────────────────────
# Field definitions per table
# ─────────────────────────────────────────────────────────────────────────────

# fmt: off
LEADS_FIELDS = [
    # name, type, [options]
    ("Lead ID",               "singleLineText",   None),
    ("Investor Type",         "singleSelect",     {"choices": [
        {"name": "fund_of_funds"}, {"name": "family_office"}, {"name": "founder_lp"},
        {"name": "corporate_investor"}, {"name": "endowment"}, {"name": "pension"},
        {"name": "insurance"}, {"name": "sovereign_wealth"}, {"name": "other"},
    ]}),
    ("Location",              "singleLineText",   None),
    ("Pipeline Stage",        "singleSelect",     {"choices": [
        {"name": "Prospect", "color": "blueLight2"},
        {"name": "Outreach Sent", "color": "yellowLight2"},
        {"name": "Replied", "color": "orangeLight2"},
        {"name": "Closed", "color": "greenLight2"},
    ]}),
    ("Status",                "singleSelect",     {"choices": [
        {"name": "active", "color": "greenLight2"},
        {"name": "contacted", "color": "blueLight2"},
        {"name": "excluded", "color": "redLight2"},
        {"name": "paused", "color": "grayLight2"},
    ]}),
    ("Gate Verdict",          "singleSelect",     {"choices": [
        {"name": "yes", "color": "greenLight2"},
        {"name": "review", "color": "yellowLight2"},
        {"name": "no", "color": "redLight2"},
    ]}),
    ("Gate Confidence",       "singleSelect",     {"choices": [
        {"name": "high"}, {"name": "medium"}, {"name": "low"},
    ]}),
    ("Gate Summary",          "multilineText",    None),
    ("ICP Tier",              "singleLineText",   None),
    ("Fit Score",             "number",           {"precision": 1}),
    ("Computed Score",        "number",           {"precision": 1}),
    ("Contact Email",         "email",            None),
    ("Latest Email Subject",  "singleLineText",   None),
    ("Latest Email Body",     "multilineText",    None),
    ("Last Outreach At",      "date",             {"dateFormat": {"name": "iso"}}),
    ("Needs Enrichment",      "checkbox",         {"icon": "check", "color": "yellowBright"}),
]

DRAFTS_FIELDS = [
    ("Draft ID",              "singleLineText",   None),
    ("Investor Name",         "singleLineText",   None),
    ("Subject",               "singleLineText",   None),
    ("Body",                  "multilineText",    None),
    ("Status",                "singleSelect",     {"choices": [
        {"name": "draft", "color": "grayLight2"},
        {"name": "approved", "color": "blueLight2"},
        {"name": "sent", "color": "greenLight2"},
        {"name": "discarded", "color": "redLight2"},
    ]}),
    ("Tone",                  "singleLineText",   None),
    ("Archetype",             "singleLineText",   None),
    ("Model",                 "singleLineText",   None),
    ("Deep Research Used",    "checkbox",         {"icon": "check", "color": "cyanBright"}),
    ("Personalization Points","multilineText",    None),
]

DOSSIERS_FIELDS = [
    ("Name Key",              "singleLineText",   None),
    ("Investor Name",         "singleLineText",   None),
    ("Latest Verdict",        "singleSelect",     {"choices": [
        {"name": "yes", "color": "greenLight2"},
        {"name": "review", "color": "yellowLight2"},
        {"name": "no", "color": "redLight2"},
    ]}),
    ("LP Commitments",        "multilineText",    None),
    ("Appetite",              "multilineText",    None),
    ("Sources",               "multilineText",    None),
    ("Research Notes",        "multilineText",    None),
    ("Analyst Notes",         "multilineText",    None),
    ("Outreach Summary",      "multilineText",    None),
    ("Rejection Reason",      "singleSelect",     {"choices": [
        {"name": "fund_size"},    {"name": "geo_mandate"},
        {"name": "deployment_pause"}, {"name": "placement_agent"}, {"name": "other"},
    ]}),
    ("Revisit Date",          "date",             {"dateFormat": {"name": "iso"}}),
]
# fmt: on


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def get_tables() -> dict[str, str]:
    """Return {table_name: table_id} for all tables in the base."""
    url = f"https://api.airtable.com/v0/meta/bases/{BASE_ID}/tables"
    resp = requests.get(url, headers=HEADERS, timeout=15)
    if resp.status_code == 403:
        sys.exit(
            "ERROR 403: Your token doesn't have schema:bases:write scope.\n"
            "Go to airtable.com/create/tokens, edit your token, and add:\n"
            "  schema:bases:read\n"
            "  schema:bases:write"
        )
    resp.raise_for_status()
    return {t["name"]: t["id"] for t in resp.json().get("tables", [])}


def existing_field_names(table_id: str) -> set[str]:
    """Return the set of field names that already exist in the table."""
    url = f"https://api.airtable.com/v0/meta/bases/{BASE_ID}/tables"
    resp = requests.get(url, headers=HEADERS, timeout=15)
    resp.raise_for_status()
    for t in resp.json().get("tables", []):
        if t["id"] == table_id:
            return {f["name"] for f in t.get("fields", [])}
    return set()


def create_field(table_id: str, name: str, field_type: str, options: dict | None) -> bool:
    """Create one field. Returns True on success, False if already exists."""
    url = f"https://api.airtable.com/v0/meta/bases/{BASE_ID}/tables/{table_id}/fields"
    body: dict = {"name": name, "type": field_type}
    if options:
        body["options"] = options
    resp = requests.post(url, headers=HEADERS, json=body, timeout=15)
    if resp.status_code == 422:
        data = resp.json()
        if "already exists" in str(data).lower():
            return False  # already there — not an error
        print(f"    WARN 422 for '{name}': {data}")
        return False
    if not resp.ok:
        print(f"    ERROR {resp.status_code} for '{name}': {resp.text[:200]}")
        return False
    return True


def setup_table(table_name: str, table_id: str, field_defs: list) -> None:
    print(f"\n{'-'*60}")
    print(f"Table: {table_name}  ({table_id})")
    print(f"{'-'*60}")
    existing = existing_field_names(table_id)
    created = skipped = errors = 0
    for name, ftype, options in field_defs:
        if name in existing:
            print(f"  [skip]   {name}  (already exists)")
            skipped += 1
            continue
        ok = create_field(table_id, name, ftype, options)
        if ok:
            print(f"  [create] {name}  [{ftype}]")
            created += 1
        else:
            errors += 1
    print(f"\n  -> {created} created, {skipped} skipped, {errors} errors")


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    print(f"Connecting to base {BASE_ID} …")
    tables = get_tables()
    print(f"Found tables: {list(tables.keys())}")

    missing = [t for t in [LEADS_TABLE, DRAFTS_TABLE, DOSSIERS_TABLE] if t not in tables]
    if missing:
        sys.exit(
            f"ERROR: Tables not found in base: {missing}\n"
            f"Create them first with exactly these names:\n"
            f"  - {LEADS_TABLE}\n  - {DRAFTS_TABLE}\n  - {DOSSIERS_TABLE}"
        )

    setup_table(LEADS_TABLE,    tables[LEADS_TABLE],    LEADS_FIELDS)
    setup_table(DRAFTS_TABLE,   tables[DRAFTS_TABLE],   DRAFTS_FIELDS)
    setup_table(DOSSIERS_TABLE, tables[DOSSIERS_TABLE], DOSSIERS_FIELDS)

    print(f"\n{'='*60}")
    print("Done. Your Airtable base is ready to receive data from Contra.")
    print("Make sure your token also has data:records:write scope.")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()

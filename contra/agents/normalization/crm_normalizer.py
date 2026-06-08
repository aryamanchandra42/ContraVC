"""Ingest FundingStack CRM export.csv into crm_contacts."""

from __future__ import annotations

import json
import re
import uuid
from pathlib import Path
from typing import Any, Dict, List

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent.parent
RAW = ROOT / "raw_data"
CRM_FILE = "export.csv"

_LEGAL_SUFFIX_RE = re.compile(
    r"\s+(ltd|limited|llc|inc|corp|plc|pte|sa|bv|gmbh|lp|llp|co)\.?$", re.IGNORECASE
)


def norm_key(name: str) -> str:
    key = (name or "").strip().lower()
    key = _LEGAL_SUFFIX_RE.sub("", key).strip()
    key = re.sub(r"[^a-z0-9]", "", key)
    return key


def _contact_cols(df: pd.DataFrame) -> Dict[str, Any]:
    contacts: Dict[str, Any] = {}
    for i in (1, 2, 3):
        prefix = f"{i}{'st' if i == 1 else 'nd' if i == 2 else 'rd'} Contact Person"
        block = {}
        for field in ("Name", "Email", "Phone", "Position", "LinkedIn"):
            col = f"{prefix} {field}"
            if col in df.columns:
                block[field.lower()] = None
        if block:
            contacts[f"contact_{i}"] = block
    return contacts


def ingest_crm_contacts(con, raw_dir: Path | None = None) -> Dict[str, int]:
    path = (raw_dir or RAW) / CRM_FILE
    if not path.exists():
        return {"rows": 0, "skipped": True}

    df = pd.read_csv(path)
    if "Investor Name" not in df.columns:
        return {"rows": 0, "error": "missing Investor Name column"}

    con.execute("DELETE FROM crm_contacts WHERE source_file = ?", [CRM_FILE])
    batch: List[tuple] = []

    for _, row in df.iterrows():
        name = str(row.get("Investor Name") or "").strip()
        if not name or name.lower() == "investor name":
            continue
        contacts_json = {}
        for i in (1, 2, 3):
            ord_ = "st" if i == 1 else "nd" if i == 2 else "rd"
            prefix = f"{i}{ord_} Contact Person"
            block = {}
            for field in ("Name", "Email", "Phone", "Position", "LinkedIn"):
                col = f"{prefix} {field}"
                val = row.get(col)
                if pd.notna(val) and str(val).strip():
                    block[field.lower()] = str(val).strip()
            if block:
                contacts_json[f"contact_{i}"] = block

        batch.append((
            str(uuid.uuid4()),
            name,
            norm_key(name),
            str(row.get("Investor Type") or "").strip() or None,
            str(row.get("Investor Location") or "").strip() or None,
            str(row.get("Investor Details") or "").strip() or None,
            json.dumps(contacts_json) if contacts_json else None,
            str(row.get("Stage") or row.get("Review") or "").strip() or None,
            CRM_FILE,
        ))

    if batch:
        con.executemany(
            """
            INSERT INTO crm_contacts (
                contact_id, investor_name, name_key, investor_type,
                investor_location, investor_details, contacts_json,
                crm_status, source_file
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            batch,
        )

    result = {"rows": len(batch)}

    try:
        from contra.crm.writer import sync_import_to_leads
        result["leads_synced"] = sync_import_to_leads(con)
    except Exception:
        result["leads_synced"] = 0

    return result

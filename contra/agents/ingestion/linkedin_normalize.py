"""
Shared LinkedIn row normalization — used by both the CSV adapter and
the Phantombuster API sync to produce a uniform _li_* field set.
"""

from __future__ import annotations

from typing import Optional

# Column aliases: map common CSV / JSON key variants → internal _li_* names.
# Keys are lowercased before lookup; values are the canonical internal names.
COLUMN_ALIASES: dict[str, str] = {
    "profileurl": "_li_profile_url",
    "linkedinurl": "_li_profile_url",
    "linkedin profile url": "_li_profile_url",
    "profile url": "_li_profile_url",
    "linkedin url": "_li_profile_url",
    "firstname": "_li_first_name",
    "first name": "_li_first_name",
    "lastname": "_li_last_name",
    "last name": "_li_last_name",
    "fullname": "_li_full_name",
    "full name": "_li_full_name",
    "name": "_li_full_name",
    "companyname": "_li_company",
    "company name": "_li_company",
    "company": "_li_company",
    "current company": "_li_company",
    "currentcompany": "_li_company",
    "title": "_li_title",
    "headline": "_li_headline",
    "position": "_li_title",
    "jobtitle": "_li_title",
    "job title": "_li_title",
    "location": "_li_location",
    "geography": "_li_location",
    "industry": "_li_industry",
    "email": "_li_email",
    "professionalemail": "_li_email",
    "professional email": "_li_email",
    "workemail": "_li_email",
    "work email": "_li_email",
    "connectiondegree": "_li_connection_degree",
    "connections": "_li_connection_degree",
    "summary": "_li_summary",
    "salesnavigatorurl": "_li_sales_nav_url",
    "sales navigator url": "_li_sales_nav_url",
}

# Minimum header overlap for auto-detecting LinkedIn CSVs without filename hint.
LINKEDIN_HEADERS = {
    "profileurl", "linkedinurl", "linkedin profile url", "profile url",
    "firstname", "first name", "lastname", "last name", "fullname", "full name",
    "companyname", "company name", "company", "title", "headline", "position",
    "location", "industry", "salesnavigatorurl", "email", "professionalemail",
    "connectiondegree", "connections", "summary",
}


def normalize_linkedin_row(
    raw: dict,
    *,
    source_file: str,
    row_number: int,
) -> Optional[dict]:
    """
    Normalize a raw LinkedIn/Phantombuster row (CSV dict or API JSON) to _li_* fields.

    Returns None when the row lacks both a full name and a company (not usable for
    matching). Otherwise returns the normalized dict with `_source_platform` and
    `_row_number` injected.

    The caller is responsible for building the `RawRecord` wrapper.
    """
    out: dict = {}

    # Copy all original keys (for provenance) and apply alias mapping.
    for original_key, value in raw.items():
        if original_key is None:
            continue
        cleaned = value.strip() if isinstance(value, str) else (value or "")
        out[original_key] = cleaned

        alias = COLUMN_ALIASES.get(original_key.strip().lower())
        if alias:
            # Don't overwrite if already set by a higher-priority key.
            if alias not in out or not out[alias]:
                out[alias] = cleaned

    # Synthesize _li_full_name from first + last when missing.
    if not out.get("_li_full_name"):
        first = out.get("_li_first_name", "")
        last = out.get("_li_last_name", "")
        combined = f"{first} {last}".strip()
        if combined:
            out["_li_full_name"] = combined

    # Require at least a name or company to be useful.
    if not out.get("_li_full_name") and not out.get("_li_company"):
        return None

    out["_source_platform"] = "linkedin_export"
    out["_row_number"] = row_number
    out["_source_file"] = source_file
    return out

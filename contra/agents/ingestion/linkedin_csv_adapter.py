"""
LinkedIn / Phantombuster CSV ingestion adapter.

Detects exports from Sales Navigator Search Export, Profile Scraper, etc.
Files: raw_data/linkedin_*.csv or headers matching Phantombuster conventions.

Does NOT automate outreach — ingestion + provenance only.
"""

from __future__ import annotations

import csv
import hashlib
from pathlib import Path
from typing import Iterable, Iterator

from agents.ingestion.base import (
    IngestionAdapter,
    RawRecord,
    Source,
    SourceManifest,
    hash_content,
    make_source_record_id,
)

_LINKEDIN_HEADERS = {
    "profileurl", "linkedinurl", "linkedin profile url", "profile url",
    "firstname", "first name", "lastname", "last name", "fullname", "full name",
    "companyname", "company name", "company", "title", "headline", "position",
    "location", "industry", "salesnavigatorurl", "email", "professionalemail",
    "connectiondegree", "connections", "summary",
}

_COLUMN_ALIASES: dict[str, str] = {
    "profileurl": "_li_profile_url",
    "linkedinurl": "_li_profile_url",
    "linkedin profile url": "_li_profile_url",
    "profile url": "_li_profile_url",
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
    "title": "_li_title",
    "headline": "_li_headline",
    "position": "_li_title",
    "location": "_li_location",
    "industry": "_li_industry",
    "email": "_li_email",
    "professionalemail": "_li_email",
    "professional email": "_li_email",
    "connectiondegree": "_li_connection_degree",
    "connections": "_li_connection_degree",
    "summary": "_li_summary",
    "salesnavigatorurl": "_li_sales_nav_url",
}


def _detect_linkedin_csv(path: Path) -> bool:
    name_lower = path.name.lower()
    if name_lower.startswith("linkedin_") or name_lower.startswith("phantombuster_"):
        return True
    try:
        with path.open(newline="", encoding="utf-8-sig", errors="replace") as fh:
            header = next(csv.reader(fh), [])
        lowered = {c.strip().lower() for c in header}
        hits = lowered & _LINKEDIN_HEADERS
        if "profileurl" in hits or "linkedinurl" in hits:
            return True
        if len(hits) >= 3 and ("firstname" in hits or "full name" in hits or "fullname" in hits):
            return True
    except Exception:
        pass
    return False


class LinkedInCsvAdapter(IngestionAdapter):
    """Ingests Phantombuster / Sales Navigator CSV exports."""

    source_type = "csv"
    schema_version = "1.0"

    def discover(self, root: Path) -> Iterable[Source]:
        for path in sorted(root.glob("**/*.csv")):
            if not _detect_linkedin_csv(path):
                continue
            file_hash = hashlib.sha256(path.read_bytes()).hexdigest()
            yield Source(
                path=path,
                source_type=self.source_type,
                relative_path=str(path.relative_to(root)),
                file_hash=file_hash,
                metadata={"source_platform": "linkedin_export"},
            )

    def extract_raw(self, source: Source) -> Iterator[RawRecord]:
        with source.path.open(newline="", encoding="utf-8-sig", errors="replace") as fh:
            reader = csv.DictReader(fh)
            if reader.fieldnames is None:
                return

            alias_map: dict[str, str] = {}
            for col in reader.fieldnames:
                key = col.strip().lower()
                if key in _COLUMN_ALIASES:
                    alias_map[col] = _COLUMN_ALIASES[key]

            for row_idx, row in enumerate(reader, start=2):
                row_dict: dict = {}
                for original_col, value in row.items():
                    if original_col is None:
                        continue
                    cleaned = value.strip() if isinstance(value, str) else (value or "")
                    row_dict[original_col] = cleaned
                    if original_col in alias_map:
                        row_dict[alias_map[original_col]] = cleaned

                if not row_dict.get("_li_full_name"):
                    first = row_dict.get("_li_first_name", "")
                    last = row_dict.get("_li_last_name", "")
                    combined = f"{first} {last}".strip()
                    if combined:
                        row_dict["_li_full_name"] = combined

                if not row_dict.get("_li_full_name") and not row_dict.get("_li_company"):
                    continue

                row_dict["_source_platform"] = "linkedin_export"
                row_dict["_row_number"] = row_idx

                source_offset = f"row:{row_idx}"
                content_hash = hash_content(row_dict)
                source_record_id = make_source_record_id(
                    source.relative_path, source_offset, content_hash
                )

                yield RawRecord(
                    source_record_id=source_record_id,
                    source_file=source.relative_path,
                    source_type=self.source_type,
                    source_offset=source_offset,
                    content_hash=content_hash,
                    raw_content=row_dict,
                    schema_version=self.schema_version,
                )

    def manifest(self, source: Source) -> SourceManifest:
        records = list(self.extract_raw(source))
        return SourceManifest(
            source_file=source.relative_path,
            source_type=self.source_type,
            file_hash=source.file_hash,
            record_count=len(records),
            quality_flags={"source_platform": "linkedin_export"},
        )

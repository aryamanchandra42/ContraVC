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
from agents.ingestion.linkedin_normalize import LINKEDIN_HEADERS, normalize_linkedin_row


def _detect_linkedin_csv(path: Path) -> bool:
    name_lower = path.name.lower()
    if name_lower.startswith("linkedin_") or name_lower.startswith("phantombuster_"):
        return True
    try:
        with path.open(newline="", encoding="utf-8-sig", errors="replace") as fh:
            header = next(csv.reader(fh), [])
        lowered = {c.strip().lower() for c in header}
        hits = lowered & LINKEDIN_HEADERS
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

            for row_idx, row in enumerate(reader, start=2):
                normalized = normalize_linkedin_row(
                    dict(row),
                    source_file=source.relative_path,
                    row_number=row_idx,
                )
                if normalized is None:
                    continue

                source_offset = f"row:{row_idx}"
                content_hash = hash_content(normalized)
                source_record_id = make_source_record_id(
                    source.relative_path, source_offset, content_hash
                )

                yield RawRecord(
                    source_record_id=source_record_id,
                    source_file=source.relative_path,
                    source_type=self.source_type,
                    source_offset=source_offset,
                    content_hash=content_hash,
                    raw_content=normalized,
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

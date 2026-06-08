"""
PULSE ingestion base types and Protocol.

Every adapter must implement IngestionAdapter and produce RawRecord instances.
The source_record_id is a deterministic SHA-256 hash and serves as the permanent
foreign key for relationship_evidence.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, Optional


# ---------------------------------------------------------------------------
# Provenance primitives
# ---------------------------------------------------------------------------

def make_source_record_id(source_file: str, source_offset: str, content_hash: str) -> str:
    """
    Deterministic SHA-256 of (source_file, source_offset, content_hash).
    Stable across runs: same inputs → same ID.
    """
    key = json.dumps(
        {"f": source_file, "o": source_offset, "h": content_hash},
        sort_keys=True,
    )
    return hashlib.sha256(key.encode()).hexdigest()


def hash_content(content: Any) -> str:
    """SHA-256 of JSON-serialized content."""
    raw = json.dumps(content, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()


def should_skip_raw_file(path: Path) -> bool:
    """
    True for editor/OS junk under raw_data/ that must never be ingested.

    Excel creates ``~$Workbook.xlsx`` lock files while a workbook is open;
    attempting to read them raises PermissionError on Windows.
    """
    name = path.name
    if name.startswith("~$"):
        return True
    if name.startswith("."):
        return True
    if name.lower() in {"desktop.ini", "thumbs.db"}:
        return True
    return False


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class Source:
    """Represents a single source file to be ingested."""
    path: Path
    source_type: str          # xlsx | pdf | docx | api
    relative_path: str        # path relative to raw_data/ root
    file_hash: str            # SHA-256 of file bytes
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class RawRecord:
    """One row/chunk extracted from a source. Maps directly to entities_raw."""
    source_record_id: str
    source_file: str
    source_type: str
    source_offset: str
    content_hash: str
    raw_content: Dict[str, Any]
    ingested_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    schema_version: str = "1.0"


@dataclass
class SourceManifest:
    """Metadata about a completed ingestion of one source file."""
    source_file: str
    source_type: str
    file_hash: str
    record_count: int
    ingested_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    schema_version: str = "1.0"
    warnings: list = field(default_factory=list)
    quality_flags: Dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------

class IngestionAdapter:
    """
    Base class (duck-typed Protocol) for all ingestion adapters.
    Subclass and override all three methods.
    """

    source_type: str = "unknown"
    schema_version: str = "1.0"

    def discover(self, root: Path) -> Iterable[Source]:
        """Discover all sources this adapter handles under root."""
        raise NotImplementedError

    def extract_raw(self, source: Source) -> Iterator[RawRecord]:
        """Yield RawRecord instances from a single source."""
        raise NotImplementedError

    def manifest(self, source: Source) -> SourceManifest:
        """Return a SourceManifest for a source (called after extract_raw completes)."""
        raise NotImplementedError


# ---------------------------------------------------------------------------
# DB persistence helper
# ---------------------------------------------------------------------------

def persist_raw_records(records: Iterable[RawRecord], con) -> int:
    """
    Insert RawRecords into entities_raw. Idempotent: skips existing source_record_ids.
    Returns count of newly inserted rows.
    """
    inserted = 0
    for rec in records:
        existing = con.execute(
            "SELECT 1 FROM entities_raw WHERE source_record_id = ?",
            [rec.source_record_id],
        ).fetchone()
        if existing:
            continue
        con.execute(
            """
            INSERT INTO entities_raw
                (source_record_id, source_file, source_type, source_offset,
                 content_hash, raw_content, ingested_at, schema_version)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                rec.source_record_id,
                rec.source_file,
                rec.source_type,
                rec.source_offset,
                rec.content_hash,
                json.dumps(rec.raw_content, default=str),
                rec.ingested_at.isoformat(),
                rec.schema_version,
            ],
        )
        inserted += 1
    return inserted

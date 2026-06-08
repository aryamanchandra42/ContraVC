"""
DOCX ingestion adapter.

Uses python-docx. Paragraph + heading provenance.
Captures section context (last heading seen) for downstream ontology extraction.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Iterable, Iterator, Optional

from agents.ingestion.base import (
    IngestionAdapter, RawRecord, Source, SourceManifest,
    hash_content, make_source_record_id,
)

try:
    import docx as python_docx
    _DOCX_AVAILABLE = True
except ImportError:
    _DOCX_AVAILABLE = False


class DocxAdapter(IngestionAdapter):
    """Ingests .docx files. One record per paragraph, with heading context."""

    source_type = "docx"
    schema_version = "1.0"
    MIN_PARA_LEN = 20

    def discover(self, root: Path) -> Iterable[Source]:
        for path in sorted(root.glob("**/*.docx")):
            file_hash = hashlib.sha256(path.read_bytes()).hexdigest()
            yield Source(
                path=path,
                source_type=self.source_type,
                relative_path=str(path.relative_to(root)),
                file_hash=file_hash,
            )

    def extract_raw(self, source: Source) -> Iterator[RawRecord]:
        if not _DOCX_AVAILABLE:
            yield _make_error_record(source, 0, "python-docx not installed")
            return

        try:
            doc = python_docx.Document(str(source.path))
        except Exception as e:
            yield _make_error_record(source, 0, str(e))
            return

        current_heading = None
        current_heading_level = None

        for para_idx, para in enumerate(doc.paragraphs):
            text = para.text.strip()
            if not text:
                continue

            style_name = para.style.name if para.style else ""
            is_heading = style_name.lower().startswith("heading")

            if is_heading:
                current_heading = text
                try:
                    current_heading_level = int(style_name.split()[-1])
                except (ValueError, IndexError):
                    current_heading_level = 1

            if len(text) < self.MIN_PARA_LEN and not is_heading:
                continue

            chunk = {
                "text": text,
                "style": style_name,
                "is_heading": is_heading,
                "heading_level": current_heading_level if is_heading else None,
                "section_heading": current_heading if not is_heading else None,
                "_para_idx": para_idx,
            }

            source_offset = f"para:{para_idx}"
            ch = hash_content(chunk)
            yield RawRecord(
                source_record_id=make_source_record_id(source.relative_path, source_offset, ch),
                source_file=source.relative_path,
                source_type=self.source_type,
                source_offset=source_offset,
                content_hash=ch,
                raw_content=chunk,
            )

    def manifest(self, source: Source) -> SourceManifest:
        records = list(self.extract_raw(source))
        warnings = [r.raw_content.get("_error", "") for r in records if "_error" in r.raw_content]
        return SourceManifest(
            source_file=source.relative_path,
            source_type=self.source_type,
            file_hash=source.file_hash,
            record_count=len(records),
            warnings=[w for w in warnings if w],
        )


def _make_error_record(source: Source, para_idx: int, error: str) -> RawRecord:
    content = {"_error": error, "_para_idx": para_idx}
    ch = hash_content(content)
    offset = f"para:{para_idx}:error"
    return RawRecord(
        source_record_id=make_source_record_id(source.relative_path, offset, ch),
        source_file=source.relative_path,
        source_type=source.source_type,
        source_offset=offset,
        content_hash=ch,
        raw_content=content,
    )

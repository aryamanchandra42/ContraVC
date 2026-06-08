"""
PDF ingestion adapter.

Uses pdfplumber for table extraction (preferred) with text fallback for prose.
Chunks with page + character-offset provenance. Flags low-quality pages.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Dict, Iterable, Iterator, List, Optional

from agents.ingestion.base import (
    IngestionAdapter, RawRecord, Source, SourceManifest,
    hash_content, make_source_record_id,
)

# pdfplumber is a core dep; if it's missing, fail loudly at import time.
try:
    import pdfplumber
    _PDFPLUMBER_AVAILABLE = True
except ImportError:
    _PDFPLUMBER_AVAILABLE = False


class PdfAdapter(IngestionAdapter):
    """Ingests .pdf files. Tries table extraction first; falls back to text chunks."""

    source_type = "pdf"
    schema_version = "1.0"

    # Minimum characters for a text chunk to be worth capturing
    MIN_CHUNK_LEN = 30

    def discover(self, root: Path) -> Iterable[Source]:
        for path in sorted(root.glob("**/*.pdf")):
            file_hash = hashlib.sha256(path.read_bytes()).hexdigest()
            yield Source(
                path=path,
                source_type=self.source_type,
                relative_path=str(path.relative_to(root)),
                file_hash=file_hash,
            )

    def extract_raw(self, source: Source) -> Iterator[RawRecord]:
        if not _PDFPLUMBER_AVAILABLE:
            yield _make_error_record(source, 0, 0, "pdfplumber not installed")
            return

        try:
            with pdfplumber.open(source.path) as pdf:
                for page_num, page in enumerate(pdf.pages, 1):
                    yield from self._extract_page(source, page, page_num)
        except Exception as e:
            yield _make_error_record(source, 0, 0, str(e))

    def _extract_page(self, source: Source, page, page_num: int) -> Iterator[RawRecord]:
        # 1. Try table extraction
        tables = page.extract_tables() or []
        table_extracted = False
        for table_idx, table in enumerate(tables):
            if not table or len(table) < 2:
                continue
            headers = [str(h).strip() if h else f"col_{i}" for i, h in enumerate(table[0])]
            for row_idx, row in enumerate(table[1:], 1):
                row_dict = {headers[i]: (str(v).strip() if v is not None else "") for i, v in enumerate(row)}
                row_dict["_page"] = page_num
                row_dict["_table_idx"] = table_idx
                row_dict["_row_idx"] = row_idx
                row_dict["_extraction_mode"] = "table"

                source_offset = f"page:{page_num}:table:{table_idx}:row:{row_idx}"
                ch = hash_content(row_dict)
                yield RawRecord(
                    source_record_id=make_source_record_id(source.relative_path, source_offset, ch),
                    source_file=source.relative_path,
                    source_type=self.source_type,
                    source_offset=source_offset,
                    content_hash=ch,
                    raw_content=row_dict,
                )
            table_extracted = True

        # 2. Text fallback (always run; captures prose not in tables)
        text = page.extract_text() or ""
        if not text.strip():
            return

        # Split into paragraphs (double newline boundaries)
        paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
        char_offset = 0
        for para_idx, para in enumerate(paragraphs):
            if len(para) < self.MIN_CHUNK_LEN:
                char_offset += len(para) + 2
                continue

            quality_flag = "low_quality" if len(para) < 60 else "ok"
            chunk = {
                "text": para,
                "_page": page_num,
                "_para_idx": para_idx,
                "_char_offset": char_offset,
                "_extraction_mode": "text",
                "_quality": quality_flag,
                "_table_also_extracted": table_extracted,
            }
            source_offset = f"page:{page_num}:char:{char_offset}"
            ch = hash_content(chunk)
            yield RawRecord(
                source_record_id=make_source_record_id(source.relative_path, source_offset, ch),
                source_file=source.relative_path,
                source_type=self.source_type,
                source_offset=source_offset,
                content_hash=ch,
                raw_content=chunk,
            )
            char_offset += len(para) + 2

    def manifest(self, source: Source) -> SourceManifest:
        records = list(self.extract_raw(source))
        warnings = [
            r.raw_content.get("_error", "") for r in records if "_error" in r.raw_content
        ]
        low_quality = sum(1 for r in records if r.raw_content.get("_quality") == "low_quality")
        return SourceManifest(
            source_file=source.relative_path,
            source_type=self.source_type,
            file_hash=source.file_hash,
            record_count=len(records),
            warnings=[w for w in warnings if w],
            quality_flags={"low_quality_chunks": low_quality},
        )


def _make_error_record(source: Source, page: int, char_offset: int, error: str) -> RawRecord:
    content = {"_error": error, "_page": page}
    ch = hash_content(content)
    offset = f"page:{page}:char:{char_offset}:error"
    return RawRecord(
        source_record_id=make_source_record_id(source.relative_path, offset, ch),
        source_file=source.relative_path,
        source_type=source.source_type,
        source_offset=offset,
        content_hash=ch,
        raw_content=content,
    )

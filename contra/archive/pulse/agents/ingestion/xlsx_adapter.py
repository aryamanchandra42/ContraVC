"""
XLSX ingestion adapter.

Uses openpyxl + pandas. One RawRecord per row, with sheet + row-number provenance.
Handles multiple sheets per workbook.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Iterable, Iterator, List, Optional

import pandas as pd

from agents.ingestion.base import (
    IngestionAdapter, RawRecord, Source, SourceManifest,
    hash_content, make_source_record_id, should_skip_raw_file,
)

RAW_DATA_DIR = Path(__file__).resolve().parent.parent.parent / "raw_data"


class XlsxAdapter(IngestionAdapter):
    """Ingests .xlsx files. One record per data row per sheet."""

    source_type = "xlsx"
    schema_version = "1.0"

    def discover(self, root: Path) -> Iterable[Source]:
        for path in sorted(root.glob("**/*.xlsx")):
            if should_skip_raw_file(path):
                continue
            file_hash = hashlib.sha256(path.read_bytes()).hexdigest()
            yield Source(
                path=path,
                source_type=self.source_type,
                relative_path=str(path.relative_to(root)),
                file_hash=file_hash,
            )

    def extract_raw(self, source: Source) -> Iterator[RawRecord]:
        xl = pd.ExcelFile(source.path, engine="openpyxl")
        for sheet_name in xl.sheet_names:
            try:
                df = xl.parse(sheet_name, dtype=str, keep_default_na=False)
            except Exception as e:
                yield _make_error_record(source, sheet_name, 0, str(e))
                continue

            # Normalize column names
            df.columns = [str(c).strip() for c in df.columns]

            for row_idx, row in df.iterrows():
                row_dict = row.to_dict()
                # Strip whitespace from string values
                row_dict = {k: (v.strip() if isinstance(v, str) else v) for k, v in row_dict.items()}
                row_dict["_sheet"] = sheet_name
                row_dict["_row_number"] = int(row_idx) + 2  # 1-indexed header + 1

                source_offset = f"{sheet_name}:{int(row_idx) + 2}"
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
                )

    def manifest(self, source: Source) -> SourceManifest:
        records = list(self.extract_raw(source))
        return SourceManifest(
            source_file=source.relative_path,
            source_type=self.source_type,
            file_hash=source.file_hash,
            record_count=len(records),
        )


def _make_error_record(source: Source, sheet_name: str, row_idx: int, error: str) -> RawRecord:
    content = {"_error": error, "_sheet": sheet_name}
    ch = hash_content(content)
    offset = f"{sheet_name}:error"
    return RawRecord(
        source_record_id=make_source_record_id(source.relative_path, offset, ch),
        source_file=source.relative_path,
        source_type=source.source_type,
        source_offset=offset,
        content_hash=ch,
        raw_content=content,
    )

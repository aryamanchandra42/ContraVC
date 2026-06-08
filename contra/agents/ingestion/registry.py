"""
Ingestion adapter registry.

Dispatches to the correct adapter by file extension or explicit source_type.
Add new adapters here when new source types are supported.
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, Iterable, List, Optional, Type

from agents.ingestion.base import IngestionAdapter, RawRecord, Source, SourceManifest
from agents.ingestion.xlsx_adapter import XlsxAdapter
from agents.ingestion.pdf_adapter import PdfAdapter
from agents.ingestion.docx_adapter import DocxAdapter
from agents.ingestion.linkedin_csv_adapter import LinkedInCsvAdapter
from agents.ingestion.nfx_xlsx_adapter import NfxXlsxAdapter
from agents.ingestion.pptx_adapter import PptxAdapter


_EXTENSION_MAP: Dict[str, Type[IngestionAdapter]] = {
    ".xlsx": XlsxAdapter,
    ".xls": XlsxAdapter,
    ".pdf": PdfAdapter,
    ".pptx": PptxAdapter,
    ".docx": DocxAdapter,
    ".doc": DocxAdapter,
}

_SOURCE_TYPE_MAP: Dict[str, Type[IngestionAdapter]] = {
    "xlsx": XlsxAdapter,
    "pdf": PdfAdapter,
    "docx": DocxAdapter,
    "linkedin": LinkedInCsvAdapter,
    "signal-nfx": NfxXlsxAdapter,
    "fundingstack": XlsxAdapter,  # placeholder for future FundingStack-format xlsx
}


def get_adapter(source_type: Optional[str] = None, path: Optional[Path] = None) -> IngestionAdapter:
    """
    Get the appropriate adapter for a given source_type or file path.
    source_type takes priority over path extension.
    """
    if source_type and source_type in _SOURCE_TYPE_MAP:
        return _SOURCE_TYPE_MAP[source_type]()
    if path:
        ext = path.suffix.lower()
        if ext in _EXTENSION_MAP:
            return _EXTENSION_MAP[ext]()
    raise ValueError(
        f"No adapter found for source_type={source_type!r}, path={path!r}. "
        f"Registered extensions: {list(_EXTENSION_MAP.keys())}"
    )


def discover_all_sources(root: Path) -> List[tuple[IngestionAdapter, Source]]:
    """
    Discover all ingestible sources under root using registered adapters.
    Returns list of (adapter, source) tuples.
    """
    seen_paths = set()
    results = []

    linkedin_adapter = LinkedInCsvAdapter()
    for source in linkedin_adapter.discover(root):
        if source.path not in seen_paths:
            seen_paths.add(source.path)
            results.append((linkedin_adapter, source))

    for ext, adapter_cls in _EXTENSION_MAP.items():
        adapter = adapter_cls()
        for source in adapter.discover(root):
            if source.path not in seen_paths:
                # CRM export is normalized via crm_contacts, not entities_raw
                if source.path.name.lower() == "export.csv":
                    continue
                seen_paths.add(source.path)
                results.append((adapter, source))

    return results


def ingest_all(root: Path, con) -> Dict[str, SourceManifest]:
    """
    Run all adapters over root, persist to entities_raw, return manifests.
    Idempotent: existing source_record_ids are skipped.
    """
    from agents.ingestion.base import persist_raw_records

    manifests = {}
    adapter_source_pairs = discover_all_sources(root)

    for adapter, source in adapter_source_pairs:
        records = list(adapter.extract_raw(source))
        inserted = persist_raw_records(records, con)
        manifest = SourceManifest(
            source_file=source.relative_path,
            source_type=source.source_type,
            file_hash=source.file_hash,
            record_count=len(records),
        )
        manifests[source.relative_path] = manifest

    return manifests

"""
PitchBook manual-export ingestion adapter.

Fallback path for when the Selenium scraper is blocked by Cloudflare.

Workflow:
  1. In PitchBook, run your LP search with desired filters.
  2. Click Export → Excel (up to 2,000 rows per export).
  3. Save the file to contra/raw_data/pitchbook/
  4. Run: contra refresh   (or pulse ingest directly)

PitchBook typically exports two useful report types:

  LP Profile export      — investor name, type, HQ, AUM
  Fund Investments export — LP name, fund name, GP, fund type, vintage,
                            geography, fund size, commitment date + amount

Both are handled by this module. File type is detected by column presence.

Column name aliases are listed in _COLUMN_ALIASES because PitchBook
occasionally renames columns between platform versions.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Dict, Iterable, Iterator, List, Optional

import pandas as pd

from agents.ingestion.base import (
    IngestionAdapter,
    RawRecord,
    Source,
    SourceManifest,
    hash_content,
    make_source_record_id,
    should_skip_raw_file,
)

RAW_DATA_DIR = Path(__file__).resolve().parent.parent.parent / "raw_data"
PITCHBOOK_DIR = RAW_DATA_DIR / "pitchbook"

# ---------------------------------------------------------------------------
# Column normalisation helpers
# ---------------------------------------------------------------------------

# Canonical internal key → list of PitchBook column name variants (case-insensitive)
_LP_PROFILE_ALIASES: Dict[str, List[str]] = {
    "lp_name":       ["investor name", "lp name", "name", "organization name"],
    "investor_type": ["investor type", "lp type", "type", "organization type"],
    "hq_location":   ["hq location", "hq", "headquarters", "location", "city"],
    "hq_country":    ["country", "hq country", "country/region"],
    "aum_usd":       ["aum (usd)", "aum", "assets under management", "aum (m)"],
    "website":       ["website", "web", "url"],
    "description":   ["description", "overview", "notes"],
}

_COMMITMENT_ALIASES: Dict[str, List[str]] = {
    "lp_name":          ["lp name", "investor name", "limited partner", "lp"],
    "fund_name":        ["fund name", "vehicle name", "fund"],
    "manager_name":     ["gp/manager name", "manager name", "gp name", "general partner", "manager"],
    "fund_type":        ["fund type", "vehicle type", "type"],
    "vintage_year":     ["vintage year", "vintage", "fund year"],
    "geography_focus":  ["geography focus", "geography", "geographic focus", "region"],
    "fund_size_usd":    ["fund size (usd)", "fund size", "vehicle size (usd)", "committed capital (usd)"],
    "commitment_date":  ["commitment date", "date", "investment date", "close date"],
    "commitment_usd":   ["commitment amount (usd)", "commitment amount", "committed (usd)", "amount committed"],
    "stage":            ["stage", "fund stage", "investment stage"],
}


def _normalize_columns(df: pd.DataFrame, aliases: Dict[str, List[str]]) -> pd.DataFrame:
    """Rename DataFrame columns to canonical internal keys using alias mapping."""
    col_lower_map: Dict[str, str] = {c.lower().strip(): c for c in df.columns}
    rename: Dict[str, str] = {}
    for canonical, variants in aliases.items():
        for variant in variants:
            if variant in col_lower_map:
                rename[col_lower_map[variant]] = canonical
                break
    return df.rename(columns=rename)


def _detect_export_type(df: pd.DataFrame) -> str:
    """
    Detect whether this is an LP Profile export or a Fund Investments export.
    Returns 'lp_profile' | 'fund_investments' | 'unknown'.
    """
    cols_lower = {c.lower().strip() for c in df.columns}

    commitment_indicators = {"commitment amount", "commitment amount (usd)", "fund name", "gp/manager name", "vintage year"}
    lp_profile_indicators = {"investor type", "lp type", "aum", "aum (usd)"}

    if commitment_indicators & cols_lower:
        return "fund_investments"
    if lp_profile_indicators & cols_lower:
        return "lp_profile"

    # Fallback: if 'fund name' column present → fund investments
    if any("fund" in c for c in cols_lower):
        return "fund_investments"
    return "lp_profile"


# ---------------------------------------------------------------------------
# LP Profile adapter
# ---------------------------------------------------------------------------

class PitchBookLPAdapter(IngestionAdapter):
    """
    Ingests PitchBook LP Profile exports.

    Expected columns (flexible via _LP_PROFILE_ALIASES):
    Investor Name | Investor Type | HQ Location | Country | AUM (USD)
    """

    source_type = "pitchbook_lp"
    schema_version = "1.0"

    def discover(self, root: Path) -> Iterable[Source]:
        pb_dir = root / "pitchbook"
        if not pb_dir.exists():
            return
        for path in sorted(pb_dir.glob("**/*.xlsx")):
            if should_skip_raw_file(path):
                continue
            # Peek to detect type
            try:
                df = pd.read_excel(path, nrows=3, dtype=str, engine="openpyxl")
                if _detect_export_type(df) == "lp_profile":
                    file_hash = hashlib.sha256(path.read_bytes()).hexdigest()
                    yield Source(
                        path=path,
                        source_type=self.source_type,
                        relative_path=str(path.relative_to(root)),
                        file_hash=file_hash,
                    )
            except Exception:
                continue

    def extract_raw(self, source: Source) -> Iterator[RawRecord]:
        try:
            df = pd.read_excel(source.path, dtype=str, keep_default_na=False, engine="openpyxl")
        except Exception as exc:
            yield _error_record(source, str(exc))
            return

        df = _normalize_columns(df, _LP_PROFILE_ALIASES)
        df.columns = [str(c).strip() for c in df.columns]

        for row_idx, row in df.iterrows():
            row_dict = {k: (v.strip() if isinstance(v, str) else v) for k, v in row.to_dict().items()}
            row_dict["_source_type"] = "pitchbook_lp"
            row_dict["_row_number"] = int(row_idx) + 2

            if not row_dict.get("lp_name"):
                continue

            source_offset = f"lp_profile:{int(row_idx) + 2}"
            content_hash = hash_content(row_dict)
            yield RawRecord(
                source_record_id=make_source_record_id(source.relative_path, source_offset, content_hash),
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


# ---------------------------------------------------------------------------
# Fund Investments / Commitments adapter
# ---------------------------------------------------------------------------

class PitchBookCommitmentAdapter(IngestionAdapter):
    """
    Ingests PitchBook Fund Investments exports.

    Expected columns (flexible via _COMMITMENT_ALIASES):
    LP Name | Fund Name | GP/Manager Name | Fund Type | Vintage Year |
    Geography Focus | Fund Size (USD) | Commitment Date | Commitment Amount (USD)

    Each row becomes one RawRecord. The normalization layer
    (pitchbook_normalizer.py) converts these to canonical DB rows.
    """

    source_type = "pitchbook_commitment"
    schema_version = "1.0"

    def discover(self, root: Path) -> Iterable[Source]:
        pb_dir = root / "pitchbook"
        if not pb_dir.exists():
            return
        for path in sorted(pb_dir.glob("**/*.xlsx")):
            if should_skip_raw_file(path):
                continue
            try:
                df = pd.read_excel(path, nrows=3, dtype=str, engine="openpyxl")
                if _detect_export_type(df) == "fund_investments":
                    file_hash = hashlib.sha256(path.read_bytes()).hexdigest()
                    yield Source(
                        path=path,
                        source_type=self.source_type,
                        relative_path=str(path.relative_to(root)),
                        file_hash=file_hash,
                    )
            except Exception:
                continue

    def extract_raw(self, source: Source) -> Iterator[RawRecord]:
        try:
            df = pd.read_excel(source.path, dtype=str, keep_default_na=False, engine="openpyxl")
        except Exception as exc:
            yield _error_record(source, str(exc))
            return

        df = _normalize_columns(df, _COMMITMENT_ALIASES)
        df.columns = [str(c).strip() for c in df.columns]

        for row_idx, row in df.iterrows():
            row_dict = {k: (v.strip() if isinstance(v, str) else v) for k, v in row.to_dict().items()}
            row_dict["_source_type"] = "pitchbook_commitment"
            row_dict["_row_number"] = int(row_idx) + 2

            if not row_dict.get("lp_name") or not row_dict.get("fund_name"):
                continue

            source_offset = f"commitment:{int(row_idx) + 2}"
            content_hash = hash_content(row_dict)
            yield RawRecord(
                source_record_id=make_source_record_id(source.relative_path, source_offset, content_hash),
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


# ---------------------------------------------------------------------------
# Shared helper
# ---------------------------------------------------------------------------

def _error_record(source: Source, error: str) -> RawRecord:
    content = {"_error": error}
    ch = hash_content(content)
    offset = "error"
    return RawRecord(
        source_record_id=make_source_record_id(source.relative_path, offset, ch),
        source_file=source.relative_path,
        source_type=source.source_type,
        source_offset=offset,
        content_hash=ch,
        raw_content=content,
    )

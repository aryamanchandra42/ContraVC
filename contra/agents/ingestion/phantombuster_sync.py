"""
Phantombuster sync orchestrator.

Flow:
  1. Launch the configured Sales Navigator phantom via API
  2. Poll until done (or timeout)
  3. Fetch result rows (JSON or CSV)
  4. Normalize each row via linkedin_normalize.normalize_linkedin_row
  5. Persist to entities_raw (idempotent)
  6. Optionally write a CSV snapshot to raw_data/ for audit
  7. Run linkedin_enrichment only (not full contra refresh)

Usage:
  from agents.ingestion.phantombuster_sync import run_phantombuster_sync
  stats = run_phantombuster_sync(con)
"""

from __future__ import annotations

import csv
import json
import logging
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from agents.ingestion.base import RawRecord, hash_content, make_source_record_id, persist_raw_records
from agents.ingestion.linkedin_normalize import normalize_linkedin_row
from agents.ingestion.phantombuster_client import PhantombusterError, fetch_result_rows, launch, poll_until_done

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent.parent


def run_phantombuster_sync(
    con,
    *,
    agent_id: Optional[str] = None,
    timeout_sec: int = int(os.environ.get("PHANTOMBUSTER_TIMEOUT_SEC", "3600")),
    save_csv: bool = True,
) -> Dict[str, Any]:
    """
    Launch a Phantombuster phantom, ingest its output, run LinkedIn contact matching.

    Returns a stats dict:
        container_id, rows_fetched, rows_inserted, matched,
        aliases_created, csv_path (or None)
    """
    resolved_agent_id = agent_id or os.environ.get("PHANTOMBUSTER_AGENT_ID", "").strip()
    if not resolved_agent_id:
        raise PhantombusterError(
            "No agent_id provided and PHANTOMBUSTER_AGENT_ID is not set."
        )

    # 1. Launch
    container_id = launch(resolved_agent_id)

    # 2. Poll
    poll_until_done(container_id, timeout_sec=timeout_sec)

    # 3. Fetch result rows
    raw_rows = fetch_result_rows(container_id)
    rows_fetched = len(raw_rows)
    logger.info("Phantombuster sync: %d rows fetched for container %s", rows_fetched, container_id)

    if not raw_rows:
        return {
            "container_id": container_id,
            "rows_fetched": 0,
            "rows_inserted": 0,
            "matched": 0,
            "aliases_created": 0,
            "csv_path": None,
        }

    # 4 + 5. Normalize and build RawRecords
    source_file = f"phantombuster/api/{resolved_agent_id}/{container_id}.json"
    records: List[RawRecord] = []
    for idx, row in enumerate(raw_rows, start=1):
        normalized = normalize_linkedin_row(
            row,
            source_file=source_file,
            row_number=idx,
        )
        if normalized is None:
            continue
        source_offset = f"row:{idx}"
        content_hash = hash_content(normalized)
        source_record_id = make_source_record_id(source_file, source_offset, content_hash)
        records.append(RawRecord(
            source_record_id=source_record_id,
            source_file=source_file,
            source_type="api",
            source_offset=source_offset,
            content_hash=content_hash,
            raw_content=normalized,
            schema_version="1.0",
        ))

    rows_inserted = persist_raw_records(records, con)
    logger.info("Phantombuster sync: %d rows inserted into entities_raw", rows_inserted)

    # 6. Optional CSV snapshot
    csv_path: Optional[str] = None
    if save_csv and raw_rows:
        csv_path = _write_csv_snapshot(raw_rows, container_id)

    # 7. LinkedIn contact matching only (not full refresh)
    from agents.normalization.linkedin_enricher import run_linkedin_enrichment
    enrich_stats = run_linkedin_enrichment(con)
    logger.info("Phantombuster sync: enrichment complete: %s", enrich_stats)

    return {
        "container_id": container_id,
        "rows_fetched": rows_fetched,
        "rows_inserted": rows_inserted,
        "matched": enrich_stats.get("matched", 0),
        "aliases_created": enrich_stats.get("aliases_created", 0),
        "csv_path": csv_path,
    }


def _write_csv_snapshot(rows: List[dict], container_id: str) -> str:
    """Write a CSV snapshot of raw Phantombuster output to raw_data/ for audit."""
    raw_data = ROOT / "raw_data"
    raw_data.mkdir(exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    csv_file = raw_data / f"phantombuster_{ts}_{container_id[:8]}.csv"

    # Collect all keys across rows for consistent headers
    all_keys: list = []
    seen: set = set()
    for row in rows:
        for k in row:
            if k not in seen:
                seen.add(k)
                all_keys.append(k)

    try:
        with csv_file.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=all_keys, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
        logger.info("Phantombuster sync: CSV snapshot saved to %s", csv_file)
        return str(csv_file)
    except Exception as exc:
        logger.warning("Phantombuster sync: CSV snapshot failed: %s", exc)
        return ""

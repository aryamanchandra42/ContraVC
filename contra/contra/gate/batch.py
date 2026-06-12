"""
Batch GATE runner for CSV/XLSX uploads.

Processes a list of NfxInvestorRecord objects through the full GATE pipeline,
in parallel (GATE_BATCH_WORKERS threads, default 4), with:
  - Rate-limiting delays between LLM calls (per worker)
  - Exponential back-off on RuntimeError (LLM rate limits)
  - Deduplication: skip investors already in CRM or already screened this batch
  - Checkpointing: progress written to processed_data/batch_gate/{batch_id}.jsonl
  - In-memory registry: active batch reports keyed by batch_id (for API polling)

Each worker gets its own DuckDB cursor (con.cursor() duplicates the connection
for thread-safe use). Set GATE_BATCH_WORKERS=1 for the old sequential behavior.

Usage:
    from contra.gate.batch import batch_gate_run
    report = batch_gate_run(con, records, source_type="signal-nfx")
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Dict, List, Optional

from contra.gate.batch_models import BatchGateItem, BatchGateReport, NfxInvestorRecord
from contra.gate.runner import run_gate

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent.parent
BATCH_DIR = ROOT / "processed_data" / "batch_gate"

# In-memory registry of active + completed batch reports (keyed by batch_id).
# Lives for the process lifetime; persisted to disk as well.
_BATCH_REGISTRY: Dict[str, BatchGateReport] = {}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def batch_gate_run(
    con,
    records: List[NfxInvestorRecord],
    source_type: str = "signal-nfx",
    delay_seconds: float = 3.0,
    max_retries: int = 3,
    batch_id: Optional[str] = None,
    compact_web: bool = True,
) -> BatchGateReport:
    """
    Run the full GATE pipeline on each record, with rate limiting.

    Returns a BatchGateReport. The report object is also registered in
    _BATCH_REGISTRY so it can be polled by the API while running.

    Args:
        con: DuckDB connection.
        records: List of NfxInvestorRecord objects to screen.
        source_type: Upload source type (e.g. "signal-nfx").
        delay_seconds: Base sleep between gate calls (LLM rate limiting).
        max_retries: Max retries on RuntimeError (LLM rate limit hit).
        batch_id: Optional fixed batch ID (for resuming a run).
    """
    batch_id = batch_id or uuid.uuid4().hex
    BATCH_DIR.mkdir(parents=True, exist_ok=True)
    checkpoint_path = BATCH_DIR / f"{batch_id}.jsonl"

    report = BatchGateReport(
        batch_id=batch_id,
        source_type=source_type,
        total=len(records),
        processed=0,
        running=True,
    )
    _BATCH_REGISTRY[batch_id] = report

    # Load prior checkpoint to support resume
    completed_names = _load_checkpoint(checkpoint_path, report)

    pending = [r for r in records if r.investor_name not in completed_names]
    for skipped in (r.investor_name for r in records if r.investor_name in completed_names):
        logger.info("[batch:%s] skipping (already checkpointed): %s", batch_id, skipped)

    workers = _batch_workers()
    report_lock = threading.Lock()

    def _record_item(item: BatchGateItem) -> None:
        with report_lock:
            report.results.append(item)
            report.processed += 1
            _increment_count(report, item.verdict)
            _append_checkpoint(checkpoint_path, item)
            completed_names.add(item.investor_name)
            logger.info(
                "[batch:%s] %d/%d — %s → %s",
                batch_id, report.processed, report.total,
                item.investor_name, item.verdict.upper(),
            )

    if workers <= 1 or len(pending) <= 1:
        for record in pending:
            _record_item(
                _screen_one(con, record, delay_seconds, max_retries, compact_web=compact_web)
            )
    else:
        def _task(record: NfxInvestorRecord) -> BatchGateItem:
            # Per-thread DuckDB cursor — duplicates the connection for safe
            # concurrent use; falls back to the shared con if unavailable.
            cur = con.cursor() if hasattr(con, "cursor") else con
            try:
                return _screen_one(cur, record, delay_seconds, max_retries, compact_web=compact_web)
            finally:
                if cur is not con:
                    try:
                        cur.close()
                    except Exception:
                        pass

        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="gate-batch") as pool:
            futures = {pool.submit(_task, r): r for r in pending}
            for fut in as_completed(futures):
                record = futures[fut]
                try:
                    item = fut.result()
                except Exception as exc:  # defensive — _screen_one already catches
                    logger.error("[batch:%s] worker crashed for %s: %s",
                                 batch_id, record.investor_name, exc)
                    item = BatchGateItem(
                        investor_name=record.investor_name,
                        firm_name=record.firm_name,
                        nfx_url=record.nfx_url,
                        verdict="error",
                        summary="Worker crashed during screening.",
                        error_detail=str(exc),
                    )
                _record_item(item)

    report.running = False
    _BATCH_REGISTRY[batch_id] = report
    return report


def _batch_workers() -> int:
    """Parallel gate workers (GATE_BATCH_WORKERS, default 4, min 1, max 8)."""
    raw = os.environ.get("GATE_BATCH_WORKERS", "").strip()
    try:
        return min(8, max(1, int(raw))) if raw else 4
    except ValueError:
        return 4


def get_batch_report(batch_id: str) -> Optional[BatchGateReport]:
    """Return a batch report from the in-memory registry (or reload from disk)."""
    if batch_id in _BATCH_REGISTRY:
        return _BATCH_REGISTRY[batch_id]
    return _load_full_checkpoint(batch_id)


def mark_crm_added(batch_id: str, investor_name: str) -> bool:
    """Flip crm_added=True for a specific investor in a batch report."""
    report = _BATCH_REGISTRY.get(batch_id)
    if not report:
        report = _load_full_checkpoint(batch_id)
        if not report:
            return False
        _BATCH_REGISTRY[batch_id] = report

    for item in report.results:
        if item.investor_name == investor_name:
            item.crm_added = True
            # Rewrite checkpoint
            _rewrite_checkpoint(BATCH_DIR / f"{batch_id}.jsonl", report)
            return True
    return False


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _screen_one(
    con,
    record: NfxInvestorRecord,
    delay_seconds: float,
    max_retries: int,
    compact_web: bool = True,
) -> BatchGateItem:
    """Screen a single investor through GATE with retry logic."""
    name = record.investor_name
    analyst_facts = record.to_analyst_facts()

    for attempt in range(max_retries + 1):
        try:
            result = run_gate(
                con, name,
                analyst_facts=analyst_facts,
                nfx_url=record.nfx_url,
                compact_web=compact_web,
                nfx_context=record.to_nfx_context_string(),
                screening_mode="nfx_individual",
            )
            verdict = "yes" if result.yes else ("review" if result.is_review else "no")

            item = BatchGateItem(
                investor_name=name,
                firm_name=record.firm_name,
                nfx_url=record.nfx_url,
                verdict=verdict,
                summary=result.summary,
                reasons=result.reasons,
                confidence=result.confidence,
                session_id=result.session_id,
            )

            # Sleep after each successful LLM call (rate limiting)
            if delay_seconds > 0:
                time.sleep(delay_seconds)

            return item

        except RuntimeError as exc:
            # LLM rate limit or provider unavailable
            if attempt < max_retries:
                wait = delay_seconds * (2 ** attempt)
                logger.warning(
                    "[batch] Rate limit hit for %s (attempt %d/%d). Waiting %.1fs: %s",
                    name, attempt + 1, max_retries, wait, exc
                )
                time.sleep(wait)
            else:
                logger.error("[batch] Giving up on %s after %d retries: %s", name, max_retries, exc)
                return BatchGateItem(
                    investor_name=name,
                    firm_name=record.firm_name,
                    nfx_url=record.nfx_url,
                    verdict="error",
                    summary="Gate failed after retries.",
                    error_detail=str(exc),
                )

        except ValueError as exc:
            # Already in CRM or other deterministic skip
            skip_msg = str(exc)
            logger.info("[batch] Skipping %s: %s", name, skip_msg)
            return BatchGateItem(
                investor_name=name,
                firm_name=record.firm_name,
                nfx_url=record.nfx_url,
                verdict="skipped",
                summary=skip_msg,
            )

        except Exception as exc:
            logger.error("[batch] Unexpected error for %s: %s", name, exc, exc_info=True)
            return BatchGateItem(
                investor_name=name,
                firm_name=record.firm_name,
                nfx_url=record.nfx_url,
                verdict="error",
                summary="Unexpected error during screening.",
                error_detail=str(exc),
            )

    # Should not reach here
    return BatchGateItem(
        investor_name=name,
        firm_name=record.firm_name,
        nfx_url=record.nfx_url,
        verdict="error",
        summary="Unknown error.",
    )


def _increment_count(report: BatchGateReport, verdict: str) -> None:
    if verdict == "yes":
        report.yes_count += 1
    elif verdict == "review":
        report.review_count += 1
    elif verdict == "no":
        report.no_count += 1
    elif verdict == "skipped":
        report.skipped_count += 1
    else:
        report.error_count += 1


def _append_checkpoint(path: Path, item: BatchGateItem) -> None:
    try:
        with path.open("a", encoding="utf-8") as fh:
            fh.write(item.model_dump_json() + "\n")
    except Exception as exc:
        logger.warning("Failed to write checkpoint: %s", exc)


def _rewrite_checkpoint(path: Path, report: BatchGateReport) -> None:
    try:
        with path.open("w", encoding="utf-8") as fh:
            for item in report.results:
                fh.write(item.model_dump_json() + "\n")
    except Exception as exc:
        logger.warning("Failed to rewrite checkpoint: %s", exc)


def _load_checkpoint(path: Path, report: BatchGateReport) -> set:
    """Load existing checkpoint into report; return set of already-processed names."""
    completed: set = set()
    if not path.exists():
        return completed
    try:
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                item = BatchGateItem.model_validate_json(line)
                report.results.append(item)
                report.processed += 1
                _increment_count(report, item.verdict)
                completed.add(item.investor_name)
    except Exception as exc:
        logger.warning("Could not load checkpoint %s: %s", path, exc)
    return completed


def _load_full_checkpoint(batch_id: str) -> Optional[BatchGateReport]:
    """Reconstruct a BatchGateReport from a checkpoint file."""
    path = BATCH_DIR / f"{batch_id}.jsonl"
    if not path.exists():
        return None
    report = BatchGateReport(
        batch_id=batch_id,
        source_type="signal-nfx",
        total=0,
        processed=0,
        running=False,
    )
    _load_checkpoint(path, report)
    report.total = report.processed
    return report

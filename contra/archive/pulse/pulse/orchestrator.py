"""
pulse.orchestrator
==================
Single entry point that wires together every pipeline stage plus the CSV
export step. Used by:
  - `pulse refresh`  CLI command
  - The "Refresh PULSE" button in pulse/explore/app.py (via subprocess)

Design principles
-----------------
* Does NOT duplicate any agent logic — it calls the same functions that the
  individual `pulse <stage>` commands call.
* Progress is reported through a callable so both CLI and Streamlit can
  consume it (plain print vs. JSON lines for the UI pipe).
* Stages that fail do not silently continue — the caller gets a RefreshResult
  with failed_stage set.
* All DB writes happen through a single connection opened before the loop and
  closed after, matching the run_all behavior in cli.py.
"""

from __future__ import annotations

import json
import sys
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------

@dataclass
class RefreshResult:
    success: bool
    stages_completed: List[str] = field(default_factory=list)
    failed_stage: Optional[str] = None
    error: Optional[str] = None
    exports: Dict[str, Any] = field(default_factory=dict)
    eval_warnings: List[str] = field(default_factory=list)
    started_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    completed_at: Optional[str] = None
    counts: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "success": self.success,
            "stages_completed": self.stages_completed,
            "failed_stage": self.failed_stage,
            "error": self.error,
            "exports": self.exports,
            "eval_warnings": self.eval_warnings,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "counts": self.counts,
        }


# ---------------------------------------------------------------------------
# Progress helpers
# ---------------------------------------------------------------------------

def _json_emit(msg: str, stage: str = "", status: str = "running") -> None:
    """Print a single JSON line for the Streamlit subprocess pipe to consume."""
    line = json.dumps({"stage": stage, "msg": msg, "status": status})
    print(line, flush=True)


def _plain_emit(msg: str, stage: str = "", status: str = "running") -> None:
    icon = {"running": "►", "done": "✓", "failed": "✗", "warn": "⚠"}.get(status, "•")
    print(f"  {icon}  {msg}", flush=True)


# ---------------------------------------------------------------------------
# Stage runners (thin wrappers that call the actual agent functions)
# ---------------------------------------------------------------------------

def _run_ingest(con, raw_dir: Path, progress: Callable) -> Dict:
    from agents.ingestion.registry import ingest_all
    progress("Ingesting source files", "ingest", "running")
    manifests = ingest_all(raw_dir, con)
    total = sum(m.record_count for m in manifests.values())
    progress(f"Ingest complete — {len(manifests)} files, {total} records", "ingest", "done")
    return {"files": len(manifests), "records": total}


def _run_normalize(con, progress: Callable) -> Dict:
    from agents.normalization.entity_resolver import resolve_entities_from_raw
    from agents.normalization.fund_normalizer import ingest_fund_rows
    from agents.normalization.interaction_normalizer import ingest_interaction_rows
    from agents.normalization.allocator_normalizer import enrich_all_allocators
    from agents.normalization.syndicate_normalizer import run_syndicate_integration
    from agents.normalization.linkedin_enricher import run_linkedin_enrichment

    progress("Resolving entities", "normalize", "running")
    counts = resolve_entities_from_raw(con)
    enrich_all_allocators(con)
    ingest_fund_rows(con)
    ingest_interaction_rows(con)
    run_linkedin_enrichment(con)
    synd = run_syndicate_integration(con)
    progress(
        f"Normalize done — {counts['allocators_created']} allocators, "
        f"{synd['coinvest']['co_invested_edges']} co-invest edges",
        "normalize", "done",
    )
    return {"allocators": counts["allocators_created"], "co_invested_edges": synd["coinvest"]["co_invested_edges"]}


def _run_extract(con, progress: Callable) -> Dict:
    from agents.ontology.pipeline import run_extraction_pipeline
    run_id = str(uuid.uuid4())
    progress("Extracting ontology terms", "extract", "running")
    counts = run_extraction_pipeline(con, run_id)
    progress(f"Extract done — {counts['terms_extracted']} terms", "extract", "done")
    return counts


def _run_derive(con, progress: Callable) -> Dict:
    from agents.uncertainty.aggregator import derive_all
    from agents.uncertainty.temporal import derive_temporal
    progress("Deriving uncertainty + temporal columns", "derive", "running")
    agg = derive_all(con)
    tc = derive_temporal(con)
    progress(f"Derive done — {agg.get('relationships_updated', 0)} relationships updated", "derive", "done")
    return {"relationships_updated": agg.get("relationships_updated", 0), "temporal_updated": tc}


def _run_graph(con, progress: Callable) -> Dict:
    from agents.graph.builder import build_graph
    from agents.graph.persist import persist_graph
    from agents.graph.prospect_inference import run_prospect_inference
    from agents.graph.invested_with_edges import build_invested_with_edges
    from agents.uncertainty.aggregator import derive_relationship_uncertainty, derive_signal_uncertainty
    from agents.uncertainty.temporal import derive_temporal

    progress("Building relationship graph", "graph", "running")
    build_invested_with_edges(con)
    infer_counts = run_prospect_inference(con)
    derive_relationship_uncertainty(con)
    derive_signal_uncertainty(con)
    derive_temporal(con)
    run_id = str(uuid.uuid4())
    G = build_graph(con)
    persist_graph(G, run_id)
    progress(
        f"Graph done — {G.number_of_nodes()} nodes, {G.number_of_edges()} edges, "
        f"{infer_counts.get('mutual_connection_edges', 0)} warm paths",
        "graph", "done",
    )
    return {"nodes": G.number_of_nodes(), "edges": G.number_of_edges(),
            "mutual_connection_edges": infer_counts.get("mutual_connection_edges", 0)}


def _run_score(con, progress: Callable) -> Dict:
    from agents.scoring.icp_scorer import run_icp_scoring
    from agents.scoring.rejection_extractor import run_rejection_extraction
    from agents.scoring.signal_extractor import run_signal_extraction
    from agents.scoring.latent_signal_extractor import run_latent_signal_extraction
    from agents.scoring.contradiction_detector import run_contradiction_detection
    from agents.scoring.signal_evidence_writer import purge_orphan_signal_evidence
    from agents.uncertainty.aggregator import derive_signal_uncertainty

    progress("Running ICP scoring + signals", "score", "running")
    purge_orphan_signal_evidence(con)
    icp = run_icp_scoring(con)
    run_rejection_extraction(con)
    run_signal_extraction(con)
    run_latent_signal_extraction(con)
    run_contradiction_detection(con)
    derive_signal_uncertainty(con)
    progress(
        f"Score done — tier_1={icp['tier_1']}, tier_2={icp['tier_2']}, "
        f"tier_3={icp['tier_3']}, tier_4={icp['tier_4']}",
        "score", "done",
    )
    return icp


def _run_calibrate(con, progress: Callable) -> Dict:
    from agents.scoring.calibration import run_calibration
    progress("Running benchmark calibration", "calibrate", "running")
    result = run_calibration(con)
    progress(f"Calibrate done — {result.get('total_scored', 0)} scored", "calibrate", "done")
    return result


def _run_exports(con, progress: Callable) -> Dict:
    from pulse.exports.outreach_pack import run_all_exports
    progress("Generating outreach CSVs", "exports", "running")
    stats = run_all_exports(con)
    pack = stats["outreach_pack"]
    progress(
        f"Exports done — Section A: {pack['section_a_tier1_approved']} Tier 1 approved prospects",
        "exports", "done",
    )
    return stats


def _run_evals(progress: Callable) -> List[str]:
    """Run lightweight invariant checks; return list of warning strings (non-fatal)."""
    warnings: List[str] = []
    try:
        import duckdb
        con = duckdb.connect(str(ROOT / "pulse.duckdb"), read_only=True)
        progress("Running data quality checks", "evals", "running")

        checks = [
            (
                "Every relationship has ≥1 evidence row",
                """
                SELECT COUNT(*) FROM relationships r
                WHERE NOT EXISTS (
                    SELECT 1 FROM relationship_evidence re WHERE re.edge_id = r.edge_id
                )
                """,
            ),
            (
                "No orphan evidence rows",
                """
                SELECT COUNT(*) FROM relationship_evidence re
                WHERE NOT EXISTS (
                    SELECT 1 FROM relationships r WHERE r.edge_id = re.edge_id
                )
                """,
            ),
        ]
        for label, sql in checks:
            try:
                n = con.execute(sql).fetchone()[0]
                if n > 0:
                    warnings.append(f"FAIL ({n} violations): {label}")
            except Exception as e:
                warnings.append(f"ERROR: {label} — {e}")
        con.close()

        if warnings:
            progress(f"Quality check: {len(warnings)} warning(s)", "evals", "warn")
        else:
            progress("Quality checks passed", "evals", "done")
    except Exception as e:
        warnings.append(f"Eval runner error: {e}")
    return warnings


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def run_refresh(
    raw_dir: Optional[Path] = None,
    *,
    json_log: bool = False,
    skip_evals: bool = False,
) -> RefreshResult:
    """
    Run the full pipeline refresh and write all export CSVs.

    Parameters
    ----------
    raw_dir:    Source files directory (defaults to raw_data/).
    json_log:   If True, emit JSON lines to stdout (for Streamlit subprocess pipe).
    skip_evals: Skip data quality checks (useful for fast dev re-runs).
    """
    import yaml

    raw_dir = raw_dir or ROOT / "raw_data"
    progress = _json_emit if json_log else _plain_emit

    # Load config
    cfg_path = ROOT / "prompts" / "pulse_defaults.yaml"
    cfg: Dict[str, Any] = {}
    if cfg_path.exists():
        with open(cfg_path, encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
    run_evals = (not skip_evals) and cfg.get("refresh", {}).get("run_evals", True)

    from agents.db import get_conn
    con = get_conn()

    result = RefreshResult(success=False)

    stage_fns = [
        ("ingest",     lambda: _run_ingest(con, raw_dir, progress)),
        ("normalize",  lambda: _run_normalize(con, progress)),
        ("extract",    lambda: _run_extract(con, progress)),
        ("derive",     lambda: _run_derive(con, progress)),
        ("graph",      lambda: _run_graph(con, progress)),
        ("score",      lambda: _run_score(con, progress)),
        ("calibrate",  lambda: _run_calibrate(con, progress)),
        ("exports",    lambda: _run_exports(con, progress)),
    ]

    if not json_log:
        print("\n══ PULSE Refresh ══════════════════════════════════════\n", flush=True)

    for stage_name, fn in stage_fns:
        try:
            stage_counts = fn()
            result.stages_completed.append(stage_name)
            result.counts[stage_name] = stage_counts
        except Exception as exc:
            import traceback
            err = f"{type(exc).__name__}: {exc}"
            progress(f"Stage '{stage_name}' failed: {err}", stage_name, "failed")
            result.failed_stage = stage_name
            result.error = err
            if json_log:
                print(json.dumps({"stage": stage_name, "msg": err, "status": "failed",
                                  "traceback": traceback.format_exc()}), flush=True)
            con.close()
            result.completed_at = datetime.now(timezone.utc).isoformat()
            return result

    if run_evals:
        result.eval_warnings = _run_evals(progress)

    con.close()
    result.success = True
    result.completed_at = datetime.now(timezone.utc).isoformat()

    if not json_log:
        pack = result.counts.get("exports", {}).get("outreach_pack", {})
        print("\n══ Done ════════════════════════════════════════════════", flush=True)
        print(f"  Outreach pack: {pack.get('out_path', '?')}")
        print(f"    Section A (Tier 1 approved):   {pack.get('section_a_tier1_approved', 0)}")
        if result.eval_warnings:
            print(f"\n  ⚠  Quality warnings:", flush=True)
            for w in result.eval_warnings:
                print(f"      {w}", flush=True)

    return result

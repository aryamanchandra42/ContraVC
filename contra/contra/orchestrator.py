"""Contra pipeline orchestrator — contra refresh."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent


@dataclass
class RefreshResult:
    success: bool
    stages_completed: List[str] = field(default_factory=list)
    failed_stage: Optional[str] = None
    error: Optional[str] = None
    counts: Dict[str, Any] = field(default_factory=dict)
    started_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    completed_at: Optional[str] = None


def _plain(msg: str, status: str = "running") -> None:
    icon = {"running": ">", "done": "ok", "failed": "X"}.get(status, "-")
    print(f"  [{icon}] {msg}", flush=True)


def _run_ingest(con, raw_dir: Path, progress: Callable) -> Dict:
    from agents.ingestion.registry import ingest_all
    progress("Ingesting source files", "running")
    manifests = ingest_all(raw_dir, con)
    total = sum(m.record_count for m in manifests.values())
    progress(f"Ingest complete — {len(manifests)} files, {total} records", "done")
    return {"files": len(manifests), "records": total}


def _run_normalize(con, raw_dir: Path, progress: Callable) -> Dict:
    from agents.normalization.entity_resolver import resolve_entities_from_raw
    from agents.normalization.fund_normalizer import ingest_fund_rows
    from agents.normalization.interaction_normalizer import ingest_interaction_rows
    from agents.normalization.allocator_normalizer import enrich_all_allocators
    from agents.normalization.syndicate_normalizer import run_syndicate_integration
    from agents.normalization.linkedin_enricher import run_linkedin_enrichment
    from agents.normalization.crm_normalizer import ingest_crm_contacts
    from agents.normalization.icp_rules_normalizer import ingest_icp_rules

    progress("Resolving entities", "running")
    counts = resolve_entities_from_raw(con)
    enrich_all_allocators(con)
    ingest_fund_rows(con)
    ingest_interaction_rows(con)
    run_linkedin_enrichment(con)
    synd = run_syndicate_integration(con)
    crm = ingest_crm_contacts(con, raw_dir)
    icp_rules = ingest_icp_rules(con)
    progress(
        f"Normalize done — {counts.get('allocators_created', 0)} allocators, "
        f"CRM {crm.get('rows', 0)}, ICP rules {icp_rules.get('rows', 0)}",
        "done",
    )
    return {"allocators": counts, "syndicate": synd, "crm": crm, "icp_rules": icp_rules}


def _run_extract(con, progress: Callable) -> Dict:
    from agents.ontology.pipeline import run_extraction_pipeline
    run_id = str(uuid.uuid4())
    progress("Extracting ontology", "running")
    counts = run_extraction_pipeline(con, run_id)
    progress(f"Extract done — {counts.get('terms_extracted', 0)} terms", "done")
    return counts


def _run_derive(con, progress: Callable) -> Dict:
    from agents.uncertainty.aggregator import derive_all
    from agents.uncertainty.temporal import derive_temporal
    progress("Deriving uncertainty", "running")
    agg = derive_all(con)
    tc = derive_temporal(con)
    progress("Derive done", "done")
    return {"agg": agg, "temporal": tc}


def _run_graph(con, progress: Callable) -> Dict:
    from agents.graph.builder import build_graph
    from agents.graph.persist import persist_graph
    from agents.graph.prospect_inference import run_prospect_inference
    from agents.graph.invested_with_edges import build_invested_with_edges
    from agents.uncertainty.aggregator import derive_relationship_uncertainty, derive_signal_uncertainty
    from agents.uncertainty.temporal import derive_temporal

    progress("Building graph", "running")
    build_invested_with_edges(con)
    infer_counts = run_prospect_inference(con)
    derive_relationship_uncertainty(con)
    derive_signal_uncertainty(con)
    derive_temporal(con)
    run_id = str(uuid.uuid4())
    G = build_graph(con)
    persist_graph(G, run_id)
    progress(f"Graph done — {G.number_of_nodes()} nodes, {G.number_of_edges()} edges", "done")
    return {"nodes": G.number_of_nodes(), "edges": G.number_of_edges(), "inference": infer_counts}


def _run_score(con, progress: Callable) -> Dict:
    from agents.scoring.icp_scorer import run_icp_scoring
    from agents.scoring.rejection_extractor import run_rejection_extraction
    from agents.scoring.signal_extractor import run_signal_extraction
    from agents.scoring.latent_signal_extractor import run_latent_signal_extraction
    from agents.scoring.syndicate_signal_extractor import run_syndicate_signal_extraction
    from agents.scoring.contradiction_detector import run_contradiction_detection
    from agents.scoring.signal_evidence_writer import purge_orphan_signal_evidence
    from agents.uncertainty.aggregator import derive_signal_uncertainty

    progress("Scoring ICP + signals", "running")
    purge_orphan_signal_evidence(con)
    icp = run_icp_scoring(con)
    run_rejection_extraction(con)
    run_signal_extraction(con)
    run_latent_signal_extraction(con)
    synd = run_syndicate_signal_extraction(con)
    run_contradiction_detection(con)
    derive_signal_uncertainty(con)
    progress(
        f"Score done — tier_1={icp.get('tier_1', 0)}, "
        f"syndicate_signals={synd.get('fund_lp_behavior', 0)}",
        "done",
    )
    return {**icp, "syndicate": synd}


def _run_calibrate(con, progress: Callable) -> Dict:
    from agents.scoring.calibration import run_calibration
    progress("Calibrating", "running")
    result = run_calibration(con)
    progress("Calibrate done", "done")
    return result


def _run_catalog(con, progress: Callable) -> Dict:
    from contra.intelligence.catalog import refresh_catalog
    progress("Refreshing data catalog", "running")
    refresh_catalog(con)
    progress("Catalog refreshed", "done")
    return {}


def run_refresh(raw_dir: Optional[Path] = None) -> RefreshResult:
    raw_dir = raw_dir or ROOT / "raw_data"
    progress = _plain
    from agents.db import get_conn

    con = get_conn()
    result = RefreshResult(success=False)

    stages = [
        ("ingest", lambda: _run_ingest(con, raw_dir, progress)),
        ("normalize", lambda: _run_normalize(con, raw_dir, progress)),
        ("extract", lambda: _run_extract(con, progress)),
        ("derive", lambda: _run_derive(con, progress)),
        ("graph", lambda: _run_graph(con, progress)),
        ("score", lambda: _run_score(con, progress)),
        ("calibrate", lambda: _run_calibrate(con, progress)),
        ("catalog", lambda: _run_catalog(con, progress)),
    ]

    print("\n== Contra Refresh ==\n", flush=True)
    for name, fn in stages:
        try:
            result.counts[name] = fn()
            result.stages_completed.append(name)
        except Exception as exc:
            result.failed_stage = name
            result.error = f"{type(exc).__name__}: {exc}"
            progress(f"Stage '{name}' failed: {result.error}", "failed")
            con.close()
            result.completed_at = datetime.now(timezone.utc).isoformat()
            return result

    # Re-apply views after pipeline
    views_path = ROOT / "schema" / "views.sql"
    if views_path.exists():
        try:
            con.execute(views_path.read_text(encoding="utf-8"))
        except Exception:
            pass

    con.close()
    result.success = True
    result.completed_at = datetime.now(timezone.utc).isoformat()
    print("\n== Done ==\n", flush=True)
    return result

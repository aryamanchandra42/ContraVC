"""
PULSE CLI — typer application.

All subcommands are idempotent. Re-running with the same inputs produces the same outputs.
Every run gets a unique run_id, structured logs in logs/{run_id}/, and a pipeline_runs row.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

import typer
from rich import print as rprint
from rich.table import Table
from rich.console import Console

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agents.db import get_conn
from pulse.run_tracker import start_run, complete_run, fail_run, get_stage_status
from pulse.logging_config import configure_logging, get_logger

app = typer.Typer(
    name="pulse",
    help="PULSE — Private-market Unified LP Signal Engine",
    no_args_is_help=True,
)

review_app = typer.Typer(help="Review queue management")
app.add_typer(review_app, name="review")

research_app = typer.Typer(help="Research agent — LP enrichment, Q&A, outreach briefs, ontology")
app.add_typer(research_app, name="research")

console = Console()


# ---------------------------------------------------------------------------
# DB snapshot helper (used by pulse status --verbose)
# ---------------------------------------------------------------------------

def _print_db_snapshot(con) -> None:
    """Print row counts, edge distribution, and key invariant checks."""
    rprint("\n[bold]Database Snapshot[/bold]")

    # Core table row counts
    tables = [
        "entities_raw", "allocators", "funds", "interactions", "investments",
        "relationships", "relationship_evidence", "signals", "rejections",
        "ontology_terms", "entity_aliases", "human_reviews",
        "icp_scores", "benchmark_rankings",
    ]
    counts_t = Table(title="Table Row Counts")
    counts_t.add_column("Table")
    counts_t.add_column("Rows", justify="right")
    for tbl in tables:
        try:
            n = con.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
            counts_t.add_row(tbl, str(n))
        except Exception:
            counts_t.add_row(tbl, "[red]error[/red]")
    console.print(counts_t)

    # Edge type distribution
    try:
        edge_rows = con.execute(
            "SELECT edge_type, COUNT(*) AS cnt FROM relationships GROUP BY edge_type ORDER BY cnt DESC"
        ).fetchall()
        if edge_rows:
            et = Table(title="Edge Type Distribution")
            et.add_column("edge_type")
            et.add_column("Count", justify="right")
            for etype, cnt in edge_rows:
                et.add_row(etype, str(cnt))
            console.print(et)
    except Exception:
        pass

    # Allocator population split
    try:
        pop_rows = con.execute(
            "SELECT COALESCE(population, 'null') AS pop, COUNT(*) FROM allocators GROUP BY pop ORDER BY 2 DESC"
        ).fetchall()
        if pop_rows:
            pt = Table(title="Allocator Population")
            pt.add_column("population")
            pt.add_column("Count", justify="right")
            for pop, cnt in pop_rows:
                pt.add_row(pop, str(cnt))
            console.print(pt)
    except Exception:
        pass

    # ICP tier distribution
    try:
        tier_rows = con.execute(
            "SELECT tier, COUNT(*) FROM icp_scores GROUP BY tier ORDER BY tier"
        ).fetchall()
        if tier_rows:
            tt = Table(title="ICP Tier Distribution")
            tt.add_column("Tier")
            tt.add_column("Count", justify="right")
            for tier, cnt in tier_rows:
                tt.add_row(tier or "null", str(cnt))
            console.print(tt)
    except Exception:
        pass

    # --- Invariant checks ---
    rprint("\n[bold]Invariant Checks[/bold]")
    _check_invariant(
        con,
        "Evidence-per-edge (every relationship has ≥1 evidence row)",
        """
        SELECT COUNT(*) FROM relationships r
        WHERE NOT EXISTS (
            SELECT 1 FROM relationship_evidence re WHERE re.edge_id = r.edge_id
        )
        """,
        expect_zero=True,
    )
    _check_invariant(
        con,
        "relationships_effective view exists",
        "SELECT COUNT(*) FROM relationships_effective",
        expect_nonzero=False,  # just check it doesn't error
    )
    _check_invariant(
        con,
        "Orphan evidence (evidence rows with no matching edge)",
        """
        SELECT COUNT(*) FROM relationship_evidence re
        WHERE NOT EXISTS (
            SELECT 1 FROM relationships r WHERE r.edge_id = re.edge_id
        )
        """,
        expect_zero=True,
    )
    _check_invariant(
        con,
        "Allocators with unknown type",
        "SELECT COUNT(*) FROM allocators WHERE allocator_type = 'unknown' OR allocator_type IS NULL",
        expect_zero=True,
    )


def _check_invariant(con, label: str, sql: str, expect_zero: bool = False, expect_nonzero: bool = False) -> None:
    try:
        n = con.execute(sql).fetchone()[0]
        if expect_zero:
            icon = "[green]PASS[/green]" if n == 0 else f"[red]FAIL ({n} violations)[/red]"
        else:
            icon = "[green]OK[/green]"
        rprint(f"  {icon}  {label}")
    except Exception as e:
        rprint(f"  [red]ERROR[/red]  {label}: {e}")


# ---------------------------------------------------------------------------
# ingest
# ---------------------------------------------------------------------------

@app.command()
def ingest(
    raw_dir: Path = typer.Option(ROOT / "raw_data", help="Directory containing source files"),
    log_level: str = typer.Option("INFO", help="Log level"),
) -> None:
    """Ingest all source files in raw_data/ → entities_raw."""
    con = get_conn()
    run_id = start_run(con, "ingest", {"raw_dir": str(raw_dir)})
    configure_logging(run_id, "ingest", log_level)
    log = get_logger("pulse.ingest")

    try:
        from agents.ingestion.registry import ingest_all
        log.info("Starting ingestion", raw_dir=str(raw_dir))
        manifests = ingest_all(raw_dir, con)

        total_records = sum(m.record_count for m in manifests.values())
        log.info("Ingestion complete", files=len(manifests), total_records=total_records)

        rprint(f"\n[bold green]Ingestion complete[/bold green]")
        t = Table(title="Source Manifests")
        t.add_column("File")
        t.add_column("Type")
        t.add_column("Records")
        t.add_column("Warnings")
        for path, m in sorted(manifests.items()):
            t.add_row(path, m.source_type, str(m.record_count), str(len(m.warnings)))
        console.print(t)

        complete_run(con, run_id, rows_processed=total_records, rows_written=total_records)
    except Exception as e:
        fail_run(con, run_id, str(e))
        log.error("Ingestion failed", error=str(e))
        raise typer.Exit(1)


# ---------------------------------------------------------------------------
# normalize
# ---------------------------------------------------------------------------

@app.command()
def normalize(log_level: str = typer.Option("INFO")) -> None:
    """Entity resolution + taxonomy mapping → canonical tables."""
    con = get_conn()
    run_id = start_run(con, "normalize")
    configure_logging(run_id, "normalize", log_level)
    log = get_logger("pulse.normalize")

    try:
        from agents.normalization.entity_resolver import resolve_entities_from_raw
        from agents.normalization.fund_normalizer import ingest_fund_rows
        from agents.normalization.interaction_normalizer import ingest_interaction_rows
        from agents.normalization.allocator_normalizer import enrich_all_allocators
        from agents.normalization.syndicate_normalizer import run_syndicate_integration

        log.info("Resolving allocator entities")
        counts = resolve_entities_from_raw(con)
        log.info("Allocator resolution done", **counts)

        log.info("Enriching allocators from raw columns + scoring text")
        enrich_counts = enrich_all_allocators(con)
        log.info("Allocator enrichment done", **enrich_counts)

        fund_count = ingest_fund_rows(con)
        log.info("Fund normalization done", funds_created=fund_count)

        interaction_count = ingest_interaction_rows(con)
        log.info("Interaction normalization done", interactions_created=interaction_count)

        from agents.normalization.linkedin_enricher import run_linkedin_enrichment
        log.info("Matching LinkedIn/Phantombuster exports to allocators")
        li_counts = run_linkedin_enrichment(con)
        log.info("LinkedIn enrichment done", **li_counts)

        log.info("Integrating AngelList syndicate roster + investments + ContraVC benchmark")
        synd = run_syndicate_integration(con)
        log.info("Syndicate integration done",
                 syndicate_lps=synd["roster"]["syndicate_lps_created"],
                 investments=synd["investments"]["investments_created"],
                 co_invested_edges=synd["coinvest"]["co_invested_edges"],
                 benchmark_rows=synd["benchmark"]["benchmark_rows"])

        total_written = counts["allocators_created"] + fund_count + interaction_count
        rprint(f"\n[bold green]Normalization complete[/bold green]")
        rprint(f"  Allocators: {counts['allocators_created']}")
        rprint(f"  Aliases: {counts['aliases_created']}")
        rprint(f"  Enriched (pass 1 / pass 2): {enrich_counts['enriched_pass1']} / {enrich_counts['enriched_pass2']}")
        rprint(f"  Funds: {fund_count}")
        rprint(f"  Interactions: {interaction_count}")
        rprint(f"  Queued for review: {counts['queued_for_review']}")
        rprint(f"\n  [bold]Syndicate integration[/bold]")
        rprint(f"  Syndicate LPs created: {synd['roster']['syndicate_lps_created']} "
               f"(matched existing: {synd['roster']['matched_existing']})")
        rprint(f"  Funds (deals): {synd['investments']['funds_created']}  "
               f"Investments: {synd['investments']['investments_created']}")
        rprint(f"  Co-invested edges: {synd['coinvest']['co_invested_edges']} "
               f"(evidence: {synd['coinvest']['co_invested_evidence']})")
        rprint(f"  ContraVC benchmark rows: {synd['benchmark']['benchmark_rows']}")

        complete_run(con, run_id, rows_written=total_written)
    except Exception as e:
        fail_run(con, run_id, str(e))
        log.error("Normalization failed", error=str(e))
        raise typer.Exit(1)


# ---------------------------------------------------------------------------
# extract
# ---------------------------------------------------------------------------

@app.command()
def extract(log_level: str = typer.Option("INFO")) -> None:
    """Ontology extraction pipeline → ontology_terms + relationship_evidence."""
    con = get_conn()
    run_id = start_run(con, "extract")
    configure_logging(run_id, "extract", log_level)
    log = get_logger("pulse.extract")

    try:
        from agents.ontology.pipeline import run_extraction_pipeline
        log.info("Starting ontology extraction", run_id=run_id)
        counts = run_extraction_pipeline(con, run_id)
        log.info("Extraction complete", **counts)

        rprint(f"\n[bold green]Extraction complete[/bold green]")
        for k, v in counts.items():
            rprint(f"  {k}: {v}")

        complete_run(con, run_id, rows_processed=counts["documents_processed"],
                     rows_written=counts["terms_extracted"])
    except Exception as e:
        fail_run(con, run_id, str(e))
        log.error("Extraction failed", error=str(e))
        raise typer.Exit(1)


# ---------------------------------------------------------------------------
# derive
# ---------------------------------------------------------------------------

@app.command()
def derive(log_level: str = typer.Option("INFO")) -> None:
    """Recompute uncertainty + temporal derivations from evidence. Idempotent."""
    con = get_conn()
    run_id = start_run(con, "derive")
    configure_logging(run_id, "derive", log_level)
    log = get_logger("pulse.derive")

    try:
        from agents.uncertainty.aggregator import derive_all
        from agents.uncertainty.temporal import derive_temporal, params_hash

        ph = params_hash()
        log.info("Computing uncertainty derivations", params_hash=ph)
        agg_counts = derive_all(con)

        log.info("Computing temporal derivations")
        temporal_count = derive_temporal(con)

        log.info("Derivations complete", **agg_counts, temporal_updated=temporal_count)
        rprint(f"\n[bold green]Derivations complete[/bold green]")
        rprint(f"  Relationships updated: {agg_counts.get('relationships_updated', 0)}")
        rprint(f"  Signals updated: {agg_counts.get('signals_updated', 0)}")
        rprint(f"  Temporal updated: {temporal_count}")
        rprint(f"  Params hash: {ph[:16]}...")

        complete_run(con, run_id, rows_written=temporal_count, derivation_params_hash=ph)
    except Exception as e:
        fail_run(con, run_id, str(e))
        log.error("Derivation failed", error=str(e))
        raise typer.Exit(1)


# ---------------------------------------------------------------------------
# graph
# ---------------------------------------------------------------------------

@app.command()
def graph(log_level: str = typer.Option("INFO")) -> None:
    """Build and persist relationship graph from _effective views."""
    con = get_conn()
    run_id = start_run(con, "graph")
    configure_logging(run_id, "graph", log_level)
    log = get_logger("pulse.graph")

    try:
        from agents.graph.builder import build_graph
        from agents.graph.persist import persist_graph
        from agents.graph.metrics import compute_all_metrics
        from agents.graph.prospect_inference import run_prospect_inference
        from agents.graph.invested_with_edges import build_invested_with_edges
        from agents.uncertainty.aggregator import derive_relationship_uncertainty, derive_signal_uncertainty
        from agents.uncertainty.temporal import derive_temporal

        log.info("Building invested_with edges from shared fund participation")
        iw_counts = build_invested_with_edges(con)
        log.info("invested_with edges complete", **iw_counts)

        log.info("Running prospect syndicate connectivity inference")
        infer_counts = run_prospect_inference(con)
        log.info("Prospect inference complete", **infer_counts)

        derive_relationship_uncertainty(con)
        derive_signal_uncertainty(con)
        derive_temporal(con)
        log.info("Re-derived uncertainty + temporal columns for inference edges")

        log.info("Building relationship graph")
        G = build_graph(con)
        log.info("Graph built", nodes=G.number_of_nodes(), edges=G.number_of_edges())

        paths = persist_graph(G, run_id)
        log.info("Graph persisted", **paths)

        metrics = compute_all_metrics(G)
        log.info("Graph metrics computed",
                 nodes=metrics["nodes"], edges=metrics["edges"],
                 components=metrics.get("connected_components", "n/a"))

        rprint(f"\n[bold green]Graph complete[/bold green]")
        rprint(f"  Nodes: {metrics['nodes']}")
        rprint(f"  Edges: {metrics['edges']}")
        rprint(f"  Density: {metrics.get('density', 0):.4f}")
        rprint(f"  Connected components: {metrics.get('connected_components', 'n/a')}")
        rprint(f"\n  [bold]Prospect inference[/bold]")
        rprint(f"  Prospects analyzed: {infer_counts.get('prospects_analyzed', 0)}")
        rprint(f"  Mutual connection edges: {infer_counts.get('mutual_connection_edges', 0)}")
        rprint(f"  Connectivity CSV: {infer_counts.get('connectivity_csv', 'n/a')}")

        complete_run(con, run_id, rows_written=G.number_of_edges() + infer_counts.get('mutual_connection_edges', 0),
                     artifact_uris=list(paths.values()))
    except Exception as e:
        fail_run(con, run_id, str(e))
        log.error("Graph build failed", error=str(e))
        raise typer.Exit(1)


# ---------------------------------------------------------------------------
# score
# ---------------------------------------------------------------------------

@app.command()
def score(log_level: str = typer.Option("INFO")) -> None:
    """ICP scoring + rejection extraction → icp_scores + rejections tables."""
    con = get_conn()
    run_id = start_run(con, "score")
    configure_logging(run_id, "score", log_level)
    log = get_logger("pulse.score")

    try:
        from agents.scoring.icp_scorer import run_icp_scoring
        from agents.scoring.rejection_extractor import run_rejection_extraction
        from agents.scoring.signal_extractor import run_signal_extraction
        from agents.scoring.signal_evidence_writer import purge_orphan_signal_evidence

        purged = purge_orphan_signal_evidence(con)
        if purged:
            log.info("Purged orphan signal_evidence rows", count=purged)

        log.info("Running ICP scoring")
        icp_counts = run_icp_scoring(con)
        log.info("ICP scoring done", **icp_counts)

        log.info("Extracting rejections")
        rej_counts = run_rejection_extraction(con)
        log.info("Rejection extraction done", **rej_counts)

        log.info("Extracting LP signals")
        sig_counts = run_signal_extraction(con)
        log.info("Signal extraction done", **sig_counts)

        from agents.scoring.latent_signal_extractor import run_latent_signal_extraction
        from agents.uncertainty.aggregator import derive_signal_uncertainty

        log.info("Extracting latent signals from investments/graph/icp")
        latent_counts = run_latent_signal_extraction(con)
        log.info("Latent signal extraction done", **latent_counts)

        from agents.scoring.contradiction_detector import run_contradiction_detection
        log.info("Detecting signal contradictions")
        contra_counts = run_contradiction_detection(con)
        log.info("Contradiction detection done", **contra_counts)

        derive_signal_uncertainty(con)

        total_written = (
            icp_counts["scored"]
            + sum(rej_counts[k] for k in ("stated", "inferred", "structural"))
            + sum(sig_counts.values())
        )

        rprint(f"\n[bold green]Scoring complete[/bold green]")
        rprint(f"\n  [bold]ICP Scores[/bold]")
        rprint(f"  Allocators scored: {icp_counts['scored']}")
        rprint(f"  Unmatched source rows: {icp_counts.get('unmatched_rows', 0)}")

        # ICP score breakdown table
        t = Table(title="ICP Score Distribution")
        t.add_column("Tier")
        t.add_column("Count")
        t.add_column("Meaning")
        t.add_row("[bold green]Tier 1[/bold green]", str(icp_counts["tier_1"]),
                  "Core pass + strong fit + client approved")
        t.add_row("[green]Tier 2[/green]", str(icp_counts["tier_2"]),
                  "Core pass + moderate fit")
        t.add_row("[yellow]Tier 3[/yellow]", str(icp_counts["tier_3"]),
                  "Core pass, weak signals or pending")
        t.add_row("[red]Tier 4[/red]", str(icp_counts["tier_4"]),
                  "Excluded or core criteria failed")
        console.print(t)

        rprint(f"\n  [bold]Rejections[/bold]")
        rprint(f"  Stated (client decision):  {rej_counts['stated']}")
        rprint(f"  Structural (blacklist):    {rej_counts['structural']}")
        rprint(f"  Inferred (from text):      {rej_counts['inferred']}")

        sig_table = Table(title="LP Signal Extraction")
        sig_table.add_column("Signal Type")
        sig_table.add_column("LPs Populated")
        for sig_type, cnt in sorted(sig_counts.items()):
            sig_table.add_row(sig_type, str(cnt))
        console.print(sig_table)

        complete_run(con, run_id, rows_written=total_written)
    except Exception as e:
        fail_run(con, run_id, str(e))
        log.error("Scoring failed", error=str(e))
        import traceback; traceback.print_exc()
        raise typer.Exit(1)


# ---------------------------------------------------------------------------
# calibrate
# ---------------------------------------------------------------------------

@app.command()
def calibrate(
    log_level: str = typer.Option("INFO"),
    re_score: bool = typer.Option(False, help="Re-run ICP scoring after writing calibrated thresholds"),
) -> None:
    """Benchmark calibration overlay + auto-tune tier thresholds vs ContraVC Top 200."""
    from agents.scoring.icp_spec import get_tier_thresholds

    con = get_conn()
    before_t1, before_t2 = get_tier_thresholds()
    run_id = start_run(con, "score", {
        "subcommand": "calibrate",
        "before_thresholds": {"TIER_1_FIT_MIN": before_t1, "TIER_2_FIT_MIN": before_t2},
    })
    configure_logging(run_id, "calibrate", log_level)
    log = get_logger("pulse.calibrate")

    try:
        from agents.scoring.calibration import run_calibration

        log.info("Running benchmark calibration")
        result = run_calibration(con)
        log.info("Calibration complete", **{k: v for k, v in result.items() if k != "auto_tune"})

        if re_score and result.get("thresholds_updated"):
            log.info("Re-scoring with calibrated thresholds")
            from agents.scoring.icp_scorer import run_icp_scoring
            icp_counts = run_icp_scoring(con)
            log.info("Re-score done", **icp_counts)
            result = run_calibration(con)

        rprint(f"\n[bold green]Calibration complete[/bold green]")
        rprint(f"  Scored prospects: {result.get('total_scored', 0)}")
        rprint(f"  Benchmark overlap: {result.get('overlap_count', 0)}")
        rprint(f"  Tier 1 in Contra Top 50: {result.get('tier1_in_contra_top50', 0)}")
        if result.get("thresholds_updated"):
            th = result["thresholds_updated"]
            rprint(f"  Updated thresholds: TIER_1={th['TIER_1_FIT_MIN']}, TIER_2={th['TIER_2_FIT_MIN']}")
        elif result.get("auto_tune", {}).get("skipped"):
            rprint(f"  [yellow]Auto-tune skipped: {result['auto_tune'].get('reason')}[/yellow]")
        rprint(f"  Overlay: {result.get('overlay_path')}")
        rprint(f"  Summary: {result.get('summary_path')}")
        rprint(f"  Ranked list: {result.get('ranked_list_path')}")

        complete_run(con, run_id, rows_written=result.get("total_scored", 0))
    except Exception as e:
        fail_run(con, run_id, str(e))
        log.error("Calibration failed", error=str(e))
        raise typer.Exit(1)


# ---------------------------------------------------------------------------
# run-all
# ---------------------------------------------------------------------------

@app.command(name="run-all")
def run_all(
    raw_dir: Path = typer.Option(ROOT / "raw_data"),
    log_level: str = typer.Option("INFO"),
) -> None:
    """Full pipeline: ingest → normalize → extract → derive → graph → score → calibrate."""
    rprint("\n[bold blue]PULSE — Full Pipeline Run[/bold blue]\n")

    for stage_name, stage_fn in [
        ("ingest", lambda: ingest(raw_dir=raw_dir, log_level=log_level)),
        ("normalize", lambda: normalize(log_level=log_level)),
        ("extract", lambda: extract(log_level=log_level)),
        ("derive", lambda: derive(log_level=log_level)),
        ("graph", lambda: graph(log_level=log_level)),
        ("score", lambda: score(log_level=log_level)),
        ("calibrate", lambda: calibrate(log_level=log_level)),
    ]:
        try:
            stage_fn()
        except SystemExit as e:
            if e.code != 0:
                rprint(f"\n[bold red]Pipeline aborted at stage: {stage_name}[/bold red]")
                raise typer.Exit(1)
        except Exception as e:
            rprint(f"\n[bold red]Pipeline aborted at stage: {stage_name}: {e}[/bold red]")
            raise typer.Exit(1)

    rprint("\n[bold green]Pipeline complete.[/bold green]")


# ---------------------------------------------------------------------------
# refresh — autonomous full-pipeline + exports (replaces run-all for partners)
# ---------------------------------------------------------------------------

@app.command()
def refresh(
    raw_dir: Path = typer.Option(ROOT / "raw_data", help="Source files directory"),
    json_log: bool = typer.Option(
        False, "--json-log",
        help="Emit JSON progress lines (used by the Streamlit UI subprocess pipe)",
    ),
    skip_evals: bool = typer.Option(False, "--skip-evals", help="Skip data quality checks"),
) -> None:
    """
    Full pipeline + CSV exports in one command. The autonomous partner workflow.

    Runs ingest → normalize → extract → derive → graph → score → calibrate → exports
    then writes First_LPs_Outreach_Pack.csv and First_LPs_Ready.csv.
    """
    from pulse.orchestrator import run_refresh

    result = run_refresh(raw_dir=raw_dir, json_log=json_log, skip_evals=skip_evals)

    if json_log:
        import json as _json
        print(_json.dumps({"stage": "refresh", "status": "complete" if result.success else "failed",
                           "result": result.to_dict()}), flush=True)
        raise typer.Exit(0 if result.success else 1)

    if not result.success:
        rprint(f"\n[bold red]Refresh failed at stage: {result.failed_stage}[/bold red]")
        rprint(f"  {result.error}")
        raise typer.Exit(1)


# ---------------------------------------------------------------------------
# explore — local Streamlit LP viewer
# ---------------------------------------------------------------------------

@app.command()
def explore(
    port: int = typer.Option(8501, "--port", "-p", help="Streamlit server port"),
) -> None:
    """Launch local read-only LP explorer (Streamlit). Requires: pip install -e \".[explore]\" """
    try:
        import streamlit  # noqa: F401
    except ImportError:
        rprint(
            "[red]Streamlit not installed.[/red] Run: [bold]pip install -e \".[explore]\"[/bold]"
        )
        raise typer.Exit(1)

    import subprocess

    app_path = ROOT / "pulse" / "explore" / "app.py"
    if not app_path.exists():
        rprint(f"[red]Explorer app not found: {app_path}[/red]")
        raise typer.Exit(1)

    rprint(f"[green]Starting PULSE LP Explorer on http://localhost:{port}[/green]")
    rprint("[dim]Read-only DB connection — avoid running ingest/normalize in parallel.[/dim]")
    cmd = [
        sys.executable,
        "-m",
        "streamlit",
        "run",
        str(app_path),
        "--server.port",
        str(port),
        "--server.headless",
        "true",
    ]
    raise typer.Exit(subprocess.call(cmd))


# ---------------------------------------------------------------------------
# crm-screen — FundingStack add/skip helper
# ---------------------------------------------------------------------------

@app.command("crm-screen")
def crm_screen(
    name: Optional[str] = typer.Argument(None, help="Investor or LP name to classify"),
    details: str = typer.Option("", "--details", "-d", help="Bio, thesis, or notes for ICP text scoring"),
    investor_type: str = typer.Option("", "--type", "-t", help="Investor type (e.g. Family Offices, Fund of Funds)"),
    location: str = typer.Option("", "--location", "-l", help="Geography / location string"),
    batch: Optional[Path] = typer.Option(None, "--batch", "-b", help="Screen a FundingStack CSV export"),
    output: Optional[Path] = typer.Option(None, "--output", "-o", help="Output path for batch results"),
    json_out: bool = typer.Option(False, "--json", help="Print full result as JSON"),
) -> None:
    """
    Classify whether to add someone to FundingStack CRM.

    Uses Contra Top 200, syndicate LP roster/investments, and Fund Rating Guide rubric.
    """
    from pulse.crm_screener import screen_batch_csv, screen_prospect

    if batch:
        out_path = screen_batch_csv(batch, output)
        rprint(f"[green]Wrote {out_path}[/green]")
        return

    if not name:
        rprint("[red]Provide a NAME or --batch CSV.[/red] Example: [bold]pulse crm-screen \"Robert Alexander\"[/bold]")
        raise typer.Exit(1)

    result = screen_prospect(
        name,
        details=details,
        investor_type=investor_type,
        location=location,
    )

    if json_out:
        import json
        rprint(json.dumps(result.to_dict(), indent=2, default=str))
        return

    color = {"add": "green", "review": "yellow", "skip": "red"}.get(result.verdict, "white")
    rprint(f"\n[bold]{result.name}[/bold]  →  [{color}]{result.verdict.upper()}[/{color}]  "
           f"(score {result.score}, {result.confidence} confidence)")
    if result.matched_name and result.matched_name.lower() != name.lower():
        rprint(f"[dim]Matched as: {result.matched_name}[/dim]")
    if result.reasons:
        rprint("\n[bold]Reasons[/bold]")
        for r in result.reasons:
            rprint(f"  • {r}")
    if result.checklist and result.verdict == "review":
        rprint("\n[bold]Fund Rating Guide — verify before adding[/bold]")
        for c in result.checklist[:5]:
            rprint(f"  • {c}")
    raise typer.Exit(0 if result.verdict == "add" else 1)


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------

@app.command()
def status(verbose: bool = typer.Option(False, "--verbose", "-v", help="Show DB row counts and invariant checks")) -> None:
    """Show last run summary per pipeline stage."""
    con = get_conn()
    stages = get_stage_status(con)

    if not stages:
        rprint("[yellow]No pipeline runs found. Run `pulse run-all` to start.[/yellow]")
        return

    t = Table(title="Pipeline Status")
    t.add_column("Stage")
    t.add_column("Status")
    t.add_column("Started")
    t.add_column("Rows Processed")
    t.add_column("Rows Written")
    t.add_column("Error")

    for s in stages:
        status_color = "green" if s["status"] == "completed" else "red" if s["status"] == "failed" else "yellow"
        t.add_row(
            s["stage"],
            f"[{status_color}]{s['status']}[/{status_color}]",
            str(s["started_at"])[:19] if s["started_at"] else "",
            str(s["rows_processed"]),
            str(s["rows_written"]),
            (s["error"] or "")[:60],
        )
    console.print(t)

    if verbose:
        _print_db_snapshot(con)


# ---------------------------------------------------------------------------
# review subcommands
# ---------------------------------------------------------------------------

@review_app.command(name="list")
def review_list(
    target_type: Optional[str] = typer.Option(None, help="Filter by target_type"),
) -> None:
    """List pending review queue items."""
    from agents.reviews.queue_writer import read_queue, VALID_TARGET_TYPES

    types_to_show = [target_type] if target_type else list(VALID_TARGET_TYPES)

    for tt in sorted(types_to_show):
        items = read_queue(tt)
        if not items:
            rprint(f"[dim]{tt}: no items[/dim]")
            continue
        t = Table(title=f"Queue: {tt}")
        t.add_column("entity_id", max_width=16)
        t.add_column("confidence")
        t.add_column("reason")
        t.add_column("surfaced_at")
        for item in items[-20:]:  # show last 20
            t.add_row(
                str(item.get("entity_id", ""))[:16],
                f"{item.get('confidence', ''):.2f}" if item.get("confidence") else "",
                item.get("reason", "")[:50],
                str(item.get("surfaced_at", ""))[:19],
            )
        console.print(t)


@review_app.command(name="ingest")
def review_ingest(
    decisions_path: Path = typer.Argument(..., help="Path to jsonl file with reviewer decisions"),
    reviewer: str = typer.Option("human", help="Reviewer identifier"),
) -> None:
    """Append reviewer decisions from a jsonl file to human_reviews."""
    con = get_conn()
    from agents.reviews.override_applier import ingest_decisions
    inserted = ingest_decisions(decisions_path, con, reviewer)
    rprint(f"[green]Inserted {inserted} review decisions.[/green]")


@review_app.command(name="status")
def review_status() -> None:
    """Show review queue counts per target_type."""
    from agents.reviews.queue_writer import queue_counts
    con = get_conn()
    from agents.reviews.override_applier import get_review_status

    rprint("\n[bold]Review Queue Sizes[/bold]")
    counts = queue_counts()
    t = Table()
    t.add_column("Queue")
    t.add_column("Items")
    for tt, cnt in sorted(counts.items()):
        t.add_row(tt, str(cnt))
    console.print(t)

    rprint("\n[bold]Human Review Decisions in DB[/bold]")
    db_status = get_review_status(con)
    t2 = Table()
    t2.add_column("Target Type")
    t2.add_column("Decision")
    t2.add_column("Count")
    for target_type, decisions in sorted(db_status.items()):
        for decision, cnt in sorted(decisions.items()):
            t2.add_row(target_type, decision, str(cnt))
    console.print(t2)


# ---------------------------------------------------------------------------
# research subcommands
# ---------------------------------------------------------------------------

@research_app.command(name="enrich")
def research_enrich(
    population: str = typer.Option(
        "institutional_prospect",
        "--population", "-p",
        help="Allocator population to target (institutional_prospect | syndicate_lp | all).",
    ),
    only_unknown: bool = typer.Option(
        True,
        "--only-unknown/--all-fields",
        help="If set, only process rows where allocator_type is 'unknown' or NULL.",
    ),
    research_fit: bool = typer.Option(
        False,
        "--research-fit",
        help=(
            "Deep fit research: run over ALL allocators (even those with taxonomy filled). "
            "Produces processed_data/research_notes/{id}.md per LP and fit_summary.csv. "
            "Use this to research LPs for MyAsiaVC fit — EM track record, AI portfolio, "
            "emerging manager history, check sizes, venture LP focus."
        ),
    ),
    limit: Optional[int] = typer.Option(
        None,
        "--limit", "-n",
        help="Maximum number of allocators to process (default: all).",
    ),
    log_level: str = typer.Option("INFO", help="Log level"),
) -> None:
    """
    Enrich allocator attributes and research LP fit via web search + LLM.

    DEFAULT MODE (taxonomy fill):
      Targets allocators with unknown type. Fills NULL taxonomy columns (allocator_type,
      geography, em_appetite, ai_appetite) via Tavily search + Groq/LLM extraction.

    FIT RESEARCH MODE (--research-fit):
      Researches ALL LPs for fit with MyAsiaVC (AI-native, emerging markets, $30M raise).
      Searches 3 targeted queries per LP (profile, EM portfolio, AI/tech portfolio).
      Writes a markdown note per LP to processed_data/research_notes/{id}.md.
      Writes a summary to processed_data/research_notes/fit_summary.csv — open this
      to see each LP's fit verdict (strong/moderate/weak), EM track record, AI portfolio,
      emerging-manager history, and check sizes.

    Requires: PULSE_LLM_PROVIDER + matching API key (GROQ_API_KEY, etc.)
    Optional: PULSE_SEARCH_PROVIDER=tavily + TAVILY_API_KEY (strongly recommended)
    """
    con = get_conn()
    run_id = start_run(con, "research", {"subcommand": "enrich", "population": population})
    configure_logging(run_id, "research_enrich", log_level)
    log = get_logger("pulse.research.enrich")

    try:
        from agents.research.enrichment_agent import run_enrichment

        log.info(
            "Starting LP enrichment",
            population=population,
            only_unknown=only_unknown,
            research_fit=research_fit,
            limit=limit,
        )
        counts = run_enrichment(
            con,
            population=population,
            only_unknown_type=only_unknown,
            research_fit=research_fit,
            limit=limit,
        )
        log.info("Enrichment complete", **counts)

        rprint(f"\n[bold green]Enrichment complete[/bold green]")
        t = Table(title="Enrichment Results")
        t.add_column("Metric")
        t.add_column("Count", justify="right")
        for k, v in counts.items():
            if k != "skipped_reason":
                t.add_row(k.replace("_", " "), str(v))
        console.print(t)

        if counts.get("fit_notes_written", 0) > 0:
            from pathlib import Path
            summary_path = Path("processed_data") / "research_notes" / "fit_summary.csv"
            rprint(
                f"\n[bold cyan]Fit research notes written![/bold cyan]\n"
                f"  Per-LP notes → [dim]processed_data/research_notes/{{allocator_id}}.md[/dim]\n"
                f"  Summary CSV  → [bold]{summary_path}[/bold]  ← open this to see fit verdicts"
            )

        if "skipped_reason" in counts:
            rprint(f"\n[yellow]Skipped: {counts['skipped_reason']}[/yellow]")
            rprint(
                "[dim]Set PULSE_LLM_PROVIDER and the matching API key to enable enrichment.[/dim]"
            )

        complete_run(con, run_id, rows_written=counts.get("columns_updated", 0))

    except Exception as e:
        fail_run(con, run_id, str(e))
        log.error("Enrichment failed", error=str(e))
        import traceback; traceback.print_exc()
        raise typer.Exit(1)


@research_app.command(name="ask")
def research_ask(
    question: str = typer.Argument(..., help="Natural-language question about the PULSE database."),
    log_level: str = typer.Option("INFO", help="Log level"),
    show_sql: bool = typer.Option(False, "--show-sql", help="Print the generated SQL."),
) -> None:
    """
    Answer a natural-language question over the PULSE database (read-only).

    Generates a DuckDB SELECT via LLM, validates it (SELECT-only, auto-LIMIT),
    executes it, and returns a narrative answer.

    Requires: PULSE_LLM_PROVIDER set + matching API key.
    All queries are read-only; no data is written.

    Examples:
      pulse research ask "Which tier_1 LPs are based in Singapore?"
      pulse research ask "Show me the top 10 LPs by fit_score with their geography"
    """
    con = get_conn(read_only=True)
    configure_logging("research_ask", "research_ask", log_level)
    log = get_logger("pulse.research.ask")

    try:
        from agents.research.qa_agent import ask as qa_ask

        log.info("Q&A question received", question=question[:80])
        answer = qa_ask(con, question)

        rprint(f"\n[bold]Answer[/bold]")
        rprint(answer.narrative)
        rprint(f"\n[dim]Confidence: {answer.confidence:.2f} | Rows: {answer.row_count} | "
               f"Tables: {', '.join(answer.cited_tables)}[/dim]")

        if show_sql:
            rprint(f"\n[bold]Generated SQL[/bold]")
            rprint(f"[dim]{answer.generated_sql}[/dim]")

        if answer.rows:
            rprint(f"\n[bold]Results ({min(answer.row_count, 20)} of {answer.row_count} shown)[/bold]")
            if answer.rows:
                t = Table()
                for col in answer.rows[0].keys():
                    t.add_column(str(col), max_width=30)
                for row in answer.rows[:20]:
                    t.add_row(*[str(v)[:30] if v is not None else "" for v in row.values()])
                console.print(t)

        log.info("Q&A complete", rows=answer.row_count)

    except Exception as e:
        log.error("Q&A failed", error=str(e))
        rprint(f"[red]Q&A failed: {e}[/red]")
        raise typer.Exit(1)


@research_app.command(name="brief")
def research_brief(
    allocator_id: str = typer.Argument(..., help="Allocator UUID to generate brief for."),
    log_level: str = typer.Option("INFO", help="Log level"),
) -> None:
    """
    Generate an outreach brief for a single LP prospect.

    Assembles ICP tier, warm paths, ego network, and signals, then synthesizes
    a structured brief (thesis fit, warm-path intro, talking points, risks).

    If PULSE_LLM_PROVIDER is set: LLM-synthesized BriefSections (structured).
    Otherwise: deterministic templated brief from the same data.

    Output: processed_data/briefs/{allocator_id}_{name}.md
    """
    con = get_conn()
    configure_logging("research_brief", "research_brief", log_level)
    log = get_logger("pulse.research.brief")

    try:
        from agents.research.brief_agent import generate_brief

        log.info("Generating brief", allocator_id=allocator_id)
        result = generate_brief(con, allocator_id)

        if result.get("error"):
            rprint(f"[red]Brief generation failed: {result['error']}[/red]")
            raise typer.Exit(1)

        rprint(f"\n[bold green]Brief generated[/bold green]")
        rprint(f"  Allocator: [bold]{result['canonical_name']}[/bold]")
        rprint(f"  Mode: {result['mode']}")
        rprint(f"  Output: {result['brief_path']}")

        log.info(
            "Brief complete",
            canonical_name=result["canonical_name"],
            mode=result["mode"],
            path=result["brief_path"],
        )

    except typer.Exit:
        raise
    except Exception as e:
        log.error("Brief generation failed", error=str(e))
        rprint(f"[red]Error: {e}[/red]")
        import traceback; traceback.print_exc()
        raise typer.Exit(1)


@research_app.command(name="ontology")
def research_ontology(
    min_confidence: float = typer.Option(
        0.40,
        "--min-confidence",
        help="Documents with heuristic terms below this confidence are re-processed by LLM.",
    ),
    limit: Optional[int] = typer.Option(
        None,
        "--limit", "-n",
        help="Maximum documents to process (default: all targeted).",
    ),
    source_types: str = typer.Option(
        "pdf,docx,api",
        "--source-types",
        help="Comma-separated source types to target (default: pdf,docx,api).",
    ),
    log_level: str = typer.Option("INFO", help="Log level"),
) -> None:
    """
    Run LLM ontology enrichment over prose documents (pdf, docx, api).

    Targets documents where the heuristic extractor produced low-confidence
    or zero terms. LLM extracts ontology terms + relationship hints, caches
    results, and persists through the existing ontology pipeline.

    Requires: PULSE_LLM_PROVIDER set (anthropic|openai|gemini) + matching API key.
    Results feed into `pulse derive` on the next run.
    """
    con = get_conn()
    run_id = start_run(con, "research", {"subcommand": "ontology", "min_confidence": min_confidence})
    configure_logging(run_id, "research_ontology", log_level)
    log = get_logger("pulse.research.ontology")

    try:
        from agents.research.ontology_enricher import run_ontology_enrichment

        types_list = [t.strip() for t in source_types.split(",") if t.strip()]
        log.info(
            "Starting ontology LLM enrichment",
            min_confidence=min_confidence,
            source_types=types_list,
            limit=limit,
        )
        counts = run_ontology_enrichment(
            con,
            min_confidence_threshold=min_confidence,
            target_source_types=types_list,
            limit=limit,
        )
        log.info("Ontology enrichment complete", **counts)

        rprint(f"\n[bold green]Ontology enrichment complete[/bold green]")
        t = Table(title="Ontology Enrichment Results")
        t.add_column("Metric")
        t.add_column("Count", justify="right")
        for k, v in counts.items():
            if k != "skipped_reason":
                t.add_row(k, str(v))
        console.print(t)

        if "skipped_reason" in counts:
            rprint(f"\n[yellow]Skipped: {counts['skipped_reason']}[/yellow]")
            rprint(
                "[dim]Set PULSE_LLM_PROVIDER and the matching API key, "
                "then run `pulse derive` to recompute confidence.[/dim]"
            )
        else:
            rprint("[dim]Run `pulse derive` to recompute uncertainty for new terms.[/dim]")

        complete_run(
            con, run_id,
            rows_processed=counts.get("documents_targeted", 0),
            rows_written=counts.get("terms_extracted", 0),
        )

    except Exception as e:
        fail_run(con, run_id, str(e))
        log.error("Ontology enrichment failed", error=str(e))
        import traceback; traceback.print_exc()
        raise typer.Exit(1)

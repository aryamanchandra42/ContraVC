"""Contra CLI."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import typer
from rich import print as rprint
from rich.console import Console
from rich.table import Table

console = Console()

app = typer.Typer(name="contra", help="Contra — LP intelligence backend + CRM gate")
intel_app = typer.Typer(help="Intelligence subcommands: syndicate, paths, contacts, summary")
app.add_typer(intel_app, name="intel")


@app.command("refresh")
def refresh_cmd(
    raw_dir: Optional[Path] = typer.Option(None, "--raw-dir", help="Path to raw_data"),
) -> None:
    """Rebuild contra.duckdb from raw_data/."""
    from contra.orchestrator import run_refresh

    result = run_refresh(raw_dir)
    if not result.success:
        rprint(f"[red]Refresh failed at {result.failed_stage}: {result.error}[/red]")
        raise typer.Exit(1)
    rprint("[green]Refresh complete.[/green]")


@app.command("catalog")
def catalog_cmd(json_out: bool = typer.Option(False, "--json")) -> None:
    """Print data estate summary."""
    from agents.db import get_conn
    from contra.intelligence.catalog import get_catalog

    con = get_conn(read_only=True)
    data = get_catalog(con)
    con.close()
    if json_out:
        rprint(json.dumps(data, indent=2, default=str))
        return
    rprint("\n[bold]Contra Data Catalog[/bold]\n")
    for t in data.get("tables", []):
        rprint(f"  {t['key']:22} {t.get('row_count', '?'):>8}  {t['description']}")
    pops = data.get("allocator_populations", {})
    if pops:
        rprint("\n[bold]Allocators by population[/bold]")
        for k, v in pops.items():
            rprint(f"  {k}: {v}")


@app.command("gate")
def gate_cmd(
    name: str = typer.Argument(..., help="LP / investor name to screen"),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """YES / REVIEW / NO — add to FundingStack CRM? (requires LLM + web search API keys)."""
    from agents.db import get_conn
    from contra.gate import run_gate

    con = get_conn(read_only=True)
    try:
        result = run_gate(con, name)
    except RuntimeError as exc:
        rprint(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    finally:
        con.close()

    if json_out:
        rprint(json.dumps(result.model_dump(), indent=2))
        return

    rec = result.assessment.recommendation
    if rec == "yes":
        label, color = "YES — ADD TO CRM", "green"
    elif rec == "review":
        label, color = "REVIEW", "yellow"
    else:
        label, color = "NO — SKIP", "red"

    rprint(f"\n[bold]{name}[/bold] → [{color}]{label}[/{color}] ({result.confidence} confidence)")
    rprint(f"[dim]{result.summary}[/dim]\n")

    # Hard blocks
    if result.assessment.hard_blocks:
        rprint("[bold red]Hard blocks[/bold red]")
        for b in result.assessment.hard_blocks:
            rprint(f"  [red]✗[/red] {b}")
        rprint()

    # Core gates
    rprint("[bold]Core Gates[/bold]  (C1=VC fund LP  C2=Emerging Mgr  C3=AI/tech  C4=Geography)")
    gate_row = []
    for g in result.assessment.core_gates:
        if g.status == "pass":
            gate_row.append(f"[green]{g.gate.upper()} PASS[/green]")
        elif g.status == "fail":
            gate_row.append(f"[red]{g.gate.upper()} FAIL[/red]")
        else:
            gate_row.append(f"[dim]{g.gate.upper()} ?[/dim]")
    rprint("  " + "   ".join(gate_row))
    for g in result.assessment.core_gates:
        rprint(f"  [dim]{g.gate.upper()}:[/dim] {g.evidence[:120]}")
    rprint()

    # Signals
    met = result.assessment.signals_met
    req = result.assessment.signals_required
    rprint(f"[bold]Signals[/bold]  ({met}/{req} met)")
    for s in result.assessment.signals:
        check = "[green]✓[/green]" if s.met else "[dim]✗[/dim]"
        rprint(f"  {check} [{s.source}] {s.label}")
        rprint(f"    [dim]{s.detail[:100]}[/dim]")
    rprint()

    # LLM reasons
    if result.reasons:
        rprint("[bold]Reasons[/bold]")
        for r in result.reasons:
            rprint(f"  - {r}")
    if result.backend_evidence:
        rprint("\n[bold]Backend evidence[/bold]")
        for r in result.backend_evidence[:5]:
            rprint(f"  - {r}")
    if result.online_evidence:
        rprint("\n[bold]Online evidence[/bold]")
        for r in result.online_evidence[:5]:
            rprint(f"  - {r}")
    if result.conflicts:
        rprint("\n[bold]Conflicts[/bold]")
        for r in result.conflicts:
            rprint(f"  [yellow]![/yellow] {r}")

    rprint(f"\n[dim]Session ID: {result.session_id}[/dim]")


# ---------------------------------------------------------------------------
# contra nfx-scrape
# ---------------------------------------------------------------------------

@app.command("nfx-scrape")
def nfx_scrape_cmd(
    max_investors: int = typer.Option(500, "--max", help="Stop after N investors scraped from NFX Signal"),
    headless: bool = typer.Option(True, "--headless/--no-headless", help="Run Chrome headlessly (default) or show window"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Scrape + dedup but skip gate calls and CRM writes"),
    delay_ms: int = typer.Option(1200, "--delay-ms", help="Pause between gate calls in ms (throttle API usage)"),
) -> None:
    """Scrape NFX Signal investors and save qualified ones to CRM via LP Gate.

    Requires NFX_USERNAME and NFX_PASSWORD in .env.
    Uses a 3-layer dedup: exact CRM match -> fuzzy allocator match -> checkpoint.
    Investors that pass the Gate (YES/REVIEW) are persisted to contra.duckdb.
    Resume-safe: a checkpoint file tracks processed names across runs.
    """
    from agents.ingestion.nfx_selenium_scraper import run_scrape

    try:
        stats = run_scrape(
            max_investors=max_investors,
            headless=headless,
            dry_run=dry_run,
            delay_ms=delay_ms,
        )
    except RuntimeError as exc:
        rprint(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc

    saved = stats.get("yes", 0) + stats.get("review", 0)
    if saved:
        rprint(f"\n[green]{saved} investor(s) saved to CRM (YES: {stats['yes']}  REVIEW: {stats['review']})[/green]")
    else:
        rprint("\n[dim]No new investors qualified for CRM in this run.[/dim]")


# ---------------------------------------------------------------------------
# contra pitchbook-scrape
# ---------------------------------------------------------------------------

@app.command("pitchbook-scrape")
def pitchbook_scrape_cmd(
    max_lps: int = typer.Option(200, "--max", help="Stop after N LPs collected from PitchBook"),
    headless: bool = typer.Option(True, "--headless/--no-headless", help="Run Chrome headlessly (default) or show window"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Collect + dedup but skip gate calls and CRM writes"),
    delay_ms: int = typer.Option(2000, "--delay-ms", help="Pause between gate calls in ms (throttle API usage)"),
    sso: bool = typer.Option(False, "--sso", help="Sign in with SSO (automated IESE/Okta/Azure AD flow)"),
    brave: bool = typer.Option(False, "--brave", help="Use existing Brave browser profile (already logged into PitchBook — fastest option, no credentials needed)"),
    connect_port: Optional[int] = typer.Option(None, "--connect-port", help="Attach to a running Brave/Chrome on this CDP port (launch browser with --remote-debugging-port=PORT first)"),
    clear_session: bool = typer.Option(False, "--clear-session", help="Delete saved session cookies and force a fresh login"),
) -> None:
    """Scrape PitchBook LP search and save qualified ones to CRM via LP Gate.

    Authentication options (pick one):

      --brave         Use Brave browser with its existing profile — already logged in,
                      no credentials needed. Close Brave before running.

      --connect-port  Attach to a running Brave/Chrome that was launched with
                      --remote-debugging-port=PORT. Brave stays open.

      --sso           Automated IESE / institutional SSO flow via browser window.
                      Requires PITCHBOOK_EMAIL + PITCHBOOK_PASSWORD in .env.

      (default)       Standard email + password login via headless Chrome.
                      Requires PITCHBOOK_EMAIL + PITCHBOOK_PASSWORD in .env.

    After any successful login the session is cached; subsequent runs skip login.
    Use --clear-session to force a fresh authentication.

    Uses a 3-layer dedup: exact CRM match -> fuzzy allocator match -> checkpoint.
    Investors that pass the Gate (YES/REVIEW) are persisted to contra.duckdb.
    Resume-safe: a checkpoint file tracks processed names across runs.
    """
    from agents.ingestion.pitchbook_scraper import run_scrape

    try:
        stats = run_scrape(
            max_lps=max_lps,
            headless=headless,
            dry_run=dry_run,
            delay_ms=delay_ms,
            use_sso=sso,
            clear_session=clear_session,
            use_brave=brave,
            connect_port=connect_port,
        )
    except RuntimeError as exc:
        rprint(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc

    saved = stats.get("yes", 0) + stats.get("review", 0)
    if saved:
        rprint(f"\n[green]{saved} LP(s) saved to CRM (YES: {stats['yes']}  REVIEW: {stats['review']})[/green]")
    else:
        rprint("\n[dim]No new LPs qualified for CRM in this run.[/dim]")


# ---------------------------------------------------------------------------
# contra enrich
# ---------------------------------------------------------------------------

@app.command("enrich")
def enrich_cmd(
    population: str = typer.Option("institutional", "--population", "-p",
                                   help="institutional | syndicate"),
    limit: int = typer.Option(20, "--limit", "-n", help="Max allocators to enrich"),
    unknown_only: bool = typer.Option(True, "--unknown-only/--all",
                                      help="Only enrich NULL/unknown type allocators"),
    research_fit: bool = typer.Option(False, "--research-fit",
                                      help="Deep fit notes (ignores unknown-only)"),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Batch-enrich allocator profiles via web search + LLM (uses .env API keys)."""
    from agents.db import get_conn
    from agents.research.enrichment_agent import run_enrichment

    pop_map = {"institutional": "institutional_prospect", "syndicate": "syndicate_lp"}
    pop = pop_map.get(population.lower(), population)

    con = get_conn()
    try:
        results = run_enrichment(
            con,
            population=pop,
            only_unknown_type=unknown_only,
            research_fit=research_fit,
            limit=limit,
        )
    finally:
        con.close()

    if json_out:
        rprint(json.dumps(results, indent=2, default=str))
        return

    rprint(f"\n[bold]Enrichment complete[/bold] (population={pop}, limit={limit})")
    for k, v in results.items():
        rprint(f"  {k}: {v}")


# ---------------------------------------------------------------------------
# contra intel subcommands
# ---------------------------------------------------------------------------

@intel_app.command("syndicate")
def intel_syndicate_cmd(
    top: int = typer.Option(30, "--top", "-n", help="Number of results"),
    min_fund_deals: int = typer.Option(1, "--min-fund-deals", help="Min fund deal count"),
    not_in_crm: bool = typer.Option(False, "--not-in-crm", help="Filter to LPs not in CRM"),
    export: Optional[Path] = typer.Option(None, "--export", help="Export CSV path"),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Rank syndicate LPs who behave like real fund LPs."""
    from agents.db import get_conn

    con = get_conn(read_only=True)
    try:
        crm_filter = "AND NOT in_crm" if not_in_crm else ""
        rows = con.execute(
            f"""
            SELECT
                canonical_name,
                fund_deal_count,
                spv_deal_count,
                total_committed_usd,
                fund_lp_ratio,
                is_fund_lp,
                is_upgrade_candidate,
                last_investment_date,
                in_crm,
                fund_lp_behavior_score,
                syndicate_depth_score,
                geography
            FROM v_syndicate_profile
            WHERE fund_deal_count >= ?
            {crm_filter}
            ORDER BY fund_lp_behavior_score DESC NULLS LAST, fund_deal_count DESC
            LIMIT ?
            """,
            [min_fund_deals, top],
        ).fetchdf()
    finally:
        con.close()

    if json_out:
        rprint(rows.to_json(orient="records", indent=2))
        return

    if export:
        rows.to_csv(export, index=False)
        rprint(f"[green]Exported {len(rows)} rows → {export}[/green]")
        return

    table = Table(title=f"Top Syndicate Fund-LPs (min_fund_deals={min_fund_deals})", show_lines=False)
    table.add_column("Name", style="bold", max_width=30)
    table.add_column("Fund", justify="right")
    table.add_column("SPV", justify="right")
    table.add_column("Total USD", justify="right")
    table.add_column("Ratio", justify="right")
    table.add_column("Score", justify="right")
    table.add_column("CRM", justify="center")
    table.add_column("Geo", max_width=12)

    for _, r in rows.iterrows():
        table.add_row(
            str(r["canonical_name"])[:30],
            str(int(r["fund_deal_count"])),
            str(int(r["spv_deal_count"])),
            f"${float(r['total_committed_usd']):,.0f}",
            f"{float(r['fund_lp_ratio']):.2f}",
            f"{float(r['fund_lp_behavior_score']):.3f}" if r["fund_lp_behavior_score"] else "-",
            "[green]Y[/green]" if r["in_crm"] else "[dim]N[/dim]",
            str(r["geography"] or "")[:12],
        )
    console.print(table)
    rprint(f"\n[dim]{len(rows)} results[/dim]")


@intel_app.command("paths")
def intel_paths_cmd(
    name: Optional[str] = typer.Argument(None, help="Prospect LP name (omit for top bridges)"),
    top_bridges: int = typer.Option(20, "--top-bridges", help="Top bridge nodes across network"),
    prospect_only: bool = typer.Option(False, "--prospect-only", help="Institutional with warm_path_count > 0"),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Show warm intro paths via mutual_connection graph edges."""
    from agents.db import get_conn

    con = get_conn(read_only=True)
    try:
        if name:
            rows = con.execute(
                """
                SELECT prospect_name, bridge_name, bridge_type, bridge_strength
                FROM v_warm_paths
                WHERE lower(prospect_name) LIKE lower(?)
                ORDER BY bridge_strength DESC NULLS LAST
                LIMIT 20
                """,
                [f"%{name}%"],
            ).fetchdf()
        elif prospect_only:
            rows = con.execute(
                """
                SELECT DISTINCT prospect_name, COUNT(*) AS path_count,
                       MAX(bridge_strength) AS best_strength
                FROM v_warm_paths
                GROUP BY prospect_name
                ORDER BY path_count DESC, best_strength DESC
                LIMIT ?
                """,
                [top_bridges],
            ).fetchdf()
        else:
            rows = con.execute(
                """
                SELECT bridge_name, bridge_type,
                       COUNT(*) AS connects_to,
                       AVG(bridge_strength) AS avg_strength
                FROM v_warm_paths
                GROUP BY bridge_name, bridge_type
                ORDER BY connects_to DESC, avg_strength DESC
                LIMIT ?
                """,
                [top_bridges],
            ).fetchdf()
    finally:
        con.close()

    if json_out:
        rprint(rows.to_json(orient="records", indent=2))
        return

    if rows.empty:
        rprint("[dim]No warm paths found.[/dim]")
        return

    rprint(f"\n[bold]Warm Paths[/bold] ({len(rows)} results)\n")
    rprint(rows.to_string(index=False))


@intel_app.command("contacts")
def intel_contacts_cmd(
    name: str = typer.Argument(..., help="LP / allocator name"),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Show LinkedIn + CRM contacts for a named LP."""
    from agents.db import get_conn
    from contra.intelligence.resolver import resolve

    con = get_conn(read_only=True)
    try:
        match = resolve(con, name)
        if not match.allocator_id:
            rprint(f"[yellow]No allocator match for '{name}'[/yellow]")
            return

        contacts = con.execute(
            """
            SELECT full_name, title, company, email, linkedin_url,
                   location, source, match_confidence
            FROM allocator_contacts
            WHERE allocator_id = ?
            ORDER BY match_confidence DESC NULLS LAST
            """,
            [match.allocator_id],
        ).fetchdf()

        from contra.intelligence.resolver import norm_key as _norm_key
        crm = con.execute(
            """
            SELECT investor_name, investor_type, investor_location, crm_status
            FROM crm_contacts WHERE name_key = ?
            LIMIT 3
            """,
            [_norm_key(name)],
        ).fetchdf()
    finally:
        con.close()

    if json_out:
        out = {
            "match": match.matched_name,
            "allocator_id": match.allocator_id,
            "contacts": contacts.to_dict(orient="records"),
            "crm": crm.to_dict(orient="records"),
        }
        rprint(json.dumps(out, indent=2, default=str))
        return

    rprint(f"\n[bold]{name}[/bold] → matched: {match.matched_name} ({match.method})\n")
    if not contacts.empty:
        rprint("[bold]LinkedIn contacts[/bold]")
        for _, r in contacts.iterrows():
            rprint(f"  {r.get('full_name') or ''} | {r.get('title') or ''} @ {r.get('company') or ''}")
            if r.get("email"):
                rprint(f"    email: {r['email']}")
            if r.get("linkedin_url"):
                rprint(f"    li: {r['linkedin_url']}")
    else:
        rprint("[dim]No LinkedIn contacts found.[/dim]")

    if not crm.empty:
        rprint("\n[bold]CRM[/bold]")
        for _, r in crm.iterrows():
            rprint(f"  {r.get('investor_name')} | {r.get('investor_type')} | {r.get('crm_status')}")


@intel_app.command("summary")
def intel_summary_cmd(json_out: bool = typer.Option(False, "--json")) -> None:
    """Intelligence gap summary: CRM holes, enrichment needs, warm path coverage."""
    from agents.db import get_conn

    con = get_conn(read_only=True)
    try:
        def q(sql: str):
            return con.execute(sql).fetchone()[0]

        tier1_not_crm = q(
            "SELECT COUNT(*) FROM v_lp_profile WHERE icp_tier = 'tier_1' AND NOT in_crm"
        )
        synd_fund_not_crm = q(
            "SELECT COUNT(*) FROM v_syndicate_profile WHERE is_fund_lp AND NOT in_crm"
        )
        synd_upgrade = q(
            "SELECT COUNT(*) FROM v_syndicate_profile WHERE is_upgrade_candidate AND NOT in_crm"
        )
        unknown_type = q(
            "SELECT COUNT(*) FROM allocators WHERE allocator_type IN ('unknown', '') OR allocator_type IS NULL"
        )
        null_geo = q(
            "SELECT COUNT(*) FROM allocators WHERE geography IS NULL OR geography = ''"
        )
        li_contacts = q(
            "SELECT COUNT(*) FROM allocator_contacts WHERE source = 'linkedin_export'"
        ) if _table_exists_safe(con, "allocator_contacts") else 0
        warm_path_inst = q(
            "SELECT COUNT(*) FROM v_lp_profile WHERE population = 'institutional_prospect' AND warm_path_count > 0"
        )
        avg_warm = con.execute(
            "SELECT AVG(warm_path_count) FROM v_lp_profile WHERE population = 'institutional_prospect'"
        ).fetchone()[0] or 0.0
    finally:
        con.close()

    summary = {
        "tier_1_not_in_crm": tier1_not_crm,
        "syndicate_fund_lps_not_in_crm": synd_fund_not_crm,
        "syndicate_upgrade_candidates": synd_upgrade,
        "allocators_unknown_type": unknown_type,
        "allocators_null_geography": null_geo,
        "linkedin_contacts_ingested": li_contacts,
        "institutional_with_warm_paths": warm_path_inst,
        "avg_warm_path_count_institutional": round(float(avg_warm), 2),
    }

    if json_out:
        rprint(json.dumps(summary, indent=2))
        return

    rprint("\n[bold]Contra Intelligence Summary[/bold]\n")
    rprint(f"  Tier-1 institutional not in CRM       : [yellow]{tier1_not_crm}[/yellow]")
    rprint(f"  Syndicate fund-LPs not in CRM         : [yellow]{synd_fund_not_crm}[/yellow]")
    rprint(f"  Syndicate upgrade candidates (not CRM): [cyan]{synd_upgrade}[/cyan]")
    rprint(f"  Allocators with unknown type          : {unknown_type}")
    rprint(f"  Allocators with NULL geography        : {null_geo}")
    rprint(f"  LinkedIn contacts ingested            : {li_contacts}")
    rprint(f"  Institutional LPs with warm paths     : {warm_path_inst}")
    rprint(f"  Avg warm_path_count (institutional)   : {avg_warm:.2f}")


def _table_exists_safe(con, name: str) -> bool:
    try:
        con.execute(f"SELECT 1 FROM {name} LIMIT 0")
        return True
    except Exception:
        return False


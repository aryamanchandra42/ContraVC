"""
PULSE LP Explorer — local read-only Streamlit UI.

Launch: pulse explore  (or double-click Launch_PULSE.bat on Windows)

The "Refresh PULSE" button in the sidebar runs the full pipeline +
exports in a subprocess so the read-only explorer connection is never
blocked.  Progress is streamed line-by-line from JSON stdout.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from agents.db import get_conn
from pulse.explore.queries import (
    OutreachFilters,
    allocator_detail,
    connectivity_csv_exists,
    connectivity_for,
    db_path,
    ego_edges,
    funnel_metrics,
    last_pipeline_run,
    list_scored_allocators,
    outreach_queue,
)

st.set_page_config(
    page_title="PULSE LP Explorer",
    page_icon="📊",
    layout="wide",
)


@contextmanager
def _explore_conn() -> Iterator:
    """Fresh read-only connection per query — DuckDB is not safe for concurrent use on one conn."""
    con = get_conn(read_only=True)
    try:
        yield con
    finally:
        con.close()


@st.cache_data(ttl=60)
def _cached_funnel(_db_mtime: float):
    with _explore_conn() as con:
        return funnel_metrics(con)


@st.cache_data(ttl=60)
def _cached_outreach(_db_mtime: float, filters_key: str):
    parts = filters_key.split("|")
    f = OutreachFilters(
        populations=parts[0].split(",") if parts[0] else None,
        tiers=parts[1].split(",") if parts[1] else None,
        min_fit_score=float(parts[2]),
        name_search=parts[3],
        tier1_approved_only=parts[4] == "1",
        has_email_only=parts[5] == "1",
        institutional_only=parts[6] == "1",
    )
    with _explore_conn() as con:
        return outreach_queue(con, f)


@st.cache_data(ttl=60)
def _cached_allocators(_db_mtime: float, population: str):
    with _explore_conn() as con:
        return list_scored_allocators(con, population)


@st.cache_data(ttl=60)
def _cached_detail(_db_mtime: float, allocator_id: str):
    with _explore_conn() as con:
        return allocator_detail(con, allocator_id)


@st.cache_data(ttl=60)
def _cached_ego(_db_mtime: float, allocator_id: str):
    with _explore_conn() as con:
        return ego_edges(con, allocator_id)


@st.cache_data(ttl=60)
def _cached_connectivity(_db_mtime: float, allocator_id: str):
    with _explore_conn() as con:
        return connectivity_for(con, allocator_id)


def _db_mtime() -> float:
    p = db_path()
    return p.stat().st_mtime if p.exists() else 0.0


def _render_header():
    st.title("PULSE LP Explorer")
    st.caption("Partner intelligence layer — filter, drill down, and download outreach lists.")

    col1, col2, col3 = st.columns(3)
    with col1:
        st.text(f"DB: {db_path()}")
    with col2:
        with _explore_conn() as con:
            run = last_pipeline_run(con)
        if run:
            st.text(
                f"Last run: {run.get('stage')} ({run.get('status')}) "
                f"@ {str(run.get('started_at', ''))[:19]}"
            )
        else:
            st.text("Last run: none — click Refresh PULSE")
    with col3:
        if not connectivity_csv_exists():
            st.info("Connectivity CSV not yet built — click Refresh PULSE to generate.")
        else:
            st.success("Connectivity data loaded")


# ---------------------------------------------------------------------------
# Refresh PULSE — autonomous pipeline button
# ---------------------------------------------------------------------------

_STAGE_LABELS = {
    "ingest":    "Ingesting source files",
    "normalize": "Normalising entities",
    "extract":   "Extracting ontology",
    "derive":    "Deriving uncertainty",
    "graph":     "Building graph",
    "score":     "Scoring LPs",
    "calibrate": "Calibrating tiers",
    "exports":   "Generating CSVs",
    "evals":     "Quality checks",
}


def _run_refresh_subprocess() -> None:
    """
    Launch `python -m pulse refresh --json-log` as a subprocess and stream
    its JSON progress lines into a Streamlit status widget.
    Called only when the user clicks the Refresh PULSE button.
    """
    cmd = [sys.executable, "-m", "pulse", "refresh", "--json-log"]
    statuses: dict = {}

    with st.status("Running PULSE refresh…", expanded=True) as status_box:
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                cwd=str(ROOT),
            )
            for raw_line in proc.stdout:
                raw_line = raw_line.strip()
                if not raw_line:
                    continue
                try:
                    msg = json.loads(raw_line)
                except json.JSONDecodeError:
                    st.write(raw_line)
                    continue

                stage = msg.get("stage", "")
                text = msg.get("msg", "")
                line_status = msg.get("status", "running")

                label = _STAGE_LABELS.get(stage, stage)

                if line_status == "done":
                    statuses[stage] = ("complete", text)
                    st.write(f"✓  **{label}** — {text}")
                elif line_status == "failed":
                    statuses[stage] = ("error", text)
                    st.error(f"✗  **{label}** failed: {text}")
                elif line_status == "warn":
                    st.warning(f"⚠  {text}")
                elif line_status == "complete":
                    # Final summary line
                    result = msg.get("result", {})
                    if result.get("success"):
                        pack = (result.get("counts") or {}).get("exports", {}).get("outreach_pack", {})
                        st.success(
                            f"Refresh complete — "
                            f"Section A: {pack.get('section_a_tier1_approved', '?')} Tier 1 approved prospects"
                        )
                    else:
                        st.error(f"Refresh failed at '{result.get('failed_stage')}': {result.get('error')}")
                else:
                    st.write(f"►  {label}: {text}")

            proc.wait()

            if proc.returncode == 0:
                status_box.update(label="Refresh complete", state="complete", expanded=False)
            else:
                status_box.update(label="Refresh failed — see errors above", state="error")

        except Exception as exc:
            st.error(f"Could not start refresh process: {exc}")
            status_box.update(label="Refresh failed", state="error")

    # Clear cached data so the UI shows fresh numbers immediately
    st.cache_data.clear()
    time.sleep(0.5)
    st.rerun()


def _render_refresh_sidebar() -> None:
    """Add the Refresh PULSE button and a trust summary to the sidebar."""
    st.sidebar.divider()
    st.sidebar.subheader("Pipeline")

    if st.sidebar.button("🔄  Refresh PULSE", type="primary", use_container_width=True,
                          help="Run full pipeline (ingest → score → calibrate → exports). Takes 1–3 minutes."):
        _run_refresh_subprocess()

    # Trust / data summary
    try:
        with _explore_conn() as con:
            inst_count = con.execute(
                "SELECT COUNT(*) FROM allocators WHERE COALESCE(population,'') = 'institutional_prospect'"
            ).fetchone()[0]
            t1_count = con.execute(
                "SELECT COUNT(*) FROM icp_scores WHERE tier = 'tier_1' "
                "AND LOWER(COALESCE(client_decision,'')) LIKE '%approved%'"
            ).fetchone()[0]

        pack_path = ROOT / "processed_data" / "First_LPs_Outreach_Pack.csv"
        section_a = 0
        if pack_path.exists():
            import csv as _csv
            with open(pack_path, encoding="utf-8") as f:
                rows = list(_csv.DictReader(f))
            section_a = sum(1 for r in rows if r.get("data_source") == "prospect_sheet")

        st.sidebar.caption(
            f"**Data summary**  \n"
            f"Institutional prospects: {inst_count}  \n"
            f"Tier 1 approved: {t1_count}  \n"
            f"Outreach pack rows: {section_a}"
        )
    except Exception:
        pass


def _render_sidebar() -> tuple:
    _render_refresh_sidebar()

    st.sidebar.divider()
    st.sidebar.header("Filters")

    population = st.sidebar.selectbox(
        "Detail / graph population",
        ["institutional_prospect", "syndicate_lp", ""],
        index=0,
        help="Population for allocator detail dropdown",
    )

    pop_filter = st.sidebar.multiselect(
        "Outreach population",
        ["institutional_prospect", "syndicate_lp"],
        default=["institutional_prospect"],
    )

    tier_options = ["tier_1", "tier_2", "tier_3", "tier_4"]
    tiers = st.sidebar.multiselect("Tier", tier_options, default=[])

    min_fit = st.sidebar.slider("Min fit score", 0.0, 1.0, 0.0, 0.05)
    name_search = st.sidebar.text_input("Search name")

    st.sidebar.subheader("Presets")
    tier1_approved = st.sidebar.checkbox("Tier 1 + client approved")
    has_email = st.sidebar.checkbox("Has email only")
    institutional_only = st.sidebar.checkbox(
        "Institutional only",
        value=True,
        help="Hide syndicate_lp rows from ICP ranked section",
    )

    filters_key = "|".join([
        ",".join(pop_filter),
        ",".join(tiers),
        str(min_fit),
        name_search,
        "1" if tier1_approved else "0",
        "1" if has_email else "0",
        "1" if institutional_only else "0",
    ])

    return population, filters_key


def _render_funnel(mtime: float):
    m = _cached_funnel(mtime)

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("entities_raw", m.get("count_entities_raw", "—"))
    c2.metric("allocators", m.get("count_allocators", "—"))
    c3.metric("icp_scores", m.get("count_icp_scores", "—"))
    c4.metric("relationships", m.get("count_relationships", "—"))

    st.subheader("ICP version")
    st.write(m.get("icp_version", ""))

    col_a, col_b = st.columns(2)

    with col_a:
        tier_df = pd.DataFrame(m.get("tier_split", []))
        if not tier_df.empty:
            fig = px.bar(
                tier_df,
                x="tier",
                y="cnt",
                title="Tier distribution",
                labels={"cnt": "Count", "tier": "Tier"},
            )
            st.plotly_chart(fig, width="stretch")
        else:
            st.info("No ICP tier data.")

    with col_b:
        src_df = pd.DataFrame(m.get("source_split", []))
        if not src_df.empty:
            fig2 = px.bar(
                src_df,
                x="source_sheet",
                y="cnt",
                title="Scored by source sheet",
                labels={"cnt": "Count"},
            )
            st.plotly_chart(fig2, width="stretch")

    pop_df = pd.DataFrame(m.get("population_split", []))
    if not pop_df.empty:
        st.subheader("Allocator populations")
        st.dataframe(pop_df, width="stretch", hide_index=True)

    csv_ranked = m.get("csv_lp_ranked_rows")
    csv_pack = m.get("csv_outreach_pack_rows")
    db_icp = m.get("count_icp_scores")
    if csv_ranked is not None and db_icp is not None:
        if csv_ranked != db_icp:
            st.info(
                f"LP_Ranked_List.csv ({csv_ranked} rows) differs from DB icp_scores "
                f"({db_icp}) — CSV may be stale; this app reads live DB."
            )
    if csv_pack is not None:
        st.caption(f"Last static outreach pack CSV: {csv_pack} rows")


def _render_outreach(mtime: float, filters_key: str):
    # ── Trust panel ──────────────────────────────────────────────────────────
    pack_path = ROOT / "processed_data" / "First_LPs_Outreach_Pack.csv"
    if pack_path.exists():
        import csv as _csv
        with open(pack_path, encoding="utf-8") as f:
            pack_rows = list(_csv.DictReader(f))
        section_a = [r for r in pack_rows if r.get("data_source") == "prospect_sheet"]
        ta, tb, tc = st.columns(3)
        ta.metric("Tier 1 — Prospect sheet", len(section_a))
        tb.metric("Total in pack", len(pack_rows))
        no_conn = sum(1 for r in section_a if not r.get("connectivity_score"))
        tc.metric("No warm path (cold)", no_conn,
                  help="Prospect sheet rows with no syndicate connectivity score. Cold outreach required.")

        with st.expander("Download outreach pack CSV"):
            st.caption("**Section A** — ICP Tier 1, client approved (from prospect spreadsheet)")
            import pandas as _pd
            pack_df = _pd.DataFrame(pack_rows)
            st.download_button(
                "⬇  Download First_LPs_Outreach_Pack.csv",
                pack_df.to_csv(index=False).encode("utf-8"),
                file_name="First_LPs_Outreach_Pack.csv",
                mime="text/csv",
                key="pack_download_trust",
            )
    else:
        st.info("Outreach pack not yet generated — click **Refresh PULSE** in the sidebar.")

    st.divider()

    # ── Live filtered queue ──────────────────────────────────────────────────
    df = _cached_outreach(mtime, filters_key)
    st.subheader(f"Live outreach queue ({len(df)} rows)")

    if df.empty:
        st.warning("No rows match filters.")
        return

    display_cols = [
        c
        for c in [
            "pack_section",
            "lp_name",
            "tier",
            "fit_score",
            "client_status",
            "decision",
            "email",
            "linkedin",
            "connectivity_score",
            "top_bridge",
            "population",
            "source_sheet",
        ]
        if c in df.columns
    ]
    st.dataframe(df[display_cols], width="stretch", hide_index=True)

    st.download_button(
        "Download CSV",
        df.to_csv(index=False).encode("utf-8"),
        file_name="outreach_queue.csv",
        mime="text/csv",
    )


def _render_detail(mtime: float, population: str):
    alloc_df = _cached_allocators(mtime, population)
    if alloc_df.empty:
        st.warning("No scored allocators for this population.")
        return

    options = {}
    for _, row in alloc_df.iterrows():
        fs = row.get("fit_score")
        fs_str = f"{float(fs):.3f}" if fs is not None and pd.notna(fs) else "—"
        label = f"{row['canonical_name']} ({row['tier']}, {fs_str})"
        options[label] = row["allocator_id"]
    labels = list(options.keys())
    choice = st.selectbox("Allocator", labels)
    allocator_id = options[choice]

    detail = _cached_detail(mtime, allocator_id)
    icp = detail.get("icp", {})

    if icp:
        st.subheader(icp.get("canonical_name", "Allocator"))
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Fit score", f"{icp.get('fit_score', 0):.3f}" if icp.get("fit_score") else "—")
        c2.metric("Tier", icp.get("tier", "—"))
        c3.metric("Type", icp.get("allocator_type", "—"))
        c4.metric("Geography", icp.get("geography", "—"))

        st.markdown("**ICP gates (C1–C4)**")
        gates = pd.DataFrame([
            {
                "gate": "C1 asset class",
                "pass": icp.get("c1_asset_class_pass"),
                "evidence": (icp.get("c1_evidence") or "")[:300],
            },
            {
                "gate": "C2 emerging manager",
                "pass": icp.get("c2_emerging_manager_pass"),
                "evidence": (icp.get("c2_evidence") or "")[:300],
            },
            {
                "gate": "C3 AI/tech",
                "pass": icp.get("c3_ai_tech_pass"),
                "evidence": (icp.get("c3_evidence") or "")[:300],
            },
            {
                "gate": "C4 geography",
                "pass": icp.get("c4_geography_pass"),
                "evidence": (icp.get("c4_evidence") or "")[:300],
            },
        ])
        st.dataframe(gates, width="stretch", hide_index=True)

        st.markdown("**Soft signals (S1–S7)**")
        soft = pd.DataFrame([{
            "signal": k,
            "value": icp.get(k),
        } for k in (
            "s1_ai_signal", "s2_emerging_manager", "s3_lp_type",
            "s4_decision_speed", "s5_stage", "s6_clean_profile", "s7_proxy_fund",
        )])
        st.dataframe(soft, width="stretch", hide_index=True)

        st.markdown("**Client fields**")
        st.write({
            "client_status": icp.get("client_status"),
            "client_decision": icp.get("client_decision"),
            "stated_reason": icp.get("stated_reason"),
            "data_miner_comment": icp.get("data_miner_comment"),
            "source_sheet": icp.get("source_sheet"),
            "source_file": icp.get("source_file"),
        })

    contacts = detail.get("contacts", {})
    if contacts:
        st.markdown("**Contacts (xlsx)**")
        st.json(contacts)

    signals = detail.get("signals", [])
    if signals:
        st.markdown("**Persisted signals**")
        st.dataframe(pd.DataFrame(signals), width="stretch", hide_index=True)

    for i, snip in enumerate(detail.get("raw_snippets", [])):
        with st.expander(
            f"Source: {snip.get('source_file')} @ {snip.get('source_offset')} ({snip.get('source_type')})"
        ):
            st.json(snip.get("snippet", {}))


def _plot_ego_network(edges_df: pd.DataFrame, center_id: str, center_name: str):
    if edges_df.empty:
        return

    nodes = {center_id: center_name}
    links = []
    for _, e in edges_df.iterrows():
        nid = str(e["neighbor_id"])
        nname = str(e["neighbor_name"])
        nodes[nid] = nname
        links.append((center_id, nid, e.get("edge_type", ""), e.get("confidence", 0)))

    if len(nodes) > 50:
        st.info(f"Network has {len(nodes)} nodes — table only (limit 50 for chart).")
        return

    import math

    node_ids = list(nodes.keys())
    n = len(node_ids)
    pos = {}
    for i, nid in enumerate(node_ids):
        angle = 2 * math.pi * i / max(n, 1)
        r = 0.0 if nid == center_id else 1.0
        pos[nid] = (r * math.cos(angle), r * math.sin(angle))

    edge_x, edge_y = [], []
    for src, tgt, _, _ in links:
        x0, y0 = pos[src]
        x1, y1 = pos[tgt]
        edge_x += [x0, x1, None]
        edge_y += [y0, y1, None]

    edge_trace = go.Scatter(
        x=edge_x,
        y=edge_y,
        mode="lines",
        line=dict(width=1, color="#888"),
        hoverinfo="none",
    )

    node_x = [pos[nid][0] for nid in node_ids]
    node_y = [pos[nid][1] for nid in node_ids]
    node_text = [nodes[nid][:40] for nid in node_ids]
    node_trace = go.Scatter(
        x=node_x,
        y=node_y,
        mode="markers+text",
        text=node_text,
        textposition="top center",
        marker=dict(size=14, color=["#e74c3c" if nid == center_id else "#3498db" for nid in node_ids]),
    )

    fig = go.Figure(data=[edge_trace, node_trace])
    fig.update_layout(
        showlegend=False,
        hovermode="closest",
        margin=dict(b=0, l=0, r=0, t=30),
        title=f"Ego network: {center_name}",
        xaxis=dict(showgrid=False, zeroline=False, showticklabels=False),
        yaxis=dict(showgrid=False, zeroline=False, showticklabels=False),
        height=450,
    )
    st.plotly_chart(fig, width="stretch")


def _render_graph(mtime: float, population: str):
    alloc_df = _cached_allocators(mtime, population)
    if alloc_df.empty:
        st.warning("No allocators for graph view.")
        return

    options = {row["canonical_name"]: row["allocator_id"] for _, row in alloc_df.iterrows()}
    name = st.selectbox("Graph center", list(options.keys()))
    allocator_id = options[name]

    conn = _cached_connectivity(mtime, allocator_id)
    if conn:
        st.subheader("Syndicate connectivity")
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Connectivity score", f"{conn.get('connectivity_score', 0):.3f}")
        c2.metric("Direct degree", conn.get("direct_syndicate_degree", 0))
        c3.metric("2-hop reach", conn.get("two_hop_syndicate_reach", 0))
        c4.metric("Top bridge", str(conn.get("top_bridge_name", ""))[:40])
    else:
        st.caption("No connectivity row — click Refresh PULSE to run prospect inference.")

    edges = _cached_ego(mtime, allocator_id)
    st.subheader(f"Direct edges ({len(edges)})")
    if edges.empty:
        st.info("No edges in relationships_effective for this allocator.")
    else:
        show = edges[
            [
                c
                for c in [
                    "edge_type",
                    "direction",
                    "neighbor_name",
                    "confidence",
                    "temporal_confidence",
                    "weight",
                    "evidence_count",
                ]
                if c in edges.columns
            ]
        ]
        st.dataframe(show, width="stretch", hide_index=True)
        _plot_ego_network(edges, allocator_id, name)


def main():
    _render_header()
    population, filters_key = _render_sidebar()
    mtime = _db_mtime()

    tab_funnel, tab_outreach, tab_detail, tab_graph = st.tabs(
        [
            "Funnel",
            "Outreach",
            "Allocator detail",
            "Graph / connectivity",
        ]
    )

    with tab_funnel:
        _render_funnel(mtime)

    with tab_outreach:
        _render_outreach(mtime, filters_key)

    with tab_detail:
        _render_detail(mtime, population)

    with tab_graph:
        _render_graph(mtime, population)


if __name__ == "__main__":
    main()

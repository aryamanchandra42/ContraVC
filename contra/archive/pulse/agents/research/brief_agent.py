"""
PULSE Outreach Brief Agent — per-LP synthesized brief with warm path + talking points.

For a given allocator_id, assembles:
  - ICP tier + component scores (C1–C4 gates, S1–S7 soft signals)
  - Warm-path routes (mutual_connection edges → bridge nodes → syndicate LPs)
  - Ego network snapshot from the persisted NetworkX graph
  - Existing contacts, stated reasons, and data gaps

Then either:
  (a) LLM available → structured BriefSections extraction (Anthropic/OpenAI/Gemini)
  (b) No LLM → deterministic templated brief from the same data (still useful)

Output: processed_data/briefs/{allocator_id}.md (UTF-8 Markdown)

The brief file is NOT written to DuckDB (it is a derived artifact, not a canonical
row). It does NOT require entities_raw provenance — it is an analytical export.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent.parent
BRIEFS_DIR = ROOT / "processed_data" / "briefs"


# ---------------------------------------------------------------------------
# Data assembly
# ---------------------------------------------------------------------------

def _get_warm_paths(con, allocator_id: str) -> List[Dict[str, Any]]:
    """
    Fetch mutual_connection warm paths for this prospect from the DB.
    Returns a list of {bridge_name, bridge_type, syndicate_lp_name, bridge_strength}.
    """
    try:
        rows = con.execute(
            """
            SELECT
                r.source_node_id,
                r.target_node_id,
                r.weight,
                r.temporal_confidence,
                r.evidence_count,
                re.provenance_pointer
            FROM relationships_effective r
            LEFT JOIN relationship_evidence re
                ON r.edge_id = re.edge_id
                AND re.evidence_type = 'graph_path_inference'
            WHERE r.edge_type = 'mutual_connection'
              AND (
                  CAST(r.source_node_id AS VARCHAR) = ?
                  OR CAST(r.target_node_id AS VARCHAR) = ?
              )
            ORDER BY r.temporal_confidence DESC NULLS LAST
            LIMIT 25
            """,
            [allocator_id, allocator_id],
        ).fetchall()
    except Exception as exc:
        logger.warning("Could not fetch warm paths for %s: %s", allocator_id, exc)
        return []

    paths = []
    for row in rows:
        source_id, target_id, weight, t_conf, ev_count, prov_raw = row
        other_id = str(target_id) if str(source_id) == allocator_id else str(source_id)

        # Resolve other node name
        try:
            name_row = con.execute(
                "SELECT canonical_name, allocator_type FROM allocators WHERE CAST(allocator_id AS VARCHAR) = ?",
                [other_id],
            ).fetchone()
            if not name_row:
                name_row = con.execute(
                    "SELECT canonical_name, fund_type FROM funds WHERE CAST(fund_id AS VARCHAR) = ?",
                    [other_id],
                ).fetchone()
        except Exception:
            name_row = None

        other_name = name_row[0] if name_row else other_id[:8]
        other_type = name_row[1] if name_row else "unknown"

        # Parse bridge info from provenance_pointer if available
        bridge_name = "unknown"
        try:
            if prov_raw:
                prov = json.loads(prov_raw) if isinstance(prov_raw, str) else prov_raw
                bridge_id = prov.get("bridge_node_id")
                if bridge_id:
                    br = con.execute(
                        "SELECT canonical_name FROM allocators WHERE CAST(allocator_id AS VARCHAR) = ?",
                        [bridge_id],
                    ).fetchone()
                    bridge_name = br[0] if br else bridge_id[:8]
        except Exception:
            pass

        paths.append({
            "syndicate_lp_name": other_name,
            "bridge_name": bridge_name,
            "bridge_type": other_type or "lp",
            "bridge_strength": float(t_conf) if t_conf else 0.0,
            "evidence_count": int(ev_count) if ev_count else 0,
        })

    return paths


def _get_ego_network_summary(allocator_id: str) -> Dict[str, Any]:
    """Load the persisted graph and compute a lightweight ego-network summary."""
    try:
        from agents.graph.persist import load_graph
        G = load_graph()
        if G.number_of_nodes() == 0:
            return {"available": False}

        if allocator_id not in G:
            return {"available": True, "node_in_graph": False}

        neighbors = list(G.neighbors(allocator_id))
        predecessors = list(G.predecessors(allocator_id)) if G.is_directed() else []
        edge_types: Dict[str, int] = {}
        for _, _, data in G.out_edges(allocator_id, data=True):
            et = data.get("edge_type", "unknown")
            edge_types[et] = edge_types.get(et, 0) + 1
        for _, _, data in G.in_edges(allocator_id, data=True):
            et = data.get("edge_type", "unknown")
            edge_types[et] = edge_types.get(et, 0) + 1

        return {
            "available": True,
            "node_in_graph": True,
            "out_degree": len(neighbors),
            "in_degree": len(predecessors),
            "edge_type_counts": edge_types,
        }
    except Exception as exc:
        logger.warning("Graph load failed (non-fatal): %s", exc)
        return {"available": False, "error": str(exc)}


def _build_brief_context(
    detail: Dict[str, Any],
    warm_paths: List[Dict[str, Any]],
    connectivity: Dict[str, Any],
    ego: Dict[str, Any],
) -> str:
    """Flatten all data sources into a prompt-ready context block."""
    lines = []

    icp = detail.get("icp", {})
    if icp:
        lines.append("=== ICP SCORING DATA ===")
        lines.append(f"Name: {icp.get('canonical_name', 'unknown')}")
        lines.append(f"Tier: {icp.get('tier', 'unknown')}")
        lines.append(f"Fit Score: {icp.get('fit_score', 0):.3f}")
        lines.append(f"Core Pass: {icp.get('core_pass', False)}")
        lines.append(f"Excluded: {icp.get('excluded', False)} | Reason: {icp.get('exclusion_reason', '')}")
        lines.append(f"Allocator Type: {icp.get('allocator_type', 'unknown')}")
        lines.append(f"Geography: {icp.get('geography', 'unknown')}")
        lines.append("")
        lines.append("Core Gate Results:")
        for gate in ("c1", "c2", "c3", "c4"):
            pass_val = icp.get(f"{gate}_asset_class_pass" if gate == "c1" else
                               f"{gate}_emerging_manager_pass" if gate == "c2" else
                               f"{gate}_ai_tech_pass" if gate == "c3" else
                               f"{gate}_geography_pass")
            evidence = icp.get(f"{gate}_evidence", "")
            lines.append(f"  {gate.upper()}: {'PASS' if pass_val else 'FAIL'} | {evidence[:120]}")
        lines.append("")
        lines.append("Soft Signals (0-1 scale):")
        for s in ("s1_ai_signal", "s2_em_signal", "s3_lp_type", "s4_stage_pref",
                  "s5_geo_overlap", "s6_network_density", "s7_proxy_fund"):
            v = icp.get(s)
            if v is not None:
                lines.append(f"  {s}: {v:.3f}")
        if icp.get("client_decision"):
            lines.append(f"\nClient Decision: {icp['client_decision']}")
        if icp.get("stated_reason"):
            lines.append(f"Stated Reason: {icp['stated_reason']}")
        if icp.get("data_miner_comment"):
            lines.append(f"Data Miner Comment: {icp['data_miner_comment']}")
        lines.append("")

    signals = detail.get("signals", [])
    if signals:
        lines.append("=== LP SIGNALS ===")
        for sig in signals:
            lines.append(
                f"  {sig.get('signal_type')}: {sig.get('normalized_value'):.3f} "
                f"(conf={sig.get('confidence', 0):.2f})"
            )
        lines.append("")

    if warm_paths:
        lines.append("=== WARM PATHS (mutual_connection graph edges) ===")
        for p in warm_paths[:10]:
            lines.append(
                f"  → {p['syndicate_lp_name']} via {p['bridge_name']} "
                f"(type={p['bridge_type']}, strength={p['bridge_strength']:.3f})"
            )
        lines.append("")

    if connectivity:
        lines.append("=== SYNDICATE CONNECTIVITY METRICS ===")
        for k, v in connectivity.items():
            if k != "allocator_id":
                lines.append(f"  {k}: {v}")
        lines.append("")

    if ego.get("node_in_graph"):
        lines.append("=== EGO NETWORK SUMMARY ===")
        lines.append(f"  Out-degree: {ego.get('out_degree', 0)}")
        lines.append(f"  In-degree: {ego.get('in_degree', 0)}")
        for et, cnt in ego.get("edge_type_counts", {}).items():
            lines.append(f"  {et}: {cnt} edges")
        lines.append("")

    contacts = detail.get("contacts", {})
    if contacts:
        lines.append("=== CONTACT DATA ===")
        for k, v in contacts.items():
            lines.append(f"  {k}: {v}")
        lines.append("")

    return "\n".join(lines)


def _identify_data_gaps(detail: Dict[str, Any]) -> List[str]:
    """Return a list of NULL allocator fields that would improve the brief."""
    icp = detail.get("icp", {})
    gaps = []
    if not icp.get("geography"):
        gaps.append("geography")
    if icp.get("allocator_type") in (None, "unknown"):
        gaps.append("allocator_type")
    if not icp.get("c1_evidence"):
        gaps.append("c1_evidence (asset class)")
    if not icp.get("c3_evidence"):
        gaps.append("c3_evidence (AI/tech signal)")
    signals = {s["signal_type"] for s in detail.get("signals", [])}
    if "network_density" not in signals:
        gaps.append("signal: network_density (run pulse graph)")
    if "social_proximity" not in signals:
        gaps.append("signal: social_proximity (run pulse graph)")
    return gaps


# ---------------------------------------------------------------------------
# Deterministic templated brief (no-LLM fallback)
# ---------------------------------------------------------------------------

def _generate_templated_brief(
    canonical_name: str,
    detail: Dict[str, Any],
    warm_paths: List[Dict[str, Any]],
    connectivity: Dict[str, Any],
) -> str:
    icp = detail.get("icp", {})
    tier = icp.get("tier", "unknown")
    fit_score = icp.get("fit_score", 0) or 0
    core_pass = icp.get("core_pass", False)
    alloc_type = icp.get("allocator_type", "unknown")
    geography = icp.get("geography", "unknown")

    gates_passed = [
        g for g in ("c1", "c2", "c3", "c4")
        if icp.get(f"{g}_asset_class_pass" if g == "c1" else
                   f"{g}_emerging_manager_pass" if g == "c2" else
                   f"{g}_ai_tech_pass" if g == "c3" else
                   f"{g}_geography_pass")
    ]

    top_signals = sorted(
        detail.get("signals", []),
        key=lambda s: s.get("normalized_value") or 0,
        reverse=True,
    )[:3]

    warm_intro = "(No warm paths identified — cold outreach required.)"
    if warm_paths:
        best = warm_paths[0]
        warm_intro = (
            f"Best warm path: reach {canonical_name} via "
            f"{best['bridge_name']} → {best['syndicate_lp_name']} "
            f"(strength={best['bridge_strength']:.2f})."
        )

    conn_score = connectivity.get("connectivity_score", "N/A")
    direct_degree = connectivity.get("direct_syndicate_degree", "N/A")

    gaps = _identify_data_gaps(detail)
    gaps_str = ", ".join(gaps) if gaps else "None"

    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    lines = [
        f"# Outreach Brief: {canonical_name}",
        f"_Generated: {generated_at} | ICP v4.1 | Mode: Templated (no LLM)_",
        "",
        "---",
        "",
        "## ICP Summary",
        f"- **Tier**: {tier}",
        f"- **Fit Score**: {fit_score:.3f}",
        f"- **Core Pass**: {'Yes' if core_pass else 'No'}",
        f"- **Gates Passed**: {', '.join(g.upper() for g in gates_passed) or 'None'}",
        f"- **Type**: {alloc_type}",
        f"- **Geography**: {geography}",
        "",
        "## Thesis Fit",
        (
            f"{canonical_name} is a {alloc_type} LP ({geography}) scoring {fit_score:.3f} "
            f"({tier}). Core gates: {', '.join(g.upper() for g in gates_passed) or 'none'} passed."
        ),
        "",
        "## Warm Path",
        warm_intro,
        f"- Connectivity score: {conn_score}",
        f"- Direct syndicate degree: {direct_degree}",
        "",
        "## Top Signals",
    ]
    for sig in top_signals:
        lines.append(f"- {sig['signal_type']}: {sig.get('normalized_value', 0):.3f}")
    if not top_signals:
        lines.append("- No signals available — run `pulse score`.")

    lines += [
        "",
        "## Recommended Next Step",
        warm_intro if warm_paths else "Cold outreach — no warm path available.",
        "",
        "## Data Gaps",
        f"{gaps_str}",
    ]

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def generate_brief(
    con,
    allocator_id: str,
) -> Dict[str, Any]:
    """
    Generate an outreach brief for a single allocator.

    Returns a dict with:
      - allocator_id, canonical_name
      - brief_path (str): absolute path to written .md file
      - mode: 'llm' or 'templated'
      - error: None or error message if brief could not be written
    """
    from pulse.explore.queries import allocator_detail, connectivity_for

    result: Dict[str, Any] = {
        "allocator_id": allocator_id,
        "canonical_name": "",
        "brief_path": None,
        "mode": "templated",
        "error": None,
    }

    # --- Load all data ---
    try:
        detail = allocator_detail(con, allocator_id)
    except Exception as exc:
        logger.error("allocator_detail failed for %s: %s", allocator_id, exc)
        result["error"] = f"allocator_detail failed: {exc}"
        return result

    icp = detail.get("icp", {})
    canonical_name = icp.get("canonical_name", allocator_id[:8])
    result["canonical_name"] = canonical_name

    if not icp:
        # Fall back to bare allocator row
        try:
            alloc_row = con.execute(
                "SELECT canonical_name FROM allocators WHERE CAST(allocator_id AS VARCHAR) = ?",
                [allocator_id],
            ).fetchone()
            if alloc_row:
                canonical_name = alloc_row[0]
                result["canonical_name"] = canonical_name
        except Exception:
            pass

    warm_paths = _get_warm_paths(con, allocator_id)
    connectivity = connectivity_for(con, allocator_id)
    ego = _get_ego_network_summary(allocator_id)

    data_gaps = _identify_data_gaps(detail)

    # --- Try LLM brief ---
    brief_md: Optional[str] = None
    mode = "templated"

    try:
        from agents.research.llm_client import get_llm_client, LLMUnavailable
        from agents.research.schemas import BriefSections

        llm_client = get_llm_client()
        context = _build_brief_context(detail, warm_paths, connectivity, ego)

        prompt = f"""You are a private-market fundraising analyst at MyAsiaVC, an AI-native VC fund.
Generate a concise, high-conviction outreach brief for the LP below.

Use ONLY the data provided. Do NOT hallucinate.
If a data point is missing, note it in data_gaps.

LP DATA:
{context}

DATA GAPS (null fields on this allocator):
{json.dumps(data_gaps)}

Generate a BriefSections object following the schema exactly.
Focus on:
- Why this LP is a strategic fit for an AI-native VC fund focused on emerging markets
- The strongest warm-path introduction route
- Concrete talking points grounded in the data
- Honest risk flags / likely objections
"""
        brief_sections: BriefSections = llm_client.structured(
            prompt=prompt,
            response_model=BriefSections,
            system=(
                "You are a seasoned private-market fundraising analyst. "
                "Be precise, data-grounded, and avoid hyperbole. "
                "Every claim must reference a specific data point."
            ),
        )
        mode = "llm"
        generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        brief_md = _render_llm_brief(
            canonical_name, allocator_id, brief_sections, generated_at, data_gaps
        )
        logger.info("LLM brief generated for '%s'", canonical_name)

    except Exception as exc:
        logger.warning(
            "LLM brief failed for '%s' (%s) — falling back to templated: %s",
            canonical_name, allocator_id, exc,
        )

    # --- Fallback to templated brief ---
    if brief_md is None:
        brief_md = _generate_templated_brief(canonical_name, detail, warm_paths, connectivity)

    result["mode"] = mode

    # --- Write to processed_data/briefs/ ---
    BRIEFS_DIR.mkdir(parents=True, exist_ok=True)
    safe_name = "".join(c if c.isalnum() or c in "-_" else "_" for c in canonical_name)[:60]
    brief_path = BRIEFS_DIR / f"{allocator_id}_{safe_name}.md"
    try:
        brief_path.write_text(brief_md, encoding="utf-8")
        result["brief_path"] = str(brief_path)
        logger.info("Brief written: %s", brief_path)
    except Exception as exc:
        logger.error("Brief file write failed: %s", exc)
        result["error"] = str(exc)

    return result


def _render_llm_brief(
    canonical_name: str,
    allocator_id: str,
    sections: "BriefSections",
    generated_at: str,
    data_gaps: List[str],
) -> str:
    """Render a BriefSections object into Markdown."""
    from agents.research.schemas import BriefSections

    talking_pts = "\n".join(f"- {pt}" for pt in sections.talking_points)
    risks = "\n".join(f"- {r}" for r in sections.risks_and_objections)
    gaps_str = "\n".join(f"- {g}" for g in (sections.data_gaps or data_gaps))

    return f"""# Outreach Brief: {canonical_name}
_Generated: {generated_at} | ICP v4.1 | Mode: LLM (structured extraction)_
_Allocator ID: {allocator_id}_

---

## Thesis Fit

{sections.thesis_fit}

## Warm Path Introduction

{sections.warm_path_intro}

## Talking Points

{talking_pts}

## Risks & Likely Objections

{risks}

## Recommended Next Step

{sections.recommended_next_step}

## Data Gaps

{gaps_str if gaps_str else "None identified."}
"""

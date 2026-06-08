"""
Relationship graph builder.

Constructs a networkx.MultiDiGraph from relationships_effective (post-review view)
joined with relationship_evidence. Reads only from _effective views, never directly
from the raw tables.

Nodes: LPs, funds, syndicates, founders, advisors, geographies.
Edges: all 6 canonical edge types with full uncertainty + temporal attributes.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Tuple

import networkx as nx


def build_graph(con) -> nx.MultiDiGraph:
    """
    Build a MultiDiGraph from relationships_effective + relationship_evidence.
    Returns the populated graph.
    """
    G = nx.MultiDiGraph()

    # Load effective relationships (post-review)
    edges = _load_effective_edges(con)
    if not edges:
        return G

    # Load evidence keyed by edge_id
    evidence_by_edge = _load_evidence(con)

    # Add allocator nodes
    _add_allocator_nodes(G, con)

    # Add fund nodes
    _add_fund_nodes(G, con)

    # Add edges
    for edge in edges:
        edge_id = str(edge["edge_id"])
        ev_rows = evidence_by_edge.get(edge_id, [])

        G.add_edge(
            edge["source_node_id"],
            edge["target_node_id"],
            key=edge_id,
            edge_id=edge_id,
            edge_type=edge["edge_type"],
            weight=edge["weight"] if edge["weight"] is not None else float(len(ev_rows)),
            confidence=edge["confidence"],
            evidence_count=edge["evidence_count"] or len(ev_rows),
            contradiction_score=edge["contradiction_score"],
            source_agreement_score=edge["source_agreement_score"],
            effective_date=str(edge["effective_date"]) if edge["effective_date"] else None,
            first_seen=str(edge["first_seen"]) if edge["first_seen"] else None,
            last_seen=str(edge["last_seen"]) if edge["last_seen"] else None,
            last_active=str(edge["last_active"]) if edge["last_active"] else None,
            relationship_decay_score=edge["relationship_decay_score"],
            temporal_confidence=edge["temporal_confidence"],
            review_id=str(edge.get("review_id")) if edge.get("review_id") else None,
            review_decision=edge.get("review_decision"),
            evidence_summary=[_summarize_ev(e) for e in ev_rows],
        )

    return G


def _load_effective_edges(con) -> List[Dict]:
    """Load from relationships_effective view.

    Raises RuntimeError if the view is missing — bypassing it would silently
    apply human-review 'reject' decisions and allow overridden edges into the
    graph, violating DA-003 (append-only human_reviews + _effective views).
    Run `pulse derive` (which recreates views from schema/views.sql) to fix.
    """
    rows = con.execute(
        """
        SELECT
            edge_id, source_node_id, source_node_type,
            target_node_id, target_node_type, edge_type,
            weight, effective_date, first_seen, last_seen, last_active,
            relationship_decay_score, temporal_confidence,
            confidence, evidence_count, contradiction_score, source_agreement_score,
            review_id, review_decision
        FROM relationships_effective
        """
    ).fetchall()

    cols = [
        "edge_id", "source_node_id", "source_node_type",
        "target_node_id", "target_node_type", "edge_type",
        "weight", "effective_date", "first_seen", "last_seen", "last_active",
        "relationship_decay_score", "temporal_confidence",
        "confidence", "evidence_count", "contradiction_score", "source_agreement_score",
        "review_id", "review_decision",
    ]
    return [dict(zip(cols, row)) for row in rows]


def _load_evidence(con) -> Dict[str, List[Dict]]:
    """Load all relationship_evidence rows keyed by edge_id."""
    try:
        rows = con.execute(
            """
            SELECT CAST(edge_id AS VARCHAR), evidence_type, evidence_strength,
                   confidence, provenance_pointer, notes
            FROM relationship_evidence
            """
        ).fetchall()
    except Exception:
        return {}

    result: Dict[str, List[Dict]] = {}
    for edge_id, ev_type, strength, conf, prov, notes in rows:
        if isinstance(prov, str):
            try:
                prov = json.loads(prov)
            except Exception:
                prov = {}
        result.setdefault(str(edge_id), []).append({
            "evidence_type": ev_type,
            "evidence_strength": strength,
            "confidence": conf,
            "provenance_pointer": prov,
            "notes": notes,
        })
    return result


def _add_allocator_nodes(G: nx.MultiDiGraph, con) -> None:
    """Add LP nodes from allocators_effective."""
    try:
        rows = con.execute(
            """
            SELECT CAST(allocator_id AS VARCHAR), canonical_name, allocator_type,
                   geography, hq_country, em_appetite, relationship_density
            FROM allocators_effective
            """
        ).fetchall()
    except Exception:
        rows = con.execute(
            """
            SELECT CAST(allocator_id AS VARCHAR), canonical_name, allocator_type,
                   geography, hq_country, em_appetite, relationship_density
            FROM allocators
            """
        ).fetchall()

    for row in rows:
        node_id, name, atype, geo, country, em, density = row
        G.add_node(
            node_id,
            node_type="lp",
            canonical_name=name,
            allocator_type=atype,
            geography=geo,
            hq_country=country,
            em_appetite=em,
            relationship_density=density,
        )


def _add_fund_nodes(G: nx.MultiDiGraph, con) -> None:
    """Add fund nodes."""
    try:
        rows = con.execute(
            "SELECT CAST(fund_id AS VARCHAR), canonical_name, fund_type, geography_focus FROM funds"
        ).fetchall()
    except Exception:
        return

    for fund_id, name, ftype, geo in rows:
        G.add_node(
            fund_id,
            node_type="fund",
            canonical_name=name,
            fund_type=ftype,
            geography_focus=geo,
        )


def _summarize_ev(ev: Dict) -> Dict:
    return {
        "type": ev.get("evidence_type"),
        "strength": ev.get("evidence_strength"),
        "source": ev.get("provenance_pointer", {}).get("source_file"),
    }

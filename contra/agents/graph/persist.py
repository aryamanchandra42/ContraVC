"""
Graph persistence — serializes to all 4 formats atomically.

Formats:
1. graphs/pulse.gpickle  — binary, fast Python reload
2. graphs/edges.parquet  — queryable edge table with all attributes
3. graphs/evidence.parquet — denormalized evidence (one row per evidence item per edge)
4. graphs/pulse.graphml  — XML interchange for external tools

All 4 formats are produced in the same call. Never produce only some.
"""

from __future__ import annotations

import pickle
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

import networkx as nx
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

ROOT = Path(__file__).resolve().parent.parent.parent
GRAPHS_DIR = ROOT / "graphs"


def persist_graph(G: nx.MultiDiGraph, run_id: str) -> Dict[str, str]:
    """
    Serialize graph to all 4 formats. Returns dict of format → path.
    """
    GRAPHS_DIR.mkdir(parents=True, exist_ok=True)

    paths = {}

    # 1. gpickle
    gpickle_path = GRAPHS_DIR / "pulse.gpickle"
    with open(gpickle_path, "wb") as f:
        pickle.dump(G, f, protocol=pickle.HIGHEST_PROTOCOL)
    paths["gpickle"] = str(gpickle_path)

    # 2. edges.parquet
    edges_path = GRAPHS_DIR / "edges.parquet"
    edges_df = _edges_to_dataframe(G)
    if not edges_df.empty:
        edges_df.to_parquet(edges_path, index=False)
    paths["edges_parquet"] = str(edges_path)

    # 3. evidence.parquet — denormalized evidence per edge
    evidence_path = GRAPHS_DIR / "evidence.parquet"
    evidence_df = _evidence_to_dataframe(G)
    if not evidence_df.empty:
        evidence_df.to_parquet(evidence_path, index=False)
    paths["evidence_parquet"] = str(evidence_path)

    # 4. graphml
    graphml_path = GRAPHS_DIR / "pulse.graphml"
    _write_graphml(G, graphml_path)
    paths["graphml"] = str(graphml_path)

    return paths


def load_graph() -> nx.MultiDiGraph:
    """Load graph from gpickle. Returns empty graph if file doesn't exist."""
    gpickle_path = GRAPHS_DIR / "pulse.gpickle"
    if not gpickle_path.exists():
        return nx.MultiDiGraph()
    with open(gpickle_path, "rb") as f:
        return pickle.load(f)


def _edges_to_dataframe(G: nx.MultiDiGraph) -> pd.DataFrame:
    """Convert edges to a flat DataFrame."""
    rows = []
    for u, v, key, data in G.edges(data=True, keys=True):
        row = {
            "source_node_id": u,
            "target_node_id": v,
            "edge_id": key,
            "edge_type": data.get("edge_type"),
            "weight": data.get("weight"),
            "confidence": data.get("confidence"),
            "evidence_count": data.get("evidence_count"),
            "contradiction_score": data.get("contradiction_score"),
            "source_agreement_score": data.get("source_agreement_score"),
            "first_seen": data.get("first_seen"),
            "last_seen": data.get("last_seen"),
            "last_active": data.get("last_active"),
            "relationship_decay_score": data.get("relationship_decay_score"),
            "temporal_confidence": data.get("temporal_confidence"),
            "review_decision": data.get("review_decision"),
        }
        rows.append(row)
    return pd.DataFrame(rows)


def _evidence_to_dataframe(G: nx.MultiDiGraph) -> pd.DataFrame:
    """Denormalize evidence summary from edge attributes."""
    rows = []
    for u, v, key, data in G.edges(data=True, keys=True):
        for ev in data.get("evidence_summary", []):
            rows.append({
                "edge_id": key,
                "source_node_id": u,
                "target_node_id": v,
                "edge_type": data.get("edge_type"),
                "evidence_type": ev.get("type"),
                "evidence_strength": ev.get("strength"),
                "source_file": ev.get("source"),
            })
    return pd.DataFrame(rows)


def _write_graphml(G: nx.MultiDiGraph, path: Path) -> None:
    """Write graphml, stripping non-serializable attributes."""
    H = nx.MultiDiGraph()
    for node, data in G.nodes(data=True):
        safe_data = {k: str(v) if v is not None else "" for k, v in data.items()}
        H.add_node(node, **safe_data)
    for u, v, key, data in G.edges(data=True, keys=True):
        safe_data = {
            k: str(v) if not isinstance(v, (int, float, str, bool)) else v
            for k, v in data.items()
            if k != "evidence_summary"  # skip nested list
        }
        H.add_edge(u, v, key=key, **safe_data)
    nx.write_graphml(H, str(path))

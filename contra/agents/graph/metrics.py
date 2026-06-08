"""
Graph metrics — read-only analysis. No scoring writebacks.

Surfaces hidden adjacency for the future inference layer.
Results are returned as dicts/DataFrames; nothing is written to the relationships table.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

import networkx as nx
import pandas as pd


def compute_all_metrics(G: nx.MultiDiGraph) -> Dict[str, Any]:
    """
    Compute all read-only metrics on the graph.
    Returns a dict of metric_name → value or DataFrame.
    """
    if G.number_of_nodes() == 0:
        return {"nodes": 0, "edges": 0}

    metrics: Dict[str, Any] = {
        "nodes": G.number_of_nodes(),
        "edges": G.number_of_edges(),
        "density": nx.density(G),
    }

    # Degree statistics
    metrics["degree_df"] = degree_dataframe(G)

    # Centrality (on undirected projection for meaningful betweenness)
    U = nx.Graph(G)
    if U.number_of_nodes() > 1:
        metrics["degree_centrality"] = nx.degree_centrality(U)
        if U.number_of_edges() > 0:
            # Exact betweenness is O(V*E) and per-node clustering is O(V*d^2);
            # both are intractable once the co-investment graph reaches thousands
            # of nodes/edges. Use k-sampled approximate betweenness and skip the
            # full clustering vector on large graphs.
            large = U.number_of_nodes() > 1500 or U.number_of_edges() > 3000
            try:
                if large:
                    k = min(500, U.number_of_nodes())
                    metrics["betweenness_centrality"] = nx.betweenness_centrality(
                        U, k=k, normalized=True, seed=42
                    )
                    metrics["betweenness_approx_k"] = k
                    metrics["average_clustering"] = nx.average_clustering(U)
                else:
                    metrics["betweenness_centrality"] = nx.betweenness_centrality(U, normalized=True)
                    metrics["clustering_coefficients"] = nx.clustering(U)
            except Exception:
                pass

    # Connected components
    undirected = G.to_undirected()
    components = list(nx.connected_components(undirected))
    metrics["connected_components"] = len(components)
    metrics["largest_component_size"] = max(len(c) for c in components) if components else 0

    # Community detection (Louvain if available, else label propagation)
    try:
        import community as louvain
        partition = louvain.best_partition(undirected)
        metrics["communities"] = partition
        metrics["num_communities"] = len(set(partition.values()))
    except ImportError:
        try:
            communities = list(nx.algorithms.community.label_propagation_communities(undirected))
            metrics["num_communities"] = len(communities)
        except Exception:
            pass

    return metrics


def degree_dataframe(G: nx.MultiDiGraph) -> pd.DataFrame:
    """Return per-node degree statistics."""
    rows = []
    for node, data in G.nodes(data=True):
        in_deg = G.in_degree(node)
        out_deg = G.out_degree(node)
        rows.append({
            "node_id": node,
            "node_type": data.get("node_type", "unknown"),
            "canonical_name": data.get("canonical_name", node),
            "in_degree": in_deg,
            "out_degree": out_deg,
            "total_degree": in_deg + out_deg,
        })
    return pd.DataFrame(rows).sort_values("total_degree", ascending=False)


def top_nodes_by_centrality(G: nx.MultiDiGraph, n: int = 20) -> pd.DataFrame:
    """Return top N nodes by betweenness centrality."""
    U = nx.Graph(G)
    if U.number_of_nodes() < 2:
        return pd.DataFrame()
    try:
        bc = nx.betweenness_centrality(U, normalized=True)
    except Exception:
        return pd.DataFrame()

    rows = []
    for node_id, score in sorted(bc.items(), key=lambda x: x[1], reverse=True)[:n]:
        data = G.nodes.get(node_id, {})
        rows.append({
            "node_id": node_id,
            "node_type": data.get("node_type", "unknown"),
            "canonical_name": data.get("canonical_name", node_id),
            "betweenness_centrality": score,
        })
    return pd.DataFrame(rows)


def contradiction_hotspots(G: nx.MultiDiGraph, threshold: float = 0.30) -> pd.DataFrame:
    """Return edges with high contradiction scores — likely candidates for human review."""
    rows = []
    for u, v, key, data in G.edges(data=True, keys=True):
        contra = data.get("contradiction_score")
        if contra is not None and contra >= threshold:
            rows.append({
                "edge_id": key,
                "source_node_id": u,
                "target_node_id": v,
                "edge_type": data.get("edge_type"),
                "contradiction_score": contra,
                "confidence": data.get("confidence"),
                "evidence_count": data.get("evidence_count"),
            })
    return pd.DataFrame(rows).sort_values("contradiction_score", ascending=False)

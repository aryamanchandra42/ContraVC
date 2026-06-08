"""
Second-order syndicate connectivity inference.

Traverses 2-hop co_invested paths from institutional prospects through bridge LPs
to syndicate_lp nodes. Writes network_density / social_proximity signals and
mutual_connection edges with graph_path_inference evidence.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import pandas as pd
import yaml

ROOT = Path(__file__).resolve().parent.parent.parent
PROCESSED = ROOT / "processed_data"
INFERENCE_YAML = ROOT / "prompts" / "graph_inference.yaml"
SOURCE_FILE = "agents/graph/prospect_inference.py"
EVIDENCE_TYPE = "graph_path_inference"
MAX_EVIDENCE_STRENGTH = 0.85


def _load_config() -> Dict[str, Any]:
    if INFERENCE_YAML.exists():
        with open(INFERENCE_YAML, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    return {}


def _edge_weight(weight: Optional[float], temporal_confidence: Optional[float]) -> float:
    w = float(weight or 1.0)
    tc = float(temporal_confidence if temporal_confidence is not None else 1.0)
    return w * tc


def _load_coinvest_graph(con) -> Tuple[Dict[str, List[Tuple[str, float, str, str]]], Dict[str, str]]:
    """
    Build undirected adjacency from co_invested edges in relationships_effective.
    One adjacency entry per edge (deduped); provenance from first evidence row.
    """
    edge_sql = """
        SELECT
            CAST(r.edge_id AS VARCHAR) AS edge_id,
            CAST(r.source_node_id AS VARCHAR) AS source_node_id,
            CAST(r.target_node_id AS VARCHAR) AS target_node_id,
            r.weight,
            r.temporal_confidence,
            (
                SELECT CAST(re.source_record_id AS VARCHAR)
                FROM relationship_evidence re
                WHERE CAST(re.edge_id AS VARCHAR) = CAST(r.edge_id AS VARCHAR)
                LIMIT 1
            ) AS source_record_id
        FROM {table} r
        WHERE r.edge_type = 'co_invested'
    """
    try:
        rows = con.execute(edge_sql.format(table="relationships_effective")).fetchall()
    except Exception:
        rows = con.execute(edge_sql.format(table="relationships")).fetchall()

    adj: Dict[str, List[Tuple[str, float, str, str]]] = defaultdict(list)
    edge_src: Dict[str, str] = {}

    for edge_id, src, tgt, weight, tc, src_rec in rows:
        if not src_rec:
            continue
        w = _edge_weight(weight, tc)
        edge_src[edge_id] = src_rec
        for a, b in ((src, tgt), (tgt, src)):
            adj[a].append((b, w, edge_id, src_rec))

    return adj, edge_src


def _load_populations(con) -> Dict[str, Dict[str, str]]:
    meta: Dict[str, Dict[str, str]] = {}
    for aid, name, pop in con.execute(
        "SELECT CAST(allocator_id AS VARCHAR), canonical_name, population FROM allocators"
    ).fetchall():
        meta[aid] = {"name": name or "", "population": pop or ""}
    return meta


def _load_prospect_ids(con, config: Dict[str, Any]) -> Set[str]:
    pops = set(config.get("prospect_populations", ["institutional_prospect"]))
    ids: Set[str] = set()
    for aid, pop in con.execute(
        "SELECT CAST(allocator_id AS VARCHAR), population FROM allocators"
    ).fetchall():
        if pop in pops:
            ids.add(aid)
    for (aid,) in con.execute(
        "SELECT CAST(allocator_id AS VARCHAR) FROM icp_scores WHERE icp_version = '4.1'"
    ).fetchall():
        ids.add(aid)
    return ids


def _compute_prospect_metrics(
    prospect_id: str,
    adj: Dict[str, List[Tuple[str, float, str, str]]],
    syndicate_ids: Set[str],
    config: Dict[str, Any],
) -> Dict[str, Any]:
    weights_cfg = config.get("connectivity_weights", {})
    min_bridge = float(config.get("min_bridge_strength", 0.15))

    direct_neighbors = adj.get(prospect_id, [])
    direct_syndicate: List[Tuple[str, float]] = []
    for nb, w, eid, _ in direct_neighbors:
        if nb in syndicate_ids:
            direct_syndicate.append((nb, w))

    direct_degree = len(direct_syndicate)
    direct_weight_sum = sum(w for _, w in direct_syndicate)

    two_hop_syndicates: Set[str] = set()
    bridge_paths: List[Dict[str, Any]] = []

    for bridge_id, w_pb, eid_pb, src_pb in direct_neighbors:
        if bridge_id == prospect_id:
            continue
        for synd_id, w_bs, eid_bs, src_bs in adj.get(bridge_id, []):
            if synd_id == prospect_id or synd_id not in syndicate_ids:
                continue
            if synd_id in {n for n, _, _, _ in direct_neighbors}:
                continue
            strength = w_pb * w_bs
            if strength < min_bridge:
                continue
            two_hop_syndicates.add(synd_id)
            bridge_paths.append({
                "syndicate_id": synd_id,
                "bridge_id": bridge_id,
                "bridge_strength": strength,
                "co_invest_edge_ids": [eid_pb, eid_bs],
                "source_record_id": src_pb or src_bs,
            })

    bridge_paths.sort(key=lambda p: p["bridge_strength"], reverse=True)
    bridge_strength_sum = sum(p["bridge_strength"] for p in bridge_paths)
    top_bridge_name = ""
    if bridge_paths:
        top_bridge_name = bridge_paths[0]["bridge_id"]

    return {
        "direct_syndicate_degree": direct_degree,
        "direct_weight_sum": direct_weight_sum,
        "two_hop_syndicate_reach": len(two_hop_syndicates),
        "bridge_strength_sum": bridge_strength_sum,
        "bridge_paths": bridge_paths,
        "top_bridge_name": top_bridge_name,
    }


def _normalize_metrics(all_metrics: List[Dict[str, Any]], config: Dict[str, Any]) -> None:
    w_cfg = config.get("connectivity_weights", {})
    w_direct = float(w_cfg.get("direct_syndicate_degree", 0.30))
    w_two_hop = float(w_cfg.get("two_hop_syndicate_reach", 0.40))
    w_bridge = float(w_cfg.get("bridge_strength", 0.30))

    max_direct = max((m["direct_syndicate_degree"] for m in all_metrics), default=1) or 1
    max_two_hop = max((m["two_hop_syndicate_reach"] for m in all_metrics), default=1) or 1
    max_bridge = max((m["bridge_strength_sum"] for m in all_metrics), default=1.0) or 1.0

    for m in all_metrics:
        nd = m["direct_syndicate_degree"] / max_direct
        nth = m["two_hop_syndicate_reach"] / max_two_hop
        nb = m["bridge_strength_sum"] / max_bridge
        m["connectivity_score"] = round(
            min(1.0, w_direct * nd + w_two_hop * nth + w_bridge * nb),
            4,
        )
        m["social_proximity"] = round(nth, 4)


def _clear_inference_artifacts(con) -> int:
    edge_rows = con.execute(
        """
        SELECT CAST(r.edge_id AS VARCHAR)
        FROM relationships r
        JOIN relationship_evidence re ON CAST(re.edge_id AS VARCHAR) = CAST(r.edge_id AS VARCHAR)
        WHERE r.edge_type = 'mutual_connection' AND re.evidence_type = ?
        """,
        [EVIDENCE_TYPE],
    ).fetchall()
    edge_ids = [r[0] for r in edge_rows]

    con.execute(
        "DELETE FROM relationship_evidence WHERE evidence_type = ?",
        [EVIDENCE_TYPE],
    )
    for eid in edge_ids:
        con.execute(
            "DELETE FROM relationships WHERE CAST(edge_id AS VARCHAR) = ?",
            [eid],
        )

    con.execute(
        """
        DELETE FROM signal_evidence
        WHERE CAST(signal_id AS VARCHAR) IN (
            SELECT CAST(signal_id AS VARCHAR) FROM signals
            WHERE signal_type IN (
                'network_density', 'social_proximity',
                'bridge_strength', 'warm_path_count'
            )
            AND source_file = ?
        )
        """,
        [SOURCE_FILE],
    )
    con.execute(
        """
        DELETE FROM signals
        WHERE signal_type IN (
            'network_density', 'social_proximity',
            'bridge_strength', 'warm_path_count'
        )
          AND source_file = ?
        """,
        [SOURCE_FILE],
    )
    return len(edge_ids)


def _write_signals(con, metrics_by_id: Dict[str, Dict[str, Any]]) -> int:
    batch: List[tuple] = []
    ev_batch: List[tuple] = []
    for aid, m in metrics_by_id.items():
        if m["bridge_paths"]:
            src_rec = m["bridge_paths"][0]["source_record_id"]
        else:
            row = con.execute(
                "SELECT source_record_id FROM allocators WHERE CAST(allocator_id AS VARCHAR) = ?",
                [aid],
            ).fetchone()
            src_rec = row[0] if row and row[0] else None
        if not src_rec:
            continue

        evidence_json = json.dumps({
            "bridge_nodes": list({p["bridge_id"] for p in m["bridge_paths"][:5]}),
            "path_count": len(m["bridge_paths"]),
            "top_co_invest_edge_ids": (
                m["bridge_paths"][0]["co_invest_edge_ids"] if m["bridge_paths"] else []
            ),
        })
        max_bridge = max(
            (x["bridge_strength_sum"] for x in metrics_by_id.values()), default=1.0
        ) or 1.0
        bridge_norm = round(min(1.0, m["bridge_strength_sum"] / max_bridge), 4)
        warm_count = len(m["bridge_paths"])
        warm_norm = round(min(1.0, warm_count / 25.0), 4)

        for sig_type, val in (
            ("network_density", m["connectivity_score"]),
            ("social_proximity", m["social_proximity"]),
            ("bridge_strength", bridge_norm),
            ("warm_path_count", warm_norm),
        ):
            sig_id = str(uuid.uuid4())
            batch.append((
                sig_id,
                aid,
                sig_type,
                evidence_json,
                val,
                src_rec,
                SOURCE_FILE,
                hashlib.sha256(f"{aid}|{sig_type}|{src_rec}".encode()).hexdigest(),
            ))
            ev_batch.append((
                str(uuid.uuid4()),
                sig_id,
                src_rec,
                "signal_connectivity",
                min(0.85, 0.5 + val * 0.35),
                min(0.85, 0.5 + val * 0.35),
                datetime.now(timezone.utc).isoformat(),
                json.dumps({
                    "source_file": SOURCE_FILE,
                    "path_count": warm_count,
                }),
                f"bridge_strength_sum={m['bridge_strength_sum']:.4f}",
            ))

    if batch:
        con.executemany(
            """
            INSERT INTO signals (
                signal_id, allocator_id, signal_type,
                raw_value, normalized_value,
                confidence, evidence_count, contradiction_score, source_agreement_score,
                source_record_id, source_file, content_hash
            ) VALUES (?, ?, ?, ?, ?, NULL, 0, NULL, NULL, ?, ?, ?)
            """,
            batch,
        )
        if ev_batch:
            con.executemany(
                """
                INSERT INTO signal_evidence (
                    evidence_id, signal_id, source_record_id, evidence_type,
                    evidence_strength, confidence, timestamp, provenance_pointer, notes
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                ev_batch,
            )
    return len(batch)


def _write_mutual_connection_edges(
    con,
    metrics_by_id: Dict[str, Dict[str, Any]],
    meta: Dict[str, Dict[str, str]],
    config: Dict[str, Any],
) -> Tuple[int, int]:
    max_edges = int(config.get("max_edges_per_prospect", 25))
    now = datetime.now(timezone.utc).isoformat()
    edge_batch: List[tuple] = []
    ev_batch: List[tuple] = []

    for prospect_id, m in metrics_by_id.items():
        seen_targets: Set[str] = set()
        count = 0
        for path in m["bridge_paths"]:
            if count >= max_edges:
                break
            synd_id = path["syndicate_id"]
            if synd_id in seen_targets:
                continue
            if not path.get("source_record_id"):
                continue
            seen_targets.add(synd_id)
            count += 1

            edge_id = str(uuid.uuid4())
            strength = min(MAX_EVIDENCE_STRENGTH, path["bridge_strength"])
            edge_batch.append((
                edge_id,
                prospect_id,
                "lp",
                synd_id,
                "lp",
                "mutual_connection",
                path["bridge_strength"],
                now,
                now,
            ))
            ev_batch.append((
                str(uuid.uuid4()),
                edge_id,
                path["source_record_id"],
                EVIDENCE_TYPE,
                strength,
                strength,
                json.dumps({
                    "bridge_node_id": path["bridge_id"],
                    "co_invest_edge_ids": path["co_invest_edge_ids"],
                    "hop_count": 2,
                    "source": SOURCE_FILE,
                    "prospect_id": prospect_id,
                    "syndicate_id": synd_id,
                }),
            ))

    if edge_batch:
        con.executemany(
            """
            INSERT INTO relationships (
                edge_id, source_node_id, source_node_type,
                target_node_id, target_node_type, edge_type,
                weight, first_seen, last_seen
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            edge_batch,
        )
        con.executemany(
            """
            INSERT INTO relationship_evidence (
                evidence_id, edge_id, source_record_id, evidence_type,
                evidence_strength, confidence, provenance_pointer
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            ev_batch,
        )

    return len(edge_batch), len(ev_batch)


def export_connectivity_csv(
    metrics_by_id: Dict[str, Dict[str, Any]],
    meta: Dict[str, Dict[str, str]],
    out_path: Optional[Path] = None,
) -> Path:
    out_path = out_path or PROCESSED / "Prospect_Syndicate_Connectivity.csv"
    PROCESSED.mkdir(parents=True, exist_ok=True)

    rows = []
    for aid, m in metrics_by_id.items():
        info = meta.get(aid, {})
        top_bridge = m.get("top_bridge_name", "")
        top_bridge_name = meta.get(top_bridge, {}).get("name", top_bridge)
        rows.append({
            "allocator_id": aid,
            "canonical_name": info.get("name", ""),
            "population": info.get("population", ""),
            "connectivity_score": m.get("connectivity_score", 0.0),
            "direct_syndicate_degree": m.get("direct_syndicate_degree", 0),
            "two_hop_syndicate_reach": m.get("two_hop_syndicate_reach", 0),
            "bridge_strength_sum": round(m.get("bridge_strength_sum", 0.0), 4),
            "top_bridge_name": top_bridge_name,
        })

    df = pd.DataFrame(rows)
    if not df.empty:
        df = df.sort_values("connectivity_score", ascending=False)

    # Use safe writer so the file can be open in Excel without aborting the pipeline.
    from pulse.safe_io import safe_write_csv
    return safe_write_csv(df, out_path)


def run_prospect_inference(con) -> Dict[str, Any]:
    """Run 2-hop syndicate connectivity inference. Idempotent."""
    config = _load_config()
    syndicate_pops = set(config.get("syndicate_populations", ["syndicate_lp"]))

    adj, _ = _load_coinvest_graph(con)
    meta = _load_populations(con)
    prospect_ids = _load_prospect_ids(con, config)

    syndicate_ids = {aid for aid, m in meta.items() if m["population"] in syndicate_pops}

    cleared = _clear_inference_artifacts(con)

    all_metrics: List[Dict[str, Any]] = []
    metrics_by_id: Dict[str, Dict[str, Any]] = {}

    for pid in prospect_ids:
        m = _compute_prospect_metrics(pid, adj, syndicate_ids, config)
        m["allocator_id"] = pid
        all_metrics.append(m)
        metrics_by_id[pid] = m

    _normalize_metrics(all_metrics, config)

    signals_written = _write_signals(con, metrics_by_id)
    edges, evidence = _write_mutual_connection_edges(con, metrics_by_id, meta, config)
    csv_path = export_connectivity_csv(metrics_by_id, meta)

    return {
        "prospects_analyzed": len(prospect_ids),
        "syndicate_nodes": len(syndicate_ids),
        "cleared_inference_edges": cleared,
        "mutual_connection_edges": edges,
        "graph_path_inference_evidence": evidence,
        "signals_written": signals_written,
        "connectivity_csv": str(csv_path),
    }

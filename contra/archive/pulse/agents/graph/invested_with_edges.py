"""
invested_with edge writer — LP pairs sharing the same fund vehicle.

Skips megadeals (>MAX_LPS_PER_FUND) to avoid combinatorial blow-up.
Prioritizes edges touching institutional_prospect / icp-scored allocators.
"""

from __future__ import annotations

import json
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from itertools import combinations
from typing import Dict, List, Set, Tuple

EVIDENCE_TYPE = "structured_xlsx_match"
EDGE_TYPE = "invested_with"
EVIDENCE_STRENGTH = 0.75
SOURCE_FILE = "agents/graph/invested_with_edges.py"
MAX_LPS_PER_FUND = 40
MAX_EDGES = 50_000


def _priority_lps(con) -> Set[str]:
    ids: Set[str] = set()
    for (aid,) in con.execute(
        "SELECT CAST(allocator_id AS VARCHAR) FROM allocators WHERE population = 'institutional_prospect'"
    ).fetchall():
        ids.add(aid)
    for (aid,) in con.execute(
        "SELECT CAST(allocator_id AS VARCHAR) FROM icp_scores WHERE icp_version = '4.1'"
    ).fetchall():
        ids.add(aid)
    return ids


def build_invested_with_edges(con) -> Dict[str, int]:
    priority = _priority_lps(con)

    rows = con.execute(
        """
        SELECT CAST(lp_id AS VARCHAR), CAST(fund_id AS VARCHAR), source_record_id
        FROM investments
        """
    ).fetchall()

    fund_members: Dict[str, List[Tuple[str, str]]] = defaultdict(list)
    for lp_id, fund_id, src_rec in rows:
        if lp_id and fund_id and src_rec:
            fund_members[fund_id].append((lp_id, src_rec))

    pair_evidence: Dict[Tuple[str, str], List[str]] = defaultdict(list)

    for fund_id in sorted(fund_members.keys()):
        members = fund_members[fund_id]
        unique_lps = sorted({m[0]: m[1] for m in members}.items(), key=lambda x: x[0])
        if len(unique_lps) < 2 or len(unique_lps) > MAX_LPS_PER_FUND:
            continue
        for (a, src_a), (b, src_b) in combinations(unique_lps, 2):
            if priority and a not in priority and b not in priority:
                continue
            key = tuple(sorted((a, b)))
            src = src_a or src_b
            if src and len(pair_evidence[key]) < 3:
                pair_evidence[key].append(src)
            if len(pair_evidence) >= MAX_EDGES:
                break
        if len(pair_evidence) >= MAX_EDGES:
            break

    if not pair_evidence:
        return {"invested_with_edges": 0, "invested_with_evidence": 0}

    con.execute(
        """
        DELETE FROM relationship_evidence
        WHERE CAST(edge_id AS VARCHAR) IN (
            SELECT CAST(edge_id AS VARCHAR) FROM relationships WHERE edge_type = ?
        )
        """,
        [EDGE_TYPE],
    )
    con.execute("DELETE FROM relationships WHERE edge_type = ?", [EDGE_TYPE])

    now = datetime.now(timezone.utc).isoformat()
    edge_batch: List[tuple] = []
    ev_batch: List[tuple] = []

    for (a, b), srcs in sorted(pair_evidence.items()):
        if not srcs:
            continue
        edge_id = str(uuid.uuid4())
        weight = float(len(srcs))
        edge_batch.append((edge_id, a, "lp", b, "lp", EDGE_TYPE, weight, now, now))
        for src_id in srcs:
            ev_batch.append((
                str(uuid.uuid4()), edge_id, src_id, EVIDENCE_TYPE,
                EVIDENCE_STRENGTH, EVIDENCE_STRENGTH,
                json.dumps({"source_file": SOURCE_FILE, "lp_a": a, "lp_b": b}),
            ))

    if edge_batch:
        con.executemany(
            """
            INSERT INTO relationships (
                edge_id, source_node_id, source_node_type,
                target_node_id, target_node_type, edge_type, weight,
                first_seen, last_seen
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

    return {
        "invested_with_edges": len(edge_batch),
        "invested_with_evidence": len(ev_batch),
    }

"""
Latent signal extractor — surfaces signals from data already in PULSE.

Sources:
  - investments table (16k+ syndicate transactions)
  - icp_scores S5/S6/S7 mirror
  - co_invested edge weights (shared_deal_count)

Writes signal_evidence rows; confidence is populated by pulse derive.
"""

from __future__ import annotations

import hashlib
import math
import uuid
from collections import defaultdict
from datetime import date
from typing import Any, Dict, List, Optional, Tuple

from agents.scoring.signal_evidence_writer import (
    delete_signals_cascade,
    insert_signals_batch,
    make_evidence_row,
)

SOURCE_FILE = "agents/scoring/latent_signal_extractor.py"


def _stable_hash(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode()).hexdigest()


def _days_since(d: Optional[date], today: Optional[date] = None) -> Optional[float]:
    if d is None:
        return None
    ref = today or date.today()
    return float((ref - d).days)


def _recency_score(days: Optional[float], half_life: float = 180.0) -> float:
    if days is None:
        return 0.25
    if days < 0:
        days = 0.0
    return round(math.exp(-days / half_life), 4)


def _extract_investment_signals(con) -> Dict[str, int]:
    rows = con.execute(
        """
        SELECT
            CAST(lp_id AS VARCHAR) AS lp_id,
            CAST(fund_id AS VARCHAR) AS fund_id,
            investment_date,
            source_record_id
        FROM investments
        """
    ).fetchall()

    by_lp: Dict[str, List[Tuple]] = defaultdict(list)
    for lp_id, fund_id, inv_date, src_rec in rows:
        by_lp[lp_id].append((fund_id, inv_date, src_rec))

    if not by_lp:
        return {"coinvest_intensity": 0, "recent_activity_recency": 0}

    max_deals = max(len({f for f, _, _ in deals}) for deals in by_lp.values()) or 1

    signal_rows: List[Tuple] = []
    evidence_rows: List[Tuple] = []

    for lp_id, deals in by_lp.items():
        funds = {f for f, _, _ in deals}
        dates = [d for _, d, _ in deals if d is not None]
        src_rec = next((s for _, _, s in deals if s), _stable_hash(lp_id))

        intensity = round(min(1.0, len(funds) / max(max_deals, 1)), 4)
        min_days = min((_days_since(d) for d in dates if d is not None), default=None)
        recency = _recency_score(min_days)

        for sig_type, val, notes in (
            ("coinvest_intensity", intensity, f"distinct_funds={len(funds)}"),
            ("recent_activity_recency", recency, f"days_since_last={min_days}"),
        ):
            sig_id = str(uuid.uuid4())
            signal_rows.append((
                sig_id, lp_id, sig_type, notes, val, src_rec, SOURCE_FILE,
                _stable_hash(lp_id, sig_type, src_rec),
            ))
            evidence_rows.append(make_evidence_row(
                sig_id, src_rec, "signal_investment_pattern", val, SOURCE_FILE, notes,
            ))

    insert_signals_batch(con, signal_rows, evidence_rows)
    return {
        "coinvest_intensity": len(by_lp),
        "recent_activity_recency": len(by_lp),
    }


def _extract_shared_deal_signals(con) -> int:
    rows = con.execute(
        """
        SELECT
            CAST(source_node_id AS VARCHAR),
            CAST(target_node_id AS VARCHAR),
            weight,
            (
                SELECT CAST(re.source_record_id AS VARCHAR)
                FROM relationship_evidence re
                WHERE CAST(re.edge_id AS VARCHAR) = CAST(r.edge_id AS VARCHAR)
                LIMIT 1
            ) AS source_record_id
        FROM relationships r
        WHERE edge_type = 'co_invested'
        """
    ).fetchall()

    per_lp: Dict[str, Tuple[float, str]] = {}
    for src, tgt, weight, src_rec in rows:
        w = float(weight or 1.0)
        rec = src_rec or _stable_hash(src, tgt)
        for lp in (src, tgt):
            prev = per_lp.get(lp)
            if prev is None or w > prev[0]:
                per_lp[lp] = (w, rec)

    signal_rows: List[Tuple] = []
    evidence_rows: List[Tuple] = []
    max_w = max((v[0] for v in per_lp.values()), default=1.0) or 1.0

    for lp_id, (weight, src_rec) in per_lp.items():
        norm = round(min(1.0, weight / max_w), 4)
        sig_id = str(uuid.uuid4())
        signal_rows.append((
            sig_id, lp_id, "shared_deal_count", str(int(weight)), norm,
            src_rec, SOURCE_FILE, _stable_hash(lp_id, "shared_deal_count", src_rec),
        ))
        evidence_rows.append(make_evidence_row(
            sig_id, src_rec, "signal_graph_metric", norm, SOURCE_FILE,
            notes=f"max_shared_deals={int(weight)}",
        ))

    insert_signals_batch(con, signal_rows, evidence_rows)
    return len(per_lp)


def _extract_icp_mirror_signals(con) -> Dict[str, int]:
    rows = con.execute(
        """
        SELECT
            CAST(i.allocator_id AS VARCHAR),
            i.s5_stage, i.s6_clean_profile, i.s7_proxy_fund,
            i.source_file,
            COALESCE(
                (
                    SELECT CAST(e.source_record_id AS VARCHAR)
                    FROM entities_raw e
                    WHERE e.source_file = i.source_file
                      AND CAST(json_extract_string(e.raw_content, '$._row_number') AS INTEGER) = i.source_row
                    LIMIT 1
                ),
                (
                    SELECT CAST(a.source_record_id AS VARCHAR)
                    FROM allocators a
                    WHERE CAST(a.allocator_id AS VARCHAR) = CAST(i.allocator_id AS VARCHAR)
                    LIMIT 1
                )
            ) AS src_rec
        FROM icp_scores i
        WHERE i.icp_version = '4.1'
        """
    ).fetchall()

    mapping = (
        ("stage_alignment", "s5_stage"),
        ("clean_profile", "s6_clean_profile"),
        ("proxy_fund_overlap", "s7_proxy_fund"),
    )
    counts = {m[0]: 0 for m in mapping}
    signal_rows: List[Tuple] = []
    evidence_rows: List[Tuple] = []

    for aid, s5, s6, s7, src_file, src_rec in rows:
        if not src_rec:
            continue
        vals = {"s5_stage": s5, "s6_clean_profile": s6, "s7_proxy_fund": s7}
        file_ref = src_file or "icp_scores"

        for sig_type, col in mapping:
            val = float(vals[col] or 0.0)
            sig_id = str(uuid.uuid4())
            signal_rows.append((
                sig_id, aid, sig_type, None, val, src_rec, SOURCE_FILE,
                _stable_hash(aid, sig_type, src_rec),
            ))
            evidence_rows.append(make_evidence_row(
                sig_id, src_rec, "signal_icp_mirror", val, file_ref,
                notes=f"icp_column={col}",
            ))
            counts[sig_type] += 1

    insert_signals_batch(con, signal_rows, evidence_rows)
    return counts


def _clear_prior(con) -> None:
    delete_signals_cascade(con, "source_file = ?", [SOURCE_FILE])


def run_latent_signal_extraction(con) -> Dict[str, Any]:
    _clear_prior(con)
    out: Dict[str, Any] = {}
    out.update(_extract_investment_signals(con))
    out["shared_deal_count"] = _extract_shared_deal_signals(con)
    out.update(_extract_icp_mirror_signals(con))
    return out

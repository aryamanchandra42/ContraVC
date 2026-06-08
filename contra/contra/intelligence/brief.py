"""Assemble IntelligenceBrief for gate and LLM context."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

import pandas as pd

from agents.scoring.icp_spec import ICP_VERSION
from contra.intelligence.resolver import MatchResult, norm_key, resolve


@dataclass
class IntelligenceBrief:
    input_name: str
    matched_name: Optional[str] = None
    match_confidence: float = 0.0
    match_method: str = "none"
    allocator_id: Optional[str] = None
    population: Optional[str] = None
    in_crm: bool = False
    icp_tier: Optional[str] = None
    icp_fit_score: Optional[float] = None
    core_pass: Optional[bool] = None
    excluded: Optional[bool] = None
    exclusion_reason: Optional[str] = None
    client_decision: Optional[str] = None
    core_gates: Dict[str, Any] = field(default_factory=dict)
    top_signals: List[Dict[str, Any]] = field(default_factory=list)
    rejection_reasons: List[str] = field(default_factory=list)
    investment_summary: Optional[Dict[str, Any]] = None
    graph_connectivity: Optional[Dict[str, Any]] = None
    syndicate_profile: Optional[Dict[str, Any]] = None
    warm_paths: List[Dict[str, Any]] = field(default_factory=list)
    contacts: List[Dict[str, Any]] = field(default_factory=list)
    benchmark_rank: Optional[int] = None
    allocator_profile: Dict[str, Any] = field(default_factory=dict)
    source_snippets: List[str] = field(default_factory=list)
    crm_row: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _crm_lookup(con, name: str) -> tuple[bool, Optional[Dict[str, Any]]]:
    key = norm_key(name)
    row = con.execute(
        """
        SELECT investor_name, investor_type, investor_location, investor_details, pipeline_stage
        FROM crm_leads
        WHERE status != 'passed'
          AND (name_key = ? OR investor_name ILIKE ?)
        LIMIT 1
        """,
        [key, f"%{name}%"],
    ).fetchone()
    if row:
        return True, {
            "investor_name": row[0],
            "investor_type": row[1],
            "investor_location": row[2],
            "investor_details": (row[3] or "")[:500],
            "crm_status": row[4],
        }
    row = con.execute(
        """
        SELECT investor_name, investor_type, investor_location, investor_details, crm_status
        FROM crm_contacts
        WHERE name_key = ? OR investor_name ILIKE ?
        LIMIT 1
        """,
        [key, f"%{name}%"],
    ).fetchone()
    if not row:
        return False, None
    return True, {
        "investor_name": row[0],
        "investor_type": row[1],
        "investor_location": row[2],
        "investor_details": (row[3] or "")[:500],
        "crm_status": row[4],
    }


def build(con, name: str, match: Optional[MatchResult] = None) -> IntelligenceBrief:
    match = match or resolve(con, name)
    in_crm, crm_row = _crm_lookup(con, name)

    brief = IntelligenceBrief(
        input_name=name,
        matched_name=match.matched_name,
        match_confidence=match.confidence,
        match_method=match.method,
        allocator_id=match.allocator_id,
        in_crm=in_crm,
        crm_row=crm_row,
    )

    if not match.allocator_id:
        return brief

    profile = con.execute(
        "SELECT * FROM v_lp_profile WHERE allocator_id = ? LIMIT 1",
        [match.allocator_id],
    ).fetchdf()
    if profile.empty:
        return brief

    p = profile.iloc[0]
    def _bool_col(val: Any) -> Optional[bool]:
        if val is None or (isinstance(val, float) and pd.isna(val)):
            return None
        try:
            if pd.isna(val):
                return None
        except (TypeError, ValueError):
            pass
        return bool(val)

    brief.population = p.get("population") if pd.notna(p.get("population")) else None
    brief.icp_tier = p.get("icp_tier") if pd.notna(p.get("icp_tier")) else None
    brief.icp_fit_score = float(p["fit_score"]) if p.get("fit_score") is not None and pd.notna(p.get("fit_score")) else None
    brief.core_pass = _bool_col(p.get("core_pass"))
    brief.excluded = _bool_col(p.get("excluded"))
    brief.exclusion_reason = p.get("exclusion_reason") if pd.notna(p.get("exclusion_reason")) else None
    brief.client_decision = p.get("client_decision") if pd.notna(p.get("client_decision")) else None
    rank_val = p.get("contra_rank")
    brief.benchmark_rank = int(rank_val) if rank_val is not None and pd.notna(rank_val) else None
    brief.core_gates = {
        "c1": p.get("c1_evidence"),
        "c2": p.get("c2_evidence"),
        "c3": p.get("c3_evidence"),
        "c4": p.get("c4_evidence"),
    }
    brief.allocator_profile = {
        "type": p.get("allocator_type"),
        "geography": p.get("geography"),
        "em_appetite": p.get("em_appetite"),
        "ai_appetite": p.get("ai_appetite"),
        "check_size_bucket": p.get("check_size_bucket"),
    }
    brief.graph_connectivity = {
        "warm_path_count": int(p.get("warm_path_count") or 0),
        "investment_count": int(p.get("investment_count") or 0),
        "signal_count": int(p.get("signal_count") or 0),
    }
    brief.in_crm = brief.in_crm or _bool_col(p.get("in_crm")) or False

    signals = con.execute(
        """
        SELECT signal_type, normalized_value, confidence, source_file
        FROM signals WHERE CAST(allocator_id AS VARCHAR) = ?
        ORDER BY confidence DESC NULLS LAST LIMIT 8
        """,
        [match.allocator_id],
    ).fetchdf()
    brief.top_signals = signals.to_dict(orient="records")

    rejects = con.execute(
        """
        SELECT stated_reason FROM rejections
        WHERE CAST(allocator_id AS VARCHAR) = ?
        LIMIT 5
        """,
        [match.allocator_id],
    ).fetchall()
    brief.rejection_reasons = [r[0] for r in rejects if r[0]]

    # Investment summary with recency buckets so the gate can weight recent
    # allocation activity more heavily than stale commitments.
    inv = con.execute(
        """
        SELECT
            COUNT(*)                                                         AS deals,
            COALESCE(SUM(commitment_usd), 0)                                 AS total_usd,
            COUNT(CASE WHEN lower(notes) IN ('venture fund', 'fund') THEN 1 END) AS fund_deals,
            COUNT(CASE WHEN lower(notes) = 'spv' THEN 1 END)                 AS spv_deals,
            MAX(investment_date)                                             AS last_date,
            MAX(CASE WHEN lower(notes) IN ('venture fund', 'fund')
                     THEN investment_date END)                              AS last_fund_date,
            COUNT(CASE WHEN investment_date >= (CURRENT_DATE - INTERVAL 24 MONTH)
                       THEN 1 END)                                          AS recent_24mo,
            COUNT(CASE WHEN investment_date <  (CURRENT_DATE - INTERVAL 24 MONTH)
                        AND investment_date >= (CURRENT_DATE - INTERVAL 5 YEAR)
                       THEN 1 END)                                          AS window_2_5yr,
            COUNT(CASE WHEN investment_date <  (CURRENT_DATE - INTERVAL 7 YEAR)
                       THEN 1 END)                                          AS older_7yr
        FROM investments WHERE lp_id = CAST(? AS UUID)
        """,
        [match.allocator_id],
    ).fetchone()
    if inv:
        brief.investment_summary = {
            "deal_count": int(inv[0] or 0),
            "total_usd": float(inv[1] or 0),
            "fund_deal_count": int(inv[2] or 0),
            "spv_deal_count": int(inv[3] or 0),
            "last_investment_date": str(inv[4]) if inv[4] is not None else None,
            "last_fund_deal_date": str(inv[5]) if inv[5] is not None else None,
            "recent_24mo": int(inv[6] or 0),
            "window_2_5yr": int(inv[7] or 0),
            "older_7yr": int(inv[8] or 0),
        }

    snippets = con.execute(
        """
        SELECT chunk_text, source_file FROM v_document_chunks
        WHERE chunk_text ILIKE ?
        LIMIT 3
        """,
        [f"%{(match.matched_name or name)[:40]}%"],
    ).fetchall()
    brief.source_snippets = [
        f"[{s[1]}] {(s[0] or '')[:300]}" for s in snippets if s[0]
    ]

    # --- Syndicate profile (works for syndicate_lp population or any LP with fund investments) ---
    try:
        sp = con.execute(
            "SELECT * FROM v_syndicate_profile WHERE allocator_id = ? LIMIT 1",
            [match.allocator_id],
        ).fetchdf()
        if not sp.empty:
            row = sp.iloc[0]
            brief.syndicate_profile = {
                "fund_deal_count": int(row.get("fund_deal_count") or 0),
                "spv_deal_count": int(row.get("spv_deal_count") or 0),
                "total_deal_count": int(row.get("total_deal_count") or 0),
                "total_committed_usd": float(row.get("total_committed_usd") or 0),
                "fund_lp_ratio": float(row.get("fund_lp_ratio") or 0),
                "is_fund_lp": bool(row.get("is_fund_lp")),
                "is_upgrade_candidate": bool(row.get("is_upgrade_candidate")),
                "fund_lp_behavior_score": float(row.get("fund_lp_behavior_score") or 0)
                    if pd.notna(row.get("fund_lp_behavior_score")) else None,
            }
    except Exception:
        pass

    # --- Warm paths (top 3 intro routes via mutual_connection edges) ---
    try:
        wp = con.execute(
            """
            SELECT prospect_name, bridge_name, bridge_type, bridge_strength
            FROM v_warm_paths
            WHERE prospect_id = ?
            ORDER BY bridge_strength DESC NULLS LAST
            LIMIT 3
            """,
            [match.allocator_id],
        ).fetchdf()
        if not wp.empty:
            brief.warm_paths = wp.to_dict(orient="records")
            # Also update graph_connectivity
            if brief.graph_connectivity:
                brief.graph_connectivity["warm_paths"] = brief.warm_paths
    except Exception:
        pass

    # --- Contacts (LinkedIn + CRM merged) ---
    try:
        contacts = con.execute(
            """
            SELECT full_name, email, linkedin_url, title, company, location, source, match_confidence
            FROM allocator_contacts
            WHERE allocator_id = ?
            ORDER BY match_confidence DESC NULLS LAST
            LIMIT 5
            """,
            [match.allocator_id],
        ).fetchdf()
        if not contacts.empty:
            brief.contacts = contacts.to_dict(orient="records")
    except Exception:
        pass

    return brief


def lookup(con, name: str) -> IntelligenceBrief:
    return build(con, name)

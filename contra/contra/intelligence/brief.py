"""Assemble IntelligenceBrief for gate and LLM context."""

from __future__ import annotations

import json
import re
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
    # True when the backend match looks like the wrong person (alias/fuzzy with
    # surname mismatch). Evaluator treats this as no_db_record to avoid using
    # the wrong person's ICP/syndicate data.
    match_untrusted: bool = False
    # Deal names from a low-confidence (fuzzy_low) DB match — shown in the gate
    # prompt with an explicit "LOW CONFIDENCE" caveat so the LLM can reason about
    # the investment type without trusting identity.
    partial_match_deals: List[str] = field(default_factory=list)
    # Top-N confirmed LP investors from our DB with similar profiles (geography,
    # sector, archetype). Used as calibration anchors: "similar LPs who committed
    # to funds give us a baseline for what a YES looks like."
    similar_confirmed_lps: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _is_match_untrusted(input_name: str, matched_name: str, match_method: str) -> bool:
    """
    Return True when a fuzzy/alias match is likely the wrong person.

    Heuristic: the last significant token of the input name (the surname or most
    distinctive word) must appear verbatim in the matched name. If it does not,
    the match is almost certainly mapping to a different individual
    (e.g. "Will Bricker" → "Will Au").

    Only fires for alias and fuzzy matches, not exact matches.
    """
    if match_method not in ("alias", "fuzzy", "fuzzy_review", "fuzzy_low"):
        return False
    if not input_name or not matched_name:
        return False

    def _alphanum_lower(s: str) -> str:
        return re.sub(r"[^a-z0-9]", "", s.lower())

    # Require at least two tokens so single-token first-names don't trigger a false mismatch.
    # Take the last token that's more than 2 characters long (skip initials/abbreviations).
    parts = [p for p in input_name.split() if len(p) > 2]
    if len(parts) < 2:
        return False
    last_token = _alphanum_lower(parts[-1])
    matched_norm = _alphanum_lower(matched_name)
    return last_token not in matched_norm


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
        match_untrusted=_is_match_untrusted(name, match.matched_name or "", match.method),
    )

    if not match.allocator_id:
        return brief

    # fuzzy_low: match confidence is below FUZZY_REVIEW (85).
    # Load investment history ONLY — enough to detect "angel-only" patterns.
    # Skip ICP scores, syndicate, warm paths (risk of loading wrong person's data).
    if match.method == "fuzzy_low":
        brief.match_untrusted = True
        try:
            inv = con.execute(
                """
                SELECT
                    COUNT(*)                                                         AS deals,
                    COALESCE(SUM(i.commitment_usd), 0)                              AS total_usd,
                    COUNT(CASE WHEN lower(i.notes) IN ('venture fund', 'fund')
                               THEN 1 END)                                          AS fund_deals,
                    COUNT(CASE WHEN lower(i.notes) = 'spv' THEN 1 END)             AS spv_deals,
                    MAX(i.investment_date)                                          AS last_date
                FROM investments i
                WHERE i.lp_id = CAST(? AS UUID)
                """,
                [match.allocator_id],
            ).fetchone()
            if inv and int(inv[0] or 0) > 0:
                brief.investment_summary = {
                    "deal_count": int(inv[0]),
                    "total_usd": float(inv[1] or 0),
                    "fund_deal_count": int(inv[2] or 0),
                    "spv_deal_count": int(inv[3] or 0),
                    "last_investment_date": str(inv[4]) if inv[4] is not None else None,
                    "_low_confidence": True,
                }
                # Fetch individual deal names so LLM can see the type of investments
                deals = con.execute(
                    """
                    SELECT f.canonical_name, f.fund_type, i.notes, i.commitment_usd
                    FROM investments i
                    JOIN funds f ON f.fund_id = i.fund_id
                    WHERE i.lp_id = CAST(? AS UUID)
                    ORDER BY i.investment_date DESC NULLS LAST
                    LIMIT 10
                    """,
                    [match.allocator_id],
                ).fetchall()
                brief.partial_match_deals = [
                    f"{r[0]} ({r[1] or r[2] or 'unknown type'})"
                    + (f" ${int(r[3]):,}" if r[3] else "")
                    for r in deals
                ]
        except Exception:
            pass
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


def find_similar_confirmed_lps(
    con,
    target=None,
    *,
    geography: Optional[str] = None,
    sector: Optional[str] = None,
    limit: int = 4,
) -> List[Dict[str, Any]]:
    """
    Return confirmed VC fund LP investors from our DB whose profiles are similar
    to the LP being screened, scored by the multi-dimensional similarity algorithm.

    Accepts either a pre-built LpSimilarityTarget (preferred) or legacy keyword
    args (geography / sector) for backward compatibility.

    Returns dicts with: name, geography, em_appetite, ai_appetite, allocator_type,
    fund_deal_count, total_fund_usd, similarity_score, match_dimensions, archetype.
    Only LPs with similarity_score >= MIN_DISPLAY_SCORE are returned.
    """
    from contra.intelligence.lp_similarity import (
        LpSimilarityTarget,
        MIN_DISPLAY_SCORE,
        build_similarity_target as _build,
        infer_db_archetype,
        score_lp_similarity,
    )
    from agents.normalization.taxonomies import normalize_geography

    # Build a minimal target from legacy kwargs when a full target wasn't supplied.
    if target is None:
        # Create a stub IntelligenceBrief-like object just for the legacy path.
        class _StubBrief:
            allocator_profile = {
                "geography": geography or "",
                "em_appetite": "unknown",
                "ai_appetite": "unknown",
                "allocator_type": "",
            }
            allocator_id = None
            input_name = ""
            investment_summary = {}

        target = _build(_StubBrief(), nfx_context=None, web_context=None)

    try:
        # Wider candidate pool: any allocator with at least one confirmed fund LP deal.
        # Join funds to collect geography_focus for thesis-overlap scoring.
        rows = con.execute(
            """
            SELECT
                a.canonical_name,
                a.geography,
                a.em_appetite,
                a.ai_appetite,
                a.allocator_type,
                CAST(a.allocator_id AS VARCHAR)                     AS allocator_id,
                COUNT(CASE WHEN lower(i.notes) IN ('venture fund', 'fund')
                           THEN 1 END)                              AS fund_deals,
                COALESCE(SUM(CASE WHEN lower(i.notes) IN ('venture fund', 'fund')
                                  THEN i.commitment_usd END), 0)    AS fund_usd,
                STRING_AGG(DISTINCT f.geography_focus, ',')         AS fund_focus_geos
            FROM allocators a
            JOIN investments i ON i.lp_id = a.allocator_id
            LEFT JOIN funds f   ON f.fund_id = i.fund_id
            GROUP BY a.canonical_name, a.geography, a.em_appetite,
                     a.ai_appetite, a.allocator_type, a.allocator_id
            HAVING COUNT(CASE WHEN lower(i.notes) IN ('venture fund', 'fund') THEN 1 END) > 0
            """,
        ).fetchall()
    except Exception:
        return []

    if not rows:
        return []

    scored: List[tuple] = []
    for row in rows:
        name, geo, em_ap, ai_ap, alloc_type, alloc_id, fund_deals, fund_usd, raw_focus = row

        # Skip the screened LP itself
        if target.exclude_id and alloc_id and str(alloc_id) == str(target.exclude_id):
            continue
        if target.exclude_name and name and name.lower() == target.exclude_name.lower():
            continue

        # Parse fund geography_focus into a set
        focus_geos = set()
        if raw_focus:
            from contra.intelligence.lp_similarity import _canonicalize_geo
            for fg in raw_focus.split(","):
                ng = _canonicalize_geo(fg.strip())
                if ng != "unknown":
                    focus_geos.add(ng)

        candidate: Dict[str, Any] = {
            "name": name,
            "geography": geo or "unknown",
            "em_appetite": em_ap or "unknown",
            "ai_appetite": ai_ap or "unknown",
            "allocator_type": alloc_type or "unknown",
            "fund_deal_count": int(fund_deals),
            "total_fund_usd": float(fund_usd),
            "fund_focus_geos": focus_geos,
        }

        result = score_lp_similarity(target, candidate)
        if result.score >= MIN_DISPLAY_SCORE:
            scored.append((result.score, int(fund_deals), candidate, result))

    # Sort: highest similarity first, ties broken by fund deal count
    scored.sort(key=lambda x: (-x[0], -x[1]))

    output: List[Dict[str, Any]] = []
    for sim_score, _fd, candidate, result in scored[:limit]:
        output.append({
            "name": candidate["name"],
            "geography": candidate["geography"],
            "em_appetite": candidate["em_appetite"],
            "ai_appetite": candidate["ai_appetite"],
            "allocator_type": candidate["allocator_type"],
            "fund_deal_count": candidate["fund_deal_count"],
            "total_fund_usd": candidate["total_fund_usd"],
            "similarity_score": result.score,
            "match_dimensions": result.match_dimensions,
            "archetype": result.archetype,
        })

    return output


def lookup(con, name: str) -> IntelligenceBrief:
    return build(con, name)

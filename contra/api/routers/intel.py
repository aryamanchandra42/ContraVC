"""GET /api/summary, /api/syndicate, /api/paths, /api/contacts/{name}."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Query

from api.deps import get_db

router = APIRouter()


def _table_exists(con, name: str) -> bool:
    try:
        con.execute(f"SELECT 1 FROM {name} LIMIT 0")
        return True
    except Exception:
        return False


@router.get("/summary", response_model=Dict[str, Any])
def summary(con=Depends(get_db)) -> Dict[str, Any]:
    def q(sql: str):
        return con.execute(sql).fetchone()[0]

    li_contacts = (
        q("SELECT COUNT(*) FROM allocator_contacts WHERE source = 'linkedin_export'")
        if _table_exists(con, "allocator_contacts")
        else 0
    )
    avg_warm = con.execute(
        "SELECT AVG(warm_path_count) FROM v_lp_profile WHERE population = 'institutional_prospect'"
    ).fetchone()[0] or 0.0

    return {
        "tier_1_not_in_crm": q("SELECT COUNT(*) FROM v_lp_profile WHERE icp_tier = 'tier_1' AND NOT in_crm"),
        "syndicate_fund_lps_not_in_crm": q("SELECT COUNT(*) FROM v_syndicate_profile WHERE is_fund_lp AND NOT in_crm"),
        "syndicate_upgrade_candidates": q("SELECT COUNT(*) FROM v_syndicate_profile WHERE is_upgrade_candidate AND NOT in_crm"),
        "allocators_unknown_type": q("SELECT COUNT(*) FROM allocators WHERE allocator_type IN ('unknown', '') OR allocator_type IS NULL"),
        "allocators_null_geography": q("SELECT COUNT(*) FROM allocators WHERE geography IS NULL OR geography = ''"),
        "linkedin_contacts_ingested": li_contacts,
        "institutional_with_warm_paths": q("SELECT COUNT(*) FROM v_lp_profile WHERE population = 'institutional_prospect' AND warm_path_count > 0"),
        "avg_warm_path_count_institutional": round(float(avg_warm), 2),
    }


@router.get("/syndicate", response_model=List[Dict[str, Any]])
def syndicate(
    top: int = Query(50, ge=1, le=500),
    min_fund_deals: int = Query(1, ge=0),
    not_in_crm: bool = Query(False),
    con=Depends(get_db),
) -> List[Dict[str, Any]]:
    crm_filter = "AND NOT in_crm" if not_in_crm else ""
    rows = con.execute(
        f"""
        SELECT
            canonical_name, fund_deal_count, spv_deal_count, total_committed_usd,
            fund_lp_ratio, is_fund_lp, is_upgrade_candidate, last_investment_date,
            in_crm, fund_lp_behavior_score, syndicate_depth_score, geography
        FROM v_syndicate_profile
        WHERE fund_deal_count >= ? {crm_filter}
        ORDER BY fund_lp_behavior_score DESC NULLS LAST, fund_deal_count DESC
        LIMIT ?
        """,
        [min_fund_deals, top],
    ).fetchdf()
    return rows.to_dict(orient="records")


@router.get("/paths", response_model=List[Dict[str, Any]])
def paths(
    name: Optional[str] = Query(None),
    top_bridges: int = Query(20, ge=1, le=200),
    prospect_only: bool = Query(False),
    con=Depends(get_db),
) -> List[Dict[str, Any]]:
    if name:
        rows = con.execute(
            """
            SELECT prospect_name, bridge_name, bridge_type, bridge_strength
            FROM v_warm_paths
            WHERE lower(prospect_name) LIKE lower(?)
            ORDER BY bridge_strength DESC NULLS LAST LIMIT 20
            """,
            [f"%{name}%"],
        ).fetchdf()
    elif prospect_only:
        rows = con.execute(
            """
            SELECT prospect_name, COUNT(*) AS path_count, MAX(bridge_strength) AS best_strength
            FROM v_warm_paths
            GROUP BY prospect_name
            ORDER BY path_count DESC, best_strength DESC
            LIMIT ?
            """,
            [top_bridges],
        ).fetchdf()
    else:
        rows = con.execute(
            """
            SELECT bridge_name, bridge_type, COUNT(*) AS connects_to, AVG(bridge_strength) AS avg_strength
            FROM v_warm_paths
            GROUP BY bridge_name, bridge_type
            ORDER BY connects_to DESC, avg_strength DESC
            LIMIT ?
            """,
            [top_bridges],
        ).fetchdf()
    return rows.to_dict(orient="records")


@router.get("/contacts/{name}", response_model=Dict[str, Any])
def contacts(name: str, con=Depends(get_db)) -> Dict[str, Any]:
    from contra.intelligence.resolver import norm_key, resolve

    match = resolve(con, name)
    if not match.allocator_id:
        return {"match": None, "allocator_id": None, "contacts": [], "crm": []}

    li = con.execute(
        """
        SELECT full_name, title, company, email, linkedin_url, location, source, match_confidence
        FROM allocator_contacts
        WHERE allocator_id = ?
        ORDER BY match_confidence DESC NULLS LAST
        """,
        [match.allocator_id],
    ).fetchdf()

    crm = con.execute(
        """
        SELECT investor_name, investor_type, investor_location, crm_status
        FROM crm_contacts WHERE name_key = ? LIMIT 3
        """,
        [norm_key(name)],
    ).fetchdf()

    return {
        "match": match.matched_name,
        "allocator_id": match.allocator_id,
        "match_confidence": match.confidence,
        "contacts": li.to_dict(orient="records"),
        "crm": crm.to_dict(orient="records"),
    }

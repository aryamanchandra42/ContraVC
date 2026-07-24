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
    def q(sql: str, default: int = 0):
        try:
            return con.execute(sql).fetchone()[0] or default
        except Exception:
            return default

    def _count_status(table: str, status: str) -> int:
        if not _table_exists(con, table):
            return 0
        return q(f"SELECT COUNT(*) FROM {table} WHERE status = '{status}'")

    avg_warm = 0.0
    try:
        avg_warm = con.execute(
            "SELECT AVG(warm_path_count) FROM v_lp_profile WHERE population = 'institutional_prospect'"
        ).fetchone()[0] or 0.0
    except Exception:
        pass

    # Funnel: mined → qualified → in CRM → contacted
    candidates_total = q("SELECT COUNT(*) FROM prospector_candidates") if _table_exists(con, "prospector_candidates") else 0
    candidates_review = _count_status("prospector_candidates", "review")
    candidates_qualified = (
        q("SELECT COUNT(*) FROM prospector_candidates WHERE status IN ('qualified', 'promoted')")
        if _table_exists(con, "prospector_candidates") else 0
    )
    scorecard_qualified = (
        q("SELECT COUNT(*) FROM lead_scorecards WHERE verdict = 'qualified'")
        if _table_exists(con, "lead_scorecards") else 0
    )
    crm_active = q("SELECT COUNT(*) FROM crm_leads WHERE status = 'active'") if _table_exists(con, "crm_leads") else 0
    crm_contacted = (
        q("SELECT COUNT(*) FROM crm_leads WHERE status IN ('contacted', 'replied', 'meeting')")
        if _table_exists(con, "crm_leads") else 0
    )
    last_run = None
    if _table_exists(con, "prospector_runs"):
        row = con.execute(
            """
            SELECT run_id, status, promoted, new_candidates, qualified,
                   CAST(started_at AS VARCHAR), CAST(completed_at AS VARCHAR)
            FROM prospector_runs ORDER BY started_at DESC LIMIT 1
            """
        ).fetchone()
        if row:
            last_run = {
                "run_id": row[0], "status": row[1], "promoted": row[2],
                "new_candidates": row[3], "qualified": row[4],
                "started_at": row[5], "completed_at": row[6],
            }

    return {
        "tier_1_not_in_crm": q("SELECT COUNT(*) FROM v_lp_profile WHERE icp_tier = 'tier_1' AND NOT in_crm"),
        "syndicate_fund_lps_not_in_crm": q("SELECT COUNT(*) FROM v_syndicate_profile WHERE is_fund_lp AND NOT in_crm"),
        "syndicate_upgrade_candidates": q("SELECT COUNT(*) FROM v_syndicate_profile WHERE is_upgrade_candidate AND NOT in_crm"),
        "allocators_unknown_type": q("SELECT COUNT(*) FROM allocators WHERE allocator_type IN ('unknown', '') OR allocator_type IS NULL"),
        "allocators_null_geography": q("SELECT COUNT(*) FROM allocators WHERE geography IS NULL OR geography = ''"),
        "linkedin_contacts_ingested": (
            q("SELECT COUNT(*) FROM allocator_contacts WHERE source = 'linkedin_export'")
            if _table_exists(con, "allocator_contacts") else 0
        ),
        "institutional_with_warm_paths": q(
            "SELECT COUNT(*) FROM v_lp_profile WHERE population = 'institutional_prospect' AND warm_path_count > 0"
        ),
        "avg_warm_path_count_institutional": round(float(avg_warm), 2),
        # Mining funnel
        "funnel": {
            "mined": candidates_total,
            "review_queue": candidates_review,
            "qualified": candidates_qualified,
            "scorecard_qualified": scorecard_qualified,
            "crm_active": crm_active,
            "contacted": crm_contacted,
        },
        "last_prospector_run": last_run,
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
    from contra.intelligence.contact_resolver import resolve_contacts

    profile = resolve_contacts(con, name)

    result = profile.to_api_dict()

    # Back-compat: include legacy "match" / "crm" fields
    result["match"] = profile.investor_name if profile.allocator_id else None
    result["match_confidence"] = profile.confidence

    try:
        from contra.intelligence.resolver import norm_key
        crm = con.execute(
            """
            SELECT investor_name, investor_type, investor_location, crm_status
            FROM crm_contacts WHERE name_key = ? LIMIT 3
            """,
            [norm_key(name)],
        ).fetchdf()
        result["crm"] = crm.to_dict(orient="records")
    except Exception:
        result["crm"] = []

    return result


@router.post("/contacts/{name}/hunt", response_model=Dict[str, Any])
def hunt_contacts(name: str, con=Depends(get_db)) -> Dict[str, Any]:
    """Manually trigger the Contact Hunter for a specific LP."""
    from contra.intelligence.contact_resolver import resolve_contacts
    
    # We need the allocator_id to persist contacts.
    profile = resolve_contacts(con, name)
    if not profile.allocator_id:
        return {"error": "Allocator ID not found for this name. Run them through the Gate first."}
        
    from agents.research.contact_hunter import hunt_and_persist_contacts
    stats = hunt_and_persist_contacts(con, lp_name=name, allocator_id=profile.allocator_id)
    
    # Return updated contacts
    updated_profile = resolve_contacts(con, name)
    return {
        "stats": stats,
        "profile": updated_profile.to_api_dict()
    }

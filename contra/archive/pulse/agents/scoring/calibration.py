"""
Benchmark calibration — join icp_scores ↔ benchmark_rankings (contravc_top200).

Exports overlay CSVs, summary JSON, auto-tunes tier thresholds via grid search,
writes prompts/icp_calibration.yaml, and exports enriched LP ranked list.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
import yaml
from rapidfuzz import fuzz, process

from agents.scoring.icp_spec import (
    ICP_VERSION,
    DEFAULT_TIER_1_FIT_MIN,
    DEFAULT_TIER_2_FIT_MIN,
    get_tier_thresholds,
)
from pulse.safe_io import safe_write_csv

ROOT = Path(__file__).resolve().parent.parent.parent
PROCESSED = ROOT / "processed_data"
CALIBRATION_YAML = ROOT / "prompts" / "icp_calibration.yaml"
RANKING_SOURCE = "contravc_top200"


def _load_config() -> Dict[str, Any]:
    if CALIBRATION_YAML.exists():
        with open(CALIBRATION_YAML, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    return {}


def _save_config(config: Dict[str, Any]) -> None:
    CALIBRATION_YAML.parent.mkdir(parents=True, exist_ok=True)
    with open(CALIBRATION_YAML, "w", encoding="utf-8") as f:
        yaml.dump(config, f, default_flow_style=False, sort_keys=False)


def _kendall_tau(ranks_a: List[float], ranks_b: List[float]) -> float:
    n = len(ranks_a)
    if n < 2:
        return 0.0
    concordant = discordant = 0
    for i in range(n):
        for j in range(i + 1, n):
            diff_a = ranks_a[i] - ranks_a[j]
            diff_b = ranks_b[i] - ranks_b[j]
            if diff_a * diff_b > 0:
                concordant += 1
            elif diff_a * diff_b < 0:
                discordant += 1
    denom = n * (n - 1) / 2
    return (concordant - discordant) / denom if denom else 0.0


def _spearman(ranks_a: List[float], ranks_b: List[float]) -> float:
    n = len(ranks_a)
    if n < 2:
        return 0.0
    mean_a = sum(ranks_a) / n
    mean_b = sum(ranks_b) / n
    num = sum((a - mean_a) * (b - mean_b) for a, b in zip(ranks_a, ranks_b))
    den_a = sum((a - mean_a) ** 2 for a in ranks_a) ** 0.5
    den_b = sum((b - mean_b) ** 2 for b in ranks_b) ** 0.5
    if den_a == 0 or den_b == 0:
        return 0.0
    return num / (den_a * den_b)


def _assign_tier(
    core_pass: bool,
    excluded: bool,
    fit_score: float,
    client_decision: str,
    tier_1_min: float,
    tier_2_min: float,
    require_approved: bool,
) -> str:
    if excluded or not core_pass:
        return "tier_4"
    if fit_score >= tier_1_min:
        if require_approved and client_decision == "approved":
            return "tier_1"
        if require_approved:
            return "tier_2"
        return "tier_1" if client_decision == "approved" else "tier_2"
    if fit_score >= tier_2_min:
        return "tier_2"
    return "tier_3"


def _calibration_bucket(pulse_tier: str, contra_rank: Optional[int]) -> str:
    if pulse_tier == "tier_1" and contra_rank is not None and contra_rank <= 100:
        return "both_high"
    if pulse_tier == "tier_1" and (contra_rank is None or contra_rank > 100):
        return "pulse_only"
    if contra_rank is not None and contra_rank <= 50 and pulse_tier in ("tier_3", "tier_4"):
        return "contra_only"
    if contra_rank is not None:
        return "disagree"
    return "pulse_only" if pulse_tier == "tier_1" else "no_benchmark"


def _fuzzy_benchmark_lookup(
    con, threshold: int = 85
) -> Dict[str, Dict[str, Any]]:
    """Map allocator_id -> best ContraVC benchmark row via fuzzy name match."""
    prospects = con.execute(
        """
        SELECT CAST(a.allocator_id AS VARCHAR), a.canonical_name
        FROM icp_scores i
        JOIN allocators a ON CAST(a.allocator_id AS VARCHAR) = CAST(i.allocator_id AS VARCHAR)
        WHERE i.icp_version = ?
          AND a.population = 'institutional_prospect'
        """,
        [ICP_VERSION],
    ).fetchall()
    bench = con.execute(
        """
        SELECT external_name, rank, priority_score, tier, prior_fund_lp
        FROM benchmark_rankings
        WHERE ranking_source = ?
        """,
        [RANKING_SOURCE],
    ).fetchall()
    if not prospects or not bench:
        return {}

    choices = {row[0]: row for row in bench if row[0]}
    out: Dict[str, Dict[str, Any]] = {}

    for aid, pname in prospects:
        if not pname:
            continue
        hit = process.extractOne(
            pname,
            choices,
            scorer=fuzz.token_sort_ratio,
            score_cutoff=threshold,
        )
        if not hit:
            continue
        matched_name = hit[0]
        row = choices[matched_name]
        out[aid] = {
            "contra_rank": row[1],
            "contra_priority": row[2],
            "contra_tier": row[3],
            "prior_fund_lp": row[4],
            "fuzzy_match_name": matched_name,
            "fuzzy_score": hit[1],
        }
    return out


def _fetch_overlay_df(con) -> pd.DataFrame:
    rows = con.execute(
        """
        SELECT
            CAST(a.allocator_id AS VARCHAR) AS allocator_id,
            a.canonical_name,
            a.population,
            a.allocator_type,
            a.geography,
            i.tier AS pulse_tier,
            i.fit_score AS pulse_fit,
            i.client_decision,
            i.core_pass,
            i.excluded,
            b.rank AS contra_rank,
            b.priority_score AS contra_priority,
            b.tier AS contra_tier,
            b.prior_fund_lp
        FROM icp_scores i
        JOIN allocators a ON CAST(a.allocator_id AS VARCHAR) = CAST(i.allocator_id AS VARCHAR)
        LEFT JOIN benchmark_rankings b
            ON b.ranking_source = ?
            AND (
                CAST(b.allocator_id AS VARCHAR) = CAST(i.allocator_id AS VARCHAR)
                OR lower(trim(b.external_name)) = lower(trim(a.canonical_name))
            )
        WHERE i.icp_version = ?
        ORDER BY i.fit_score DESC
        """,
        [RANKING_SOURCE, ICP_VERSION],
    ).fetchdf()

    if rows.empty:
        return rows

    fuzzy = _fuzzy_benchmark_lookup(con)
    if fuzzy:
        for idx, row in rows.iterrows():
            if pd.notna(row["contra_rank"]):
                continue
            hit = fuzzy.get(row["allocator_id"])
            if not hit:
                continue
            rows.at[idx, "contra_rank"] = hit["contra_rank"]
            rows.at[idx, "contra_priority"] = hit["contra_priority"]
            rows.at[idx, "contra_tier"] = hit["contra_tier"]
            rows.at[idx, "prior_fund_lp"] = hit["prior_fund_lp"]

    rows["pulse_fit_rank"] = rows["pulse_fit"].rank(ascending=False, method="min")
    rows["in_contra_top200"] = rows["contra_rank"].notna()
    rows["rank_delta"] = rows.apply(
        lambda r: r["pulse_fit_rank"] - r["contra_rank"]
        if pd.notna(r["contra_rank"])
        else None,
        axis=1,
    )
    rows["tier_agreement"] = rows.apply(
        lambda r: r["pulse_tier"] == r["contra_tier"]
        if pd.notna(r["contra_tier"])
        else None,
        axis=1,
    )
    rows["calibration_bucket"] = rows.apply(
        lambda r: _calibration_bucket(r["pulse_tier"], r["contra_rank"] if pd.notna(r["contra_rank"]) else None),
        axis=1,
    )
    return rows


def _compute_summary(df: pd.DataFrame) -> Dict[str, Any]:
    overlap = df[df["in_contra_top200"]].copy()
    tier1 = df[df["pulse_tier"] == "tier_1"]

    inst_overlap = int(
        len(df[(df["population"] == "institutional_prospect") & df["in_contra_top200"]])
    )

    summary: Dict[str, Any] = {
        "total_scored": int(len(df)),
        "overlap_count": int(len(overlap)),
        "institutional_overlap_count": inst_overlap,
        "tier1_count": int(len(tier1)),
        "tier1_in_contra_top200": int(
            len(tier1[tier1["contra_rank"].notna()])
        ),
        "tier1_in_contra_top50": int(
            len(tier1[(tier1["contra_rank"].notna()) & (tier1["contra_rank"] <= 50)])
        ),
    }

    if len(overlap) >= 2:
        fit_ranks = overlap["pulse_fit_rank"].tolist()
        contra_ranks = overlap["contra_rank"].tolist()
        summary["spearman_fit_vs_contra"] = round(_spearman(fit_ranks, contra_ranks), 4)
        summary["kendall_tau_fit_vs_contra"] = round(_kendall_tau(fit_ranks, contra_ranks), 4)

    if len(overlap) > 0:
        agreed = overlap["tier_agreement"].dropna()
        summary["tier_agreement_rate"] = round(float(agreed.mean()), 4) if len(agreed) else 0.0

        top100 = overlap[overlap["contra_rank"] <= 100]
        if len(top100) > 0:
            t100_agreed = top100["tier_agreement"].dropna()
            summary["tier_agreement_top100"] = round(float(t100_agreed.mean()), 4) if len(t100_agreed) else 0.0

        fp = tier1[(tier1["contra_rank"].isna()) | (tier1["contra_rank"] > 100)]
        summary["false_positive_rate_tier1"] = round(len(fp) / max(len(tier1), 1), 4)

        fn = overlap[(overlap["contra_rank"] <= 50) & (overlap["pulse_tier"].isin(["tier_3", "tier_4"]))]
        contra50 = overlap[overlap["contra_rank"] <= 50]
        summary["false_negative_rate_contra50"] = round(
            len(fn) / max(len(contra50), 1), 4
        )

    inst = df[df["population"] == "institutional_prospect"]
    inst_overlap = inst[inst["in_contra_top200"]]
    summary["institutional_overlap_count"] = int(len(inst_overlap))
    if len(inst_overlap) >= 2:
        summary["institutional_kendall_tau"] = round(
            _kendall_tau(
                inst_overlap["pulse_fit_rank"].tolist(),
                inst_overlap["contra_rank"].tolist(),
            ),
            4,
        )

    return summary


def _grid_search(
    df: pd.DataFrame,
    config: Dict[str, Any],
) -> Tuple[Dict[str, float], Dict[str, Any]]:
    search = config.get("search", {})
    weights = config.get("objective_weights", {})
    require_approved = search.get("require_client_approved_for_tier_1", True)

    t1_cfg = search.get("tier_1_fit_min", {})
    t2_cfg = search.get("tier_2_fit_min", {})

    def _frange(lo: float, hi: float, step: float) -> List[float]:
        vals: List[float] = []
        v = lo
        while v <= hi + 1e-9:
            vals.append(round(v, 3))
            v += step
        return vals

    t1_vals = _frange(
        t1_cfg.get("min", 0.50),
        t1_cfg.get("max", 0.75),
        t1_cfg.get("step", 0.025),
    )
    t2_vals = _frange(
        t2_cfg.get("min", 0.25),
        t2_cfg.get("max", 0.55),
        t2_cfg.get("step", 0.025),
    )

    overlap = df[df["in_contra_top200"]].copy()
    if len(overlap) < config.get("min_overlap_for_autotune", 10):
        return (
            {
                "TIER_1_FIT_MIN": get_tier_thresholds()[0],
                "TIER_2_FIT_MIN": get_tier_thresholds()[1],
            },
            {"skipped": True, "reason": "overlap < min_overlap_for_autotune"},
        )

    best_score = -1.0
    best_thresholds = {
        "TIER_1_FIT_MIN": DEFAULT_TIER_1_FIT_MIN,
        "TIER_2_FIT_MIN": DEFAULT_TIER_2_FIT_MIN,
    }
    best_metrics: Dict[str, Any] = {}

    contra50 = overlap[overlap["contra_rank"] <= 50]
    top100 = overlap[overlap["contra_rank"] <= 100]

    for t1 in t1_vals:
        for t2 in t2_vals:
            if t2 >= t1:
                continue

            tiers = overlap.apply(
                lambda r: _assign_tier(
                    bool(r["core_pass"]),
                    bool(r["excluded"]),
                    float(r["pulse_fit"]),
                    str(r["client_decision"] or "pending"),
                    t1,
                    t2,
                    require_approved,
                ),
                axis=1,
            )

            if len(contra50) > 0:
                recall50 = float(
                    tiers[contra50.index].isin(["tier_1", "tier_2"]).mean()
                )
            else:
                recall50 = 0.0

            if len(top100) > 0:
                contra_tiers = top100["contra_tier"].fillna("")
                sim_tiers = tiers[top100.index]
                agreement = float((sim_tiers == contra_tiers).mean())
            else:
                agreement = 0.0

            tau = _kendall_tau(
                overlap["pulse_fit_rank"].tolist(),
                overlap["contra_rank"].tolist(),
            )
            if tau < 0:
                tau_norm = (tau + 1) / 2
            else:
                tau_norm = tau

            objective = (
                weights.get("recall_at_50", 0.5) * recall50
                + weights.get("tier_agreement_top100", 0.3) * agreement
                + weights.get("kendall_tau", 0.2) * tau_norm
            )

            if objective > best_score:
                best_score = objective
                best_thresholds = {"TIER_1_FIT_MIN": t1, "TIER_2_FIT_MIN": t2}
                best_metrics = {
                    "objective": round(objective, 4),
                    "recall_at_50": round(recall50, 4),
                    "tier_agreement_top100": round(agreement, 4),
                    "kendall_tau": round(tau, 4),
                }

    return best_thresholds, best_metrics


def export_lp_ranked_list(con, out_path: Optional[Path] = None) -> Path:
    """Export ranked LP list with connectivity enrichment columns."""
    out_path = out_path or PROCESSED / "LP_Ranked_List.csv"
    PROCESSED.mkdir(parents=True, exist_ok=True)

    conn_path = PROCESSED / "Prospect_Syndicate_Connectivity.csv"
    conn_df = pd.DataFrame()
    if conn_path.exists():
        conn_df = pd.read_csv(conn_path)

    rows = con.execute(
        """
        SELECT
            CAST(i.allocator_id AS VARCHAR) AS allocator_id,
            a.canonical_name,
            a.allocator_type,
            a.geography,
            i.tier,
            i.fit_score,
            i.client_status,
            i.client_decision,
            i.stated_reason,
            i.data_miner_comment,
            s_em.normalized_value AS em_participation,
            s_geo.normalized_value AS geo_overlap,
            s_dep.normalized_value AS deploy_velocity,
            s_exp.normalized_value AS profile_quality,
            s_rsp.normalized_value AS response_speed,
            s_op.normalized_value AS operator_background,
            s_nd.normalized_value AS network_density,
            s_sp.normalized_value AS social_proximity
        FROM icp_scores i
        JOIN allocators a ON CAST(a.allocator_id AS VARCHAR) = CAST(i.allocator_id AS VARCHAR)
        LEFT JOIN signals s_em
            ON CAST(s_em.allocator_id AS VARCHAR) = CAST(i.allocator_id AS VARCHAR)
            AND s_em.signal_type = 'em_participation'
        LEFT JOIN signals s_geo
            ON CAST(s_geo.allocator_id AS VARCHAR) = CAST(i.allocator_id AS VARCHAR)
            AND s_geo.signal_type = 'geography_overlap'
        LEFT JOIN signals s_dep
            ON CAST(s_dep.allocator_id AS VARCHAR) = CAST(i.allocator_id AS VARCHAR)
            AND s_dep.signal_type = 'deployment_velocity'
        LEFT JOIN signals s_exp
            ON CAST(s_exp.allocator_id AS VARCHAR) = CAST(i.allocator_id AS VARCHAR)
            AND s_exp.signal_type = 'exploratory_check'
        LEFT JOIN signals s_rsp
            ON CAST(s_rsp.allocator_id AS VARCHAR) = CAST(i.allocator_id AS VARCHAR)
            AND s_rsp.signal_type = 'response_speed'
        LEFT JOIN signals s_op
            ON CAST(s_op.allocator_id AS VARCHAR) = CAST(i.allocator_id AS VARCHAR)
            AND s_op.signal_type = 'operator_background'
        LEFT JOIN signals s_nd
            ON CAST(s_nd.allocator_id AS VARCHAR) = CAST(i.allocator_id AS VARCHAR)
            AND s_nd.signal_type = 'network_density'
        LEFT JOIN signals s_sp
            ON CAST(s_sp.allocator_id AS VARCHAR) = CAST(i.allocator_id AS VARCHAR)
            AND s_sp.signal_type = 'social_proximity'
        WHERE i.icp_version = ?
        ORDER BY i.fit_score DESC
        """,
        [ICP_VERSION],
    ).fetchdf()

    if not conn_df.empty and not rows.empty:
        rows = rows.merge(
            conn_df[[
                "allocator_id", "connectivity_score", "direct_syndicate_degree",
                "two_hop_syndicate_reach", "top_bridge_name",
            ]],
            on="allocator_id",
            how="left",
        )
    elif not rows.empty:
        rows["connectivity_score"] = None
        rows["direct_syndicate_degree"] = None
        rows["two_hop_syndicate_reach"] = None
        rows["top_bridge_name"] = None

    if rows.empty:
        safe_write_csv(
            pd.DataFrame(columns=["Rank", "LP Name", "Type", "Geography", "Tier", "Fit Score"]),
            out_path,
        )
        return out_path

    sig_cols = [
        "em_participation", "geo_overlap", "deploy_velocity",
        "profile_quality", "response_speed", "operator_background",
        "network_density", "social_proximity",
    ]
    for col in sig_cols:
        rows[col] = pd.to_numeric(rows[col], errors="coerce").fillna(0.0)
    rows["composite_signal"] = rows[sig_cols[:6]].mean(axis=1).round(3)

    export = pd.DataFrame({
        "Rank": range(1, len(rows) + 1),
        "LP Name": rows["canonical_name"],
        "Type": rows["allocator_type"],
        "Geography": rows["geography"],
        "Tier": rows["tier"],
        "Fit Score": rows["fit_score"].round(3),
        "Composite Signal": rows["composite_signal"],
        "EM Participation": rows["em_participation"],
        "Geo Overlap": rows["geo_overlap"],
        "Deploy Velocity": rows["deploy_velocity"],
        "Profile Quality": rows["profile_quality"],
        "Response Speed": rows["response_speed"],
        "Operator Background": rows["operator_background"],
        "Network Density": rows["network_density"],
        "Social Proximity": rows["social_proximity"],
        "Connectivity Score": rows.get("connectivity_score"),
        "Direct Syndicate Degree": rows.get("direct_syndicate_degree"),
        "Two Hop Reach": rows.get("two_hop_syndicate_reach"),
        "Top Bridge Name": rows.get("top_bridge_name"),
        "Client Status": rows["client_status"],
        "Decision": rows["client_decision"],
        "Stated Reason": rows["stated_reason"],
        "Miner Comment": rows["data_miner_comment"],
    })
    safe_write_csv(export, out_path)
    return out_path


def run_calibration(con) -> Dict[str, Any]:
    """
    Build calibration overlay, auto-tune tier thresholds, export reports.
    Returns summary dict for CLI logging.
    """
    PROCESSED.mkdir(parents=True, exist_ok=True)

    config = _load_config()
    before_t1, before_t2 = get_tier_thresholds()

    df = _fetch_overlay_df(con)
    summary = _compute_summary(df)

    overlay_path = PROCESSED / "calibration_overlay.csv"
    safe_write_csv(df, overlay_path)

    tier1_path = PROCESSED / "calibration_tier1_vs_contra.csv"
    safe_write_csv(df[df["pulse_tier"] == "tier_1"], tier1_path)

    best_thresholds, tune_metrics = _grid_search(df, config)

    if not tune_metrics.get("skipped"):
        config["winning_thresholds"] = best_thresholds
        config["last_run"] = {
            "at": datetime.now(timezone.utc).isoformat(),
            "before": {"TIER_1_FIT_MIN": before_t1, "TIER_2_FIT_MIN": before_t2},
            "after": best_thresholds,
            "metrics": tune_metrics,
            "summary": summary,
        }
        _save_config(config)
        summary["auto_tune"] = tune_metrics
        summary["thresholds_updated"] = best_thresholds
    else:
        summary["auto_tune"] = tune_metrics
        summary["thresholds_updated"] = None

    summary_path = PROCESSED / "calibration_summary.json"
    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)

    ranked_path = export_lp_ranked_list(con)

    return {
        **summary,
        "overlay_path": str(overlay_path),
        "tier1_path": str(tier1_path),
        "summary_path": str(summary_path),
        "ranked_list_path": str(ranked_path),
    }

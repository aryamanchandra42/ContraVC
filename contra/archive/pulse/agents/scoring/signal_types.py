"""Canonical signal type catalogue — single source for schema + validation."""

from __future__ import annotations

# Original 8 + latent / connectivity expansion (Phase 4b)
VALID_SIGNAL_TYPES = frozenset({
    "response_speed",
    "exploratory_check",
    "operator_background",
    "em_participation",
    "geography_overlap",
    "social_proximity",
    "network_density",
    "deployment_velocity",
    "bridge_strength",
    "warm_path_count",
    "coinvest_intensity",
    "recent_activity_recency",
    "stage_alignment",
    "proxy_fund_overlap",
    "clean_profile",
    "shared_deal_count",
})

VALID_SIGNAL_EVIDENCE_TYPES = frozenset({
    "signal_heuristic",
    "signal_investment_pattern",
    "signal_graph_metric",
    "signal_icp_mirror",
    "signal_connectivity",
    "contradicts_value",
})

"""
Edge type catalogue and evidence aggregation rules.

This is the single source of truth for relationship edge types in PULSE.
Adding a new edge type requires: updating EDGE_CATALOGUE, updating the CHECK constraint
in schema/duckdb.sql and schema/postgres.sql, and documenting in docs/decision_archive.md.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List


@dataclass
class EdgeTypeSpec:
    edge_type: str
    description: str
    typical_sources: List[str]      # what evidence sources typically produce this edge type
    weight_default: float           # default weight when no evidence-based weight is available
    min_evidence_for_auto: int      # min evidence rows before auto-accepting (below = queue for review)


EDGE_CATALOGUE: Dict[str, EdgeTypeSpec] = {
    "invested_with": EdgeTypeSpec(
        edge_type="invested_with",
        description="LP A and LP B invested in the same fund (same vehicle, same GP)",
        typical_sources=["investments", "xlsx_co_invest_columns", "fund_rating_guide"],
        weight_default=1.0,
        min_evidence_for_auto=1,
    ),
    "introduced_by": EdgeTypeSpec(
        edge_type="introduced_by",
        description="Entity A was introduced to Entity B by Entity C (A→C means C introduced A to B)",
        typical_sources=["interactions", "notes", "heuristic_keyword_match"],
        weight_default=1.0,
        min_evidence_for_auto=1,
    ),
    "co_invested": EdgeTypeSpec(
        edge_type="co_invested",
        description="Two LPs co-invested directly (not merely in the same fund)",
        typical_sources=["investments", "xlsx_co_invest", "heuristic_keyword_match"],
        weight_default=1.0,
        min_evidence_for_auto=2,
    ),
    "syndicate_overlap": EdgeTypeSpec(
        edge_type="syndicate_overlap",
        description="Two entities appear in overlapping syndicate structures across multiple deals",
        typical_sources=["investments", "xlsx_syndicate_columns"],
        weight_default=1.0,
        min_evidence_for_auto=2,
    ),
    "mutual_connection": EdgeTypeSpec(
        edge_type="mutual_connection",
        description="Two entities share a common connection node (inferred from existing edges)",
        typical_sources=["graph_inference", "normalization"],
        weight_default=0.5,
        min_evidence_for_auto=1,
    ),
    "repeated_exposure": EdgeTypeSpec(
        edge_type="repeated_exposure",
        description="Two entities have had repeated contact or exposure across multiple touchpoints",
        typical_sources=["interactions", "heuristic_keyword_match"],
        weight_default=1.0,
        min_evidence_for_auto=2,
    ),
    "co_mentioned": EdgeTypeSpec(
        edge_type="co_mentioned",
        description="Two known entities co-occur in the same sentence or text window; weak-signal evidence of a network connection",
        typical_sources=["heuristic_co_occurrence", "pdf_text", "docx_text", "xlsx_notes"],
        weight_default=0.4,
        min_evidence_for_auto=3,
    ),
    "cross_file_corroboration": EdgeTypeSpec(
        edge_type="cross_file_corroboration",
        description="Self-referential edge confirming the same entity is observed in multiple source files; anchors cross-file match evidence",
        typical_sources=["entity_resolver", "normalization"],
        weight_default=1.0,
        min_evidence_for_auto=1,
    ),
}


def aggregate_weight(evidence_rows: List[Dict]) -> float:
    """
    Aggregate edge weight from evidence rows.
    Default: weight = evidence_count (each evidence row contributes 1).
    Can be overridden by a human_reviews row with decision='revise'.
    """
    return float(len(evidence_rows))


def get_spec(edge_type: str) -> EdgeTypeSpec:
    if edge_type not in EDGE_CATALOGUE:
        raise ValueError(
            f"Unknown edge type '{edge_type}'. "
            f"Valid types: {list(EDGE_CATALOGUE.keys())}"
        )
    return EDGE_CATALOGUE[edge_type]

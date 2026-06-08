"""
PULSE Pydantic v2 models — runtime validation surface for every entity.

All models mirror schema/duckdb.sql and schema/postgres.sql exactly.
Do not add columns here without adding them to both SQL files.

Uncertainty columns (confidence, evidence_count, etc.) are populated only
by pulse derive — never by ingestion adapters or hand-written code.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from typing import Annotated, Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field


# ---------------------------------------------------------------------------
# Shared primitives
# ---------------------------------------------------------------------------

Probability = Annotated[float, Field(ge=0.0, le=1.0)]
ProvenancePointer = Dict[str, Any]  # {source_file, source_offset, row_id}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Provenance substrate
# ---------------------------------------------------------------------------

class EntityRaw(BaseModel):
    """Every ingested row before normalization. Append-only, never modified."""

    model_config = ConfigDict(extra="forbid")

    source_record_id: str           # deterministic SHA-256 hash of (source_file + offset + content_hash)
    source_file: str                # path relative to raw_data/
    source_type: str                # xlsx | pdf | docx | api | csv
    source_offset: str              # sheet:row | page:N:char:N | para:N
    content_hash: str               # SHA-256 of raw row/chunk bytes
    raw_content: Dict[str, Any]     # JSON-serialized raw row
    ingested_at: datetime = Field(default_factory=utcnow)
    schema_version: str = "1.0"


# ---------------------------------------------------------------------------
# Canonical entities
# ---------------------------------------------------------------------------

class Allocator(BaseModel):
    """Institutional allocator (LP). Uncertainty + scoring columns null in Phase 1-4."""

    model_config = ConfigDict(extra="forbid")

    allocator_id: uuid.UUID = Field(default_factory=uuid.uuid4)
    canonical_name: str
    aliases: List[str] = Field(default_factory=list)
    allocator_type: Optional[str] = None     # see taxonomies.AllocatorType
    geography: Optional[str] = None          # see taxonomies.Geography
    hq_country: Optional[str] = None
    stage_preference: Optional[str] = None  # see taxonomies.StagePreference
    check_size_min_usd: Optional[float] = None
    check_size_max_usd: Optional[float] = None
    check_size_bucket: Optional[str] = None  # see taxonomies.CheckSizeBucket
    em_appetite: Optional[str] = None        # see taxonomies.Appetite
    ai_appetite: Optional[str] = None        # see taxonomies.Appetite
    relationship_density: Optional[float] = None
    institutional_flexibility: Optional[str] = None  # see taxonomies.Flexibility
    # Population universe: 'institutional_prospect' | 'syndicate_lp' | 'benchmark_target'
    population: Optional[str] = None

    # Scoring — reserved for Phase 5-6; null in Phase 1-4
    inferred_scores: Optional[Dict[str, Any]] = None
    confidences: Optional[Dict[str, Any]] = None

    # Provenance
    source_record_id: str
    source_file: str
    ingested_at: datetime = Field(default_factory=utcnow)
    content_hash: str
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class Fund(BaseModel):
    """GP fund / vehicle."""

    model_config = ConfigDict(extra="forbid")

    fund_id: uuid.UUID = Field(default_factory=uuid.uuid4)
    canonical_name: str
    aliases: List[str] = Field(default_factory=list)
    fund_type: Optional[str] = None
    manager_name: Optional[str] = None
    vintage_year: Optional[int] = None
    geography_focus: Optional[str] = None
    strategy: Optional[str] = None
    target_size_usd: Optional[float] = None
    close_size_usd: Optional[float] = None

    source_record_id: str
    source_file: str
    ingested_at: datetime = Field(default_factory=utcnow)
    content_hash: str


class Interaction(BaseModel):
    """Meeting, call, email, or any touchpoint between PULSE and an allocator."""

    model_config = ConfigDict(extra="forbid")

    interaction_id: uuid.UUID = Field(default_factory=uuid.uuid4)
    allocator_id: uuid.UUID
    interaction_type: str           # meeting | call | email | intro | conference
    occurred_at: Optional[datetime] = None
    notes: Optional[str] = None
    sentiment: Optional[str] = None  # positive | neutral | negative | unknown
    follow_up_required: bool = False
    follow_up_notes: Optional[str] = None
    relationship_strength: Optional[Probability] = None
    progression_stage: Optional[str] = None  # see taxonomies.ProgressionStage

    source_record_id: str
    source_file: str
    ingested_at: datetime = Field(default_factory=utcnow)
    content_hash: str


class Investment(BaseModel):
    """LP→Fund investment record."""

    model_config = ConfigDict(extra="forbid")

    investment_id: uuid.UUID = Field(default_factory=uuid.uuid4)
    lp_id: uuid.UUID                # → allocators.allocator_id
    fund_id: uuid.UUID              # → funds.fund_id
    investment_date: Optional[date] = None
    commitment_usd: Optional[float] = None
    syndicate_overlap: Optional[bool] = None
    co_investment_flag: bool = False
    notes: Optional[str] = None

    source_record_id: str
    source_file: str
    ingested_at: datetime = Field(default_factory=utcnow)
    content_hash: str


class BenchmarkRanking(BaseModel):
    """External, pre-computed LP ranking (e.g. ContraVC Top 200) used to calibrate the ICP scorer."""

    model_config = ConfigDict(extra="forbid")

    benchmark_id: uuid.UUID = Field(default_factory=uuid.uuid4)
    allocator_id: Optional[uuid.UUID] = None   # null until resolved to a PULSE allocator
    external_name: str
    ranking_source: str                        # e.g. 'contravc_top200'
    rank: Optional[int] = None
    priority_score: Optional[float] = None
    tier: Optional[str] = None
    prior_fund_lp: Optional[bool] = None
    spvs_backed: Optional[int] = None
    funds_backed: Optional[int] = None
    median_check_usd: Optional[float] = None
    total_invested_usd: Optional[float] = None
    al_activity_usd: Optional[float] = None
    linkedin_url: Optional[str] = None

    source_record_id: str
    source_file: str
    content_hash: str
    ingested_at: datetime = Field(default_factory=utcnow)


# ---------------------------------------------------------------------------
# Relationship graph
# ---------------------------------------------------------------------------

VALID_EDGE_TYPES = frozenset({
    "invested_with",
    "introduced_by",
    "co_invested",
    "syndicate_overlap",
    "mutual_connection",
    "repeated_exposure",
    "co_mentioned",
    "cross_file_corroboration",
})

VALID_NODE_TYPES = frozenset({
    "lp", "fund", "syndicate", "founder", "advisor", "geography",
})


class Relationship(BaseModel):
    """Graph edge. Uncertainty + temporal columns populated by pulse derive."""

    model_config = ConfigDict(extra="forbid")

    edge_id: uuid.UUID = Field(default_factory=uuid.uuid4)
    source_node_id: str             # entity id (polymorphic)
    source_node_type: str           # lp | fund | syndicate | founder | advisor | geography
    target_node_id: str
    target_node_type: str
    edge_type: str                  # must be in VALID_EDGE_TYPES
    weight: float = 1.0

    # Temporal — populated by pulse derive
    effective_date: Optional[date] = None
    first_seen: Optional[datetime] = None
    last_seen: Optional[datetime] = None
    last_active: Optional[datetime] = None
    relationship_decay_score: Optional[Probability] = None
    temporal_confidence: Optional[Probability] = None

    # Uncertainty — populated by pulse derive
    confidence: Optional[Probability] = None
    evidence_count: int = 0
    contradiction_score: Optional[Probability] = None
    source_agreement_score: Optional[Probability] = None

    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    def model_post_init(self, __context: Any) -> None:
        if self.edge_type not in VALID_EDGE_TYPES:
            raise ValueError(f"Invalid edge_type '{self.edge_type}'. Must be one of {VALID_EDGE_TYPES}")
        if self.source_node_type not in VALID_NODE_TYPES:
            raise ValueError(f"Invalid source_node_type '{self.source_node_type}'")
        if self.target_node_type not in VALID_NODE_TYPES:
            raise ValueError(f"Invalid target_node_type '{self.target_node_type}'")


class RelationshipEvidence(BaseModel):
    """First-class evidence backing a relationship edge. Append-only."""

    model_config = ConfigDict(extra="forbid")

    evidence_id: uuid.UUID = Field(default_factory=uuid.uuid4)
    edge_id: uuid.UUID              # → relationships.edge_id
    source_record_id: str           # → entities_raw.source_record_id
    evidence_type: str              # cross_file_match | structured_xlsx_match | heuristic_keyword_match | llm_enriched | co_investment_pattern | graph_path_inference | contradicts_edge | contradicts_value
    evidence_strength: Probability
    confidence: Probability
    timestamp: datetime = Field(default_factory=utcnow)
    provenance_pointer: ProvenancePointer   # {source_file, source_offset, row_id}
    notes: Optional[str] = None


# ---------------------------------------------------------------------------
# Signals, rejections
# ---------------------------------------------------------------------------

VALID_SIGNAL_TYPES = frozenset({
    "response_speed", "exploratory_check", "operator_background",
    "em_participation", "geography_overlap", "social_proximity",
    "network_density", "deployment_velocity",
    "bridge_strength", "warm_path_count", "coinvest_intensity",
    "recent_activity_recency", "stage_alignment", "proxy_fund_overlap",
    "clean_profile", "shared_deal_count",
})

VALID_SIGNAL_EVIDENCE_TYPES = frozenset({
    "signal_heuristic", "signal_investment_pattern", "signal_graph_metric",
    "signal_icp_mirror", "signal_connectivity", "contradicts_value",
})


class SignalEvidence(BaseModel):
    """First-class evidence backing a signal. Append-only."""

    model_config = ConfigDict(extra="forbid")

    evidence_id: uuid.UUID = Field(default_factory=uuid.uuid4)
    signal_id: uuid.UUID
    source_record_id: str
    evidence_type: str              # signal_heuristic | signal_investment_pattern | ...
    evidence_strength: Probability
    confidence: Probability
    timestamp: datetime = Field(default_factory=utcnow)
    provenance_pointer: ProvenancePointer
    notes: Optional[str] = None


class Signal(BaseModel):
    """Weak signal associated with an allocator."""

    model_config = ConfigDict(extra="forbid")

    signal_id: uuid.UUID = Field(default_factory=uuid.uuid4)
    allocator_id: uuid.UUID
    signal_type: str                # must be in VALID_SIGNAL_TYPES
    raw_value: Optional[str] = None
    normalized_value: Optional[float] = None

    # Uncertainty — populated by pulse derive
    confidence: Optional[Probability] = None
    evidence_count: int = 0
    contradiction_score: Optional[Probability] = None
    source_agreement_score: Optional[Probability] = None
    effective_date: Optional[date] = None
    first_seen: Optional[datetime] = None
    last_seen: Optional[datetime] = None
    last_active: Optional[datetime] = None
    temporal_confidence: Optional[Probability] = None

    source_record_id: str
    source_file: str
    ingested_at: datetime = Field(default_factory=utcnow)
    content_hash: str


class Rejection(BaseModel):
    """Stated or inferred allocator rejection of a fund."""

    model_config = ConfigDict(extra="forbid")

    rejection_id: uuid.UUID = Field(default_factory=uuid.uuid4)
    allocator_id: uuid.UUID
    rejection_type: str             # stated | inferred | structural
    reason_tags: List[str] = Field(default_factory=list)
    stated_reason: Optional[str] = None
    inferred_reason: Optional[str] = None
    structural_constraint: Optional[str] = None
    future_conversion_prob: Optional[Probability] = None

    confidence: Optional[Probability] = None
    evidence_count: int = 0
    contradiction_score: Optional[Probability] = None

    source_record_id: str
    source_file: str
    ingested_at: datetime = Field(default_factory=utcnow)
    content_hash: str


# ---------------------------------------------------------------------------
# Ontology
# ---------------------------------------------------------------------------

class OntologyTerm(BaseModel):
    """Discovered archetype, category, or institutional pattern."""

    model_config = ConfigDict(extra="forbid")

    term_id: uuid.UUID = Field(default_factory=uuid.uuid4)
    term: str
    category: str                   # allocator_archetype | em_signal | rejection_pattern | geography_cluster | committee_constraint
    description: Optional[str] = None
    canonical_label: Optional[str] = None

    confidence: Optional[Probability] = None
    evidence_count: int = 0
    contradiction_score: Optional[Probability] = None
    source_agreement_score: Optional[Probability] = None
    first_seen: Optional[datetime] = None
    last_seen: Optional[datetime] = None


class EntityAlias(BaseModel):
    """Fuzzy-resolution alias mapping."""

    model_config = ConfigDict(extra="forbid")

    alias_id: uuid.UUID = Field(default_factory=uuid.uuid4)
    canonical_id: str               # canonical entity UUID (allocator_id or fund_id)
    entity_type: str                # allocator | fund
    alias_text: str                 # the raw name as it appears in the source
    source_file: str
    confidence: Probability
    source_agreement_score: Optional[Probability] = None
    resolver_method: str = "rapidfuzz"
    ingested_at: datetime = Field(default_factory=utcnow)


# ---------------------------------------------------------------------------
# ICP scoring
# ---------------------------------------------------------------------------

class IcpScore(BaseModel):
    """ICP fit score for an allocator. Soft signals populated by pulse score, not pulse derive."""

    model_config = ConfigDict(extra="forbid")

    score_id: uuid.UUID = Field(default_factory=uuid.uuid4)
    allocator_id: uuid.UUID
    icp_version: str = "4.1"
    c1_asset_class_pass: Optional[bool] = None
    c1_evidence: Optional[str] = None
    c2_emerging_manager_pass: Optional[bool] = None
    c2_evidence: Optional[str] = None
    c3_ai_tech_pass: Optional[bool] = None
    c3_evidence: Optional[str] = None
    c4_geography_pass: Optional[bool] = None
    c4_evidence: Optional[str] = None
    core_pass: Optional[bool] = None
    excluded: bool = False
    exclusion_reason: Optional[str] = None
    s1_ai_signal: Optional[Probability] = None
    s2_emerging_manager: Optional[Probability] = None
    s3_lp_type: Optional[Probability] = None
    s4_decision_speed: Optional[Probability] = None
    s5_stage: Optional[Probability] = None
    s6_clean_profile: Optional[Probability] = None
    s7_proxy_fund: Optional[Probability] = None
    fit_score: Optional[Probability] = None
    tier: Optional[str] = None
    client_status: Optional[str] = None
    client_decision: Optional[str] = None
    stated_reason: Optional[str] = None
    data_miner_comment: Optional[str] = None
    source_sheet: Optional[str] = None
    source_row: Optional[int] = None
    source_file: Optional[str] = None
    scored_at: datetime = Field(default_factory=utcnow)


# ---------------------------------------------------------------------------
# Human review
# ---------------------------------------------------------------------------

VALID_REVIEW_TARGET_TYPES = frozenset({
    "alias", "allocator_archetype", "ontology_term", "signal",
    "relationship_edge", "rejection",
})

VALID_REVIEW_DECISIONS = frozenset({"confirm", "reject", "revise", "defer"})


class HumanReview(BaseModel):
    """Append-only reviewer override. Never updated or deleted."""

    model_config = ConfigDict(extra="forbid")

    review_id: uuid.UUID = Field(default_factory=uuid.uuid4)
    target_type: str                # must be in VALID_REVIEW_TARGET_TYPES
    entity_id: str                  # polymorphic UUID of the target entity
    reviewer: str
    decision: str                   # must be in VALID_REVIEW_DECISIONS
    override_payload: Optional[Dict[str, Any]] = None   # corrected value when decision='revise'
    confidence_adjustment: Optional[float] = Field(default=None, ge=-1.0, le=1.0)
    override_reason: Optional[str] = None
    notes: Optional[str] = None
    reviewed_at: datetime = Field(default_factory=utcnow)
    supersedes: Optional[uuid.UUID] = None              # prior review_id being revised

    def model_post_init(self, __context: Any) -> None:
        if self.target_type not in VALID_REVIEW_TARGET_TYPES:
            raise ValueError(f"Invalid target_type '{self.target_type}'")
        if self.decision not in VALID_REVIEW_DECISIONS:
            raise ValueError(f"Invalid decision '{self.decision}'")
        if self.decision == "revise" and self.override_payload is None:
            raise ValueError("decision='revise' requires override_payload")


# ---------------------------------------------------------------------------
# Pipeline run tracking
# ---------------------------------------------------------------------------

class PipelineRun(BaseModel):
    """Tracks one execution of a pipeline stage."""

    model_config = ConfigDict(extra="forbid")

    run_id: uuid.UUID = Field(default_factory=uuid.uuid4)
    stage: str                      # ingest | normalize | extract | derive | graph | review
    status: str                     # running | completed | failed
    params: Dict[str, Any] = Field(default_factory=dict)
    artifact_uris: List[str] = Field(default_factory=list)
    derivation_params_hash: Optional[str] = None  # SHA-256 of uncertainty.yaml at derive time
    started_at: datetime = Field(default_factory=utcnow)
    completed_at: Optional[datetime] = None
    error: Optional[str] = None
    rows_processed: int = 0
    rows_written: int = 0

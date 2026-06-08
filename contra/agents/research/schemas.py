"""
Strict Pydantic v2 output schemas for every PULSE research-agent capability.

All probability fields are Annotated[float, Field(ge=0, le=1)].
ConfigDict(extra="forbid") is set on every model — no silent field injection.

These schemas are the contracts between the LLM extraction layer (instructor)
and the write-back layer (enrichment_agent, qa_agent, brief_agent).
"""

from __future__ import annotations

from typing import Annotated, Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field


Probability = Annotated[float, Field(ge=0.0, le=1.0)]


# ---------------------------------------------------------------------------
# Shared building block
# ---------------------------------------------------------------------------

class EnrichedField(BaseModel):
    """A single enriched value with provenance metadata."""

    model_config = ConfigDict(extra="forbid")

    value: Optional[str] = Field(
        None,
        description="Canonical value; use enum strings from PULSE taxonomies where applicable.",
    )
    confidence: Probability = Field(
        0.5,
        description="Model confidence that this value is correct, in [0, 1].",
    )
    source_urls: List[str] = Field(
        default_factory=list,
        description="URLs that support this value; empty if derived from local data only.",
    )
    reasoning: Optional[str] = Field(
        None,
        description="One-sentence rationale for the assigned value.",
    )


# ---------------------------------------------------------------------------
# Enrichment agent output
# ---------------------------------------------------------------------------

class EnrichmentResult(BaseModel):
    """
    Structured extraction of allocator attributes AND ICP-fit intelligence
    from web research.

    Taxonomy fields (allocator_type, geography, etc.) fill NULL columns in the
    allocators table. Fit-intelligence fields (em_track_record, ai_exposure, etc.)
    are written as signals and a research note — they go beyond classification
    into actual fit assessment for an AI-native VC fund in emerging markets.

    For taxonomy fields: use exact canonical enum values listed in descriptions.
    For fit fields: extract evidence from web results; null if no evidence found.
    """

    model_config = ConfigDict(extra="forbid")

    # --- Taxonomy classification (fill NULL allocator columns) ---

    allocator_type: EnrichedField = Field(
        default_factory=EnrichedField,
        description=(
            "LP category. Must be one of: pension_fund, sovereign_wealth, endowment, "
            "foundation, family_office_single, family_office_multi, fund_of_funds, "
            "insurance, bank, asset_manager, development_finance, corporate, "
            "high_net_worth, angel, unknown."
        ),
    )
    geography: EnrichedField = Field(
        default_factory=EnrichedField,
        description=(
            "Primary investment geography. Must be one of: southeast_asia, south_asia, "
            "east_asia, asia_pacific, middle_east, africa, north_america, europe, "
            "latin_america, global, emerging_markets, unknown."
        ),
    )
    hq_country: EnrichedField = Field(
        default_factory=EnrichedField,
        description="ISO country name where the entity is headquartered.",
    )
    em_appetite: EnrichedField = Field(
        default_factory=EnrichedField,
        description=(
            "Emerging-market appetite based on actual portfolio evidence. "
            "One of: high, medium, low, none, unknown."
        ),
    )
    ai_appetite: EnrichedField = Field(
        default_factory=EnrichedField,
        description=(
            "AI/deep-tech appetite based on actual portfolio evidence. "
            "One of: high, medium, low, none, unknown."
        ),
    )
    stage_preference: EnrichedField = Field(
        default_factory=EnrichedField,
        description=(
            "Preferred investment stage. One of: pre_seed, seed, series_a, series_b, "
            "growth, late_stage, multi_stage, unknown."
        ),
    )

    # --- ICP fit intelligence (written as signals + research notes) ---

    em_track_record: EnrichedField = Field(
        default_factory=EnrichedField,
        description=(
            "Has this LP actually invested in emerging markets (SEA, India, Africa, MENA, LATAM)? "
            "value: 'yes' | 'no' | 'unknown'. "
            "reasoning: cite specific portfolio companies or funds if found."
        ),
    )
    emerging_manager_history: EnrichedField = Field(
        default_factory=EnrichedField,
        description=(
            "Has this LP backed first-time or emerging fund managers before? "
            "value: 'yes' | 'no' | 'unknown'. "
            "reasoning: cite fund names or evidence if found."
        ),
    )
    ai_portfolio_evidence: EnrichedField = Field(
        default_factory=EnrichedField,
        description=(
            "Does their portfolio include AI, ML, or deep-tech companies? "
            "value: 'yes' | 'no' | 'unknown'. "
            "reasoning: list 1-3 AI portfolio companies if found."
        ),
    )
    check_size_evidence: EnrichedField = Field(
        default_factory=EnrichedField,
        description=(
            "What is their typical LP commitment or check size? "
            "value: free-text dollar amount or range (e.g. '$500K–$2M into funds'). "
            "reasoning: source where this was found."
        ),
    )
    venture_focus: EnrichedField = Field(
        default_factory=EnrichedField,
        description=(
            "Do they invest in venture capital / early-stage funds (as an LP)? "
            "value: 'yes' | 'no' | 'unknown'. "
            "reasoning: cite evidence."
        ),
    )
    recent_activity: EnrichedField = Field(
        default_factory=EnrichedField,
        description=(
            "Any notable recent investment activity (last 1-2 years)? "
            "value: one-line summary of recent activity or 'none found'. "
            "Do not fabricate; only include if found in search results."
        ),
    )
    fit_assessment: EnrichedField = Field(
        default_factory=EnrichedField,
        description=(
            "Overall fit for an AI-native, emerging-market-focused VC fund raising $30M. "
            "value: 'strong' | 'moderate' | 'weak' | 'unknown'. "
            "reasoning: 2-3 sentence explanation grounded only in the evidence above."
        ),
    )
    summary: Optional[str] = Field(
        None,
        description=(
            "3-sentence profile: (1) who they are, (2) what they invest in, "
            "(3) why they are or are not a fit for an AI-native EM VC fund. "
            "Only use facts from the provided search results — no hallucination."
        ),
    )


# ---------------------------------------------------------------------------
# Q&A agent output
# ---------------------------------------------------------------------------

class QAAnswer(BaseModel):
    """
    Response from the analyst Q&A agent.

    generated_sql is the SELECT statement that was executed.
    rows contains the result set (list of dicts, column → value).
    narrative is a concise natural-language synthesis of the result.
    """

    model_config = ConfigDict(extra="forbid")

    generated_sql: str = Field(description="The exact SQL SELECT that was executed.")
    rows: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="Result rows as list-of-dicts.",
    )
    row_count: int = Field(0, description="Total rows returned.")
    narrative: str = Field(
        description="1–3 sentence plain-English synthesis of the query result."
    )
    cited_tables: List[str] = Field(
        default_factory=list,
        description="Table/view names referenced in the SQL.",
    )
    confidence: Probability = Field(
        0.8,
        description="How confident the agent is that the SQL correctly answers the question.",
    )


# ---------------------------------------------------------------------------
# Outreach brief agent output
# ---------------------------------------------------------------------------

class WarmPathEntry(BaseModel):
    """A single warm-path route to an LP."""

    model_config = ConfigDict(extra="forbid")

    bridge_name: str = Field(description="Canonical name of the bridge/introducer node.")
    bridge_type: str = Field(description="Node type: lp | fund | advisor | founder.")
    syndicate_lp_name: str = Field(description="The syndicate LP on the other side of the bridge.")
    bridge_strength: float = Field(description="Computed path strength ∈ (0, 1].")
    co_invest_evidence: Optional[str] = Field(
        None,
        description="Brief description of the co-investment evidence backing this path.",
    )


class BriefSections(BaseModel):
    """
    Structured outreach brief for a single LP prospect.

    All content must be grounded in the PULSE data provided; no hallucination.
    """

    model_config = ConfigDict(extra="forbid")

    thesis_fit: str = Field(
        description=(
            "2–3 sentences on why this LP is a fit for MyAsiaVC: ICP tier rationale, "
            "core criteria (C1–C4) that pass, and the strongest soft signals (S1–S7)."
        ),
    )
    warm_path_intro: str = Field(
        description=(
            "1–2 sentences on the best warm introduction route available: "
            "who to ask, via which bridge node, and why the connection is credible."
        ),
    )
    talking_points: List[str] = Field(
        description=(
            "3–5 bullet talking points for the first meeting. Each must reference "
            "a specific data point (score, signal, edge, geography, stage pref)."
        ),
    )
    risks_and_objections: List[str] = Field(
        description=(
            "2–3 likely objections or risk factors (e.g. rejection patterns, "
            "low signal, unknown geography). Be specific."
        ),
    )
    recommended_next_step: str = Field(
        description="One concrete recommended next action (e.g. 'Request warm intro via X').",
    )
    data_gaps: List[str] = Field(
        default_factory=list,
        description="Fields currently NULL on this allocator that would improve the brief.",
    )


# ---------------------------------------------------------------------------
# Ontology enrichment output
# ---------------------------------------------------------------------------

class OntologyTermExtraction(BaseModel):
    """A single ontology term extracted by the LLM."""

    model_config = ConfigDict(extra="forbid")

    term: str = Field(description="The canonical term label.")
    category: str = Field(
        description=(
            "One of: allocator_archetype, em_signal, rejection_pattern, "
            "geography_cluster, committee_constraint."
        ),
    )
    canonical_label: Optional[str] = Field(
        None,
        description="Normalised label matching the existing ontology if known.",
    )
    confidence: Probability = Field(description="Extraction confidence ∈ [0, 1].")
    matched_pattern: Optional[str] = Field(
        None,
        description="The text fragment that triggered this extraction.",
    )
    notes: Optional[str] = Field(
        None,
        description="Brief note on context or ambiguity.",
    )


class OntologyEnrichment(BaseModel):
    """LLM output for ontology extraction over a single document chunk."""

    model_config = ConfigDict(extra="forbid")

    terms: List[OntologyTermExtraction] = Field(
        default_factory=list,
        description="All ontology terms discovered in the document.",
    )
    relationship_hints: List[RelationshipHintExtraction] = Field(
        default_factory=list,
        description="Any relationship hints between named entities in the document.",
    )


class RelationshipHintExtraction(BaseModel):
    """A relationship hint extracted by the LLM."""

    model_config = ConfigDict(extra="forbid")

    source_entity_name: str
    target_entity_name: str
    edge_type: str = Field(
        description=(
            "Must be one of: invested_with, introduced_by, co_invested, "
            "syndicate_overlap, mutual_connection, repeated_exposure, co_mentioned."
        ),
    )
    evidence_strength: Probability
    confidence: Probability
    notes: Optional[str] = None


# Fix forward reference — OntologyEnrichment references RelationshipHintExtraction
OntologyEnrichment.model_rebuild()

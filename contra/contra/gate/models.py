"""Gate verdict schemas — v2 with structured assessment."""

from __future__ import annotations

import uuid
from typing import List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


# ---------------------------------------------------------------------------
# Assessment building blocks (deterministic layer)
# ---------------------------------------------------------------------------

class CoreGateCheck(BaseModel):
    """Pass/fail/unknown status for a single ICP core gate."""
    model_config = ConfigDict(extra="forbid")

    gate: Literal["c1", "c2", "c3", "c4"]
    status: Literal["pass", "fail", "unknown"]
    evidence: str
    # Where the assessment came from: "backend" (DB/ICP score), "web" (LLM inference
    # from web research), or "analyst" (analyst-provided fact)
    source: Literal["backend", "web", "analyst"] = "backend"


class GateSignal(BaseModel):
    """One qualifying signal toward the ≥2 threshold for CRM admission."""
    model_config = ConfigDict(extra="forbid")

    id: str
    label: str
    met: bool
    source: Literal["backend", "syndicate", "web", "analyst"]
    detail: str


# Graded appetite scale used across inferred appetite dimensions.
AppetiteLevel = Literal["strong", "moderate", "weak", "none", "unknown"]

# Behavioral allocator archetypes (assigned from allocation behavior, not raw type).
Archetype = Literal[
    "fund_of_funds",
    "family_office",
    "institutional_lp",
    "emerging_manager_specialist",
    "asia_specialist",
    "technology_specialist",
    "founder_lp",
    "corporate_investor",
    "generalist",
    "unknown",
]


class AppetiteProfile(BaseModel):
    """
    Inferred allocator appetite — the explainable output of the Appetite Engine.

    Every dimension is a graded level + a one-line cited rationale. These are
    INFERENCES from historical allocation behavior, not explicit allocator claims.
    """
    model_config = ConfigDict(extra="forbid")

    # Graded appetite per dimension
    em_appetite: AppetiteLevel = "unknown"
    fund_i_appetite: AppetiteLevel = "unknown"
    ai_tech_appetite: AppetiteLevel = "unknown"
    venture_appetite: AppetiteLevel = "unknown"
    geography_appetite: AppetiteLevel = "unknown"

    # Behavioral archetype
    archetype: Archetype = "unknown"
    archetype_evidence: str = ""

    # Negative inference — disqualifying evidence actively searched for
    negative_flags: List[str] = Field(default_factory=list)
    negative_evidence: str = ""

    # Explainable similarity to the MyAsiaVC manager profile (no arbitrary number)
    myasiavc_similarity: Literal["high", "medium", "low", "none"] = "none"
    similarity_rationale: str = ""

    # Cited allocation decisions the inference was built from (managers/companies backed)
    allocation_evidence: List[str] = Field(default_factory=list)

    def appetite_signals_met(self) -> int:
        """Count appetite dimensions at moderate-or-stronger (toward the >=2 bar)."""
        strong_or_moderate = {"strong", "moderate"}
        return sum(
            1 for level in (
                self.em_appetite,
                self.fund_i_appetite,
                self.ai_tech_appetite,
                self.venture_appetite,
                self.geography_appetite,
            )
            if level in strong_or_moderate
        )


class GateAssessment(BaseModel):
    """
    Pure-Python assessment produced before the LLM explain pass.
    The LLM must not contradict recommendation except via conflicts[].
    """
    model_config = ConfigDict(extra="forbid")

    recommendation: Literal["yes", "no", "review"]
    hard_blocks: List[str] = Field(default_factory=list)
    core_gates: List[CoreGateCheck] = Field(default_factory=list)
    signals: List[GateSignal] = Field(default_factory=list)
    signals_met: int = 0
    signals_required: int = 2

    # Inferred appetite (filled after the LLM explain pass); None before inference.
    appetite: Optional[AppetiteProfile] = None


# ---------------------------------------------------------------------------
# Full gate result (replaces bare GateVerdict for API responses)
# ---------------------------------------------------------------------------

class GateResult(BaseModel):
    """Complete gate result — assessment + LLM explanation + session tracking."""
    model_config = ConfigDict(extra="forbid")

    session_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    lp_name: str

    # Deterministic assessment
    assessment: GateAssessment

    # Top-level convenience flags
    yes: bool = Field(description="True = add to CRM, False = skip/review")
    is_review: bool = Field(default=False, description="True when recommendation=review")

    # LLM explanation layer
    confidence: Literal["high", "medium", "low"]
    reasons: List[str] = Field(min_length=1, max_length=8)
    backend_evidence: List[str] = Field(default_factory=list)
    online_evidence: List[str] = Field(default_factory=list)
    conflicts: List[str] = Field(default_factory=list)
    summary: str = Field(description="Two-sentence plain-English verdict")
    db_queries_used: List[str] = Field(default_factory=list)

    # Inferred allocator appetite (None for hard-block / no-LLM paths)
    appetite: Optional[AppetiteProfile] = None

    # All URLs actually fetched during web research (shown in UI regardless of LLM citation)
    source_urls: List[str] = Field(default_factory=list)

    # Analyst-provided context (grows through chat)
    analyst_facts: List[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Legacy alias — kept so existing callers don't break during transition
# ---------------------------------------------------------------------------

GateVerdict = GateResult


# ---------------------------------------------------------------------------
# LLM extraction schemas (internal use by verdict.py and chat.py)
# ---------------------------------------------------------------------------

class GateExplanation(BaseModel):
    """
    Schema returned by the LLM explain pass — the LLM is the primary decision-maker.

    Core gate assessments are FLAT scalar fields (not a nested list) so that small
    JSON-mode models (e.g. Groq llama-3.1-8b) can fill them reliably.
    """
    model_config = ConfigDict(extra="forbid")

    # LLM's holistic verdict — overrides the evaluator's signal-count heuristic
    # (except hard blocks, which always win)
    llm_recommendation: Literal["yes", "no", "review"]
    confidence: Literal["high", "medium", "low"]
    reasons: List[str] = Field(min_length=1, max_length=8)
    backend_evidence: List[str] = Field(default_factory=list)
    online_evidence: List[str] = Field(default_factory=list)
    conflicts: List[str] = Field(default_factory=list)
    summary: str

    # LLM-assessed core gates from web evidence — fills gaps the evaluator left as unknown.
    # Flat fields keep the schema simple for small models.
    c1_status: Literal["pass", "fail", "unknown"] = "unknown"
    c1_evidence: str = ""
    c2_status: Literal["pass", "fail", "unknown"] = "unknown"
    c2_evidence: str = ""
    c3_status: Literal["pass", "fail", "unknown"] = "unknown"
    c3_evidence: str = ""
    c4_status: Literal["pass", "fail", "unknown"] = "unknown"
    c4_evidence: str = ""

    # Web signal extraction — used to optionally add a web signal on re-eval
    web_em_ai_vc: bool = False
    web_em_ai_evidence: str = ""

    # ----- Appetite Engine (flat fields keep the schema small-model friendly) -----
    # Graded appetite inferred from historical allocation behavior + one-line evidence.
    em_appetite: AppetiteLevel = "unknown"
    em_appetite_evidence: str = ""
    fund_i_appetite: AppetiteLevel = "unknown"
    fund_i_appetite_evidence: str = ""
    ai_tech_appetite: AppetiteLevel = "unknown"
    ai_tech_appetite_evidence: str = ""
    venture_appetite: AppetiteLevel = "unknown"
    venture_appetite_evidence: str = ""
    geography_appetite: AppetiteLevel = "unknown"
    geography_appetite_evidence: str = ""

    # Behavioral archetype
    archetype: Archetype = "unknown"
    archetype_evidence: str = ""

    # Negative inference — disqualifiers actively searched for (may be empty)
    negative_flags: List[str] = Field(default_factory=list)
    negative_evidence: str = ""

    # Explainable similarity to the MyAsiaVC manager profile
    myasiavc_similarity: Literal["high", "medium", "low", "none"] = "none"
    similarity_rationale: str = ""

    # Cited allocation decisions (managers/companies backed) the inference rests on
    allocation_evidence: List[str] = Field(default_factory=list)

    def llm_core_gates(self) -> List[CoreGateCheck]:
        """Reconstruct CoreGateCheck objects from the flat scalar fields."""
        pairs = [
            ("c1", self.c1_status, self.c1_evidence),
            ("c2", self.c2_status, self.c2_evidence),
            ("c3", self.c3_status, self.c3_evidence),
            ("c4", self.c4_status, self.c4_evidence),
        ]
        return [
            CoreGateCheck(gate=g, status=s, evidence=e or "(no web evidence)", source="web")  # type: ignore[arg-type]
            for g, s, e in pairs
        ]

    def to_appetite_profile(self, allocation_evidence: Optional[List[str]] = None) -> AppetiteProfile:
        """Build the structured AppetiteProfile from the flat LLM fields."""
        return AppetiteProfile(
            em_appetite=self.em_appetite,
            fund_i_appetite=self.fund_i_appetite,
            ai_tech_appetite=self.ai_tech_appetite,
            venture_appetite=self.venture_appetite,
            geography_appetite=self.geography_appetite,
            archetype=self.archetype,
            archetype_evidence=self.archetype_evidence,
            negative_flags=self.negative_flags,
            negative_evidence=self.negative_evidence,
            myasiavc_similarity=self.myasiavc_similarity,
            similarity_rationale=self.similarity_rationale,
            allocation_evidence=self.allocation_evidence or (allocation_evidence or []),
        )


class AnalystFactExtraction(BaseModel):
    """Schema used by chat.py to extract new facts from an analyst message."""
    model_config = ConfigDict(extra="forbid")

    has_new_facts: bool
    facts: List[str] = Field(
        default_factory=list,
        description="Explicit LP facts stated by the analyst (not questions)",
    )
    is_question_only: bool = True

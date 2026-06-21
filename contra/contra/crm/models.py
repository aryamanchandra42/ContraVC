"""CRM lead schemas — API and LLM extraction."""

from __future__ import annotations

from typing import List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


class CrmLeadExtraction(BaseModel):
    """LLM-structured fields for a new CRM lead from gate session."""
    model_config = ConfigDict(extra="forbid")

    investor_name: str
    investor_type: str = ""
    investor_location: str = ""
    investor_details: str = ""
    pipeline_stage: str = "Prospect"
    contact_name: Optional[str] = None
    contact_email: Optional[str] = None
    contact_linkedin: Optional[str] = None
    enrichment_gaps: List[str] = Field(default_factory=list)


class CrmLead(BaseModel):
    """API response for a CRM lead row."""
    model_config = ConfigDict(extra="forbid")

    lead_id: str
    investor_name: str
    name_key: str
    allocator_id: Optional[str] = None
    source: str
    status: str
    investor_type: Optional[str] = None
    investor_location: Optional[str] = None
    investor_details: Optional[str] = None
    contacts_json: Optional[dict] = None
    contact_email: Optional[str] = None
    pipeline_stage: Optional[str] = None
    computed_score: Optional[float] = None
    manual_rank: Optional[int] = None
    effective_rank: Optional[int] = None
    gate_session_id: Optional[str] = None
    gate_verdict: Optional[str] = None
    gate_confidence: Optional[str] = None
    gate_summary: Optional[str] = None
    icp_tier: Optional[str] = None
    fit_score: Optional[float] = None
    contra_rank: Optional[int] = None
    warm_path_count: Optional[int] = None
    syndicate_score: Optional[float] = None
    needs_enrichment: bool = False
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class CrmProspect(BaseModel):
    """Prospect not yet in CRM — from ICP, syndicate, or benchmark."""
    model_config = ConfigDict(extra="forbid")

    allocator_id: Optional[str] = None
    investor_name: str
    investor_type: Optional[str] = None
    investor_location: Optional[str] = None
    icp_tier: Optional[str] = None
    fit_score: Optional[float] = None
    contra_rank: Optional[int] = None
    warm_path_count: Optional[int] = None
    syndicate_score: Optional[float] = None
    suggested_source: str
    prospect_score: Optional[float] = None


class CrmIcpQueueItem(BaseModel):
    """One row from v_crm_icp_queue — ICP prospect awaiting gate review."""
    model_config = ConfigDict(extra="forbid")

    allocator_id: Optional[str] = None
    investor_name: str
    allocator_type: Optional[str] = None
    investor_location: Optional[str] = None
    icp_tier: Optional[str] = None
    fit_score: Optional[float] = None
    client_decision: Optional[str] = None
    client_status: Optional[str] = None
    core_pass: Optional[bool] = None
    warm_path_count: Optional[int] = None
    readiness: Literal["READY", "NEAR_READY", "PENDING"]
    gate_verdict: Optional[Literal["yes", "review", "no"]] = None
    gate_session_id: Optional[str] = None
    gate_reviewed_at: Optional[str] = None


class CrmLeadUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    manual_rank: Optional[int] = None
    status: Optional[Literal["active", "review", "contacted", "passed"]] = None
    pipeline_stage: Optional[str] = None
    investor_details: Optional[str] = None


class CrmManualAdd(BaseModel):
    model_config = ConfigDict(extra="forbid")

    investor_name: str
    investor_type: Optional[str] = None
    investor_location: Optional[str] = None
    investor_details: Optional[str] = None
    pipeline_stage: Optional[str] = "Prospect"


class CrmPromoteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    allocator_id: str
    source: Literal["icp", "syndicate", "benchmark", "manual"] = "icp"
    investor_name: Optional[str] = None

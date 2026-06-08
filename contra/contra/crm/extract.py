"""LLM extraction of CRM fields from gate session context."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from contra.crm.models import CrmLeadExtraction
from contra.gate.models import GateResult

ROOT = Path(__file__).resolve().parent.parent.parent


def _load_yaml() -> Dict[str, Any]:
    path = ROOT / "prompts" / "navigator" / "crm_extract.yaml"
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def extract_crm_fields(
    result: GateResult,
    brief_dict: Dict[str, Any],
) -> CrmLeadExtraction:
    """Call LLM to structure CRM lead fields from gate session."""
    from agents.research.llm_client import LLMUnavailable, get_llm_client

    try:
        llm = get_llm_client()
    except LLMUnavailable as exc:
        raise RuntimeError(
            "LLM required for CRM extract. Set PULSE_LLM_PROVIDER and API key."
        ) from exc

    cfg = _load_yaml()
    system = cfg.get("system") or "Extract CRM lead fields. Return CrmLeadExtraction JSON."
    template = cfg.get("user_template") or "{lp_name}"

    verdict = result.assessment.recommendation
    if result.yes:
        verdict = "yes"
    elif result.is_review:
        verdict = "review"

    prompt = template.format(
        lp_name=result.lp_name,
        gate_verdict=verdict,
        gate_confidence=result.confidence,
        gate_summary=result.summary,
        gate_reasons=json.dumps(result.reasons[:8]),
        online_evidence=json.dumps(result.online_evidence[:6]),
        brief_json=json.dumps(brief_dict, default=str)[:3500],
        appetite_json=json.dumps(
            result.appetite.model_dump() if result.appetite else {},
            default=str,
        )[:1500],
        contacts_json=json.dumps(brief_dict.get("contacts") or [])[:800],
    )

    return llm.structured(
        prompt=prompt,
        response_model=CrmLeadExtraction,
        system=system,
        max_tokens=1024,
    )


def merge_extraction(
    extraction: CrmLeadExtraction,
    brief_dict: Dict[str, Any],
    result: GateResult,
) -> Dict[str, Any]:
    """Merge LLM extraction with deterministic brief/gate facts (DB wins on conflicts)."""
    profile = brief_dict.get("allocator_profile") or {}
    contacts_json: Dict[str, Any] = {}

    brief_contacts = brief_dict.get("contacts") or []
    if brief_contacts:
        for i, c in enumerate(brief_contacts[:3], start=1):
            contacts_json[f"contact_{i}"] = {
                k: v for k, v in {
                    "name": c.get("full_name"),
                    "email": c.get("email"),
                    "linkedin": c.get("linkedin_url"),
                    "position": c.get("title"),
                }.items() if v
            }
    elif extraction.contact_name or extraction.contact_email or extraction.contact_linkedin:
        contacts_json["contact_1"] = {
            k: v for k, v in {
                "name": extraction.contact_name,
                "email": extraction.contact_email,
                "linkedin": extraction.contact_linkedin,
            }.items() if v
        }

    investor_name = (
        brief_dict.get("matched_name")
        or extraction.investor_name
        or result.lp_name
    )
    investor_type = profile.get("allocator_type") or extraction.investor_type or None
    investor_location = profile.get("geography") or extraction.investor_location or None

    crm_row = brief_dict.get("crm_row") or {}
    if crm_row.get("investor_type"):
        investor_type = crm_row["investor_type"]
    if crm_row.get("investor_location"):
        investor_location = crm_row["investor_location"]

    details = extraction.investor_details or result.summary
    if brief_dict.get("icp_tier"):
        details = f"[{brief_dict['icp_tier']}] {details}"

    gc = brief_dict.get("graph_connectivity") or {}
    syndicate = brief_dict.get("syndicate_profile") or {}

    return {
        "investor_name": investor_name,
        "investor_type": investor_type,
        "investor_location": investor_location,
        "investor_details": details[:2000] if details else None,
        "contacts_json": contacts_json or None,
        "pipeline_stage": extraction.pipeline_stage or ("Qualified" if result.yes else "Review"),
        "allocator_id": brief_dict.get("allocator_id"),
        "icp_tier": brief_dict.get("icp_tier"),
        "fit_score": brief_dict.get("icp_fit_score"),
        "contra_rank": brief_dict.get("benchmark_rank"),
        "warm_path_count": gc.get("warm_path_count", 0),
        "syndicate_score": syndicate.get("fund_lp_behavior_score"),
        "needs_enrichment": bool(extraction.enrichment_gaps)
            or not investor_type
            or not investor_location,
        "gate_verdict": "yes" if result.yes else ("review" if result.is_review else "no"),
        "gate_confidence": result.confidence,
        "gate_summary": result.summary,
        "gate_reasons_json": result.reasons,
        "appetite_json": result.appetite.model_dump() if result.appetite else None,
        "gate_session_id": result.session_id,
    }

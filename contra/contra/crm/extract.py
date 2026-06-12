"""CRM field extraction from gate session context.

Deterministic-first: a CRM lead's fields are almost entirely derivable from the
GateResult + IntelligenceBrief we already computed, so by default no LLM call is
made (faster adds, zero cost). Set CRM_EXTRACT_USE_LLM=true to restore the LLM
pass (with deterministic fallback on failure).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from contra.crm.models import CrmLeadExtraction
from contra.gate.models import GateResult

ROOT = Path(__file__).resolve().parent.parent.parent

_ARCHETYPE_LABELS = {
    "fund_of_funds": "Fund of Funds",
    "family_office": "Family Office",
    "institutional_lp": "Institutional LP",
    "emerging_manager_specialist": "Emerging-Manager LP",
    "asia_specialist": "Asia-Focused LP",
    "technology_specialist": "Technology-Focused LP",
    "founder_lp": "Founder LP",
    "corporate_investor": "Corporate Investor",
    "generalist": "Generalist LP",
}


def _load_yaml() -> Dict[str, Any]:
    path = ROOT / "prompts" / "navigator" / "crm_extract.yaml"
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def derive_crm_fields(
    result: GateResult,
    brief_dict: Dict[str, Any],
) -> CrmLeadExtraction:
    """Build CRM lead fields deterministically from gate output (no LLM)."""
    appetite = result.appetite
    profile = brief_dict.get("allocator_profile") or {}
    nfx = result.nfx_profile or {}

    investor_type = (
        profile.get("allocator_type")
        or _ARCHETYPE_LABELS.get(appetite.archetype if appetite else "", "")
        or ""
    )
    investor_location = (
        profile.get("geography")
        or nfx.get("locations")
        or ""
    )

    detail_parts: List[str] = [result.summary.strip()] if result.summary else []
    if result.lp_commitments_found:
        detail_parts.append(
            "Confirmed LP commitments: " + "; ".join(result.lp_commitments_found[:5])
        )
    if appetite and appetite.myasiavc_similarity in ("high", "medium"):
        detail_parts.append(
            f"MyAsiaVC similarity: {appetite.myasiavc_similarity} — {appetite.similarity_rationale}"
        )
    if result.reasons:
        detail_parts.append("Gate notes: " + " | ".join(result.reasons[:3]))

    contacts = brief_dict.get("contacts") or []
    first_contact = contacts[0] if contacts and not brief_dict.get("match_untrusted") else {}

    gaps: List[str] = []
    if not investor_type:
        gaps.append("investor_type")
    if not investor_location:
        gaps.append("investor_location")
    if not (first_contact.get("email") if isinstance(first_contact, dict) else None):
        gaps.append("contact_email")

    return CrmLeadExtraction(
        investor_name=result.lp_name,
        investor_type=investor_type,
        investor_location=investor_location,
        investor_details=" ".join(detail_parts)[:1900],
        pipeline_stage="Qualified" if result.yes else "Review",
        contact_name=(first_contact.get("full_name") if isinstance(first_contact, dict) else None),
        contact_email=(first_contact.get("email") if isinstance(first_contact, dict) else None),
        contact_linkedin=(first_contact.get("linkedin_url") if isinstance(first_contact, dict) else None),
        enrichment_gaps=gaps,
    )


def extract_crm_fields(
    result: GateResult,
    brief_dict: Dict[str, Any],
) -> CrmLeadExtraction:
    """Structure CRM lead fields from a gate session (deterministic by default)."""
    use_llm = os.environ.get("CRM_EXTRACT_USE_LLM", "false").lower().strip() in (
        "1", "true", "yes", "on",
    )
    if not use_llm:
        return derive_crm_fields(result, brief_dict)

    try:
        return _extract_crm_fields_llm(result, brief_dict)
    except Exception:
        return derive_crm_fields(result, brief_dict)


def _extract_crm_fields_llm(
    result: GateResult,
    brief_dict: Dict[str, Any],
) -> CrmLeadExtraction:
    """Call LLM to structure CRM lead fields from gate session."""
    from agents.research.llm_client import LLMUnavailable, get_llm_client
    from agents.research.nim_router import get_nim_task_client, nim_enabled

    try:
        llm = get_nim_task_client("crm", auto_switch=True) if nim_enabled() else get_llm_client()
    except LLMUnavailable as exc:
        raise RuntimeError(
            "LLM required for CRM extract. Set PULSE_LLM_PROVIDER=anthropic and ANTHROPIC_API_KEY."
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


def _gate_db_identity_trusted(brief_dict: Dict[str, Any]) -> bool:
    """True when backend allocator_id is safe to attach to a CRM lead from this gate run."""
    if brief_dict.get("match_untrusted"):
        return False
    if not brief_dict.get("allocator_id"):
        return False
    method = (brief_dict.get("match_method") or "none").lower()
    if method in ("exact", "alias"):
        return True
    if method == "fuzzy":
        return float(brief_dict.get("match_confidence") or 0) >= 0.92
    return False


def merge_extraction(
    extraction: CrmLeadExtraction,
    brief_dict: Dict[str, Any],
    result: GateResult,
) -> Dict[str, Any]:
    """Merge LLM extraction with deterministic brief/gate facts (DB wins on conflicts)."""
    screened_name = (brief_dict.get("input_name") or result.lp_name or "").strip()
    trusted_db = _gate_db_identity_trusted(brief_dict)

    profile = brief_dict.get("allocator_profile") or {} if trusted_db else {}
    contacts_json: Dict[str, Any] = {}

    brief_contacts = (brief_dict.get("contacts") or []) if trusted_db else []
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

    # Always keep the name the analyst screened — never swap in a fuzzy DB match.
    investor_name = screened_name or extraction.investor_name or result.lp_name
    investor_type = profile.get("allocator_type") or extraction.investor_type or None
    investor_location = profile.get("geography") or extraction.investor_location or None

    crm_row = brief_dict.get("crm_row") or {}
    if crm_row.get("investor_type"):
        investor_type = crm_row["investor_type"]
    if crm_row.get("investor_location"):
        investor_location = crm_row["investor_location"]

    details = extraction.investor_details or result.summary
    icp_tier = brief_dict.get("icp_tier") if trusted_db else None
    if icp_tier:
        details = f"[{icp_tier}] {details}"
    if not trusted_db and brief_dict.get("matched_name"):
        details = (
            f"(Screened as '{screened_name}'; DB match '{brief_dict['matched_name']}' "
            f"not trusted — profile from gate/web only.) {details}"
        )

    gc = brief_dict.get("graph_connectivity") or {} if trusted_db else {}
    syndicate = brief_dict.get("syndicate_profile") or {} if trusted_db else {}

    return {
        "investor_name": investor_name,
        "investor_type": investor_type,
        "investor_location": investor_location,
        "investor_details": details[:2000] if details else None,
        "contacts_json": contacts_json or None,
        "pipeline_stage": extraction.pipeline_stage or ("Qualified" if result.yes else "Review"),
        "allocator_id": brief_dict.get("allocator_id") if trusted_db else None,
        "icp_tier": icp_tier,
        "fit_score": brief_dict.get("icp_fit_score") if trusted_db else None,
        "contra_rank": brief_dict.get("benchmark_rank") if trusted_db else None,
        "warm_path_count": gc.get("warm_path_count", 0),
        "syndicate_score": syndicate.get("fund_lp_behavior_score") if trusted_db else None,
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

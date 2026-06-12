"""CRM merge must keep the screened Gate name, not a fuzzy DB match."""

from __future__ import annotations

from contra.crm.extract import merge_extraction
from contra.crm.models import CrmLeadExtraction
from contra.gate.models import GateAssessment, GateResult


def _result(lp_name: str) -> GateResult:
    return GateResult(
        session_id="sess1",
        lp_name=lp_name,
        assessment=GateAssessment(recommendation="review"),
        yes=False,
        is_review=True,
        confidence="medium",
        reasons=["test"],
        summary="Review — needs LP confirmation.",
    )


def test_untrusted_match_keeps_screened_name_not_matched_name():
    extraction = CrmLeadExtraction(
        investor_name="Adeel Hussain",
        investor_details="Wrong LLM name.",
    )
    brief = {
        "input_name": "Adeo Ressi",
        "matched_name": "Adeel Hussain",
        "match_untrusted": True,
        "match_method": "fuzzy_review",
        "match_confidence": 0.88,
        "allocator_id": "wrong-uuid",
        "icp_tier": "tier_1",
        "icp_fit_score": 0.9,
    }
    merged = merge_extraction(extraction, brief, _result("Adeo Ressi"))
    assert merged["investor_name"] == "Adeo Ressi"
    assert merged["allocator_id"] is None
    assert merged["icp_tier"] is None


def test_exact_match_links_allocator():
    extraction = CrmLeadExtraction(investor_name="Adeo Ressi")
    brief = {
        "input_name": "Adeo Ressi",
        "matched_name": "Adeo Ressi",
        "match_untrusted": False,
        "match_method": "exact",
        "match_confidence": 1.0,
        "allocator_id": "good-uuid",
        "icp_tier": "tier_2",
    }
    merged = merge_extraction(extraction, brief, _result("Adeo Ressi"))
    assert merged["investor_name"] == "Adeo Ressi"
    assert merged["allocator_id"] == "good-uuid"
    assert merged["icp_tier"] == "tier_2"

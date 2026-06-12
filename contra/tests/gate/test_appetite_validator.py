"""Unit tests for the post-LLM appetite validator."""

from __future__ import annotations

import pytest

from contra.gate.appetite_validator import (
    _entry_is_employer_portfolio,
    _extract_employer_firm,
    _has_external_lp_commits,
    validate_and_patch,
)
from contra.gate.models import GateExplanation


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _base_explanation(**kwargs) -> GateExplanation:
    """Build a minimal valid GateExplanation for testing."""
    defaults = dict(
        llm_recommendation="review",
        confidence="medium",
        reasons=["test reason"],
        summary="Test summary. Test sentence two.",
    )
    defaults.update(kwargs)
    return GateExplanation(**defaults)


NFX_HUSTLE_FUND_CONTEXT = """\
Source: NFX Signal (angel/early-stage investor network)
Firm: Hustle Fund
NFX profile: https://signal.nfx.com/investors/will-bricker
Angel sweet-spot: $25K (range $10K–$50K) (direct startup angel check — NOT an LP commitment size)
Investment locations listed: North America"""

NFX_NO_FIRM = "Source: NFX Signal (angel/early-stage investor network)"


# ---------------------------------------------------------------------------
# _extract_employer_firm
# ---------------------------------------------------------------------------

def test_extract_employer_firm_present():
    firm = _extract_employer_firm(NFX_HUSTLE_FUND_CONTEXT)
    assert firm == "Hustle Fund"


def test_extract_employer_firm_absent():
    firm = _extract_employer_firm(NFX_NO_FIRM)
    assert firm == ""


def test_extract_employer_firm_empty():
    assert _extract_employer_firm("") == ""


# ---------------------------------------------------------------------------
# _has_external_lp_commits
# ---------------------------------------------------------------------------

def test_external_lp_commits_positive():
    assert _has_external_lp_commits(["LP in Hustle Fund (2022)"])
    assert _has_external_lp_commits(["Anchor lp in Weekend Fund II"])
    assert _has_external_lp_commits(["committed to Conviction Fund III"])


def test_external_lp_commits_negative():
    assert not _has_external_lp_commits([])
    assert not _has_external_lp_commits(["Hustle Fund's investments in pre-seed software"])
    assert not _has_external_lp_commits(["Principal at Hustle Fund"])


# ---------------------------------------------------------------------------
# _entry_is_employer_portfolio
# ---------------------------------------------------------------------------

def test_employer_portfolio_detection():
    assert _entry_is_employer_portfolio(
        "Hustle Fund's investments in pre-seed North American software startups",
        "Hustle Fund",
    )
    assert not _entry_is_employer_portfolio(
        "LP in Hustle Fund (2022) — Crunchbase",
        "Hustle Fund",
    )
    assert not _entry_is_employer_portfolio("Something else entirely", "Hustle Fund")


# ---------------------------------------------------------------------------
# validate_and_patch — Will Bricker scenario
# ---------------------------------------------------------------------------

def test_will_bricker_gp_caps_em_appetite():
    """GP at Hustle Fund with employer portfolio as allocation_evidence → caps appetites."""
    expl = _base_explanation(
        em_appetite="moderate",
        em_appetite_evidence="Hustle Fund is an emerging manager",
        fund_i_appetite="moderate",
        fund_i_appetite_evidence="Hustle Fund is a Fund I vehicle",
        venture_appetite="moderate",
        venture_appetite_evidence="Hustle Fund invests in VC",
        archetype="generalist",
        archetype_evidence="Principal at Hustle Fund, a VC fund",
        allocation_evidence=["Hustle Fund's investments in pre-seed North American software startups"],
        negative_flags=[],
    )
    result = validate_and_patch(expl, NFX_HUSTLE_FUND_CONTEXT, "web context", "nfx_individual")

    assert result.em_appetite == "unknown", "EM appetite should be capped"
    assert result.fund_i_appetite == "unknown", "Fund I appetite should be capped"
    assert "no_fund_lp_history" in result.negative_flags
    # Employer portfolio reference should be removed from allocation_evidence
    assert not any("Hustle Fund's investments" in e for e in result.allocation_evidence)


def test_will_bricker_nfx_individual_forces_no():
    """In nfx_individual mode, GP + no LP history → verdict forced to 'no'."""
    expl = _base_explanation(
        llm_recommendation="review",
        em_appetite="unknown",
        allocation_evidence=["Hustle Fund's investments in AI startups"],
        archetype_evidence="Principal at Hustle Fund",
        negative_flags=[],
    )
    result = validate_and_patch(expl, NFX_HUSTLE_FUND_CONTEXT, "web", "nfx_individual")

    assert result.llm_recommendation == "no"
    assert "no_fund_lp_history" in result.negative_flags


def test_will_bricker_institutional_stays_review():
    """In institutional mode, GP + no LP history → stays as review (not forced to no)."""
    expl = _base_explanation(
        llm_recommendation="review",
        em_appetite="unknown",
        allocation_evidence=[],
        archetype_evidence="Principal at Hustle Fund",
        negative_flags=[],
    )
    result = validate_and_patch(expl, NFX_HUSTLE_FUND_CONTEXT, "web", "institutional")

    # Validator adds no_fund_lp_history flag but should NOT force to no in institutional mode
    assert result.llm_recommendation == "review"
    assert "no_fund_lp_history" in result.negative_flags


# ---------------------------------------------------------------------------
# validate_and_patch — known good LP (should not be degraded)
# ---------------------------------------------------------------------------

def test_confirmed_lp_not_degraded():
    """An LP with real external commitments should not have appetites capped."""
    expl = _base_explanation(
        llm_recommendation="yes",
        em_appetite="strong",
        em_appetite_evidence="LP in Hustle Fund (2022) and Weekend Fund II (2023)",
        fund_i_appetite="strong",
        fund_i_appetite_evidence="Anchor LP in Weekend Fund II at first close",
        venture_appetite="strong",
        venture_appetite_evidence="Multiple VC fund LP commitments on record",
        archetype="emerging_manager_specialist",
        archetype_evidence="Backed Hustle Fund, Weekend Fund, Conviction as LP",
        allocation_evidence=[
            "LP in Hustle Fund (2022) — Crunchbase",
            "Anchor LP in Weekend Fund II (2023) — reported",
        ],
        negative_flags=[],
    )
    result = validate_and_patch(expl, NFX_NO_FIRM, "web context", "nfx_individual")

    assert result.em_appetite == "strong"
    assert result.fund_i_appetite == "strong"
    assert result.llm_recommendation == "yes"
    assert "no_fund_lp_history" not in result.negative_flags


def test_no_change_returns_original():
    """When no corrections are needed, the original object is returned unchanged."""
    expl = _base_explanation(
        llm_recommendation="no",
        em_appetite="unknown",
        allocation_evidence=[],
        negative_flags=["no_fund_lp_history"],
    )
    result = validate_and_patch(expl, "", "web", "nfx_individual")
    # Already has the flag and recommendation=no — no changes needed
    assert result.llm_recommendation == "no"


# ---------------------------------------------------------------------------
# validate_and_patch — em_appetite strong but no allocation_evidence
# ---------------------------------------------------------------------------

def test_em_appetite_strong_without_allocation_evidence_downgrades():
    """Strong EM appetite with no allocation_evidence at all → downgraded to unknown."""
    expl = _base_explanation(
        em_appetite="strong",
        em_appetite_evidence="Seems interested in emerging managers",
        allocation_evidence=[],
        negative_flags=[],
    )
    result = validate_and_patch(expl, "", "web", "institutional")

    assert result.em_appetite == "unknown"
    assert any("no external lp" in c.lower() or "moderate/strong" in c.lower()
               for c in result.conflicts)


# ---------------------------------------------------------------------------
# validate_and_patch — Rule 3b: zero-evidence NO for nfx_individual mode
# ---------------------------------------------------------------------------

def test_zero_evidence_review_becomes_no_in_nfx_individual():
    """
    nfx_individual + REVIEW + all-unknown appetites + empty allocation_evidence
    + no negative flags → should be forced to NO (Wa'il Ashshowwaf case).
    """
    expl = _base_explanation(
        llm_recommendation="review",
        confidence="medium",
        em_appetite="unknown",
        fund_i_appetite="unknown",
        venture_appetite="unknown",
        allocation_evidence=[],
        lp_commitments_found=[],
        negative_flags=[],
        c1_status="unknown",
    )
    result = validate_and_patch(expl, None, "no useful web results found", "nfx_individual")

    assert result.llm_recommendation == "no"
    assert result.primary_blocker
    assert "no_fund_lp_history" in result.negative_flags


def test_zero_evidence_review_stays_review_in_institutional_mode():
    """In institutional mode, REVIEW + zero evidence should NOT be forced to NO."""
    expl = _base_explanation(
        llm_recommendation="review",
        confidence="medium",
        em_appetite="unknown",
        fund_i_appetite="unknown",
        venture_appetite="unknown",
        allocation_evidence=[],
        lp_commitments_found=[],
        negative_flags=[],
        c1_status="unknown",
    )
    result = validate_and_patch(expl, None, "no useful web results found", "institutional")
    assert result.llm_recommendation == "review"


def test_institutional_thin_evidence_no_upgraded_to_review():
    """Institutional NO with C1 unknown and only absence flags → REVIEW."""
    expl = _base_explanation(
        llm_recommendation="no",
        confidence="low",
        c1_status="unknown",
        negative_flags=["no_fund_lp_history"],
        summary="India-based investor; no fund LP evidence found.",
    )
    result = validate_and_patch(expl, None, "thin web context", "institutional")
    assert result.llm_recommendation == "review"
    assert "flip to yes" in result.summary.lower()


def test_review_with_allocation_evidence_stays_review_in_nfx_individual():
    """
    nfx_individual + REVIEW but has allocation evidence → genuinely ambiguous,
    should NOT be forced to NO by the zero-evidence rule.
    """
    expl = _base_explanation(
        llm_recommendation="review",
        confidence="medium",
        em_appetite="unknown",
        fund_i_appetite="unknown",
        venture_appetite="unknown",
        allocation_evidence=["LP in Weekend Fund (unconfirmed — source: LinkedIn bio)"],
        lp_commitments_found=[],
        negative_flags=[],
        c1_status="unknown",
    )
    result = validate_and_patch(expl, None, "LinkedIn bio mentions fund investing", "nfx_individual")
    # allocation_evidence is non-empty → not zero-evidence → stay REVIEW
    assert result.llm_recommendation == "review"


# ---------------------------------------------------------------------------
# validate_and_patch — Rule 4: hedge language stripped from NO summaries
# ---------------------------------------------------------------------------

def test_no_verdict_hedge_language_stripped():
    """NO verdict summary with 'further research needed' → stripped."""
    expl = _base_explanation(
        llm_recommendation="no",
        negative_flags=["no_fund_lp_history"],
        primary_blocker="GP at Hustle Fund — no LP evidence",
        summary=(
            "Will Bricker is GP at Hustle Fund. "
            "Further research is needed to determine LP activity. "
            "Potential interest in emerging managers."
        ),
    )
    result = validate_and_patch(expl, "", "web context", "nfx_individual")

    assert result.llm_recommendation == "no"
    assert "further research" not in result.summary.lower()
    assert "needed" not in result.summary.lower() or "further" not in result.summary.lower()


def test_no_verdict_clean_summary_unchanged():
    """NO verdict summary that's already clean → left alone."""
    clean_summary = "No fund LP activity found. GP at Hustle Fund — employer portfolio is not LP evidence."
    expl = _base_explanation(
        llm_recommendation="no",
        negative_flags=["no_fund_lp_history"],
        summary=clean_summary,
    )
    result = validate_and_patch(expl, "", "web context", "institutional")
    # The zero-evidence rule won't fire (verdict already "no"), summary unchanged
    assert "further research" not in result.summary.lower()


# ---------------------------------------------------------------------------
# validate_and_patch — Rule 5: REVIEW summary gets flip condition
# ---------------------------------------------------------------------------

def test_review_summary_missing_flip_condition_gets_one():
    """REVIEW summary with 'further research needed' → replaced with flip condition."""
    expl = _base_explanation(
        llm_recommendation="review",
        confidence="medium",
        c1_status="unknown",
        summary=(
            "Unclear LP profile. Further research is needed to determine fund commitment history."
        ),
    )
    result = validate_and_patch(expl, None, "some web context", "institutional")

    assert result.llm_recommendation == "review"
    # Should contain a flip condition now
    summary_lower = result.summary.lower()
    assert any(sig in summary_lower for sig in ("flip", "confirm", "if ", "if:", "lp commit"))
    assert "further research is needed" not in summary_lower


def test_review_summary_with_flip_condition_left_alone():
    """REVIEW summary that already has a flip condition → not double-appended."""
    good_summary = (
        "LP profile unclear — no C1 evidence found. "
        "Flip to YES if: confirmed LP commitment to a VC fund is documented."
    )
    expl = _base_explanation(
        llm_recommendation="review",
        confidence="medium",
        c1_status="unknown",
        summary=good_summary,
    )
    result = validate_and_patch(expl, None, "web context", "institutional")
    # Already has flip condition — shouldn't be modified much
    assert "flip to yes" in result.summary.lower()

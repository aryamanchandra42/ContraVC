"""Unit tests for screening_mode-aware evaluator logic."""

from __future__ import annotations

import pytest

from contra.gate.evaluator import apply_appetite_adjustments
from contra.gate.models import AppetiteProfile


def _appetite(**kwargs) -> AppetiteProfile:
    return AppetiteProfile(**kwargs)


# ---------------------------------------------------------------------------
# apply_appetite_adjustments — screening_mode=nfx_individual
# ---------------------------------------------------------------------------

def test_nfx_no_fund_lp_forces_no():
    """In nfx_individual mode, no_fund_lp_history in flags → always NO."""
    ap = _appetite(negative_flags=["no_fund_lp_history"])
    assert apply_appetite_adjustments("review", ap, "nfx_individual") == "no"
    assert apply_appetite_adjustments("yes", ap, "nfx_individual") == "no"


def test_nfx_no_fund_lp_already_no_stays_no():
    ap = _appetite(negative_flags=["no_fund_lp_history"])
    assert apply_appetite_adjustments("no", ap, "nfx_individual") == "no"


def test_institutional_no_fund_lp_does_not_force_no():
    """In institutional mode, no_fund_lp_history keeps REVIEW (absence ≠ confirmed misfit)."""
    ap = _appetite(negative_flags=["no_fund_lp_history"])
    assert apply_appetite_adjustments("review", ap, "institutional") == "review"
    assert apply_appetite_adjustments("yes", ap, "institutional") == "review"


def test_strong_negatives_pe_only():
    """pe_only is a strong negative in both modes."""
    ap = _appetite(negative_flags=["pe_only"])
    assert apply_appetite_adjustments("yes", ap, "institutional") == "review"
    assert apply_appetite_adjustments("review", ap, "institutional") == "no"


def test_angel_only_strong_negative():
    """angel_only now treated as strong negative."""
    ap = _appetite(negative_flags=["angel_only"])
    assert apply_appetite_adjustments("yes", ap, "nfx_individual") == "no"
    assert apply_appetite_adjustments("review", ap, "institutional") == "no"


def test_soft_negative_only_downgrades_yes():
    """Soft negatives (not in _STRONG_NEGATIVES) only pull yes→review."""
    ap = _appetite(negative_flags=["min_check_too_large"])
    assert apply_appetite_adjustments("yes", ap, "institutional") == "review"
    assert apply_appetite_adjustments("review", ap, "institutional") == "review"


def test_no_appetite_no_change():
    assert apply_appetite_adjustments("yes", None, "nfx_individual") == "yes"
    assert apply_appetite_adjustments("review", None, "institutional") == "review"


def test_already_no_not_touched():
    ap = _appetite(negative_flags=["pe_only"])
    assert apply_appetite_adjustments("no", ap, "institutional") == "no"

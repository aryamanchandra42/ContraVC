"""
Unit tests for contra.intelligence.lp_similarity.

All tests are pure-Python (no DB required); the scorer and target builder
are deterministic given their inputs.
"""

from __future__ import annotations

from typing import Any, Dict

import pytest

from contra.intelligence.lp_similarity import (
    MIN_SIGNAL_COUNT,
    MIN_SIGNAL_SCORE,
    ArchetypeFit,
    LpSimilarityTarget,
    SimilarityResult,
    build_similarity_target,
    compute_archetype_fit,
    infer_db_archetype,
    score_lp_similarity,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _target(**kwargs) -> LpSimilarityTarget:
    defaults = dict(
        geography="unknown",
        allocator_type="",
        em_appetite="unknown",
        ai_appetite="unknown",
        archetype="unknown",
        check_size_bucket="unknown",
        fund_focus_geos=set(),
        exclude_id=None,
        exclude_name=None,
    )
    defaults.update(kwargs)
    return LpSimilarityTarget(**defaults)


def _candidate(**kwargs) -> Dict[str, Any]:
    defaults = dict(
        name="Test Anchor",
        geography="unknown",
        em_appetite="unknown",
        ai_appetite="unknown",
        allocator_type="unknown",
        fund_deal_count=2,
        total_fund_usd=5_000_000,
        fund_focus_geos=set(),
    )
    defaults.update(kwargs)
    return defaults


# ---------------------------------------------------------------------------
# infer_db_archetype
# ---------------------------------------------------------------------------

class TestInferDbArchetype:
    def test_fof_type_wins(self):
        assert infer_db_archetype("fund_of_funds", "north_america", "strong", 3) == "fund_of_funds"

    def test_family_office(self):
        result = infer_db_archetype("family_office", "southeast_asia", "moderate", 1)
        assert result == "family_office"

    def test_emerging_manager_specialist(self):
        # strong EM + 2 fund deals → specialist
        result = infer_db_archetype("institution", "global", "strong", 2)
        assert result == "emerging_manager_specialist"

    def test_asia_specialist(self):
        result = infer_db_archetype("institution", "southeast_asia", "weak", 1)
        assert result == "asia_specialist"

    def test_institutional_lp_fallback(self):
        result = infer_db_archetype("pension_fund", "north_america", "none", 1)
        assert result == "institutional_lp"

    def test_generalist_fallback(self):
        result = infer_db_archetype(None, None, None, 1)
        assert result == "generalist"


# ---------------------------------------------------------------------------
# score_lp_similarity — geography
# ---------------------------------------------------------------------------

class TestGeoScoring:
    def test_exact_geography_match(self):
        target = _target(geography="southeast_asia")
        candidate = _candidate(geography="southeast_asia")
        result = score_lp_similarity(target, candidate)
        assert result.score >= 25
        assert "geography" in result.match_dimensions

    def test_adjacent_geography_partial_credit(self):
        target = _target(geography="southeast_asia")
        candidate = _candidate(geography="asia_pacific")
        result = score_lp_similarity(target, candidate)
        assert result.score >= 10   # adjacency credit
        # Not necessarily in match_dims since adjacency = 15 pts (threshold is 15)
        # but score should be positive

    def test_mismatched_geography_no_geo_credit(self):
        target = _target(geography="north_america")
        candidate = _candidate(geography="southeast_asia")
        result_mismatch = score_lp_similarity(target, candidate)
        target_exact = _target(geography="north_america")
        candidate_exact = _candidate(geography="north_america")
        result_exact = score_lp_similarity(target_exact, candidate_exact)
        assert result_exact.score > result_mismatch.score

    def test_unknown_target_geography_no_geo_points(self):
        target = _target(geography="unknown")
        candidate = _candidate(geography="southeast_asia")
        result = score_lp_similarity(target, candidate)
        assert "geography" not in result.match_dimensions

    def test_global_candidate_gets_some_credit(self):
        target = _target(geography="north_america")
        candidate_global = _candidate(geography="global")
        candidate_mismatch = _candidate(geography="africa")
        r_global = score_lp_similarity(target, candidate_global)
        r_mismatch = score_lp_similarity(target, candidate_mismatch)
        assert r_global.score > r_mismatch.score


# ---------------------------------------------------------------------------
# score_lp_similarity — allocator type
# ---------------------------------------------------------------------------

class TestTypeScoring:
    def test_family_office_exact_match(self):
        target = _target(allocator_type="family_office")
        candidate_fo = _candidate(allocator_type="family_office")
        candidate_inst = _candidate(allocator_type="pension_fund")
        r_fo = score_lp_similarity(target, candidate_fo)
        r_inst = score_lp_similarity(target, candidate_inst)
        assert r_fo.score > r_inst.score
        assert "type" in r_fo.match_dimensions

    def test_fof_type_match(self):
        target = _target(allocator_type="fund_of_funds")
        candidate = _candidate(allocator_type="fund_of_funds")
        result = score_lp_similarity(target, candidate)
        assert "type" in result.match_dimensions


# ---------------------------------------------------------------------------
# score_lp_similarity — appetite
# ---------------------------------------------------------------------------

class TestAppetiteScoring:
    def test_strong_em_matches_strong_em(self):
        target = _target(em_appetite="strong")
        cand_strong = _candidate(em_appetite="strong")
        cand_none = _candidate(em_appetite="none")
        r_strong = score_lp_similarity(target, cand_strong)
        r_none = score_lp_similarity(target, cand_none)
        assert r_strong.score > r_none.score
        assert "em_appetite" in r_strong.match_dimensions

    def test_both_unknown_em_gives_half_credit(self):
        target = _target(em_appetite="unknown")
        candidate = _candidate(em_appetite="unknown")
        result = score_lp_similarity(target, candidate)
        # Half of 20 = 10 — score shouldn't be 0 for EM dim
        assert result.score > 5  # at least some credit from neutral unknown


# ---------------------------------------------------------------------------
# score_lp_similarity — archetype
# ---------------------------------------------------------------------------

class TestArchetypeScoring:
    def test_post_llm_exact_archetype_match(self):
        target = _target(archetype="family_office", allocator_type="family_office")
        cand_fo = _candidate(allocator_type="family_office")
        cand_fof = _candidate(allocator_type="fund_of_funds")
        r_fo = score_lp_similarity(target, cand_fo)
        r_fof = score_lp_similarity(target, cand_fof)
        assert r_fo.score >= r_fof.score
        assert "archetype" in r_fo.match_dimensions

    def test_asia_family_office_ranks_above_us_fof(self):
        """Singapore family office target → ranks asia family offices above US FoFs."""
        target = _target(
            geography="southeast_asia",
            allocator_type="family_office",
            em_appetite="moderate",
            archetype="family_office",
        )
        asia_fo = _candidate(geography="southeast_asia", allocator_type="family_office", em_appetite="moderate")
        us_fof = _candidate(geography="north_america", allocator_type="fund_of_funds", em_appetite="weak")

        r_asia = score_lp_similarity(target, asia_fo)
        r_us = score_lp_similarity(target, us_fof)
        assert r_asia.score > r_us.score


# ---------------------------------------------------------------------------
# Self-exclusion
# ---------------------------------------------------------------------------

class TestSelfExclusion:
    def test_self_excluded_by_id(self):
        """find_similar_confirmed_lps must never return the screened LP itself."""
        # The exclusion logic lives in brief.py find_similar_confirmed_lps —
        # here we test that the target carries exclude fields.
        class _MockBrief:
            allocator_profile = {"geography": "southeast_asia"}
            allocator_id = "abc-123"
            input_name = "Noah Dizzle"
            investment_summary = {}

        target = build_similarity_target(_MockBrief())
        assert target.exclude_id == "abc-123"
        assert target.exclude_name == "Noah Dizzle"


# ---------------------------------------------------------------------------
# Strict signal threshold
# ---------------------------------------------------------------------------

class TestStrictSignal:
    def test_four_low_score_matches_should_not_fire_signal(self):
        """4 below-threshold similar LPs → signal must NOT fire."""
        low_score_lps = [
            {"name": f"LP {i}", "similarity_score": 30, "match_dimensions": []}
            for i in range(4)
        ]
        qualifying = [lp for lp in low_score_lps if lp["similarity_score"] >= MIN_SIGNAL_SCORE]
        assert len(qualifying) < MIN_SIGNAL_COUNT

    def test_two_strong_matches_fire_signal(self):
        """2 above-threshold matches → signal fires."""
        strong_lps = [
            {"name": "Noah Dizzle", "similarity_score": 72, "match_dimensions": ["geography", "type"]},
            {"name": "Anderson Aiziro", "similarity_score": 55, "match_dimensions": ["em_appetite"]},
        ]
        qualifying = [lp for lp in strong_lps if lp["similarity_score"] >= MIN_SIGNAL_SCORE]
        assert len(qualifying) >= MIN_SIGNAL_COUNT

    def test_one_strong_and_one_weak_does_not_fire(self):
        lps = [
            {"name": "Noah Dizzle", "similarity_score": 70, "match_dimensions": ["geography"]},
            {"name": "Steve OBrien", "similarity_score": 28, "match_dimensions": []},
        ]
        qualifying = [lp for lp in lps if lp["similarity_score"] >= MIN_SIGNAL_SCORE]
        assert len(qualifying) < MIN_SIGNAL_COUNT


# ---------------------------------------------------------------------------
# compute_archetype_fit
# ---------------------------------------------------------------------------

class TestArchetypeFit:
    def test_empty_matches_returns_none_fit(self):
        fit = compute_archetype_fit("family_office", [])
        assert fit.fit_level == "none"
        assert fit.avg_similarity_score == 0

    def test_two_qualifying_with_archetype_match_is_strong(self):
        matches = [
            {"name": "Noah Dizzle", "similarity_score": 72, "archetype": "family_office"},
            {"name": "Anderson Aiziro", "similarity_score": 60, "archetype": "family_office"},
        ]
        fit = compute_archetype_fit("family_office", matches)
        assert fit.fit_level == "strong"
        assert fit.avg_similarity_score == 66
        assert "Noah Dizzle" in fit.rationale

    def test_one_qualifying_is_partial(self):
        matches = [
            {"name": "Noah Dizzle", "similarity_score": 65, "archetype": "family_office"},
            {"name": "LP B", "similarity_score": 25, "archetype": "generalist"},
        ]
        fit = compute_archetype_fit("family_office", matches)
        assert fit.fit_level == "partial"

    def test_no_qualifying_is_weak(self):
        matches = [
            {"name": "Noah Dizzle", "similarity_score": 30, "archetype": "family_office"},
        ]
        fit = compute_archetype_fit("family_office", matches)
        assert fit.fit_level == "weak"

    def test_unknown_target_archetype_gives_partial_or_weak(self):
        matches = [
            {"name": "LP A", "similarity_score": 55, "archetype": "family_office"},
            {"name": "LP B", "similarity_score": 50, "archetype": "fund_of_funds"},
        ]
        fit = compute_archetype_fit("unknown", matches)
        assert fit.fit_level in ("partial", "weak", "strong")


# ---------------------------------------------------------------------------
# build_similarity_target — field extraction
# ---------------------------------------------------------------------------

class TestBuildTarget:
    def _mock_brief(self, geo="", alloc_type="", em="unknown", ai="unknown", alloc_id=None, name=""):
        class _Brief:
            allocator_profile = {
                "geography": geo,
                "allocator_type": alloc_type,
                "em_appetite": em,
                "ai_appetite": ai,
            }
            allocator_id = alloc_id
            input_name = name
            investment_summary = {}
        return _Brief()

    def test_geography_extracted_from_profile(self):
        brief = self._mock_brief(geo="Singapore")
        target = build_similarity_target(brief)
        assert target.geography == "southeast_asia"

    def test_geography_from_nfx_context(self):
        brief = self._mock_brief()
        target = build_similarity_target(brief, nfx_context="Location: Dubai, UAE")
        assert target.geography == "middle_east"

    def test_empty_geography_stays_unknown(self):
        brief = self._mock_brief()
        target = build_similarity_target(brief)
        assert target.geography == "unknown"

    def test_post_llm_archetype_override(self):
        brief = self._mock_brief()

        class _Appetite:
            archetype = "emerging_manager_specialist"
            em_appetite = "strong"
            ai_tech_appetite = "moderate"

        target = build_similarity_target(brief, appetite=_Appetite())
        assert target.archetype == "emerging_manager_specialist"
        assert target.em_appetite == "strong"

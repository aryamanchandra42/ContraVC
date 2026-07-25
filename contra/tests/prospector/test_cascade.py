"""
Tests for the deterministic cascade stages (2, 3, 4).

These stages cost nothing to run and decide what the paid stages get to see, so
they are the ones where a silent logic error is most expensive: Stage 3 rejecting
good LPs wastes the whole pipeline, and Stage 4 accepting weak evidence puts junk
in the CRM.

The negation tests are not hypothetical. Measured against the 158 labelled
allocators in `icp_scores`, naive substring matching hard-excluded 78 of the 112
client-APPROVED LPs, because the E8 phrase "no emerging manager" matches inside
"No emerging manager evidence in scoring text".
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from contra.prospector.corroborate import (
    _first_party,
    _gate_priority,
    _name_variants,
    find_commitment_span,
)
from contra.prospector.harvest import (
    _doc_score,
    _fold_acronyms,
    _initials,
    _normalize_span,
)
from contra.prospector.models import Candidate, Corroboration, domain_of
from contra.prospector.prerank import prerank, score_candidate
from contra.prospector.resolve import _is_non_entity, _span_role_conflict
from contra.prospector.seeds import queries_for_seed


# ---------------------------------------------------------------------------
# Stage 3 — negation and meta guards
# ---------------------------------------------------------------------------

class TestPrerankNegationGuards:
    def test_meta_commentary_is_not_an_exclusion(self):
        """The bug that hard-excluded 78 of 112 approved LPs."""
        res = score_candidate(
            "Forbes Family Trust",
            "VC fund evidence: fund, vc, venture capital. "
            "No emerging manager evidence in scoring text or client comments.",
        )
        assert not res.excluded, f"falsely excluded: {res.hard_exclusion}"

    def test_negated_phrase_is_not_an_exclusion(self):
        res = score_candidate(
            "Acme Family Office",
            "The family office has no private equity focus and backs venture funds.",
        )
        assert not res.excluded, f"falsely excluded: {res.hard_exclusion}"

    def test_real_disqualifier_still_fires(self):
        res = score_candidate("JMCR Partners", "They have a private equity focus.")
        assert res.excluded
        assert "private equity focus" in res.hard_exclusion

    def test_self_negated_phrase_still_fires(self):
        """E8/E10 phrases carry their own polarity and must not be guard-skipped."""
        res = score_candidate(
            "Sakal Group",
            "The group does not invest in funds; it makes direct investments only.",
        )
        assert res.excluded

    def test_negated_positive_check_does_not_score(self):
        """'does not back emerging managers' must not count as EM appetite."""
        negative = score_candidate(
            "Alpha Trust",
            "Alpha Trust commits to venture funds but does not back emerging managers.",
        )
        assert negative.checks["c2_new_managers"]["met"] is False

    def test_negation_does_not_cross_a_clause_boundary(self):
        """A negator in an earlier clause must not suppress a later assertion."""
        res = score_candidate(
            "Alpha Trust",
            "Alpha Trust does not invest in real estate; it backs emerging managers "
            "in venture funds.",
        )
        assert res.checks["c2_new_managers"]["met"] is True
        assert not res.excluded

    def test_absent_region_does_not_score_geography(self):
        res = score_candidate(
            "Beta Capital",
            "Beta Capital is an LP in venture funds. "
            "No qualifying region (Asia/NA/ME) in scoring text.",
        )
        assert res.checks["c4_geography"]["met"] is False

    def test_sanctioned_jurisdiction_excluded(self):
        res = score_candidate(
            "Some Holding",
            "A fund investor headquartered in Iran committing to venture funds.",
        )
        assert res.excluded
        assert "sanctioned" in res.hard_exclusion


class TestPrerankScoring:
    def test_positive_candidate_scores_well(self):
        res = score_candidate(
            "Asia Growth Family Office",
            "Asia Growth Family Office committed to Fund I of an emerging manager "
            "focused on artificial intelligence across Southeast Asia.",
            entity_type="family office",
            geography="Singapore",
            doc_type="fund_close",
            confidence="high",
            source_diversity=3,
        )
        assert not res.excluded
        assert res.score >= 80
        for check in ("c1_fund_lp", "c2_new_managers", "c3_thesis", "c4_geography"):
            assert res.checks[check]["met"] is True, check

    def test_c1_required_any_gate(self):
        """C1 needs fund/vc/venture language, not merely an LP-shaped sentence."""
        res = score_candidate("Vague Entity", "An investor with a broad mandate.")
        assert res.checks["c1_fund_lp"]["met"] is False

    def test_source_diversity_increases_score(self):
        args = ("Gamma Office", "Gamma Office is a limited partner in a venture fund.")
        one = score_candidate(*args, source_diversity=1)
        three = score_candidate(*args, source_diversity=3)
        assert three.score > one.score

    def test_prerank_splits_and_ranks(self):
        strong = Candidate(
            name="Strong LP",
            span="Strong LP committed to Fund I, an emerging manager in AI across India.",
            doc_type="fund_close", confidence="high",
            domains=["a.com", "b.com", "c.com"],
        )
        weak = Candidate(name="Weak LP", span="Weak LP attended a conference.")
        excluded = Candidate(
            name="PE Shop",
            span="PE Shop has a private equity focus and commits to funds.",
        )

        survivors, dropped = prerank([weak, strong, excluded], cutoff=40)

        assert [c.name for c in survivors] == ["Strong LP"]
        assert {c.name for c in dropped} == {"Weak LP", "PE Shop"}
        assert all(c.status == "rejected" for c in dropped)
        assert all(c.stage == "prerank" for c in dropped)

    def test_ranking_is_descending(self):
        cands = [
            Candidate(name=f"LP {i}", span="limited partner in a venture fund, Asia",
                      domains=[f"{d}.com" for d in range(i)])
            for i in (1, 3, 2)
        ]
        survivors, _ = prerank(cands, cutoff=0)
        scores = [c.prerank_score for c in survivors]
        assert scores == sorted(scores, reverse=True)


# ---------------------------------------------------------------------------
# Stage 1 — extraction hygiene
# ---------------------------------------------------------------------------

class TestHarvestHygiene:
    @pytest.mark.parametrize("name,expected", [
        ("Japan Investment Corporation", "JIC"),
        ("International Finance Corporation", "IFC"),
        ("Bank of the West", "BW"),
        ("Invesco", ""),
    ])
    def test_initials(self, name, expected):
        assert _initials(name) == expected

    def test_acronym_folded_into_expanded_name(self):
        """Extractors emit both halves of "International Finance Corp (IFC)"."""
        full = Candidate(name="International Finance Corporation",
                         span="IFC committed to the fund.", domains=["a.com"])
        acro = Candidate(name="IFC", span="IFC is an LP.", domains=["b.com"])

        kept = _fold_acronyms([full, acro])

        assert [c.name for c in kept] == ["International Finance Corporation"]
        # The acronym's source counts as real diversity, not a duplicate.
        assert set(kept[0].domains) == {"a.com", "b.com"}

    def test_unrelated_acronym_survives(self):
        """An acronym with no expanded form present must not be dropped."""
        kept = _fold_acronyms([
            Candidate(name="PIF", span="PIF backed the fund."),
            Candidate(name="Temasek Holdings", span="Temasek is an LP."),
        ])
        assert {c.name for c in kept} == {"PIF", "Temasek Holdings"}

    def test_short_capitalised_word_is_not_an_acronym(self):
        kept = _fold_acronyms([
            Candidate(name="Invesco", span="Invesco is an LP."),
            Candidate(name="Iron Nations Ventures Endowment Sovereign Company Order",
                      span="x"),
        ])
        assert "Invesco" in {c.name for c in kept}

    def test_span_must_appear_in_document(self):
        """A hallucinated span is discarded; whitespace differences are tolerated."""
        doc = "Acme  Trust\ncommitted capital to the fund."
        assert _normalize_span("Acme Trust committed capital to the fund.", doc) != ""
        assert _normalize_span("Acme Trust is a sovereign wealth fund.", doc) == ""


class TestDocScoring:
    @staticmethod
    def _result(title, url="https://example.com/a", snippet=""):
        return SimpleNamespace(title=title, url=url, snippet=snippet, score=1.0)

    def test_close_announcement_outranks_listicle(self):
        """Listicles name famous mega-LPs; close announcements name in-ICP ones."""
        close = self._result(
            "Neon Fund announces first close; limited partners include family offices"
        )
        listicle = self._result("Top 100 largest limited partners — LP database")

        assert _doc_score(close) > _doc_score(listicle)

    def test_junk_domain_is_excluded_outright(self):
        junk = self._result("limited partners include", url="https://facebook.com/x")
        assert _doc_score(junk) < -100

    def test_penalised_page_is_deprioritised_not_excluded(self):
        """A run where everything looks like a listicle must still harvest."""
        listicle = self._result("Top 100 largest limited partners — LP database")
        assert -100 < _doc_score(listicle)

    @pytest.mark.parametrize("title", [
        # Both of these outranked a real Indonesian fund close in a measured run,
        # taking fetch slots and extraction calls to return no LP names at all.
        "The Limited Partnership Agreement: The VC Contract Nobody Reads",
        "The Nuts and Bolts of Your First Close | Hustle Fund",
        "What Startup Founders Need To Understand About VC Limited Partners",
        "How Does Hustle Fund Choose Startups?",
        "A Guide to Limited Partners and First Close Mechanics",
    ])
    def test_explainer_content_ranks_below_real_close(self, title):
        """
        How-to content scores high for the wrong reason: an article ABOUT limited
        partnerships is dense in LP vocabulary while naming no LP.
        """
        close = self._result(
            "Intudo Ventures announces final close; limited partners include family offices"
        )
        assert _doc_score(self._result(title)) < _doc_score(close)

    def test_fund_roundup_ranks_below_real_close(self):
        """A roundup of FUNDS names only GPs — every name dies at Stage 2."""
        close = self._result(
            "Intudo Ventures announces final close; limited partners include family offices"
        )
        roundup = self._result("Oldest Venture Capital Firms with Offices in Indonesia")
        assert _doc_score(roundup) < _doc_score(close)

    def test_dollar_amount_not_mistaken_for_explainer(self):
        """
        A "101" substring must not penalise "$101 million" — the educational marker
        has to be anchored, or real close announcements get demoted by their size.
        """
        close = self._result("Neon Fund closes $101 million; limited partners include")
        plain = self._result("Neon Fund closes $99 million; limited partners include")
        assert _doc_score(close) == _doc_score(plain)


# ---------------------------------------------------------------------------
# Stage 2 — identity
# ---------------------------------------------------------------------------

class TestResolveIdentity:
    @pytest.mark.parametrize("name", [
        "undisclosed", "various", "family offices", "limited partners",
        "institutional investors", "the fund", "n/a", "several",
    ])
    def test_placeholders_rejected(self, name):
        assert _is_non_entity(name) is True

    @pytest.mark.parametrize("name", [
        "Forbes Family Trust", "Lagoon Capital", "Bugshan Investment", "Jun Mao",
    ])
    def test_real_names_accepted(self, name):
        assert _is_non_entity(name) is False

    @pytest.mark.parametrize("span", [
        "Jane Doe is the managing partner of the new fund.",
        "He founded the fund in 2021.",
        "Acme Advisors acted as placement agent for the raise.",
        "The firm runs its own fund and does not invest as an LP.",
        # Corporate venture arms reached the gate in a live run and were rejected
        # there as the wrong entity type; this stage catches them for free.
        "Intel Capital is the corporate venture arm of Intel Corporation.",
        "Salesforce Ventures, the venture arm of Salesforce, invests in startups.",
    ])
    def test_gp_and_service_roles_rejected(self, span):
        assert _span_role_conflict(span) != ""

    @pytest.mark.parametrize("span", [
        "Acme Family Office is a limited partner in the fund.",
        "The trust committed capital to Fund I as an anchor investor.",
        # Fund-of-funds raise capital AND commit it onward, so fundraising language
        # cannot settle identity. These spans previously tripped a GP pattern here,
        # which killed the highest-priority LP type in the ICP spec at Stage 2 —
        # before the gate could weigh cheque size or emerging-manager appetite.
        "Top Tier raises capital from institutional investors to invest in funds.",
        "Horsley Bridge manages assets on behalf of pensions and endowments.",
    ])
    def test_lp_roles_accepted(self, span):
        assert _span_role_conflict(span) == ""

    def test_portfolio_company_rejected(self):
        span = "The fund invested in Startup Inc in its Series A round."
        assert "portfolio company" in _span_role_conflict(span)


# ---------------------------------------------------------------------------
# Stage 4 — the independence test
# ---------------------------------------------------------------------------

class TestCommitmentSpan:
    def test_finds_commitment_near_name(self):
        text = (
            "In other news, the weather was fine. Meridian Family Office is a "
            "limited partner in Neon Fund I, according to the announcement. "
            "Unrelated filler follows."
        )
        span = find_commitment_span("Meridian Family Office", text)
        assert span
        assert "Meridian Family Office" in span
        assert "limited partner" in span.lower()

    def test_returns_verbatim_text_from_source(self):
        text = "Apex Trust committed capital to the venture fund last year."
        span = find_commitment_span("Apex Trust", text)
        assert span.strip() in text

    def test_name_without_commitment_language_fails(self):
        text = "Meridian Family Office sponsored the conference dinner."
        assert find_commitment_span("Meridian Family Office", text) == ""

    def test_commitment_too_far_from_name_fails(self):
        text = (
            "Meridian Family Office was mentioned here. "
            + ("padding text. " * 60)
            + "Separately, someone is a limited partner in a fund."
        )
        assert find_commitment_span("Meridian Family Office", text) == ""

    def test_absent_name_fails(self):
        text = "Someone else is a limited partner in the fund."
        assert find_commitment_span("Meridian Family Office", text) == ""

    def test_corporate_suffix_variants_match(self):
        variants = _name_variants("Bugshan Investment Holdings LLC")
        assert any("Bugshan Investment" in v for v in variants)

    def test_suffix_stripped_name_still_matches_source(self):
        text = "Bugshan Investment is an anchor investor in the fund."
        assert find_commitment_span("Bugshan Investment Holdings LLC", text) != ""


class TestGatePriority:
    """The gate queue must be ordered by evidence, never by prerank score."""

    @staticmethod
    def _cand(name, domains, score):
        return Candidate(
            name=name, prerank_score=score,
            corroborations=[
                Corroboration(quote=f"{name} committed to a fund.",
                              url=f"https://{d}/x", domain=d)
                for d in domains
            ],
        )

    def test_more_sources_outranks_higher_prerank(self):
        """The live-run failure: score-60 GPs took every gate slot from real LPs."""
        gp = self._cand("Top Tier Capital Partners", ["a.com"], 60)
        lp = self._cand("The Rockefeller Foundation",
                        ["rockefellerfoundation.org", "arabellaadvisors.com"], 5)

        ranked = sorted([gp, lp], key=_gate_priority, reverse=True)

        assert ranked[0].name == "The Rockefeller Foundation"

    def test_first_party_breaks_the_tie(self):
        own = self._cand("Qualcomm Ventures", ["qualcomm.com", "other.com"], 5)
        third = self._cand("Some Trust", ["blog.com", "news.com"], 90)

        ranked = sorted([third, own], key=_gate_priority, reverse=True)

        assert ranked[0].name == "Qualcomm Ventures"

    def test_first_party_detection(self):
        assert _first_party(
            self._cand("The Rockefeller Foundation", ["rockefellerfoundation.org"], 0)
        ) is True
        assert _first_party(
            self._cand("The Rockefeller Foundation", ["techcrunch.com"], 0)
        ) is False


# ---------------------------------------------------------------------------
# Shared record behaviour
# ---------------------------------------------------------------------------

class TestCandidateModel:
    @pytest.mark.parametrize("url,expected", [
        ("https://www.pitchbook.com/profiles/x", "pitchbook.com"),
        ("http://techcrunch.com/2024/01/x", "techcrunch.com"),
        ("anthropic://web-search/abc123", ""),
        ("", ""),
    ])
    def test_domain_extraction(self, url, expected):
        assert domain_of(url) == expected

    def test_source_diversity_counts_distinct_domains(self):
        cand = Candidate(name="X", domains=["a.com", "a.com", "b.com", ""])
        assert cand.source_diversity == 2

    def test_analyst_facts_capped_at_two(self):
        """The gate scores at most 2 analyst facts; sending more is wasted."""
        cand = Candidate(name="X", corroborations=[
            Corroboration(quote=f"quote {i}", url=f"https://s{i}.com", domain=f"s{i}.com")
            for i in range(4)
        ])
        assert len(cand.analyst_facts()) == 2

    def test_analyst_fact_preserves_commitment_language(self):
        """The gate counts a fact as a signal only if LP language survives."""
        cand = Candidate(name="Apex Trust", corroborations=[Corroboration(
            quote="Apex Trust committed capital to Neon Fund I as a limited partner.",
            url="https://example.com/x", domain="example.com",
        )])
        fact = cand.analyst_facts()[0]
        assert "limited partner" in fact.lower()
        assert "committed" in fact.lower()

    def test_drop_records_stage_and_reason(self):
        cand = Candidate(name="X").drop("resolve", "name is a fund, not an LP")
        assert cand.stage == "resolve"
        assert cand.status == "rejected"
        assert cand.drop_reason == "name is a fund, not an LP"
        assert cand.verdict_reason == "name is a fund, not an LP"


# ---------------------------------------------------------------------------
# Seed geography rotation
# ---------------------------------------------------------------------------

class TestGeoRotation:
    TEMPLATE = {"seed_type": "query_template", "value": '{geo} venture fund first close'}
    GEOS = ["Alpha", "Beta", "Gamma", "Delta"]

    def _geo(self, rotation: int) -> str:
        query = queries_for_seed(self.TEMPLATE, self.GEOS, rotation=rotation)[0]
        return query.split()[0]

    def test_same_rotation_is_reproducible(self):
        assert self._geo(0) == self._geo(0)

    def test_rotation_advances_geography(self):
        """
        A template pinned to one geography can never cover new ground. A live run
        returned only Japanese and Korean names because every template was locked to
        its crc32 geography on every run.
        """
        assert self._geo(0) != self._geo(1)

    def test_rotation_covers_every_geography(self):
        seen = {self._geo(r) for r in range(len(self.GEOS))}
        assert seen == set(self.GEOS)

    def test_explicit_seed_geography_overrides_rotation(self):
        seed = dict(self.TEMPLATE, geography="Singapore")
        for rotation in range(4):
            assert queries_for_seed(seed, self.GEOS, rotation=rotation)[0].startswith("Singapore")

    def test_templates_still_differ_within_one_run(self):
        """Within a single run, distinct templates must not collapse onto one geo."""
        others = [
            {"seed_type": "query_template", "value": '{geo} emerging manager program'},
            {"seed_type": "query_template", "value": '{geo} family office limited partner'},
        ]
        geos = {queries_for_seed(s, self.GEOS, rotation=3)[0].split()[0] for s in others}
        geos.add(self._geo(3))
        assert len(geos) > 1

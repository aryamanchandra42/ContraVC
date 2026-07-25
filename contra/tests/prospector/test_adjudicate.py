"""
Tests for cascade Stage 5 — the promotion path.

The property under test is the one the user asked for: a YES verdict must land a
row in crm_leads, and it must get there through the gate rather than around it.

The gate itself is stubbed. What matters here is the wiring — that a YES is
promoted with its gate provenance, that a REVIEW is not, that an evidence-thin NO
earns a revisit date while a confirmed misfit does not, and that candidates beyond
the gate budget are deferred rather than lost.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from contra.prospector.adjudicate import _is_evidence_thin, adjudicate
from contra.prospector.models import Candidate, Corroboration


def _cand(name: str) -> Candidate:
    return Candidate(
        name=name,
        span=f"{name} committed capital to Fund I as a limited partner.",
        corroborations=[Corroboration(
            quote=f"{name} committed capital to Fund I as a limited partner.",
            url="https://example.com/x", domain="example.com",
        )],
    )


def _item(name: str, verdict: str, summary: str = "", reasons=None):
    return SimpleNamespace(
        investor_name=name, verdict=verdict, summary=summary or f"Gate says {verdict}",
        reasons=reasons or [], session_id=f"sess-{name}", confidence="high",
    )


@pytest.fixture
def stub_gate(monkeypatch):
    """Replace batch_gate_run with a scripted set of verdicts."""
    calls = {}

    def _install(items, *, promote_ok=True):
        def fake_batch(con, records, **kwargs):
            calls["records"] = records
            calls["kwargs"] = kwargs
            return SimpleNamespace(results=items)

        def fake_add_lead(con, session_id):
            calls.setdefault("promoted_sessions", []).append(session_id)
            if not promote_ok:
                raise ValueError("Already in CRM")
            return SimpleNamespace(lead_id=f"lead-{session_id}")

        monkeypatch.setattr("contra.gate.batch.batch_gate_run", fake_batch)
        monkeypatch.setattr("contra.crm.writer.add_lead_from_gate", fake_add_lead)
        return calls

    return _install


class TestPromotion:
    def test_yes_is_promoted_with_gate_provenance(self, stub_gate):
        calls = stub_gate([_item("Apex Trust", "yes")])
        cands = [_cand("Apex Trust")]

        gated, deferred, stats = adjudicate(None, cands)

        assert stats["yes"] == 1
        assert stats["promoted"] == 1
        assert gated[0].status == "promoted"
        assert gated[0].lead_id == "lead-sess-Apex Trust"
        assert gated[0].gate_verdict == "yes"
        assert gated[0].stage == "gate"
        # Promotion must go through the gate session, not a bare insert.
        assert calls["promoted_sessions"] == ["sess-Apex Trust"]

    def test_yes_stays_qualified_when_crm_insert_is_rejected(self, stub_gate):
        """A duplicate in CRM is not a run failure."""
        stub_gate([_item("Apex Trust", "yes")], promote_ok=False)

        gated, _, stats = adjudicate(None, [_cand("Apex Trust")])

        assert gated[0].gate_verdict == "yes"
        assert gated[0].status == "qualified"
        assert stats["promoted"] == 0

    def test_review_is_not_promoted(self, stub_gate):
        calls = stub_gate([_item("Maybe Trust", "review")])

        gated, _, stats = adjudicate(None, [_cand("Maybe Trust")])

        assert gated[0].status == "review"
        assert stats["promoted"] == 0
        assert "promoted_sessions" not in calls

    def test_institutional_mode_is_used(self, stub_gate):
        """nfx_individual would reject institutional LPs outright."""
        calls = stub_gate([_item("Apex Trust", "yes")])

        adjudicate(None, [_cand("Apex Trust")])

        assert calls["kwargs"]["screening_mode"] == "institutional"

    def test_only_commitment_quotes_are_sent_as_analyst_facts(self, stub_gate):
        """The gate scores at most 2 facts; context must not crowd out evidence."""
        calls = stub_gate([_item("Apex Trust", "yes")])
        cand = _cand("Apex Trust")
        cand.entity_type = "family office"
        cand.geography = "Singapore"

        adjudicate(None, [cand])

        facts = calls["records"][0].to_analyst_facts()
        assert len(facts) == 1
        assert "limited partner" in facts[0].lower()
        assert calls["records"][0].to_nfx_context_string() == ""


class TestNoVerdictHandling:
    def test_evidence_thin_no_gets_a_revisit_date(self, stub_gate):
        stub_gate([_item("Quiet Trust", "no",
                         summary="No fund LP history could be found for this entity.")])

        gated, _, stats = adjudicate(None, [_cand("Quiet Trust")])

        assert stats["no"] == 1
        assert gated[0].status == "rejected"
        assert gated[0].revisit_date, "absence of evidence should earn another look"

    def test_confirmed_misfit_no_is_permanent(self, stub_gate):
        stub_gate([_item("Buyout Co", "no",
                         summary="Buyout Co is a private equity only investor; "
                                 "does not invest in funds.")])

        gated, _, _ = adjudicate(None, [_cand("Buyout Co")])

        assert gated[0].revisit_date is None, "a confirmed misfit never changes"

    @pytest.mark.parametrize("summary,thin", [
        ("No fund LP history found.", True),
        ("Could not find evidence of fund commitments.", True),
        ("Insufficient data to confirm.", True),
        ("This is a PE-only buyout shop.", False),
        ("They are direct-only investors.", False),
        ("Already in CRM.", False),
    ])
    def test_evidence_thin_classification(self, summary, thin):
        assert _is_evidence_thin(_item("X", "no", summary=summary)) is thin


class TestBudget:
    def test_candidates_beyond_budget_are_deferred_not_lost(self, stub_gate):
        stub_gate([_item("A", "yes")])
        cands = [_cand("A"), _cand("B"), _cand("C")]

        gated, deferred, stats = adjudicate(None, cands, max_gate=1)

        assert [c.name for c in gated] == ["A"]
        assert [c.name for c in deferred] == ["B", "C"]
        assert all(c.status == "review" for c in deferred)
        assert all("deferred" in c.verdict_reason for c in deferred)

    def test_zero_budget_defers_everything(self, stub_gate):
        stub_gate([])

        gated, deferred, stats = adjudicate(None, [_cand("A")], max_gate=0)

        assert not gated
        assert len(deferred) == 1
        assert stats["gated"] == 0

    def test_missing_gate_result_does_not_crash(self, stub_gate):
        """A worker crash inside the batch must not lose the candidate."""
        stub_gate([])  # gate returned nothing for the candidate

        gated, _, stats = adjudicate(None, [_cand("Ghost")])

        assert stats["error"] == 1
        assert gated[0].status == "review"

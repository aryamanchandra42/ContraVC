"""
Stage 5 of the cascade — ADJUDICATE.

Owns the FINAL VERDICT, and delegates it entirely to the real LP gate. No second
opinion, no parallel scorecard: whatever `run_gate` decides is what the miner
records, so a mined lead and an analyst-screened lead mean exactly the same thing.

Why cold-mined names could not previously pass
----------------------------------------------
`gate/evaluator.py::evaluate` requires 2 of 9 signals. Five of those nine are
gated behind `no_db_record` — `icp_qualified`, `syndicate_fund_lp`,
`syndicate_upgrade`, `warm_path` and `benchmark_rank` all need an `allocators`
row. But the miner's own dedupe drops anything that matches `allocators`, so
every candidate reaching this stage is cold by construction and those five are
unreachable. That left three appetite signals and a weak precedent signal, and
the observed result was 10 `no` / 5 `review` / 0 `yes` across every gate review
ever recorded.

The unlock is that the gate already has a channel for evidence the database does
not hold: `analyst_facts`. `_eval_signals` scores `analyst_facts[:2]` as met when
the text contains LP-confirming language ("committed to", "limited partner",
"anchored", "fund i", "emerging manager"), which is precisely what a Stage 4
corroboration quote is. So two independently corroborated commitments reach the
2-signal bar through the designed path, and no threshold anywhere is loosened:
`apply_appetite_adjustments` and `verify_evidence` keep their full veto, hard
blocks still force NO, and manual screens in the LP Gate UI are untouched.

The tradeoff this creates is explicit: the entire false-positive burden now rests
on Stage 4's independence test. That is the right place for it — cheap,
single-purpose, deterministic and measured — but it is why Stage 4 refuses
provider syntheses and same-domain "confirmation".
"""

from __future__ import annotations

import logging
import os
from typing import Dict, List, Optional, Tuple

from contra.prospector.models import Candidate

logger = logging.getLogger(__name__)


def _max_gate() -> int:
    raw = os.environ.get("PROSPECTOR_MAX_GATE", "").strip()
    try:
        return max(0, int(raw)) if raw else 6
    except ValueError:
        return 6


def _delay_seconds() -> float:
    raw = os.environ.get("PROSPECTOR_GATE_DELAY_SEC", "").strip()
    try:
        return max(0.0, float(raw)) if raw else 2.0
    except ValueError:
        return 2.0


def _to_records(candidates: List[Candidate]):
    from contra.gate.batch_models import ProspectRecord

    return [
        ProspectRecord(
            investor_name=c.name,
            entity_type=c.entity_type or None,
            geography=c.geography or None,
            commitment_facts=c.analyst_facts(),
        )
        for c in candidates
    ]


def _promote(con, cand: Candidate) -> Optional[str]:
    """
    Insert a YES verdict as a CRM lead through the standard gate path.

    Uses `add_lead_from_gate` rather than a bare insert so the lead carries the
    same gate provenance an analyst-added lead does — verdict, confidence,
    summary, reasons and appetite profile. The 30-minute session is still warm
    because the gate ran moments ago in this same process.
    """
    from contra.crm.writer import add_lead_from_gate

    try:
        lead = add_lead_from_gate(con, cand.gate_session_id)
        return lead.lead_id
    except ValueError as exc:
        # Already in CRM, or session expired — neither is fatal to the run.
        logger.info("Prospector promote skipped for %s: %s", cand.name, exc)
        return None
    except Exception as exc:
        logger.warning("Prospector promote failed for %s: %s", cand.name, exc)
        return None


def _revisit_date(days: int = 90) -> str:
    from datetime import date, timedelta

    return (date.today() + timedelta(days=days)).isoformat()


def adjudicate(
    con,
    candidates: List[Candidate],
    *,
    promote: bool = True,
    max_gate: Optional[int] = None,
) -> Tuple[List[Candidate], List[Candidate], Dict[str, int]]:
    """
    Screen candidates through the LP gate and promote the passes.

    Returns (gated, deferred, stats). `deferred` are candidates that were within
    budget-order but not screened this run; they stay in the review queue rather
    than being lost.
    """
    budget = _max_gate() if max_gate is None else max_gate
    stats = {"gated": 0, "yes": 0, "review": 0, "no": 0, "error": 0, "promoted": 0}

    if not candidates or budget <= 0:
        for cand in candidates:
            cand.status = "review"
            cand.verdict_reason = (
                cand.verdict_reason or "Corroborated; awaiting gate budget"
            )
        return [], candidates, stats

    to_gate = candidates[:budget]
    deferred = candidates[budget:]
    for cand in deferred:
        cand.status = "review"
        cand.verdict_reason = f"{cand.verdict_reason} — deferred, gate budget reached"

    from contra.gate.batch import batch_gate_run

    report = batch_gate_run(
        con,
        _to_records(to_gate),
        source_type="prospector",
        delay_seconds=_delay_seconds(),
        compact_web=True,
        # Institutional, not nfx_individual: these are entities found in fund-close
        # documents, and nfx_individual would NO any GP-adjacent name outright.
        screening_mode="institutional",
    )

    by_name = {item.investor_name: item for item in report.results}

    for cand in to_gate:
        item = by_name.get(cand.name)
        if item is None:
            cand.status = "review"
            cand.verdict_reason = "Gate produced no result"
            stats["error"] += 1
            continue

        stats["gated"] += 1
        cand.advance("gate")
        cand.gate_verdict = item.verdict
        cand.gate_session_id = item.session_id or ""
        cand.gate_summary = item.summary or ""

        if item.verdict == "yes":
            stats["yes"] += 1
            cand.status = "qualified"
            cand.verdict_reason = item.summary or "Gate verdict: yes"
            if promote and cand.gate_session_id:
                lead_id = _promote(con, cand)
                if lead_id:
                    cand.lead_id = lead_id
                    cand.status = "promoted"
                    stats["promoted"] += 1

        elif item.verdict == "review":
            stats["review"] += 1
            cand.status = "review"
            cand.verdict_reason = item.summary or "Gate verdict: review"

        elif item.verdict == "no":
            stats["no"] += 1
            cand.status = "rejected"
            cand.verdict_reason = item.summary or "Gate verdict: no"
            # A NO for absence of evidence is not the same as a confirmed misfit.
            # Only the former earns another look, and Stage 2 honours the date.
            if _is_evidence_thin(item):
                cand.revisit_date = _revisit_date()

        else:  # skipped | error
            stats["error"] += 1
            cand.status = "review"
            cand.verdict_reason = item.summary or f"Gate {item.verdict}"

    return to_gate, deferred, stats


def _is_evidence_thin(item) -> bool:
    """
    True when a NO looks like "we could not find enough" rather than "this is a misfit".

    Mirrors the evaluator's own distinction between `_ABSENCE_FLAGS` and
    `_CONFIRMED_MISFIT_FLAGS`: absence of LP history is a reason to look again
    later, whereas a PE-only or direct-only mandate never changes.
    """
    text = " ".join(filter(None, [
        item.summary or "",
        " ".join(item.reasons or []),
    ])).lower()

    confirmed_misfit = (
        "pe-only", "pe only", "private equity only", "buyout",
        "direct-only", "direct only", "angel only", "angel-only",
        "does not invest in funds", "secondaries", "crypto-only",
        "already in", "blacklist",
    )
    if any(p in text for p in confirmed_misfit):
        return False

    thin = (
        "no fund lp history", "insufficient", "could not find", "no evidence",
        "unable to verify", "not enough", "no confirmed", "unclear",
    )
    return any(p in text for p in thin)

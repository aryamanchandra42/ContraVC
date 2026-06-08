"""Run gate: two-phase deterministic evaluation + LLM explain pass."""

from __future__ import annotations

import uuid
from typing import Dict, List, Optional

from contra.gate.evaluator import apply_appetite_adjustments, build_allocation_evidence, evaluate
from contra.gate.models import CoreGateCheck, GateAssessment, GateResult
from contra.gate.research import search_lp, search_lp_with_nfx
from contra.gate.session import GateSession, create_session
from contra.gate.verdict import explain, explain_hard_block
from contra.intelligence.brief import lookup
from contra.intelligence.navigator import run_drill_down


def _merge_core_gates(
    evaluator_gates: List[CoreGateCheck],
    llm_gates: List[CoreGateCheck],
) -> List[CoreGateCheck]:
    """
    Merge evaluator (DB-backed) core gates with the LLM's web-inferred assessment.

    Rule: the evaluator's status wins when it is a definite pass/fail (it is grounded
    in the ICP score or syndicate data). When the evaluator says 'unknown', defer to
    the LLM's probabilistic assessment from web research.
    """
    llm_by_gate: Dict[str, CoreGateCheck] = {g.gate: g for g in llm_gates}
    merged: List[CoreGateCheck] = []
    for ev in evaluator_gates:
        llm = llm_by_gate.get(ev.gate)
        if ev.status == "unknown" and llm is not None and llm.status != "unknown":
            # LLM filled a gap the evaluator could not resolve
            merged.append(CoreGateCheck(
                gate=ev.gate,
                status=llm.status,
                evidence=llm.evidence,
                source=llm.source if llm.source != "backend" else "web",
            ))
        else:
            merged.append(ev)
    return merged


def run_gate(
    con,
    name: str,
    analyst_facts: Optional[List[str]] = None,
    _session_id: Optional[str] = None,
    nfx_url: Optional[str] = None,
    compact_web: bool = False,
    nfx_context: Optional[str] = None,
) -> GateResult:
    """
    Full gate run — returns GateResult and registers a session for chat follow-up.

    Phase 1: build IntelligenceBrief (backend lookup)
    Phase 2: deterministic evaluation (evaluator.py)
    Phase 3: LLM explain pass (verdict.py) — also extracts web signal
    Phase 4: optional re-evaluation if web signal was found and signals_met < 2
    """
    analyst_facts = analyst_facts or []
    session_id = _session_id or uuid.uuid4().hex

    brief = lookup(con, name)

    # ----- Short-circuit for hard CRM block (no LLM needed) ----------------
    if brief.in_crm:
        assessment = evaluate(brief, analyst_facts)
        explanation = explain_hard_block(assessment, name)
        result = GateResult(
            session_id=session_id,
            lp_name=name,
            assessment=assessment,
            yes=False,
            is_review=False,
            confidence=explanation.confidence,
            reasons=explanation.reasons,
            backend_evidence=explanation.backend_evidence,
            online_evidence=explanation.online_evidence,
            conflicts=explanation.conflicts,
            summary=explanation.summary,
            db_queries_used=[],
            analyst_facts=analyst_facts,
        )
        _register_session(
            session_id, name, brief, "(already in CRM — web search skipped)",
            assessment, result, explanation,
        )
        return result

    # ----- Web research + drill-down ----------------------------------------
    # compact_web=True cuts web context from 2800→1200 chars (~40% fewer tokens)
    # used by the batch runner to stay under Groq free-tier TPD limits
    max_chars = 1200 if compact_web else 2800
    if nfx_url:
        web_context, _urls = search_lp_with_nfx(name, nfx_url=nfx_url, max_chars=max_chars)
    else:
        web_context, _urls = search_lp(name, max_chars=max_chars)
    drill_results = run_drill_down(con, brief)
    allocation_evidence = build_allocation_evidence(brief)

    # ----- Phase 1: deterministic assessment (pre-appetite) -----------------
    assessment = evaluate(brief, analyst_facts)

    # ----- Phase 2: LLM explain pass — infers appetite ----------------------
    explanation = explain(
        name=name,
        brief=brief,
        assessment=assessment,
        web_context=web_context,
        drill_results=drill_results,
        analyst_facts=analyst_facts,
        allocation_evidence=allocation_evidence,
        nfx_context=nfx_context,
    )

    # ----- Re-eval with the inferred appetite -------------------------------
    # The appetite profile adds graded appetite signals (toward the >=2 bar) and
    # drives soft negative/archetype downgrades. The DB allocation evidence is
    # carried into the profile as the audit trail behind the inference.
    appetite = explanation.to_appetite_profile(
        allocation_evidence=[allocation_evidence] if allocation_evidence else [],
    )
    assessment = evaluate(brief, analyst_facts, appetite=appetite)

    # ----- LLM is the primary decision-maker --------------------------------
    # Hard blocks always win; otherwise the LLM's holistic call decides, then soft
    # appetite/negative adjustments may nudge it down (never up, never past a block).
    if assessment.hard_blocks:
        final_rec = "no"
    else:
        final_rec = apply_appetite_adjustments(explanation.llm_recommendation, appetite)

    # Merge evaluator's DB-backed core gates with the LLM's web-inferred ones
    merged_core_gates = _merge_core_gates(assessment.core_gates, explanation.llm_core_gates())
    assessment = assessment.model_copy(update={
        "recommendation": final_rec,
        "core_gates": merged_core_gates,
        "appetite": appetite,
    })

    # ----- Assemble result --------------------------------------------------
    db_queries = [d.get("template_id", "?") for d in drill_results]

    result = GateResult(
        session_id=session_id,
        lp_name=name,
        assessment=assessment,
        yes=final_rec == "yes",
        is_review=final_rec == "review",
        confidence=explanation.confidence,
        reasons=explanation.reasons,
        backend_evidence=explanation.backend_evidence,
        online_evidence=explanation.online_evidence,
        conflicts=explanation.conflicts,
        summary=explanation.summary,
        db_queries_used=db_queries,
        appetite=appetite,
        analyst_facts=analyst_facts,
    )

    if final_rec in ("yes", "review"):
        from contra.gate.persist import persist_gate_findings

        new_id = persist_gate_findings(
            con,
            name=name,
            brief=brief,
            explanation=explanation,
            web_context=web_context,
            verdict=final_rec,
            session_id=session_id,
        )
        if new_id and not brief.allocator_id:
            brief.allocator_id = new_id

    _register_session(session_id, name, brief, web_context, assessment, result, explanation)
    return result


def _register_session(session_id, lp_name, brief, web_context, assessment, result, explanation) -> None:
    session = GateSession(
        session_id=session_id,
        lp_name=lp_name,
        brief_dict=brief.to_dict(),
        web_context=web_context,
        assessment_dict=assessment.model_dump(),
        result_dict=result.model_dump(),
        explanation_dict=explanation.model_dump(),
    )
    create_session(session)

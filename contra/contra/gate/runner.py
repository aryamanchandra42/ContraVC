"""Run gate: two-phase deterministic evaluation + LLM explain pass."""

from __future__ import annotations

import uuid
from typing import Dict, List, Optional

from contra.gate.appetite_validator import validate_and_patch
from contra.gate.evaluator import apply_appetite_adjustments, build_allocation_evidence, evaluate
from contra.gate.evidence_verifier import verify_evidence
from contra.gate.models import CoreGateCheck, GateAssessment, GateResult
from contra.gate.research import search_lp, search_lp_with_nfx
from contra.gate.session import GateSession, create_session
from contra.gate.verdict import explain_hard_block, explain_with_escalation
from contra.intelligence.brief import find_similar_confirmed_lps, lookup
from contra.intelligence.lp_similarity import (
    build_similarity_target,
    compute_archetype_fit,
)
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


def _web_budget(compact_web: bool) -> int:
    """
    Web-context character budget for the verdict prompt.

    GATE_WEB_MAX_CHARS (single-LP, default 20000) and GATE_WEB_MAX_CHARS_BATCH
    (batch, default 9000). The old 1200-char batch cap — a relic of free-tier
    Groq TPM limits — starved the verdict model of evidence.
    """
    import os

    key = "GATE_WEB_MAX_CHARS_BATCH" if compact_web else "GATE_WEB_MAX_CHARS"
    default = 9000 if compact_web else 20000
    raw = os.environ.get(key, "").strip()
    try:
        return max(2000, int(raw)) if raw else default
    except ValueError:
        return default


def _pitchbook_facts(name: str) -> List[str]:
    """Deterministic ground-truth facts from the authenticated PitchBook profile."""
    try:
        from agents.research.pitchbook_fetch import pb_deterministic_facts

        return pb_deterministic_facts(name)
    except Exception:
        return []


def _pitchbook_status(source_urls: List[str]) -> str:
    """
    Derive PitchBook enrichment status from the source URLs returned by web research.

    Checks whether a PitchBook profile was injected by looking at source_urls, and
    whether PitchBook cookies are available at all.
    """
    try:
        from agents.research.pitchbook_fetch import cookies_available
        if not cookies_available():
            return "no_cookies"
    except Exception:
        return "no_cookies"

    pb_fetched = any("pitchbook.com" in url for url in (source_urls or []))
    return "fetched" if pb_fetched else "not_found"


def _parse_nfx_context(nfx_context: Optional[str]) -> dict:
    """Parse the NFX context string back into a structured dict for the UI."""
    if not nfx_context:
        return {}
    profile: dict = {}
    field_map = {
        "investor": "investor_name",
        "firm": "firm_name",
        "nfx url": "nfx_url",
        "sweet spot": "sweet_spot",
        "check min": "check_min",
        "check max": "check_max",
        "locations": "locations",
        "intro source": "intro_source",
        "intro strength": "intro_strength",
    }
    for line in nfx_context.splitlines():
        if ":" not in line:
            continue
        key, _, val = line.partition(":")
        key_norm = key.strip().lower()
        if key_norm in field_map and val.strip():
            profile[field_map[key_norm]] = val.strip()
    return profile


def run_gate(
    con,
    name: str,
    analyst_facts: Optional[List[str]] = None,
    _session_id: Optional[str] = None,
    nfx_url: Optional[str] = None,
    compact_web: bool = False,
    nfx_context: Optional[str] = None,
    screening_mode: str = "institutional",
) -> GateResult:
    """
    Full gate run — returns GateResult and registers a session for chat follow-up.

    Phase 1: build IntelligenceBrief (backend lookup + match-trust check)
    Phase 2: deterministic pre-LLM evaluation
    Phase 3: LLM explain pass — infers appetite + verdict
    Phase 4: appetite validator — deterministic guardrails on LLM output
    Phase 5: post-LLM evaluate with validated appetite profile
    Phase 6: final decision assembly

    screening_mode controls verdict strictness:
      "nfx_individual"  — NFX Signal batch; GP + no LP history → NO
      "institutional"   — named entity screens; uncertain → REVIEW
    """
    analyst_facts = analyst_facts or []
    session_id = _session_id or uuid.uuid4().hex

    brief = lookup(con, name)

    # ----- Short-circuit for hard CRM block (no LLM needed) ----------------
    if brief.in_crm:
        assessment = evaluate(brief, analyst_facts, screening_mode=screening_mode)
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
            primary_blocker=assessment.hard_blocks[0] if assessment.hard_blocks else "",
        )
        _register_session(
            session_id, name, brief, "(already in CRM — web search skipped)",
            assessment, result, explanation,
        )
        return result

    # ----- Web research + drill-down ----------------------------------------
    # Generous evidence budgets: verdict quality is evidence-bound, not
    # context-bound (Claude/GPT verdict models have 128k+ context windows).
    max_chars = _web_budget(compact_web)
    search_kw = dict(
        max_chars=max_chars,
        screening_mode=screening_mode,
        match_untrusted=brief.match_untrusted,
    )
    if nfx_url:
        web_context, source_urls = search_lp_with_nfx(name, nfx_url=nfx_url, **search_kw)
    else:
        web_context, source_urls = search_lp(name, **search_kw)

    pb_status = _pitchbook_status(source_urls)

    from contra.gate.knowledge_enrich import enrich_gate_knowledge

    web_context = enrich_gate_knowledge(
        name, web_context, brief, screening_mode=screening_mode,
    )

    # ----- PitchBook ground-truth facts (deterministic C1 evidence) ---------
    # A PB profile listing fund commitments deterministically passes C1 in the
    # evaluator and is surfaced to the LLM as a hard fact, not an inference.
    pb_facts = _pitchbook_facts(name)
    eval_facts = analyst_facts + pb_facts

    drill_results = run_drill_down(con, brief)
    allocation_evidence = build_allocation_evidence(brief)

    # ----- Pre-LLM similar LP lookup (coarse — no appetite info yet) --------
    pre_target = build_similarity_target(
        brief, nfx_context=nfx_context, web_context=web_context,
    )
    brief.similar_confirmed_lps = find_similar_confirmed_lps(con, pre_target, limit=4)

    # ----- Phase 1: deterministic assessment (pre-appetite) -----------------
    assessment = evaluate(brief, eval_facts, screening_mode=screening_mode)

    # ----- Phase 2: LLM explain pass — triage + strong-model escalation -----
    explanation, verdict_meta = explain_with_escalation(
        name=name,
        brief=brief,
        assessment=assessment,
        web_context=web_context,
        drill_results=drill_results,
        analyst_facts=eval_facts,
        allocation_evidence=allocation_evidence,
        nfx_context=nfx_context,
        screening_mode=screening_mode,
    )

    # ----- Phase 3: appetite validator — deterministic guardrails -----------
    # Catches LLM errors like citing employer-fund portfolio as LP evidence,
    # over-inferring EM appetite from GP role, or hedging when mode says NO.
    explanation = validate_and_patch(
        explanation,
        nfx_context=nfx_context,
        web_context=web_context,
        screening_mode=screening_mode,
    )

    # ----- Phase 3b: evidence verifier — drop unquotable LP commitment claims
    # and downgrade hollow YES verdicts (false-positive guard).
    explanation, verification_notes = verify_evidence(
        explanation,
        web_context=web_context,
        analyst_facts=eval_facts,
        screening_mode=screening_mode,
    )

    # ----- Post-LLM similar LP re-score (appetite-informed) -----------------
    appetite = explanation.to_appetite_profile(
        allocation_evidence=[allocation_evidence] if allocation_evidence else [],
    )
    post_target = build_similarity_target(
        brief, nfx_context=nfx_context, web_context=web_context, appetite=appetite,
    )
    refined_lps = find_similar_confirmed_lps(con, post_target, limit=4)
    brief.similar_confirmed_lps = refined_lps

    # ----- Phase 4: re-eval with the validated appetite profile -------------
    assessment = evaluate(brief, eval_facts, appetite=appetite, screening_mode=screening_mode)

    # ----- Final decision: LLM is primary, hard blocks always win -----------
    if assessment.hard_blocks:
        final_rec = "no"
    else:
        final_rec = apply_appetite_adjustments(
            explanation.llm_recommendation, appetite, screening_mode
        )

    # Merge evaluator's DB-backed core gates with LLM's web-inferred ones
    merged_core_gates = _merge_core_gates(assessment.core_gates, explanation.llm_core_gates())
    assessment = assessment.model_copy(update={
        "recommendation": final_rec,
        "core_gates": merged_core_gates,
        "appetite": appetite,
    })

    # ----- Assemble result --------------------------------------------------
    db_queries = [d.get("template_id", "?") for d in drill_results]

    primary_blocker = ""
    if final_rec == "no":
        primary_blocker = (
            explanation.primary_blocker
            or (assessment.hard_blocks[0] if assessment.hard_blocks else "")
            or next(
                (f.replace("_", " ").capitalize() for f in (appetite.negative_flags or [])
                 if f in {"no_fund_lp_history", "pe_only", "direct_only", "no_venture",
                          "angel_only", "nfx_angel_only"}),
                ""
            )
        )

    archetype_fit = compute_archetype_fit(appetite.archetype, brief.similar_confirmed_lps)
    archetype_fit_dict = {
        "fit_level": archetype_fit.fit_level,
        "avg_similarity_score": archetype_fit.avg_similarity_score,
        "rationale": archetype_fit.rationale,
    }

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
        source_urls=source_urls,
        analyst_facts=analyst_facts,
        lp_commitments_found=explanation.lp_commitments_found,
        primary_blocker=primary_blocker,
        pitchbook_status=pb_status,
        verdict_model=verdict_meta.get("model", ""),
        escalated=bool(verdict_meta.get("escalated")),
        verification_notes=verification_notes,
        partial_match_deals=brief.partial_match_deals,
        partial_match_investment_summary=brief.investment_summary or {} if brief.match_method == "fuzzy_low" else {},
        similar_confirmed_lps=brief.similar_confirmed_lps,
        archetype_fit=archetype_fit_dict,
        nfx_profile=_parse_nfx_context(nfx_context),
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

        # Extract analyst-provided contact facts (email/LinkedIn/X pasted during gate)
        if brief.allocator_id and analyst_facts:
            try:
                from contra.intelligence.contact_extract import extract_and_persist_gate_contacts
                extract_and_persist_gate_contacts(
                    con,
                    lp_name=name,
                    allocator_id=brief.allocator_id,
                    web_context="",
                    source_urls=[],
                    analyst_facts=analyst_facts,
                )
            except Exception:
                pass

        # Durable LP dossier — outlives the 30-minute in-memory session
        from contra.crm.dossier import upsert_dossier_from_gate

        upsert_dossier_from_gate(con, result, web_context, allocator_id=brief.allocator_id)

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

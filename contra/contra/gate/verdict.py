"""LLM explain pass — takes a pre-computed GateAssessment and produces GateExplanation."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from contra.gate.models import GateAssessment, GateExplanation
from contra.intelligence.brief import IntelligenceBrief

ROOT = Path(__file__).resolve().parent.parent.parent

_DEFAULT_GATE_EXPLAIN_MAX_TOKENS = 8192


def _gate_explain_max_tokens() -> int:
    import os

    raw = os.environ.get("GATE_EXPLAIN_MAX_TOKENS", "").strip()
    if not raw:
        return _DEFAULT_GATE_EXPLAIN_MAX_TOKENS
    try:
        return max(2048, int(raw))
    except ValueError:
        return _DEFAULT_GATE_EXPLAIN_MAX_TOKENS


def _load_yaml(name: str) -> Dict[str, Any]:
    path = ROOT / "prompts" / "navigator" / name
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _compact_backend_supplement(brief: IntelligenceBrief) -> str:
    """
    Extra backend fields not already rendered in the user template.

    Omits the full brief JSON (was ~4k chars and mostly duplicated structured fields).
    """
    parts: List[str] = []
    profile = brief.allocator_profile or {}
    if profile:
        parts.append(f"Allocator profile: {json.dumps(profile, default=str)}")
    if brief.investment_summary:
        parts.append(f"Investment summary: {json.dumps(brief.investment_summary, default=str)}")
    if brief.warm_paths:
        parts.append(f"Warm paths: {json.dumps(brief.warm_paths[:2], default=str)}")
    if brief.contacts:
        parts.append(f"Contacts: {json.dumps(brief.contacts[:2], default=str)}")
    if brief.source_snippets:
        parts.append("Source snippets: " + " | ".join(s[:200] for s in brief.source_snippets[:2]))
    text = "\n".join(parts) if parts else "(none — not in database or no extra fields)"
    return text[:900]


def _format_similar_confirmed_lps(brief: IntelligenceBrief) -> str:
    """Render similar confirmed LP profiles as a compact prompt block with similarity scores."""
    from contra.intelligence.lp_similarity import MIN_SIGNAL_SCORE

    lps = brief.similar_confirmed_lps or []
    if not lps:
        return "(none found — database may not have enough comparable LP profiles yet)"
    lines = []
    for lp in lps:
        score = lp.get("similarity_score", 0)
        dims = ", ".join(lp.get("match_dimensions", [])) or "—"
        archetype = lp.get("archetype", "unknown")
        qualifier = "✓ strong match" if score >= MIN_SIGNAL_SCORE else "~ weak match"
        lines.append(
            f"  · {lp['name']} [{score}% similarity — {qualifier}] "
            f"| archetype: {archetype} "
            f"| geo: {lp['geography']} | EM appetite: {lp['em_appetite']} "
            f"| AI appetite: {lp['ai_appetite']} | type: {lp['allocator_type']} "
            f"| {lp['fund_deal_count']} fund LP deal(s) "
            f"| matched dims: {dims}"
        )
    return "\n".join(lines)


def _compact_drill_down(drill_results: List[Dict[str, Any]]) -> str:
    """Keep drill-down rows compact for small-context LLMs."""
    compact = []
    for d in drill_results[:2]:
        rows = d.get("rows") or []
        compact.append({
            "template_id": d.get("template_id"),
            "row_count": d.get("row_count", len(rows)),
            "rows": rows[:4],
            "error": d.get("error"),
        })
    return json.dumps(compact, default=str)[:900]


def _assessment_text_blocks(assessment: GateAssessment) -> Dict[str, str]:
    """Render assessment fields as readable text blocks for the prompt."""
    hard_blocks_text = (
        "\n".join(f"  ✗ {b}" for b in assessment.hard_blocks)
        if assessment.hard_blocks else "  (none)"
    )
    core_gates_text = "\n".join(
        f"  {g.gate.upper()} [{g.status.upper()}] {g.evidence[:120]}"
        for g in assessment.core_gates
    )
    signals_text = "\n".join(
        f"  {'✓' if s.met else '✗'} [{s.source}] {s.label}: {s.detail[:100]}"
        for s in assessment.signals
    )
    return {
        "hard_blocks_text": hard_blocks_text,
        "core_gates_text": core_gates_text,
        "signals_text": signals_text,
    }


def _format_partial_match_deals(brief: IntelligenceBrief) -> str:
    """Render low-confidence DB match investment deals for the prompt."""
    if not brief.partial_match_deals:
        return "(none — no low-confidence DB match with investment data)"
    inv = brief.investment_summary or {}
    total = inv.get("deal_count", len(brief.partial_match_deals))
    fund_count = inv.get("fund_deal_count", 0)
    header = (
        f"LOW-CONFIDENCE MATCH (≈{brief.match_confidence:.0%}): "
        f"{total} deal(s) found, {fund_count} fund LP deal(s), "
        f"{total - fund_count} direct/SPV deal(s)"
    )
    lines = [header] + [f"  · {d}" for d in brief.partial_match_deals]
    return "\n".join(lines)


def _build_explain_prompt(
    name: str,
    brief: IntelligenceBrief,
    assessment: GateAssessment,
    web_context: str,
    drill_results: List[Dict[str, Any]],
    analyst_facts: Optional[List[str]] = None,
    allocation_evidence: str = "",
    nfx_context: Optional[str] = None,
    screening_mode: str = "institutional",
) -> str:
    tpl = _load_yaml("gate_explain.yaml")
    template = tpl.get("user_template") or "{lp_name}\n{backend_json}\n{web_context}"
    text_blocks = _assessment_text_blocks(assessment)
    gc = brief.graph_connectivity or {}

    nfx_block = nfx_context or "(not from NFX Signal)"

    match_warning = ""
    if brief.match_untrusted and brief.matched_name:
        match_warning = (
            f"⚠ UNRELIABLE MATCH: Backend resolved '{name}' to '{brief.matched_name}' "
            f"(surname mismatch — likely different person). "
            f"IGNORE all ICP tier, fit score, syndicate, and core gate data above. "
            f"Base your assessment solely on web research and general knowledge."
        )

    return template.format(
        lp_name=name,
        screening_mode=screening_mode,
        recommendation=assessment.recommendation.upper(),
        signals_met=assessment.signals_met,
        signals_required=assessment.signals_required,
        hard_blocks_text=text_blocks["hard_blocks_text"],
        core_gates_text=text_blocks["core_gates_text"],
        signals_text=text_blocks["signals_text"],
        matched_name=brief.matched_name or "—",
        match_confidence=f"{brief.match_confidence:.2f}",
        match_warning=match_warning,
        population=brief.population or "—",
        icp_tier=brief.icp_tier or "—",
        fit_score=brief.icp_fit_score if brief.icp_fit_score is not None else "—",
        core_pass=brief.core_pass,
        excluded=brief.excluded,
        exclusion_reason=brief.exclusion_reason or "—",
        client_decision=brief.client_decision or "—",
        contra_rank=brief.benchmark_rank or "—",
        warm_path_count=gc.get("warm_path_count", 0),
        investment_count=gc.get("investment_count", 0),
        top_signals_json=json.dumps(brief.top_signals[:8], default=str),
        rejection_reasons_json=json.dumps(brief.rejection_reasons),
        syndicate_profile_json=json.dumps(brief.syndicate_profile or {}, default=str),
        analyst_facts_json=json.dumps(analyst_facts or []),
        drill_down_json=_compact_drill_down(drill_results),
        allocation_evidence=allocation_evidence or "(no investment history on record)",
        partial_match_deals=_format_partial_match_deals(brief),
        similar_confirmed_lps=_format_similar_confirmed_lps(brief),
        known_em_funds=(
            "Hustle Fund, Weekend Fund, Conviction, Village Global, Precursor Ventures, "
            "Afore Capital, Iterative, Saison Capital (+ similar EM / Fund-I vehicles)"
        ),
        backend_supplement=_compact_backend_supplement(brief),
        web_context=web_context,
        nfx_context=nfx_block,
    )


def _gate_model_for_mode(screening_mode: str) -> str:
    """Mode-aware NIM catalog model (institutional vs NFX individual)."""
    import os

    if screening_mode == "nfx_individual":
        return (
            os.environ.get("GATE_LLM_MODEL_NFX", "").strip()
            or "nvidia/nemotron-3-super-120b-a12b"
        )
    return (
        os.environ.get("GATE_LLM_MODEL_INSTITUTIONAL", "").strip()
        or "writer/palmyra-fin-70b-32k"
    )


def _get_gate_llm_client(screening_mode: str = "institutional"):
    """
    Return the best available LLM client for gate verdict decisions.

    Priority:
      1. GATE_LLM_PROVIDER / GATE_LLM_MODEL env vars (explicit override)
      2. ANTHROPIC_API_KEY / CLAUDE_API_KEY → claude-haiku-4-5 (cost-efficient default)
      3. OPENAI_API_KEY → gpt-4o
      4. PULSE_LLM_PROVIDER default (Groq / etc.)

    NVIDIA NIM is used upstream via knowledge_enrich.py — not as the default verdict model.
    """
    import os
    from agents.research.llm_client import (
        HAIKU_MODEL,
        LLMUnavailable,
        anthropic_configured,
        get_anthropic_haiku_client,
        get_llm_client,
    )

    gate_provider = os.environ.get("GATE_LLM_PROVIDER", "").strip()
    gate_model = os.environ.get("GATE_LLM_MODEL", "").strip()
    if gate_provider:
        if gate_provider == "nvidia" and not gate_model:
            model = _gate_model_for_mode(screening_mode)
        elif gate_provider == "anthropic" and not gate_model:
            model = HAIKU_MODEL
        else:
            model = gate_model or None
        return get_llm_client(provider=gate_provider, model=model)

    if anthropic_configured():
        return get_anthropic_haiku_client()

    pulse = os.environ.get("PULSE_LLM_PROVIDER", "").strip().lower()
    if pulse == "anthropic":
        return get_llm_client(provider="anthropic")

    openai_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if openai_key:
        try:
            return get_llm_client(provider="openai", model="gpt-4o")
        except LLMUnavailable:
            pass

    return get_llm_client()


# ---------------------------------------------------------------------------
# Escalation — strong model double-checks borderline / positive verdicts
# ---------------------------------------------------------------------------

def _escalation_enabled() -> bool:
    import os

    return os.environ.get("GATE_ESCALATION", "true").lower().strip() in (
        "1", "true", "yes", "on",
    )


def _escalation_model() -> str:
    import os

    return os.environ.get("GATE_ESCALATION_MODEL", "").strip() or "claude-sonnet-4-5"


def _run_explain(llm, name, brief, assessment, web_context, drill_results,
                 analyst_facts, allocation_evidence, nfx_context,
                 screening_mode) -> GateExplanation:
    system_cfg = _load_yaml("gate_explain.yaml")
    system = system_cfg.get("system") or "You are an LP screening explainer. Return GateExplanation JSON."
    prompt = _build_explain_prompt(
        name, brief, assessment, web_context, drill_results,
        analyst_facts, allocation_evidence, nfx_context,
        screening_mode=screening_mode,
    )
    return llm.structured(
        prompt=prompt,
        response_model=GateExplanation,
        system=system,
        max_tokens=_gate_explain_max_tokens(),
    )


def explain(
    name: str,
    brief: IntelligenceBrief,
    assessment: GateAssessment,
    web_context: str,
    drill_results: List[Dict[str, Any]],
    analyst_facts: Optional[List[str]] = None,
    allocation_evidence: str = "",
    nfx_context: Optional[str] = None,
    screening_mode: str = "institutional",
) -> GateExplanation:
    """
    Call the LLM as the primary decision-maker.

    The deterministic assessment is passed as ADVISORY context. The LLM returns its
    own llm_recommendation (yes/no/review) plus llm_core_gates assessed from web
    evidence — these can fill gaps the evaluator left as 'unknown'.
    """
    explanation, _meta = explain_with_escalation(
        name, brief, assessment, web_context, drill_results,
        analyst_facts, allocation_evidence, nfx_context,
        screening_mode=screening_mode,
    )
    return explanation


def explain_with_escalation(
    name: str,
    brief: IntelligenceBrief,
    assessment: GateAssessment,
    web_context: str,
    drill_results: List[Dict[str, Any]],
    analyst_facts: Optional[List[str]] = None,
    allocation_evidence: str = "",
    nfx_context: Optional[str] = None,
    screening_mode: str = "institutional",
) -> tuple[GateExplanation, Dict[str, Any]]:
    """
    Tiered verdict:

    1. TRIAGE — cheap model (Haiku) screens every LP. Clear NOs stop here; the
       bulk of an NFX batch is NO, so most spend stays on the cheap tier.
    2. ESCALATION — when triage says "yes" or "review" (the verdicts that drive
       outreach and analyst time), a stronger model (default claude-sonnet-4-5)
       re-decides with the same evidence. Its verdict wins.

    Returns (explanation, meta) where meta = {"model": ..., "escalated": bool}.
    Disable via GATE_ESCALATION=false; model via GATE_ESCALATION_MODEL.
    """
    from agents.research.llm_client import LLMUnavailable, anthropic_configured, get_llm_client

    try:
        triage_llm = _get_gate_llm_client(screening_mode=screening_mode)
    except LLMUnavailable as exc:
        raise RuntimeError(
            "LLM required for contra gate. Set PULSE_LLM_PROVIDER=anthropic and ANTHROPIC_API_KEY, "
            "or set GATE_LLM_PROVIDER explicitly."
        ) from exc

    explanation = _run_explain(
        triage_llm, name, brief, assessment, web_context, drill_results,
        analyst_facts, allocation_evidence, nfx_context, screening_mode,
    )
    meta: Dict[str, Any] = {
        "model": getattr(triage_llm, "model", "unknown"),
        "escalated": False,
    }

    esc_model = _escalation_model()
    should_escalate = (
        _escalation_enabled()
        and explanation.llm_recommendation in ("yes", "review")
        and anthropic_configured()
        and getattr(triage_llm, "model", "") != esc_model
    )
    if should_escalate:
        try:
            esc_llm = get_llm_client(provider="anthropic", model=esc_model)
            explanation = _run_explain(
                esc_llm, name, brief, assessment, web_context, drill_results,
                analyst_facts, allocation_evidence, nfx_context, screening_mode,
            )
            meta = {"model": esc_model, "escalated": True}
        except Exception as exc:
            import logging
            logging.getLogger(__name__).warning(
                "Gate escalation failed for '%s' (%s) — keeping triage verdict", name, exc
            )

    return explanation, meta


# ---------------------------------------------------------------------------
# Fallback explanation for hard-block / no-LLM cases
# ---------------------------------------------------------------------------

def explain_hard_block(assessment: GateAssessment, lp_name: str) -> GateExplanation:
    """Build a GateExplanation without LLM when a hard block is present."""
    block = assessment.hard_blocks[0] if assessment.hard_blocks else "Unknown block"
    return GateExplanation(
        llm_recommendation="no",
        confidence="high",
        reasons=[block] + assessment.hard_blocks[1:],
        backend_evidence=[block],
        online_evidence=[],
        conflicts=[],
        summary=f"Skip — {block}. No further evaluation needed.",
        web_em_ai_vc=False,
        web_em_ai_evidence="",
    )

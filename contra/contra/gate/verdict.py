"""LLM explain pass — takes a pre-computed GateAssessment and produces GateExplanation."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from contra.gate.models import GateAssessment, GateExplanation
from contra.intelligence.brief import IntelligenceBrief

ROOT = Path(__file__).resolve().parent.parent.parent


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


def _get_gate_llm_client():
    """
    Return the best available LLM client for gate decisions.

    Priority (highest quality first):
      1. GATE_LLM_PROVIDER / GATE_LLM_MODEL env vars (explicit gate override)
      2. OPENAI_API_KEY → gpt-4o  (best structured-reasoning quality)
      3. PULSE_LLM_PROVIDER default (Groq Llama / Anthropic / etc.)

    To use GPT-4o for gate decisions: set OPENAI_API_KEY in .env and optionally
    add GATE_LLM_PROVIDER=openai (or leave it — auto-detection kicks in).
    GPT-4o costs ~$0.016/screen vs Groq free tier; batch of 38 ≈ $0.60.
    """
    import os
    from agents.research.llm_client import LLMUnavailable, get_llm_client

    # Explicit override
    gate_provider = os.environ.get("GATE_LLM_PROVIDER", "").strip()
    gate_model = os.environ.get("GATE_LLM_MODEL", "").strip()
    if gate_provider:
        return get_llm_client(provider=gate_provider, model=gate_model or None)

    # Auto-prefer OpenAI gpt-4o when key is available — better reasoning quality
    openai_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if openai_key:
        try:
            return get_llm_client(provider="openai", model="gpt-4o")
        except LLMUnavailable:
            pass

    # Fall back to the configured default provider
    return get_llm_client()


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
    from agents.research.llm_client import LLMUnavailable

    try:
        llm = _get_gate_llm_client()
    except LLMUnavailable as exc:
        raise RuntimeError(
            "LLM required for contra gate. Set PULSE_LLM_PROVIDER=groq/anthropic/openai "
            "with a matching API key, or set GATE_LLM_PROVIDER explicitly."
        ) from exc

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
        max_tokens=1800,
    )


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

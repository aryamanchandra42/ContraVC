"""
Gate chat — answer analyst questions and re-screen with new facts.

Process:
  1. Receive session_id + message.
  2. Extract analyst facts (structured LLM call).
  3a. If new facts → merge into session, re-run gate evaluator + explainer,
      return updated GateResult alongside the reply.
  3b. If question-only → plain chat reply using session context, no re-screen.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from contra.gate.models import AnalystFactExtraction, GateAssessment, GateResult
from contra.gate.session import get_session, update_session

ROOT = Path(__file__).resolve().parent.parent.parent


def _load_yaml(name: str) -> Dict[str, Any]:
    path = ROOT / "prompts" / "navigator" / name
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


# ---------------------------------------------------------------------------
# Text rendering helpers (shared with verdict.py)
# ---------------------------------------------------------------------------

def _render_assessment(assessment: GateAssessment) -> Dict[str, str]:
    hard_blocks_text = (
        "\n".join(f"  ✗ {b}" for b in assessment.hard_blocks)
        if assessment.hard_blocks else "  (none)"
    )
    core_gates_text = "\n".join(
        f"  {g.gate.upper()} [{g.status.upper()}] {g.evidence}"
        for g in assessment.core_gates
    )
    signals_text = "\n".join(
        f"  {'✓' if s.met else '✗'} [{s.source}] {s.label}: {s.detail}"
        for s in assessment.signals
    )
    return {
        "hard_blocks_text": hard_blocks_text,
        "core_gates_text": core_gates_text,
        "signals_text": signals_text,
    }


def _render_history(message_history: List[Dict[str, str]]) -> str:
    lines = []
    for msg in message_history[-10:]:  # keep last 10 messages for context
        role = msg.get("role", "user").capitalize()
        lines.append(f"{role}: {msg.get('content', '')}")
    return "\n".join(lines) if lines else "(no prior messages)"


# ---------------------------------------------------------------------------
# Fact extraction
# ---------------------------------------------------------------------------

def _extract_facts(llm, lp_name: str, message: str) -> AnalystFactExtraction:
    cfg = _load_yaml("gate_chat.yaml")
    system = cfg.get("fact_extraction_system") or "Extract LP facts from analyst message."
    template = cfg.get("fact_extraction_template") or "{lp_name}\n{message}"
    prompt = template.format(lp_name=lp_name, message=message)
    try:
        return llm.structured(
            prompt=prompt,
            response_model=AnalystFactExtraction,
            system=system,
            max_tokens=512,
        )
    except Exception:
        return AnalystFactExtraction(has_new_facts=False, facts=[], is_question_only=True)


# ---------------------------------------------------------------------------
# Plain chat reply (question-only path)
# ---------------------------------------------------------------------------

def _build_chat_prompt(
    lp_name: str,
    assessment: GateAssessment,
    result_dict: Dict[str, Any],
    analyst_facts: List[str],
    message_history: List[Dict[str, str]],
    message: str,
) -> str:
    cfg = _load_yaml("gate_chat.yaml")
    template = cfg.get("chat_template") or "{lp_name}\n{message}"
    blocks = _render_assessment(assessment)
    reasons_text = "\n".join(f"  - {r}" for r in result_dict.get("reasons", []))
    online_evidence_text = "\n".join(
        f"  - {e}" for e in (result_dict.get("online_evidence") or [])[:6]
    ) or "  (none on file)"
    history_text = _render_history(message_history)

    return template.format(
        lp_name=lp_name,
        recommendation=assessment.recommendation.upper(),
        signals_met=assessment.signals_met,
        signals_required=assessment.signals_required,
        hard_blocks_text=blocks["hard_blocks_text"],
        core_gates_text=blocks["core_gates_text"],
        signals_text=blocks["signals_text"],
        analyst_facts_json=json.dumps(analyst_facts),
        summary=result_dict.get("summary", ""),
        reasons_text=reasons_text,
        online_evidence_text=online_evidence_text,
        history_text=history_text,
        message=message,
    )


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

class ChatResponse:
    def __init__(
        self,
        reply: str,
        updated_result: Optional[GateResult],
        rescreened: bool,
    ) -> None:
        self.reply = reply
        self.updated_result = updated_result
        self.rescreened = rescreened


def process_message(con, session_id: str, message: str) -> ChatResponse:
    """
    Handle one analyst message for an existing gate session.

    Returns ChatResponse with:
      reply           — plain-English assistant response
      updated_result  — new GateResult if re-screened, else None
      rescreened      — True if evaluator re-ran
    """
    from agents.research.llm_client import LLMUnavailable, get_llm_client

    session = get_session(session_id)
    if session is None:
        return ChatResponse(
            reply="Session not found or expired (sessions last 30 minutes). Please run a new gate screen.",
            updated_result=None,
            rescreened=False,
        )

    try:
        llm = get_llm_client()
    except LLMUnavailable:
        return ChatResponse(
            reply="LLM unavailable — set PULSE_LLM_PROVIDER and the matching API key to use gate chat.",
            updated_result=None,
            rescreened=False,
        )

    # Record user message
    update_session(session_id, new_message={"role": "user", "content": message})

    # Extract facts
    extraction = _extract_facts(llm, session.lp_name, message)

    updated_result: Optional[GateResult] = None
    rescreened = False

    if extraction.has_new_facts and extraction.facts:
        # Merge new facts and re-run
        merged_facts = list(session.analyst_facts) + [
            f for f in extraction.facts if f not in session.analyst_facts
        ]

        from contra.gate.runner import run_gate
        updated_result = run_gate(
            con,
            name=session.lp_name,
            analyst_facts=merged_facts,
            _session_id=session_id,
        )
        rescreened = True

        # Build reply that acknowledges the new facts
        new_assessment = updated_result.assessment
        blocks = _render_assessment(new_assessment)
        fact_list = "\n".join(f"  • {f}" for f in extraction.facts)
        reply = (
            f"I found new facts in your message:\n{fact_list}\n\n"
            f"Re-screened with updated context:\n"
            f"  Recommendation: **{new_assessment.recommendation.upper()}** "
            f"({new_assessment.signals_met}/{new_assessment.signals_required} signals met)\n\n"
            f"Core gates:\n{blocks['core_gates_text']}\n\n"
            f"{updated_result.summary}"
        )
    else:
        # Question-only → plain chat reply
        assessment = GateAssessment(**session.assessment_dict)
        prompt = _build_chat_prompt(
            lp_name=session.lp_name,
            assessment=assessment,
            result_dict=session.result_dict,
            analyst_facts=session.analyst_facts,
            message_history=session.message_history,
            message=message,
        )
        cfg = _load_yaml("gate_chat.yaml")
        system = cfg.get("system") or "You are a helpful LP screening assistant."
        reply = llm.chat(
            messages=[{"role": "user", "content": prompt}],
            system=system,
            max_tokens=1024,
        )

    update_session(session_id, new_message={"role": "assistant", "content": reply})
    return ChatResponse(reply=reply, updated_result=updated_result, rescreened=rescreened)

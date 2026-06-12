"""Optional NVIDIA NIM pass — synthesize web research before OpenAI gate explain."""

from __future__ import annotations

import logging

from contra.intelligence.brief import IntelligenceBrief

logger = logging.getLogger(__name__)

_SYSTEM = """You are a private-markets research analyst preparing LP screening notes for a VC fund.

From the web snippets below, extract ONLY evidence relevant to whether this person/entity commits
capital to VC funds as a limited partner (LP). Be explicit about:

1. Confirmed external LP fund commitments (fund name, role as LP if stated)
2. Allocator type clues (family office, FoF, endowment, angel, GP at a fund, etc.)
3. Emerging-markets / Asia / MENA appetite signals
4. AI / technology fund appetite signals
5. Disqualifiers: PE-only, direct-only angel, wrong geography

CRITICAL: If snippets describe a GP's employer fund portfolio, label it "GP portfolio (not LP activity)"
— do not treat it as LP commitment evidence.

Output concise bullet points. If nothing useful, say "No additional LP signals found."
"""


def knowledge_enrich_enabled() -> bool:
    from agents.research.nim_router import nim_enabled
    import os
    return os.environ.get("GATE_KNOWLEDGE_ENRICH", "true").lower().strip() in (
        "1", "true", "yes", "on",
    ) and nim_enabled()


def enrich_gate_knowledge(
    lp_name: str,
    web_context: str,
    brief: IntelligenceBrief,
    screening_mode: str = "institutional",
) -> str:
    """
    Run an NVIDIA NIM model (DeepSeek / Mistral / Llama per task router) to synthesize
    web snippets into analyst notes appended for the OpenAI gate explain pass.
    """
    if not knowledge_enrich_enabled() or not web_context.strip():
        return web_context

    from agents.research.llm_client import LLMUnavailable
    from agents.research.nim_router import call_nim_chat_with_fallback, nim_model_for_task

    task = "knowledge"
    matched = brief.matched_name or "—"
    prompt = (
        f"LP name: {lp_name}\n"
        f"Screening mode: {screening_mode}\n"
        f"DB match: {matched} (confidence {brief.match_confidence:.0%})\n"
        f"ICP tier: {brief.icp_tier or '—'}\n\n"
        f"WEB SNIPPETS:\n{web_context[:3500]}"
    )

    try:
        notes, model_used = call_nim_chat_with_fallback(
            task,
            screening_mode=screening_mode,
            brief=brief,
            messages=[{"role": "user", "content": prompt}],
            system=_SYSTEM,
            max_tokens=900,
        )
        notes = notes.strip()
    except LLMUnavailable as exc:
        logger.warning("Gate knowledge enrich skipped — %s", exc)
        return web_context
    except Exception as exc:
        logger.warning("Gate knowledge enrich failed (%s) — using raw web context", exc)
        return web_context

    if not notes or notes.lower().startswith("no additional lp signals"):
        return web_context

    label_model = model_used or nim_model_for_task(
        task, screening_mode=screening_mode, brief=brief,
    )
    return (
        f"{web_context}\n\n"
        f"--- NVIDIA knowledge synthesis ({label_model}) ---\n"
        f"{notes}"
    )

"""
NVIDIA NIM model router — assigns catalog models to Contra workloads.

OpenAI remains the default gate *verdict* model. NVIDIA models enrich knowledge,
batch research, chat, and CRM extraction via build.nvidia.com catalog IDs.

Override any task via .env:
  NIM_MODEL_KNOWLEDGE_INSTITUTIONAL=deepseek-ai/deepseek-v3.2
  NIM_MODEL_KNOWLEDGE_NFX=deepseek-ai/deepseek-v3.2
  NIM_MODEL_CHAT=minimaxai/minimax-m2.5
  NIM_MODEL_ENRICH=mistralai/mistral-small-3.1-24b-instruct-2503
  NIM_MODEL_FALLBACK=meta/llama-3.3-70b-instruct
"""

from __future__ import annotations

import logging
import os
from typing import Any, List, Optional

logger = logging.getLogger(__name__)

# Catalog model IDs on integrate.api.nvidia.com (free-tier availability varies by account).
_TASK_DEFAULTS: dict[str, str] = {
    # Gate knowledge synthesis (before OpenAI explain)
    "knowledge_institutional": "deepseek-ai/deepseek-v3.2",
    "knowledge_nfx": "deepseek-ai/deepseek-v3.2",
    "knowledge_sea": "mistralai/mistral-small-4-119b-2603",
    "knowledge": "meta/llama-3.3-70b-instruct",
    # Batch allocator enrichment + ontology
    "enrich": "mistralai/mistral-small-3.1-24b-instruct-2503",
    "ontology": "deepseek-ai/deepseek-v3.2",
    # Gate chat
    "chat": "minimaxai/minimax-m2.5",
    "chat_facts": "google/gemma-3-12b-it",
    # CRM field extraction from gate session
    "crm": "mistralai/mistral-small-3.1-24b-instruct-2503",
    # Outreach brief synthesis (if enabled)
    "brief": "nvidia/llama3-chatqa-1.5-70b",
    # Last-resort when a task-specific model 404s or rate-limits
    "fallback": "meta/llama-3.3-70b-instruct",
}

# Env var aliases (legacy + per-task)
_TASK_ENV_KEYS: dict[str, tuple[str, ...]] = {
    "knowledge_institutional": (
        "NIM_MODEL_KNOWLEDGE_INSTITUTIONAL",
        "GATE_KNOWLEDGE_MODEL_INSTITUTIONAL",
    ),
    "knowledge_nfx": (
        "NIM_MODEL_KNOWLEDGE_NFX",
        "GATE_KNOWLEDGE_MODEL_NFX",
    ),
    "knowledge_sea": ("NIM_MODEL_KNOWLEDGE_SEA",),
    "knowledge": ("GATE_KNOWLEDGE_MODEL", "NIM_MODEL_KNOWLEDGE"),
    "enrich": ("NIM_MODEL_ENRICH", "ENRICH_LLM_MODEL"),
    "ontology": ("NIM_MODEL_ONTOLOGY",),
    "chat": ("NIM_MODEL_CHAT",),
    "chat_facts": ("NIM_MODEL_CHAT_FACTS",),
    "crm": ("NIM_MODEL_CRM",),
    "brief": ("NIM_MODEL_BRIEF",),
    "fallback": ("NIM_MODEL_FALLBACK",),
}

_SEA_GEO_MARKERS = (
    "asia", "southeast", "sea", "singapore", "indonesia", "vietnam", "thailand",
    "malaysia", "philippines", "india", "mena", "middle east", "uae", "saudi",
)


def _env_model(*keys: str) -> str:
    for key in keys:
        val = os.environ.get(key, "").strip()
        if val:
            return val
    return ""


def nim_enabled() -> bool:
    from agents.research.llm_client import nvidia_configured
    return os.environ.get("NIM_ENABLED", "true").lower().strip() in (
        "1", "true", "yes", "on",
    ) and nvidia_configured()


def nim_use_for_chat() -> bool:
    return os.environ.get("NIM_FOR_CHAT", "true").lower().strip() in (
        "1", "true", "yes", "on",
    )


def _brief_suggests_sea(brief: Any) -> bool:
    """Heuristic: allocator geography hints at SEA/MENA → prefer Mistral."""
    parts: List[str] = []
    for attr in ("geography", "hq_country", "population"):
        val = getattr(brief, attr, None)
        if val:
            parts.append(str(val).lower())
    profile = getattr(brief, "allocator_profile", None) or {}
    if isinstance(profile, dict):
        for key in ("geography", "hq_country", "region"):
            if profile.get(key):
                parts.append(str(profile[key]).lower())
    blob = " ".join(parts)
    return any(m in blob for m in _SEA_GEO_MARKERS)


def nim_model_for_task(
    task: str,
    *,
    screening_mode: str = "institutional",
    brief: Any = None,
) -> str:
    """
    Resolve the NVIDIA catalog model ID for a Contra workload.

    task: knowledge_institutional | knowledge_nfx | knowledge_sea | knowledge |
          enrich | ontology | chat | chat_facts | crm | brief | fallback
    """
    if task == "knowledge":
        if screening_mode == "nfx_individual":
            task = "knowledge_nfx"
        elif brief is not None and _brief_suggests_sea(brief):
            task = "knowledge_sea"
        else:
            task = "knowledge_institutional"

    env_keys = _TASK_ENV_KEYS.get(task, (f"NIM_MODEL_{task.upper()}",))
    override = _env_model(*env_keys)
    if override:
        return override
    return _TASK_DEFAULTS.get(task, _TASK_DEFAULTS["fallback"])


def nim_fallback_models(primary: str) -> List[str]:
    """Ordered models to try when primary NIM model fails (404 / rate limit)."""
    fb = nim_model_for_task("fallback")
    chain = [
        primary,
        fb,
        "meta/llama-3.3-70b-instruct",
        "mistralai/mistral-small-3.1-24b-instruct-2503",
    ]
    seen: set[str] = set()
    out: List[str] = []
    for m in chain:
        if m and m not in seen:
            out.append(m)
            seen.add(m)
    return out


def get_nim_task_client(
    task: str,
    *,
    screening_mode: str = "institutional",
    brief: Any = None,
    temperature: float = 0.0,
    auto_switch: bool = False,
) -> Any:
    """
    Return an NVIDIA NIM client for a named Contra task.

    auto_switch=False by default — callers that need resilience should use
    call_nim_with_fallback() instead.
    """
    from agents.research.llm_client import LLMUnavailable, get_nvidia_llm_client

    if not nim_enabled():
        raise LLMUnavailable("NVIDIA NIM not configured (set NVAPI_API_KEY).")

    model = nim_model_for_task(task, screening_mode=screening_mode, brief=brief)
    return get_nvidia_llm_client(model=model, temperature=temperature, auto_switch=auto_switch)


def call_nim_chat_with_fallback(
    task: str,
    *,
    screening_mode: str = "institutional",
    brief: Any = None,
    messages: list,
    system: str,
    max_tokens: int = 1024,
) -> tuple[str, str]:
    """Run NIM chat; on failure try fallback models. Returns (text, model_used)."""
    from agents.research.llm_client import LLMUnavailable, get_nvidia_llm_client

    primary = nim_model_for_task(task, screening_mode=screening_mode, brief=brief)
    last_exc: Optional[Exception] = None

    for model in nim_fallback_models(primary):
        try:
            client = get_nvidia_llm_client(model=model, auto_switch=False)
            text = client.chat(
                messages=messages,
                system=system,
                max_tokens=max_tokens,
            )
            if model != primary:
                logger.info("NIM chat fell back: %s → %s", primary, model)
            return text, model
        except Exception as exc:
            last_exc = exc
            logger.warning("NIM model %s failed for task %s: %s", model, task, exc)
            continue

    raise LLMUnavailable(f"All NIM models failed for task {task!r}: {last_exc}")


def get_enrich_llm_client(
    temperature: float = 0.0,
    *,
    auto_switch: Optional[bool] = None,
) -> Any:
    """Batch enrich / ontology — NVIDIA task model when configured."""
    from agents.research.llm_client import LLMUnavailable, get_llm_client, get_nvidia_llm_client

    enrich_provider = os.environ.get("ENRICH_LLM_PROVIDER", "").strip().lower()
    enrich_model = os.environ.get("ENRICH_LLM_MODEL", "").strip() or None
    if enrich_provider == "nvidia" and nim_enabled():
        model = enrich_model or nim_model_for_task("enrich")
        return get_nvidia_llm_client(
            model=model,
            temperature=temperature,
            auto_switch=auto_switch if auto_switch is not None else True,
        )
    if enrich_provider and enrich_provider not in ("none", ""):
        return get_llm_client(
            provider=enrich_provider,
            model=enrich_model,
            temperature=temperature,
            auto_switch=auto_switch,
        )
    if nim_enabled():
        model = nim_model_for_task("enrich")
        return get_nvidia_llm_client(
            model=model,
            temperature=temperature,
            auto_switch=auto_switch if auto_switch is not None else True,
        )
    return get_llm_client(temperature=temperature, auto_switch=auto_switch)


def get_gate_chat_llm(kind: str = "qa") -> Any:
    """
    Gate chat LLM — Claude Haiku when Anthropic is configured for gate, else NVIDIA
    (when NIM_FOR_CHAT=true), else PULSE_LLM_PROVIDER default.
    kind: 'qa' for follow-up questions, 'facts' for fact extraction.
    """
    import os

    from agents.research.llm_client import LLMUnavailable, anthropic_configured, get_anthropic_haiku_client, get_llm_client

    gate_provider = os.environ.get("GATE_LLM_PROVIDER", "").strip().lower()
    use_haiku_chat = gate_provider == "anthropic" or (not gate_provider and anthropic_configured())
    if use_haiku_chat:
        return get_anthropic_haiku_client()

    if nim_enabled() and nim_use_for_chat():
        task = "chat" if kind == "qa" else "chat_facts"
        try:
            return get_nim_task_client(task, auto_switch=True)
        except LLMUnavailable:
            pass
    return get_llm_client()

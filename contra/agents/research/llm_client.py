"""
Provider-agnostic LLM client for PULSE research agents.

Wraps `instructor` to enforce strict Pydantic output schemas.
Provider is selected by PULSE_LLM_PROVIDER env var:
    anthropic  → Anthropic Claude (ANTHROPIC_API_KEY)
    openai     → OpenAI GPT (OPENAI_API_KEY)
    gemini     → Google Gemini (GEMINI_API_KEY)
    groq       → Groq inference API (GROQ_API_KEY) — OpenAI-compatible
    nvidia     → NVIDIA NIM (NVAPI_API_KEY) — OpenAI-compatible catalog models
    none / ""  → no LLM; raises LLMUnavailable so callers fall back to local paths

Usage:
    client = get_llm_client()        # may raise LLMUnavailable
    result  = client.structured(
        prompt="Classify this LP...",
        response_model=EnrichmentResult,
        system="You are a private-market analyst.",
    )

Auto-switch (PULSE_LLM_AUTO_SWITCH=true, default): on context-size failures the client
retries with larger models on the same provider, then other configured providers.

Records model name + temperature in cache metadata (deterministic=False doctrine).
"""

from __future__ import annotations

import logging
import os
from typing import Any, List, Optional, Tuple, Type, TypeVar

from pydantic import BaseModel

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

# ---------------------------------------------------------------------------
# Exception hierarchy
# ---------------------------------------------------------------------------

class LLMUnavailable(RuntimeError):
    """Raised when no LLM provider is configured or credentials are missing."""


class LLMExtractionError(RuntimeError):
    """Raised when the LLM returns output that cannot be parsed into the schema."""


# Cost-efficient gate + chat default when using Anthropic (Haiku 3.5 retired 2026-02-19)
HAIKU_MODEL = "claude-haiku-4-5"


def _anthropic_api_key() -> str:
    """ANTHROPIC_API_KEY with CLAUDE_API_KEY alias for .env convenience."""
    return (
        os.environ.get("ANTHROPIC_API_KEY", "").strip()
        or os.environ.get("CLAUDE_API_KEY", "").strip()
    )


def anthropic_configured() -> bool:
    return bool(_anthropic_api_key())


def primary_llm_provider() -> str:
    return os.environ.get("PULSE_LLM_PROVIDER", "none").lower().strip()


def anthropic_only_mode() -> bool:
    """True when PULSE_LLM_PROVIDER=anthropic — no cross-provider fallbacks."""
    return primary_llm_provider() == "anthropic"


def get_anthropic_haiku_client(
    temperature: float = 0.0,
    *,
    auto_switch: Optional[bool] = None,
) -> Any:
    """Claude 3.5 Haiku — default for gate verdict and chat when Anthropic is configured."""
    return get_llm_client(
        provider="anthropic",
        model=HAIKU_MODEL,
        temperature=temperature,
        auto_switch=auto_switch,
    )


# ---------------------------------------------------------------------------
# Provider implementations
# ---------------------------------------------------------------------------

class _AnthropicClient:
    provider = "anthropic"

    def __init__(self, model: str, temperature: float) -> None:
        import anthropic
        import instructor

        api_key = _anthropic_api_key()
        if not api_key:
            raise LLMUnavailable("ANTHROPIC_API_KEY or CLAUDE_API_KEY must be set.")

        self.model = model
        self.temperature = temperature
        self._raw = anthropic.Anthropic(api_key=api_key)
        self._client = instructor.from_anthropic(self._raw)

    def structured(
        self,
        prompt: str,
        response_model: Type[T],
        system: str = "You are a precise structured-data extraction assistant.",
        max_tokens: int = 2048,
    ) -> T:
        try:
            return self._client.messages.create(
                model=self.model,
                max_tokens=max_tokens,
                temperature=self.temperature,
                system=system,
                messages=[{"role": "user", "content": prompt}],
                response_model=response_model,
            )
        except Exception as exc:
            raise LLMExtractionError(f"Anthropic extraction failed: {exc}") from exc

    def chat(
        self,
        messages: list,
        system: str = "You are a helpful assistant.",
        max_tokens: int = 1024,
    ) -> str:
        try:
            resp = self._raw.messages.create(
                model=self.model,
                max_tokens=max_tokens,
                temperature=self.temperature,
                system=system,
                messages=messages,
            )
            return resp.content[0].text
        except Exception as exc:
            raise LLMExtractionError(f"Anthropic chat failed: {exc}") from exc

    @property
    def meta(self) -> dict:
        return {"provider": self.provider, "model": self.model, "temperature": self.temperature}


class _OpenAIClient:
    provider = "openai"

    def __init__(self, model: str, temperature: float) -> None:
        import instructor
        from openai import OpenAI

        self.model = model
        self.temperature = temperature
        self._raw = OpenAI()
        self._client = instructor.from_openai(self._raw)

    def structured(
        self,
        prompt: str,
        response_model: Type[T],
        system: str = "You are a precise structured-data extraction assistant.",
        max_tokens: int = 2048,
    ) -> T:
        try:
            return self._client.chat.completions.create(
                model=self.model,
                temperature=self.temperature,
                max_tokens=max_tokens,
                response_model=response_model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": prompt},
                ],
            )
        except Exception as exc:
            raise LLMExtractionError(f"OpenAI extraction failed: {exc}") from exc

    def chat(
        self,
        messages: list,
        system: str = "You are a helpful assistant.",
        max_tokens: int = 1024,
    ) -> str:
        try:
            resp = self._raw.chat.completions.create(
                model=self.model,
                temperature=self.temperature,
                max_tokens=max_tokens,
                messages=[{"role": "system", "content": system}] + messages,
            )
            return resp.choices[0].message.content or ""
        except Exception as exc:
            raise LLMExtractionError(f"OpenAI chat failed: {exc}") from exc

    @property
    def meta(self) -> dict:
        return {"provider": self.provider, "model": self.model, "temperature": self.temperature}


_NIM_DEFAULT_BASE_URL = "https://integrate.api.nvidia.com/v1"
# Reasoning NIM models emit a thinking trace that breaks instructor JSON mode.
_NIM_STRUCTURED_EXTRA_BODY = {"chat_template_kwargs": {"enable_thinking": False}}


def _get_nvidia_api_key() -> str:
    """NVAPI_API_KEY from build.nvidia.com; NGC_API_KEY for nvcr.io / self-hosted NIM."""
    return (
        os.environ.get("NVAPI_API_KEY", "").strip()
        or os.environ.get("NGC_API_KEY", "").strip()
        or os.environ.get("NVA_API_KEY", "").strip()
    )


def _get_nim_base_url() -> str:
    return os.environ.get("NIM_BASE_URL", "").strip() or _NIM_DEFAULT_BASE_URL


def nvidia_configured() -> bool:
    """True when hosted NIM API key or a local NIM endpoint is set."""
    return bool(
        _get_nvidia_api_key()
        or os.environ.get("NIM_BASE_URL", "").strip()
        or os.environ.get("NIM_RERANK_BASE_URL", "").strip()
    )


class _NvidiaNIMClient:
    """
    NVIDIA NIM — OpenAI-compatible hosted API or self-hosted NIM container.

    Requires: pip install openai; NVAPI_API_KEY and/or NIM_BASE_URL.
    Model IDs use the catalog path, e.g. meta/llama-3.3-70b-instruct.
    """
    provider = "nvidia"

    def __init__(self, model: str, temperature: float) -> None:
        import instructor
        from openai import OpenAI

        self.model = model
        self.temperature = temperature
        api_key = _get_nvidia_api_key() or "EMPTY"
        self._raw = OpenAI(base_url=_get_nim_base_url(), api_key=api_key)
        self._client = instructor.from_openai(self._raw)

    def structured(
        self,
        prompt: str,
        response_model: Type[T],
        system: str = "You are a precise structured-data extraction assistant.",
        max_tokens: int = 2048,
    ) -> T:
        try:
            return self._client.chat.completions.create(
                model=self.model,
                temperature=self.temperature,
                max_tokens=max_tokens,
                response_model=response_model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": prompt},
                ],
                extra_body=_NIM_STRUCTURED_EXTRA_BODY,
            )
        except Exception as exc:
            raise LLMExtractionError(f"NVIDIA NIM extraction failed: {exc}") from exc

    def chat(
        self,
        messages: list,
        system: str = "You are a helpful assistant.",
        max_tokens: int = 1024,
    ) -> str:
        try:
            resp = self._raw.chat.completions.create(
                model=self.model,
                temperature=self.temperature,
                max_tokens=max_tokens,
                messages=[{"role": "system", "content": system}] + messages,
                extra_body=_NIM_STRUCTURED_EXTRA_BODY,
            )
            return resp.choices[0].message.content or ""
        except Exception as exc:
            raise LLMExtractionError(f"NVIDIA NIM chat failed: {exc}") from exc

    @property
    def meta(self) -> dict:
        return {"provider": self.provider, "model": self.model, "temperature": self.temperature}


def _get_groq_api_key() -> str:
    """
    Returns the first non-exhausted Groq API key found in the environment.

    Checks GROQ_API_KEY, GROQ_API_KEY_2, GROQ_API_KEY_3, …, GROQ_API_KEY_9.
    All keys share the same model pool — cycling them spreads TPD usage across
    multiple free accounts.
    """
    slots = ["GROQ_API_KEY"] + [f"GROQ_API_KEY_{i}" for i in range(2, 10)]
    for slot in slots:
        key = os.environ.get(slot, "").strip()
        if key:
            return key
    return ""


class _GroqClient:
    """
    Groq inference API — uses the OpenAI-compatible endpoint with instructor.

    Groq runs open-source models (Llama-3, Mixtral, Gemma) at very low latency
    and is free for moderate usage. Structured extraction works via instructor's
    JSON mode (same path as OpenAI).

    Multi-key rotation: set GROQ_API_KEY, GROQ_API_KEY_2, GROQ_API_KEY_3 …
    to spread token usage across multiple free accounts. The client picks the
    first available key; the auto-switcher cycles through all (provider, model)
    candidates, so each key+model pair gets its own attempt before giving up.

    Requires: pip install groq; GROQ_API_KEY env var.
    Default model: llama-3.3-70b-versatile
    Compact fallback: llama-3.1-8b-instant (only works with compact_web=True)
    """
    provider = "groq"

    def __init__(self, model: str, temperature: float, api_key: str = "") -> None:
        try:
            import instructor
            from groq import Groq
        except ImportError as exc:
            raise LLMUnavailable(
                "groq package is not installed. Run: pip install groq"
            ) from exc

        self.model = model
        self.temperature = temperature
        key = api_key or _get_groq_api_key()
        self._raw = Groq(api_key=key) if key else Groq()
        self._client = instructor.from_groq(self._raw, mode=instructor.Mode.JSON)

    def _make_client_for_key(self, api_key: str):
        import instructor
        from groq import Groq
        raw = Groq(api_key=api_key)
        return raw, instructor.from_groq(raw, mode=instructor.Mode.JSON)

    def structured(
        self,
        prompt: str,
        response_model: Type[T],
        system: str = "You are a precise structured-data extraction assistant.",
        max_tokens: int = 2048,
    ) -> T:
        keys = _all_groq_keys()
        last_exc: Exception = RuntimeError("No Groq keys configured")
        for key in keys:
            raw, client = self._make_client_for_key(key)
            try:
                result = client.chat.completions.create(
                    model=self.model,
                    temperature=self.temperature,
                    max_tokens=max_tokens,
                    response_model=response_model,
                    messages=[
                        {"role": "system", "content": system},
                        {"role": "user", "content": prompt},
                    ],
                )
                return result
            except Exception as exc:
                msg = str(exc).lower()
                is_quota = any(m in msg for m in ("tokens per day", "tpd", "rate limit", "429"))
                if is_quota and len(keys) > 1:
                    logger.info("Groq key exhausted (TPD), rotating to next key")
                    last_exc = exc
                    continue
                raise LLMExtractionError(f"Groq extraction failed: {exc}") from exc
        raise LLMExtractionError(f"All Groq keys exhausted: {last_exc}") from last_exc

    def chat(
        self,
        messages: list,
        system: str = "You are a helpful assistant.",
        max_tokens: int = 1024,
    ) -> str:
        keys = _all_groq_keys()
        last_exc: Exception = RuntimeError("No Groq keys configured")
        for key in keys:
            raw, _ = self._make_client_for_key(key)
            try:
                resp = raw.chat.completions.create(
                    model=self.model,
                    temperature=self.temperature,
                    max_tokens=max_tokens,
                    messages=[{"role": "system", "content": system}] + messages,
                )
                return resp.choices[0].message.content or ""
            except Exception as exc:
                msg = str(exc).lower()
                is_quota = any(m in msg for m in ("tokens per day", "tpd", "rate limit", "429"))
                if is_quota and len(keys) > 1:
                    logger.info("Groq key exhausted (TPD), rotating to next key")
                    last_exc = exc
                    continue
                raise LLMExtractionError(f"Groq chat failed: {exc}") from exc
        raise LLMExtractionError(f"All Groq keys exhausted: {last_exc}") from last_exc

    @property
    def meta(self) -> dict:
        return {"provider": self.provider, "model": self.model, "temperature": self.temperature}


# ---------------------------------------------------------------------------
# Auto-switcher — retry on context / request-size failures
# ---------------------------------------------------------------------------

# Substrings that indicate the prompt is too large for the current model tier.
_OVERSIZED_ERROR_MARKERS = (
    "request too large",
    "reduce your message size",
    "reduce the length",
    "context length",
    "maximum context",
    "context window",
    "prompt is too long",
    "too many tokens",
    "token limit",
    "exceeds the maximum",
    "input is too long",
)

# Substrings that indicate a rate-limit, quota exhaustion, or dead model — switchable.
_RATE_LIMIT_MARKERS = (
    "rate limit",
    "rate_limit",
    "tokens per day",
    "tokens per minute",
    "requests per day",
    "requests per minute",
    "quota exceeded",
    "quota_exceeded",
    "too many requests",
    "429",
    "tpd",
    "tpm",
    "rpm",
    "please try again in",
    "try again later",
    "decommissioned",
    "model_decommissioned",
    "no longer supported",
    "deprecated",
    "not found",
    "404",
    "function ",
)

# Larger-context models to try within the same provider before cross-provider fallback.
# Only include models that are currently live — decommissioned models cause 400 errors.
_PROVIDER_UPGRADES: dict[str, tuple[str, ...]] = {
    # 8b: free-tier TPM cap is 6k tokens/request. Only works when compact_web=True (~4.5k tokens).
    "groq": ("llama-3.3-70b-versatile", "llama-3.1-8b-instant"),
    "anthropic": ("claude-sonnet-4-6",),
    "openai": ("gpt-4o",),
    "nvidia": (
        "deepseek-ai/deepseek-v3.2",
        "minimaxai/minimax-m2.5",
        "mistralai/mistral-small-4-119b-2603",
        "meta/llama-3.3-70b-instruct",
    ),
}

# Cross-provider fallbacks when the primary (and upgrades) cannot fit the prompt.
# Order: cost-effective models with generous context windows.
_CROSS_PROVIDER_FALLBACKS: tuple[tuple[str, str], ...] = (
    ("nvidia", "meta/llama-3.3-70b-instruct"),
    ("anthropic", HAIKU_MODEL),
    ("openai", "gpt-4o-mini"),
    ("groq", "llama-3.3-70b-versatile"),
)


def _auto_switch_enabled() -> bool:
    return os.environ.get("PULSE_LLM_AUTO_SWITCH", "true").lower().strip() in (
        "1", "true", "yes", "on",
    )


def _has_api_key(provider: str) -> bool:
    if provider == "groq":
        return bool(_get_groq_api_key())
    if provider == "nvidia":
        return nvidia_configured()
    if provider == "anthropic":
        return anthropic_configured()
    key_env = _KEY_ENV.get(provider)
    return bool(key_env and os.environ.get(key_env, "").strip())


def _all_groq_keys() -> list[str]:
    """Return all configured Groq API keys (GROQ_API_KEY, GROQ_API_KEY_2, …)."""
    slots = ["GROQ_API_KEY"] + [f"GROQ_API_KEY_{i}" for i in range(2, 10)]
    return [os.environ.get(s, "").strip() for s in slots if os.environ.get(s, "").strip()]


def _is_oversized_error(exc: Exception) -> bool:
    """True when the failure is likely fixable by switching to a larger-context model."""
    msg = str(exc).lower()
    return any(marker in msg for marker in _OVERSIZED_ERROR_MARKERS)


def _is_rate_limit_error(exc: Exception) -> bool:
    """True when the failure is a provider rate-limit / daily quota exhaustion (switchable)."""
    msg = str(exc).lower()
    return any(marker in msg for marker in _RATE_LIMIT_MARKERS)


def _is_switchable_error(exc: Exception) -> bool:
    """True when switching to another provider/model is worth attempting."""
    return _is_oversized_error(exc) or _is_rate_limit_error(exc)


def _build_fallback_candidates(
    primary_provider: str,
    primary_model: str,
) -> List[Tuple[str, str]]:
    """
    Ordered (provider, model) pairs to try. Primary first, then same-provider
    upgrades, then any other configured providers with large-context defaults.
    """
    candidates: List[Tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()

    def add(provider: str, model: str) -> None:
        key = (provider, model)
        if key in seen or provider not in _BUILDERS or not _has_api_key(provider):
            return
        candidates.append(key)
        seen.add(key)

    add(primary_provider, primary_model)

    for model in _PROVIDER_UPGRADES.get(primary_provider, ()):
        if model != primary_model:
            add(primary_provider, model)

    if not anthropic_only_mode():
        for provider, model in _CROSS_PROVIDER_FALLBACKS:
            add(provider, model)

    return candidates


def _create_raw_client(provider: str, model: str, temperature: float) -> Any:
    if not _has_api_key(provider):
        raise LLMUnavailable(f"Provider '{provider}' requires {_KEY_ENV[provider]} to be set.")
    try:
        return _BUILDERS[provider](model=model, temperature=temperature)
    except ImportError as exc:
        raise LLMUnavailable(
            f"Package for provider '{provider}' is not installed. "
            f"Run: pip install -r requirements-llm.txt\n{exc}"
        ) from exc


class ResilientLLMClient:
    """
    Wraps one or more provider clients and auto-switches on context-size failures.

    On oversized-request errors (e.g. Groq 413 / TPM limit), retries with larger
    models on the same provider, then other configured providers. Non-size errors
    from the primary candidate are raised immediately.
    """

    def __init__(
        self,
        candidates: List[Tuple[str, str]],
        temperature: float = 0.0,
    ) -> None:
        if not candidates:
            raise LLMUnavailable("No LLM candidates available for auto-switch.")
        self._candidates = candidates
        self._temperature = temperature
        self._primary = candidates[0]
        self._active: Optional[Tuple[str, str]] = None

    @property
    def meta(self) -> dict:
        if self._active:
            provider, model = self._active
            return {"provider": provider, "model": model, "temperature": self._temperature}
        provider, model = self._primary
        return {"provider": provider, "model": model, "temperature": self._temperature}

    def structured(
        self,
        prompt: str,
        response_model: Type[T],
        system: str = "You are a precise structured-data extraction assistant.",
        max_tokens: int = 2048,
    ) -> T:
        return self._call_with_fallback(
            "structured",
            prompt=prompt,
            response_model=response_model,
            system=system,
            max_tokens=max_tokens,
        )

    def chat(
        self,
        messages: list,
        system: str = "You are a helpful assistant.",
        max_tokens: int = 1024,
    ) -> str:
        return self._call_with_fallback(
            "chat",
            messages=messages,
            system=system,
            max_tokens=max_tokens,
        )

    def _call_with_fallback(self, method: str, **kwargs: Any) -> Any:
        failures: List[str] = []
        for provider, model in self._candidates:
            try:
                client = _create_raw_client(provider, model, self._temperature)
                result = getattr(client, method)(**kwargs)
                self._active = (provider, model)
                if (provider, model) != self._primary:
                    logger.warning(
                        "LLM auto-switched after context-size failure: %s/%s → %s/%s",
                        self._primary[0], self._primary[1], provider, model,
                    )
                return result
            except LLMUnavailable:
                failures.append(f"{provider}/{model}: unavailable (missing key or package)")
                continue
            except LLMExtractionError as exc:
                if _is_switchable_error(exc):
                    reason = "rate-limited" if _is_rate_limit_error(exc) else "oversized"
                    failures.append(f"{provider}/{model}: {exc}")
                    logger.info(
                        "LLM candidate %s/%s %s, trying next provider",
                        provider, model, reason,
                    )
                    continue
                raise
            except Exception as exc:
                if _is_switchable_error(exc):
                    reason = "rate-limited" if _is_rate_limit_error(exc) else "oversized"
                    failures.append(f"{provider}/{model}: {exc}")
                    logger.info(
                        "LLM candidate %s/%s %s, trying next provider",
                        provider, model, reason,
                    )
                    continue
                raise LLMExtractionError(
                    f"{provider}/{model} {method} failed: {exc}"
                ) from exc

        detail = "\n".join(failures) if failures else "no candidates"
        raise LLMExtractionError(
            f"All LLM candidates exhausted ({method}). Configure another provider API key "
            f"or set PULSE_LLM_MODEL to a larger-context model.\n{detail}"
        )


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

# Default models per provider
_DEFAULT_MODELS: dict[str, str] = {
    "anthropic": HAIKU_MODEL,
    # gpt-4o is the default for OpenAI — significantly better structured reasoning
    # than gpt-4o-mini for gate screening decisions. ~$0.016/call vs $0.001 for mini.
    # Set PULSE_LLM_MODEL=gpt-4o-mini to revert to the cheaper mini model.
    "openai": "gpt-4o",
    "groq": "llama-3.3-70b-versatile",
    "nvidia": "meta/llama-3.3-70b-instruct",
}

# Env var names for API keys
_KEY_ENV: dict[str, str] = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "groq": "GROQ_API_KEY",
    "nvidia": "NVAPI_API_KEY",
}

_BUILDERS: dict[str, type] = {
    "anthropic": _AnthropicClient,
    "openai": _OpenAIClient,
    "groq": _GroqClient,
    "nvidia": _NvidiaNIMClient,
}

_MODEL_PREFIXES: dict[str, tuple[str, ...]] = {
    "anthropic": ("claude-",),
    "openai": ("gpt-", "o1", "o3", "chatgpt-"),
    "groq": ("llama-", "mixtral-", "gemma-", "qwen/", "deepseek-"),
    "nvidia": (),
}


def _model_matches_provider(provider: str, model: str) -> bool:
    if provider == "nvidia":
        return bool(model.strip())
    prefixes = _MODEL_PREFIXES.get(provider, ())
    low = model.lower().strip()
    return any(low.startswith(p) for p in prefixes)


def _resolve_model(provider: str, model: Optional[str] = None) -> str:
    default = _DEFAULT_MODELS[provider]
    requested = (model or os.environ.get("PULSE_LLM_MODEL") or "").strip()
    if not requested:
        return default
    if _model_matches_provider(provider, requested):
        return requested
    logger.warning(
        "PULSE_LLM_MODEL=%r does not match provider %r — using default %r. "
        "Remove PULSE_LLM_MODEL from .env or set a %s model name.",
        requested, provider, default, provider,
    )
    return default


def get_llm_client(
    provider: Optional[str] = None,
    model: Optional[str] = None,
    temperature: float = 0.0,
    *,
    auto_switch: Optional[bool] = None,
) -> Any:
    """
    Return a configured LLM client for structured extraction.

    provider:    overrides PULSE_LLM_PROVIDER env var.
    model:       overrides the per-provider default.
    auto_switch: when True (default via PULSE_LLM_AUTO_SWITCH), returns a
                 ResilientLLMClient that retries with larger models / other
                 configured providers on context-size failures (e.g. Groq 413).

    Raises LLMUnavailable if:
      - provider is "none" or empty
      - the required API key is missing
      - the provider package is not installed

    Callers should catch LLMUnavailable and fall back to local/deterministic paths.
    """
    resolved_provider = (provider or os.environ.get("PULSE_LLM_PROVIDER", "none")).lower().strip()

    if resolved_provider in ("none", ""):
        raise LLMUnavailable(
            "No LLM provider configured. Set PULSE_LLM_PROVIDER to anthropic, openai, groq, or nvidia."
        )

    if resolved_provider not in _BUILDERS:
        raise LLMUnavailable(
            f"Unknown LLM provider '{resolved_provider}'. "
            f"Valid options: {sorted(_BUILDERS.keys())}."
        )

    if not _has_api_key(resolved_provider):
        raise LLMUnavailable(
            f"Provider '{resolved_provider}' requires {_KEY_ENV[resolved_provider]} to be set."
        )

    resolved_model = _resolve_model(resolved_provider, model)

    use_auto_switch = _auto_switch_enabled() if auto_switch is None else auto_switch

    if use_auto_switch:
        candidates = _build_fallback_candidates(resolved_provider, resolved_model)
        logger.debug(
            "Resilient LLM client created",
            extra={
                "primary": f"{resolved_provider}/{resolved_model}",
                "candidates": [f"{p}/{m}" for p, m in candidates],
            },
        )
        return ResilientLLMClient(candidates=candidates, temperature=temperature)

    client = _create_raw_client(resolved_provider, resolved_model, temperature)
    logger.debug(
        "LLM client created",
        extra={"provider": resolved_provider, "model": resolved_model},
    )
    return client


def get_nvidia_llm_client(
    model: Optional[str] = None,
    temperature: float = 0.0,
    *,
    auto_switch: Optional[bool] = None,
) -> Any:
    """NVIDIA NIM client for knowledge enrichment and optional secondary workloads."""
    resolved_model = (model or os.environ.get("ENRICH_LLM_MODEL") or "").strip() or _DEFAULT_MODELS["nvidia"]
    return get_llm_client(
        provider="nvidia",
        model=resolved_model,
        temperature=temperature,
        auto_switch=auto_switch,
    )


def get_enrich_llm_client(
    temperature: float = 0.0,
    *,
    auto_switch: Optional[bool] = None,
) -> Any:
    """Delegate to nim_router — per-task NVIDIA models for batch enrich / ontology."""
    from agents.research.nim_router import get_enrich_llm_client as _enrich
    return _enrich(temperature=temperature, auto_switch=auto_switch)

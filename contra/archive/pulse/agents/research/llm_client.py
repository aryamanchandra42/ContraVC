"""
Provider-agnostic LLM client for PULSE research agents.

Wraps `instructor` to enforce strict Pydantic output schemas.
Provider is selected by PULSE_LLM_PROVIDER env var:
    anthropic  → Anthropic Claude (ANTHROPIC_API_KEY)
    openai     → OpenAI GPT (OPENAI_API_KEY)
    gemini     → Google Gemini (GEMINI_API_KEY)
    groq       → Groq inference API (GROQ_API_KEY) — OpenAI-compatible
    none / ""  → no LLM; raises LLMUnavailable so callers fall back to local paths

Usage:
    client = get_llm_client()        # may raise LLMUnavailable
    result  = client.structured(
        prompt="Classify this LP...",
        response_model=EnrichmentResult,
        system="You are a private-market analyst.",
    )

Records model name + temperature in cache metadata (deterministic=False doctrine).
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional, Type, TypeVar

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


# ---------------------------------------------------------------------------
# Provider implementations
# ---------------------------------------------------------------------------

class _AnthropicClient:
    provider = "anthropic"

    def __init__(self, model: str, temperature: float) -> None:
        import anthropic
        import instructor

        self.model = model
        self.temperature = temperature
        self._client = instructor.from_anthropic(anthropic.Anthropic())

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
        self._client = instructor.from_openai(OpenAI())

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

    @property
    def meta(self) -> dict:
        return {"provider": self.provider, "model": self.model, "temperature": self.temperature}


class _GroqClient:
    """
    Groq inference API — uses the OpenAI-compatible endpoint with instructor.

    Groq runs open-source models (Llama-3, Mixtral, Gemma) at very low latency
    and is free for moderate usage. Structured extraction works via instructor's
    JSON mode (same path as OpenAI).

    Requires: pip install groq; GROQ_API_KEY env var.
    Default model: llama-3.1-8b-instant  (fast + free tier)
    For better accuracy on complex schemas: llama-3.3-70b-versatile
    """
    provider = "groq"

    def __init__(self, model: str, temperature: float) -> None:
        try:
            import instructor
            from groq import Groq
        except ImportError as exc:
            raise LLMUnavailable(
                "groq package is not installed. Run: pip install groq"
            ) from exc

        self.model = model
        self.temperature = temperature
        self._client = instructor.from_groq(Groq(), mode=instructor.Mode.JSON)

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
            raise LLMExtractionError(f"Groq extraction failed: {exc}") from exc

    @property
    def meta(self) -> dict:
        return {"provider": self.provider, "model": self.model, "temperature": self.temperature}


class _GeminiClient:
    provider = "gemini"

    def __init__(self, model: str, temperature: float) -> None:
        import instructor
        import google.generativeai as genai

        api_key = os.environ.get("GEMINI_API_KEY", "")
        if not api_key:
            raise LLMUnavailable("GEMINI_API_KEY is not set.")
        genai.configure(api_key=api_key)
        self.model = model
        self.temperature = temperature
        self._client = instructor.from_gemini(
            client=genai.GenerativeModel(model_name=model),
            mode=instructor.Mode.GEMINI_JSON,
        )

    def structured(
        self,
        prompt: str,
        response_model: Type[T],
        system: str = "You are a precise structured-data extraction assistant.",
        max_tokens: int = 2048,
    ) -> T:
        try:
            return self._client.chat.completions.create(
                response_model=response_model,
                messages=[
                    {"role": "user", "content": f"{system}\n\n{prompt}"},
                ],
            )
        except Exception as exc:
            raise LLMExtractionError(f"Gemini extraction failed: {exc}") from exc

    @property
    def meta(self) -> dict:
        return {"provider": self.provider, "model": self.model, "temperature": self.temperature}


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

# Default models per provider
_DEFAULT_MODELS: dict[str, str] = {
    "anthropic": "claude-3-5-haiku-20241022",
    "openai": "gpt-4o-mini",
    "gemini": "gemini-1.5-flash",
    "groq": "llama-3.1-8b-instant",   # fast + generous free tier; upgrade to llama-3.3-70b-versatile for accuracy
}

# Env var names for API keys
_KEY_ENV: dict[str, str] = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "groq": "GROQ_API_KEY",
}

_BUILDERS: dict[str, type] = {
    "anthropic": _AnthropicClient,
    "openai": _OpenAIClient,
    "gemini": _GeminiClient,
    "groq": _GroqClient,
}


def get_llm_client(
    provider: Optional[str] = None,
    model: Optional[str] = None,
    temperature: float = 0.0,
) -> Any:
    """
    Return a configured LLM client for structured extraction.

    provider: overrides PULSE_LLM_PROVIDER env var.
    model:    overrides the per-provider default.

    Raises LLMUnavailable if:
      - provider is "none" or empty
      - the required API key is missing
      - the provider package is not installed

    Callers should catch LLMUnavailable and fall back to local/deterministic paths.
    """
    resolved_provider = (provider or os.environ.get("PULSE_LLM_PROVIDER", "none")).lower().strip()

    if resolved_provider in ("none", ""):
        raise LLMUnavailable(
            "No LLM provider configured. Set PULSE_LLM_PROVIDER to anthropic, openai, gemini, or groq."
        )

    if resolved_provider not in _BUILDERS:
        raise LLMUnavailable(
            f"Unknown LLM provider '{resolved_provider}'. "
            f"Valid options: {sorted(_BUILDERS.keys())}."
        )

    key_env = _KEY_ENV[resolved_provider]
    api_key = os.environ.get(key_env, "")
    if not api_key:
        raise LLMUnavailable(
            f"Provider '{resolved_provider}' requires {key_env} to be set."
        )

    resolved_model = model or os.environ.get(
        "PULSE_LLM_MODEL", _DEFAULT_MODELS[resolved_provider]
    )

    try:
        client = _BUILDERS[resolved_provider](model=resolved_model, temperature=temperature)
        logger.debug(
            "LLM client created",
            extra={"provider": resolved_provider, "model": resolved_model},
        )
        return client
    except ImportError as exc:
        raise LLMUnavailable(
            f"Package for provider '{resolved_provider}' is not installed. "
            f"Run: pip install -r requirements-llm.txt\n{exc}"
        ) from exc

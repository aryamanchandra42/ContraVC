"""Anthropic web-search model selection and 404 fallback."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agents.research.web_search import AnthropicWebSearchProvider


class _FakeMessages:
    def __init__(self, outcomes: dict[str, object]) -> None:
        self.outcomes = outcomes
        self.calls: list[str] = []

    def create(self, *, model: str, **kwargs):
        self.calls.append(model)
        outcome = self.outcomes[model]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def _provider_with_fake_client(monkeypatch, outcomes: dict[str, object]) -> AnthropicWebSearchProvider:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.delenv("ANTHROPIC_SEARCH_MODEL", raising=False)
    provider = AnthropicWebSearchProvider.__new__(AnthropicWebSearchProvider)
    provider._client = SimpleNamespace(messages=_FakeMessages(outcomes))
    provider.model = AnthropicWebSearchProvider._SEARCH_MODELS[0]
    return provider


def test_retired_env_model_is_ignored(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("ANTHROPIC_SEARCH_MODEL", "claude-3-5-sonnet-20241022")
    monkeypatch.setitem(
        __import__("sys").modules,
        "anthropic",
        SimpleNamespace(Anthropic=lambda api_key: SimpleNamespace()),
    )
    provider = AnthropicWebSearchProvider()
    assert provider.model == "claude-sonnet-4-6"


def test_messages_with_search_falls_back_on_404(monkeypatch):
    err = RuntimeError(
        "Error code: 404 - {'type': 'error', 'error': {'type': 'not_found_error', "
        "'message': 'model: claude-sonnet-4-0'}}"
    )
    ok = SimpleNamespace(
        content=[SimpleNamespace(type="text", text="found it", citations=[])]
    )
    provider = _provider_with_fake_client(
        monkeypatch,
        {
            "claude-sonnet-4-0": err,
            "claude-sonnet-4-6": ok,
        },
    )
    provider.model = "claude-sonnet-4-0"

    text, citations = provider._messages_with_search("search for LPs")

    assert text == "found it"
    assert citations == []
    assert provider.model == "claude-sonnet-4-6"
    assert provider._client.messages.calls == [
        "claude-sonnet-4-0",
        "claude-sonnet-4-6",
    ]


def test_messages_with_search_raises_when_all_models_missing(monkeypatch):
    err = RuntimeError("Error code: 404 - model not found")
    provider = _provider_with_fake_client(
        monkeypatch,
        {model: err for model in AnthropicWebSearchProvider._SEARCH_MODELS},
    )

    with pytest.raises(RuntimeError, match="404"):
        provider._messages_with_search("search for LPs")

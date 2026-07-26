"""web_search_log wrapper — never breaks search on log failure."""

from __future__ import annotations

from agents.research.search_log import LoggingSearchProvider
from agents.research.web_search import SearchResponse, SearchResult


class _FakeProvider:
    def search(self, query: str, max_results: int = 5) -> SearchResponse:
        return SearchResponse(
            query=query,
            results=[SearchResult(title="t", url="https://example.com", snippet="s")],
        )

    def fetch(self, url: str) -> str:
        return "ok"


def test_logging_provider_returns_inner_results(monkeypatch):
    calls = []

    def _fake_log(**kwargs):
        calls.append(kwargs)

    monkeypatch.setattr("agents.research.search_log.log_web_search", _fake_log)
    wrapped = LoggingSearchProvider(_FakeProvider(), provider_name="fake")
    resp = wrapped.search("family office LP", max_results=3)
    assert len(resp.results) == 1
    assert resp.results[0].url == "https://example.com"
    assert len(calls) == 1
    assert calls[0]["provider"] == "fake"
    assert calls[0]["query"] == "family office LP"
    assert calls[0]["error"] == ""


def test_logging_provider_rethrows_and_logs_error(monkeypatch):
    class Boom:
        def search(self, query: str, max_results: int = 5):
            raise RuntimeError("quota")

        def fetch(self, url: str) -> str:
            return ""

    calls = []
    monkeypatch.setattr("agents.research.search_log.log_web_search", lambda **k: calls.append(k))
    wrapped = LoggingSearchProvider(Boom(), provider_name="boom")
    try:
        wrapped.search("x")
        assert False, "expected RuntimeError"
    except RuntimeError as exc:
        assert "quota" in str(exc)
    assert calls and "quota" in calls[0]["error"]

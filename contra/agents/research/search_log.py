"""
Durable log of every web search — MotherDuck table `web_search_log`.

Anthropic/OpenAI/Tavily calls are otherwise only billed on the provider dashboard.
This table keeps the query, provider, hit URLs, and caller context so spend is
auditable and research is recoverable after the in-memory gate session expires.

Past Anthropic usage (e.g. the 809 searches already billed) cannot be backfilled —
logging starts from deploy forward.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Dict, Generator, List, Optional

logger = logging.getLogger(__name__)

_source_var: ContextVar[str] = ContextVar("web_search_source", default="unknown")
_meta_var: ContextVar[Dict[str, Any]] = ContextVar("web_search_meta", default={})
_write_lock = threading.Lock()


@contextmanager
def search_context(
    source: str,
    *,
    investor_name: str = "",
    run_id: str = "",
    session_id: str = "",
    **extra: Any,
) -> Generator[None, None, None]:
    """Annotate searches in this call stack (gate / prospector / discovery)."""
    meta = {
        "investor_name": investor_name or None,
        "run_id": run_id or None,
        "session_id": session_id or None,
        **{k: v for k, v in extra.items() if v is not None},
    }
    t_src = _source_var.set(source)
    t_meta = _meta_var.set(meta)
    try:
        yield
    finally:
        _source_var.reset(t_src)
        _meta_var.reset(t_meta)


def log_web_search(
    *,
    provider: str,
    query: str,
    results: Optional[List[Any]] = None,
    cached: bool = False,
    error: str = "",
    duration_ms: Optional[float] = None,
    max_results: Optional[int] = None,
) -> None:
    """Best-effort insert. Never raises — search must not fail because of logging."""
    try:
        _write_log(
            provider=provider,
            query=query,
            results=results or [],
            cached=cached,
            error=error,
            duration_ms=duration_ms,
            max_results=max_results,
        )
    except Exception as exc:
        logger.debug("web_search_log write skipped: %s", exc)


def _log_connection():
    """Prefer the API process shared connection; fall back to a fresh one."""
    try:
        from api.deps import _shared_connection

        return _shared_connection()
    except Exception:
        from agents.db import get_conn

        return get_conn()


def _write_log(
    *,
    provider: str,
    query: str,
    results: List[Any],
    cached: bool,
    error: str,
    duration_ms: Optional[float],
    max_results: Optional[int],
) -> None:
    source = _source_var.get() or "unknown"
    meta = dict(_meta_var.get() or {})
    urls: List[Dict[str, Any]] = []
    for r in results[:20]:
        urls.append({
            "title": (getattr(r, "title", None) or "")[:300],
            "url": (getattr(r, "url", None) or "")[:800],
            "snippet": (getattr(r, "snippet", None) or "")[:500],
        })

    log_id = uuid.uuid4().hex
    row = [
        log_id,
        (provider or "")[:64],
        (source or "unknown")[:64],
        (query or "")[:2000],
        len(results),
        json.dumps(urls),
        bool(cached),
        (error or "")[:800] or None,
        float(duration_ms) if duration_ms is not None else None,
        int(max_results) if max_results is not None else None,
        (meta.get("investor_name") or None),
        (meta.get("run_id") or None),
        (meta.get("session_id") or None),
        json.dumps(meta) if meta else None,
    ]

    with _write_lock:
        con = _log_connection()
        cur = con.cursor() if hasattr(con, "cursor") else con
        try:
            cur.execute(
                """
                INSERT INTO web_search_log (
                    log_id, provider, source, query, result_count, urls_json,
                    cached, error, duration_ms, max_results,
                    investor_name, run_id, session_id, meta_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                row,
            )
        finally:
            if cur is not con:
                try:
                    cur.close()
                except Exception:
                    pass


class LoggingSearchProvider:
    """Wraps any WebSearchProvider and persists each search to web_search_log."""

    def __init__(self, inner: Any, provider_name: str = "") -> None:
        self._inner = inner
        self.provider = (
            provider_name
            or getattr(inner, "provider", None)
            or type(inner).__name__.replace("Provider", "").replace("WebSearch", "").lower()
        )
        # Expose common attrs used by harvest/corroborate.
        if hasattr(inner, "model"):
            self.model = inner.model

    def search(self, query: str, max_results: int = 5) -> Any:
        t0 = time.perf_counter()
        err = ""
        resp = None
        try:
            resp = self._inner.search(query, max_results=max_results)
            return resp
        except Exception as exc:
            err = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            elapsed = (time.perf_counter() - t0) * 1000
            results = list(getattr(resp, "results", None) or []) if resp is not None else []
            cached = bool(getattr(resp, "cached", False)) if resp is not None else False
            log_web_search(
                provider=self.provider,
                query=query,
                results=results,
                cached=cached,
                error=err,
                duration_ms=round(elapsed, 1),
                max_results=max_results,
            )

    def fetch(self, url: str) -> str:
        return self._inner.fetch(url)

    def research(self, prompt: str):
        if hasattr(self._inner, "research"):
            return self._inner.research(prompt)
        raise AttributeError(f"{type(self._inner).__name__} has no research()")

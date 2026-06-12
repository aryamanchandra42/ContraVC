"""
NVIDIA NeMo Retriever reranking — score Tavily passages against an LP-specific query.

Uses POST /v1/ranking (not chat/completions). Model default:
  nvidia/llama-nemotron-rerank-vl-1b-v2  (text + image; text-only passages OK)

Hosted integrate.api.nvidia.com may not expose /v1/ranking on all accounts — if the
API returns 404, results keep Tavily order. Self-host the rerank NIM container and set
NIM_RERANK_BASE_URL=http://localhost:8001/v1 (Contra API uses :8000).
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from typing import List, Optional

from agents.research.web_search import SearchResult

logger = logging.getLogger(__name__)

_DEFAULT_RERANK_MODEL = "nvidia/llama-nemotron-rerank-vl-1b-v2"
_TEXT_RERANK_MODEL = "nvidia/llama-nemotron-rerank-1b-v2"
_DEFAULT_LOCAL_RERANK_PORT = "8001"
# Contra API commonly binds :8000 — rerank NIM must use a different host port.
_CONTRA_API_PORTS = frozenset({"8000"})

_RERANK_CIRCUIT_OPEN = False
_RERANK_PORT_WARNED = False


def _rerank_flag_enabled() -> bool:
    return os.environ.get("NIM_RERANK_ENABLED", "false").lower().strip() in (
        "1", "true", "yes", "on",
    )


def _is_local_base(base: str) -> bool:
    low = base.lower()
    return "localhost" in low or "127.0.0.1" in low


def _local_port_conflict(base: str) -> bool:
    """True when rerank URL points at the same port Contra API typically uses."""
    if not _is_local_base(base):
        return False
    for port in _CONTRA_API_PORTS:
        if f":{port}" in base or base.rstrip("/").endswith(f":{port}"):
            return True
    return False


def rerank_enabled() -> bool:
    from agents.research.llm_client import nvidia_configured

    if _RERANK_CIRCUIT_OPEN:
        return False
    if not _rerank_flag_enabled():
        return False
    if not nvidia_configured():
        return False
    base = _rerank_base_url()
    if _local_port_conflict(base):
        global _RERANK_PORT_WARNED
        if not _RERANK_PORT_WARNED:
            _RERANK_PORT_WARNED = True
            logger.warning(
                "NIM rerank disabled: %s conflicts with Contra API on :8000. "
                "Run scripts/run-nim-rerank.ps1 (port %s) and set "
                "NIM_RERANK_BASE_URL=http://localhost:%s/v1",
                base,
                _DEFAULT_LOCAL_RERANK_PORT,
                _DEFAULT_LOCAL_RERANK_PORT,
            )
        return False
    return True


def _rerank_base_url() -> str:
    explicit = os.environ.get("NIM_RERANK_BASE_URL", "").strip()
    if explicit:
        return explicit.rstrip("/")
    nim = os.environ.get("NIM_BASE_URL", "").strip().rstrip("/")
    if nim:
        return nim
    return "https://integrate.api.nvidia.com/v1"


def _rerank_model() -> str:
    return (
        os.environ.get("NIM_RERANK_MODEL", "").strip()
        or _DEFAULT_RERANK_MODEL
    )


def build_lp_rerank_query(lp_name: str, screening_mode: str = "institutional") -> str:
    """Question the reranker scores each web passage against."""
    if screening_mode == "nfx_individual":
        return (
            f"Does {lp_name} commit capital to venture capital funds as a limited partner, "
            f"not only make direct angel investments or GP investments at their employer fund?"
        )
    return (
        f"Does {lp_name} commit capital to VC funds as a limited partner, with evidence of "
        f"emerging-manager, emerging-markets, or AI/technology fund appetite?"
    )


def _passage_text(result: SearchResult, max_chars: int = 1200) -> str:
    parts = [result.title or "", result.snippet or ""]
    if result.raw_content:
        parts.append(result.raw_content[:800])
    parts.append(result.url or "")
    text = "\n".join(p for p in parts if p).strip()
    return text[:max_chars] if text else result.url


def rerank_search_results(
    results: List[SearchResult],
    query: str,
    *,
    top_k: Optional[int] = None,
) -> List[SearchResult]:
    """
    Re-order SearchResult list by NVIDIA reranker relevance scores.
    On API failure, returns the input list unchanged.
    """
    if not results or not rerank_enabled():
        return results

    from agents.research.llm_client import _get_nvidia_api_key

    base = _rerank_base_url()
    api_key = _get_nvidia_api_key()
    local = "localhost" in base or "127.0.0.1" in base
    if not api_key and not local:
        return results

    passages = [{"text": _passage_text(r)} for r in results]
    payload = {
        "model": _rerank_model(),
        "query": {"text": query},
        "passages": passages,
        "truncate": "END",
    }
    url = f"{base}/ranking"
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )

    global _RERANK_CIRCUIT_OPEN

    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:200]
        _RERANK_CIRCUIT_OPEN = True
        if exc.code == 404 and "Not Found" in body and _is_local_base(base):
            logger.warning(
                "NIM rerank unavailable at %s (got FastAPI 404 — likely Contra API, not rerank NIM). "
                "Keeping Tavily order. Self-host: .\\scripts\\run-nim-rerank.ps1 then set "
                "NIM_RERANK_BASE_URL=http://localhost:%s/v1",
                url,
                _DEFAULT_LOCAL_RERANK_PORT,
            )
        else:
            logger.warning(
                "NIM rerank unavailable (%s %s) — keeping Tavily order. "
                "Self-host rerank NIM or set NIM_RERANK_ENABLED=false.",
                exc.code,
                body,
            )
        return results
    except Exception as exc:
        _RERANK_CIRCUIT_OPEN = True
        logger.warning("NIM rerank failed (%s) — keeping Tavily order", exc)
        return results

    rankings = data.get("rankings") or data.get("results") or []
    if not rankings:
        logger.debug("NIM rerank returned no rankings: %s", list(data.keys()))
        return results

    # Response: list of {index, logit} or {index, score} — sort desc
    def _score(item: dict) -> float:
        for key in ("logit", "score", "relevance_score"):
            if key in item:
                try:
                    return float(item[key])
                except (TypeError, ValueError):
                    pass
        return 0.0

    def _index(item: dict, pos: int) -> int:
        for key in ("index", "passage_index", "document_index"):
            if key in item:
                return int(item[key])
        return pos

    ordered = sorted(
        enumerate(rankings),
        key=lambda pair: _score(pair[1]),
        reverse=True,
    )
    reranked: List[SearchResult] = []
    for pos, item in ordered:
        idx = _index(item, pos)
        if 0 <= idx < len(results):
            r = results[idx]
            r.score = _score(item)
            reranked.append(r)

    # Append any missing indices (safety)
    seen = {id(r) for r in reranked}
    for r in results:
        if id(r) not in seen:
            reranked.append(r)

    limit = top_k or int(os.environ.get("NIM_RERANK_TOP_K", "0") or "0")
    if limit > 0:
        reranked = reranked[:limit]

    logger.debug(
        "NIM rerank applied (%s): top=%s",
        _rerank_model(),
        reranked[0].url[:60] if reranked else "—",
    )
    return reranked

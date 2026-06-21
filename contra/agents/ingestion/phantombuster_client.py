"""
Phantombuster API v2 client.

Exposes three operations:
  - launch(agent_id)              → container_id
  - poll_until_done(container_id) → container dict
  - fetch_result_rows(container_id) → list[dict]

Auth: PHANTOMBUSTER_API_KEY env var → X-Phantombuster-Key-1 header.

No third-party HTTP library required — uses stdlib urllib.
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_BASE = "https://api.phantombuster.com/api/v2"
_DEFAULT_TIMEOUT_SEC = int(os.environ.get("PHANTOMBUSTER_TIMEOUT_SEC", "3600"))
_POLL_INTERVAL_SEC = 30
_TERMINAL_STATUSES = {"finished", "error", "launch error", "stopped"}


class PhantombusterError(RuntimeError):
    """Raised for API errors, timeouts, or empty results."""


def _api_key() -> str:
    key = os.environ.get("PHANTOMBUSTER_API_KEY", "").strip()
    if not key:
        raise PhantombusterError(
            "PHANTOMBUSTER_API_KEY is not set. Add it to contra/.env."
        )
    return key


def _request(method: str, path: str, body: Optional[dict] = None) -> Any:
    """Make one HTTPS request to the Phantombuster API. Returns parsed JSON."""
    url = f"{_BASE}{path}"
    data = json.dumps(body).encode() if body else None
    headers = {
        "X-Phantombuster-Key-1": _api_key(),
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        body_text = exc.read().decode(errors="replace")
        raise PhantombusterError(
            f"Phantombuster API {method} {path} → HTTP {exc.code}: {body_text}"
        ) from exc


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def launch(agent_id: str) -> str:
    """
    Launch a Phantombuster agent using its saved configuration.
    Returns the container_id for this specific run.
    """
    resp = _request("POST", "/agents/launch", {"id": agent_id, "manualLaunch": True})
    container_id = (
        resp.get("containerId")
        or resp.get("container_id")
        or (resp.get("data") or {}).get("containerId")
    )
    if not container_id:
        raise PhantombusterError(
            f"Launch succeeded but no container_id returned: {resp}"
        )
    logger.info("Phantombuster: launched agent %s → container %s", agent_id, container_id)
    return str(container_id)


def get_container(container_id: str) -> Dict[str, Any]:
    """Fetch current container state (status, output, etc.)."""
    resp = _request("GET", f"/containers/fetch?id={container_id}")
    # v2 may wrap in a top-level key
    return resp.get("container") or resp


def poll_until_done(
    container_id: str,
    timeout_sec: int = _DEFAULT_TIMEOUT_SEC,
) -> Dict[str, Any]:
    """
    Poll container status until terminal (finished / error) or timeout.
    Returns the final container dict.
    """
    deadline = time.time() + timeout_sec
    last_status = ""
    while time.time() < deadline:
        container = get_container(container_id)
        status = (container.get("status") or container.get("statusString") or "").lower()
        if status != last_status:
            logger.info("Phantombuster container %s: %s", container_id, status)
            last_status = status
        if status in _TERMINAL_STATUSES:
            if status != "finished":
                raise PhantombusterError(
                    f"Phantombuster container {container_id} ended with status '{status}'"
                )
            return container
        time.sleep(_POLL_INTERVAL_SEC)

    raise PhantombusterError(
        f"Phantombuster container {container_id} did not finish within {timeout_sec}s"
    )


def fetch_result_rows(container_id: str) -> List[dict]:
    """
    Fetch the structured result object for a completed container.
    Returns a list of profile row dicts.

    Handles two common Phantombuster result shapes:
      1. resultObject is a JSON array of dicts (most Sales Nav exports)
      2. resultObject contains {csvUrl: "..."} — downloads and parses the CSV
    """
    resp = _request("GET", f"/containers/fetch-result-object?id={container_id}")
    result_obj = resp.get("resultObject") or resp.get("data") or resp

    # Shape 1: already a list
    if isinstance(result_obj, list):
        logger.info("Phantombuster: got %d rows from result array", len(result_obj))
        return result_obj

    # Shape 2: string-encoded JSON
    if isinstance(result_obj, str):
        try:
            parsed = json.loads(result_obj)
            if isinstance(parsed, list):
                logger.info("Phantombuster: decoded %d rows from JSON string", len(parsed))
                return parsed
        except json.JSONDecodeError:
            pass

    # Shape 3: dict wrapping csvUrl or resultObject key
    if isinstance(result_obj, dict):
        csv_url = result_obj.get("csvUrl") or result_obj.get("outputUrl")
        if csv_url:
            return _fetch_csv_rows(csv_url)
        # May already contain rows under a different key
        for key in ("items", "leads", "profiles", "rows"):
            if isinstance(result_obj.get(key), list):
                logger.info("Phantombuster: got %d rows from key '%s'", len(result_obj[key]), key)
                return result_obj[key]

    logger.warning("Phantombuster: could not parse result object, shape: %s", type(result_obj))
    return []


def _fetch_csv_rows(url: str) -> List[dict]:
    """Download a Phantombuster CSV output URL and parse into row dicts."""
    import csv
    import io

    req = urllib.request.Request(url, headers={"Accept": "*/*"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            content = resp.read().decode("utf-8-sig", errors="replace")
    except Exception as exc:
        raise PhantombusterError(f"Failed to download Phantombuster CSV: {url}: {exc}") from exc

    reader = csv.DictReader(io.StringIO(content))
    rows = list(reader)
    logger.info("Phantombuster: downloaded CSV with %d rows from %s", len(rows), url)
    return rows

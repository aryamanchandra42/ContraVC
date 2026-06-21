"""
LP dossier — durable institutional memory for screened/confirmed LPs.

Gate sessions live in memory for 30 minutes; the dossier is the permanent
record. Every YES/REVIEW gate run upserts one row per LP (name_key) capturing:

  - latest verdict + model + session id, plus full verdict history
  - confirmed LP commitments (verifier-cleaned)
  - appetite profile
  - all source URLs and the research notes used to decide
  - outreach history (appended by the outreach agent)
  - analyst notes (free text)

Where to look up everything known about a confirmed LP: GET /api/crm/dossier/{name}.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from agents.normalization.crm_normalizer import norm_key
from contra.gate.models import GateResult

logger = logging.getLogger(__name__)

_HISTORY_CAP = 25


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_json(val: Any, default: Any) -> Any:
    if val is None:
        return default
    if isinstance(val, (list, dict)):
        return val
    try:
        return json.loads(val)
    except Exception:
        return default


def upsert_dossier_from_gate(
    con,
    result: GateResult,
    web_context: str,
    allocator_id: Optional[str] = None,
) -> None:
    """Write/refresh the LP's dossier after a YES/REVIEW gate run. Never raises."""
    try:
        key = norm_key(result.lp_name)
        verdict = "yes" if result.yes else ("review" if result.is_review else "no")

        existing = con.execute(
            "SELECT verdict_history_json, outreach_history_json, analyst_notes, allocator_id "
            "FROM lp_dossiers WHERE name_key = ?",
            [key],
        ).fetchone()

        history: List[Dict[str, Any]] = []
        outreach: List[Dict[str, Any]] = []
        analyst_notes = ""
        prev_allocator = None
        if existing:
            history = _load_json(existing[0], [])
            outreach = _load_json(existing[1], [])
            analyst_notes = existing[2] or ""
            prev_allocator = existing[3]

        history.append({
            "at": _now(),
            "verdict": verdict,
            "confidence": result.confidence,
            "session_id": result.session_id,
            "model": result.verdict_model,
            "escalated": result.escalated,
            "summary": (result.summary or "")[:400],
        })
        history = history[-_HISTORY_CAP:]

        row = [
            result.lp_name,
            allocator_id or prev_allocator,
            verdict,
            result.session_id,
            result.verdict_model,
            json.dumps(result.lp_commitments_found or []),
            json.dumps(result.appetite.model_dump() if result.appetite else {}),
            json.dumps(result.source_urls or []),
            (web_context or "")[:20000],
            json.dumps(history),
            json.dumps(outreach),
            analyst_notes,
            key,
        ]
        if existing:
            con.execute(
                """
                UPDATE lp_dossiers SET
                    investor_name = ?, allocator_id = ?, latest_verdict = ?,
                    latest_session_id = ?, verdict_model = ?,
                    lp_commitments_json = ?, appetite_json = ?, sources_json = ?,
                    research_notes = ?, verdict_history_json = ?,
                    outreach_history_json = ?, analyst_notes = ?, updated_at = NOW()
                WHERE name_key = ?
                """,
                row,
            )
        else:
            con.execute(
                """
                INSERT INTO lp_dossiers (
                    investor_name, allocator_id, latest_verdict, latest_session_id,
                    verdict_model, lp_commitments_json, appetite_json, sources_json,
                    research_notes, verdict_history_json, outreach_history_json,
                    analyst_notes, name_key
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                row,
            )
    except Exception as exc:
        logger.warning("Dossier upsert failed for '%s': %s", result.lp_name, exc)


def get_dossier(con, name: str) -> Optional[Dict[str, Any]]:
    """Fetch the dossier for an LP by name (normalized key match)."""
    key = norm_key(name)
    row = con.execute(
        """
        SELECT name_key, investor_name, allocator_id, latest_verdict, latest_session_id,
               verdict_model, lp_commitments_json, appetite_json, sources_json,
               research_notes, verdict_history_json, outreach_history_json,
               analyst_notes, created_at, updated_at
        FROM lp_dossiers WHERE name_key = ?
        """,
        [key],
    ).fetchone()
    if not row:
        return None
    return {
        "name_key": row[0],
        "investor_name": row[1],
        "allocator_id": str(row[2]) if row[2] else None,
        "latest_verdict": row[3],
        "latest_session_id": row[4],
        "verdict_model": row[5],
        "lp_commitments": _load_json(row[6], []),
        "appetite": _load_json(row[7], {}),
        "sources": _load_json(row[8], []),
        "research_notes": row[9] or "",
        "verdict_history": _load_json(row[10], []),
        "outreach_history": _load_json(row[11], []),
        "analyst_notes": row[12] or "",
        "created_at": str(row[13]) if row[13] else None,
        "updated_at": str(row[14]) if row[14] else None,
    }


def append_outreach_event(con, name: str, event: Dict[str, Any]) -> None:
    """Append an outreach event (draft created / sent / replied) to the dossier."""
    try:
        key = norm_key(name)
        row = con.execute(
            "SELECT outreach_history_json FROM lp_dossiers WHERE name_key = ?", [key]
        ).fetchone()
        if not row:
            return
        events = _load_json(row[0], [])
        events.append({"at": _now(), **event})
        con.execute(
            "UPDATE lp_dossiers SET outreach_history_json = ?, updated_at = NOW() WHERE name_key = ?",
            [json.dumps(events[-_HISTORY_CAP:]), key],
        )
    except Exception as exc:
        logger.warning("Dossier outreach append failed for '%s': %s", name, exc)


def set_analyst_notes(con, name: str, notes: str) -> bool:
    """Replace the free-text analyst notes on a dossier."""
    key = norm_key(name)
    cur = con.execute(
        "UPDATE lp_dossiers SET analyst_notes = ?, updated_at = NOW() WHERE name_key = ?",
        [notes[:5000], key],
    )
    try:
        return cur.fetchall() is not None
    except Exception:
        return True

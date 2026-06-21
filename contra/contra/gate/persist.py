"""
Persist gate web findings into allocators (COALESCE-only updates).

Runs synchronously after YES/REVIEW verdicts and on Add to CRM.
Uses its own writable DuckDB connection so gate screening can stay read-only.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any, Dict, Optional

from agents.normalization.taxonomies import normalize_geography, normalize_lp_type_label
from agents.research.enrichment_agent import _apply_enrichment_to_allocator, _write_research_raw_record
from contra.gate.models import GateExplanation
from contra.intelligence.brief import IntelligenceBrief

logger = logging.getLogger(__name__)

_ARCHETYPE_TYPE = {
    "family_office": "family_office",
    "fund_of_funds": "fund_of_funds",
    "institutional_lp": "institution",
    "emerging_manager_specialist": "fund_of_funds",
    "asia_specialist": "institution",
    "technology_specialist": "institution",
    "founder_lp": "individual",
    "corporate_investor": "corporate",
    "generalist": "institution",
}

_APPETITE_LEVEL = {
    "strong": "high",
    "moderate": "medium",
    "weak": "low",
    "none": "none",
}

_GEO_HINTS = (
    "singapore", "southeast asia", "asia", "north america", "united states",
    "middle east", "europe", "global", "uk", "london", "dubai", "uae",
    "hong kong", "china", "india", "australia", "canada",
)


def _appetite_db(level: str) -> Optional[str]:
    return _APPETITE_LEVEL.get((level or "").lower())


def _canon_str(val: Any) -> Optional[str]:
    """Normalize taxonomy enum or string to a DB-safe canonical string."""
    if val is None:
        return None
    if hasattr(val, "value"):
        val = val.value
    s = str(val).strip()
    if not s or s.lower() == "unknown":
        return None
    return s


def _infer_geography(explanation: GateExplanation, brief: IntelligenceBrief) -> Optional[str]:
    profile = brief.allocator_profile or {}
    if profile.get("geography"):
        geo = _canon_str(normalize_geography(str(profile["geography"])))
        if geo:
            return geo

    text = " ".join([
        explanation.c4_evidence or "",
        *(explanation.online_evidence or [])[:4],
        explanation.summary or "",
    ]).lower()
    for hint in _GEO_HINTS:
        if hint in text:
            geo = _canon_str(normalize_geography(hint))
            if geo:
                return geo
    return None


def _build_updates(explanation: GateExplanation, brief: IntelligenceBrief) -> Dict[str, str]:
    updates: Dict[str, str] = {}

    archetype = explanation.archetype or "unknown"
    if archetype != "unknown":
        raw_type = _ARCHETYPE_TYPE.get(archetype, archetype)
        normalized = _canon_str(normalize_lp_type_label(raw_type))
        if normalized:
            updates["allocator_type"] = normalized

    geo = _infer_geography(explanation, brief)
    if geo:
        updates["geography"] = geo

    em = _appetite_db(explanation.em_appetite)
    if em:
        updates["em_appetite"] = em
    ai = _appetite_db(explanation.ai_tech_appetite)
    if ai:
        updates["ai_appetite"] = ai

    return updates


def _create_allocator(
    con,
    canonical_name: str,
    input_name: str,
    updates: Dict[str, str],
    session_id: str,
) -> str:
    allocator_id = str(uuid.uuid4())
    source_record_id = f"gate:{session_id}:{allocator_id}"
    con.execute(
        """
        INSERT INTO allocators (
            allocator_id, canonical_name, allocator_type, geography,
            em_appetite, ai_appetite, population,
            source_record_id, source_file, content_hash, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, 'institutional_prospect', ?, 'gate/persist', ?, NOW(), NOW())
        """,
        [
            allocator_id,
            canonical_name,
            updates.get("allocator_type"),
            updates.get("geography"),
            updates.get("em_appetite"),
            updates.get("ai_appetite"),
            source_record_id,
            source_record_id[:64],
        ],
    )
    if input_name.strip().lower() != canonical_name.strip().lower():
        con.execute(
            """
            INSERT INTO entity_aliases
                (alias_id, canonical_id, entity_type, alias_text, source_file, confidence)
            VALUES (?, ?, 'allocator', ?, 'gate/persist', 0.85)
            """,
            [str(uuid.uuid4()), allocator_id, input_name.strip()],
        )
    return allocator_id


def persist_gate_findings(
    con,
    name: str,
    brief: IntelligenceBrief,
    explanation: GateExplanation,
    web_context: str,
    verdict: str,
    session_id: str,
) -> Optional[str]:
    """
    Write gate web/LLM findings to allocators. Returns allocator_id if persisted.
    Uses the caller's DuckDB connection — never opens a second connection.
    """
    if verdict not in ("yes", "review"):
        return brief.allocator_id

    updates = _build_updates(explanation, brief)
    if not updates and not explanation.summary:
        return brief.allocator_id

    trusted_db = not brief.match_untrusted and (
        brief.match_method in ("exact", "alias")
        or (brief.match_method == "fuzzy" and brief.match_confidence >= 0.92)
    )
    canonical_name = (brief.matched_name or name) if trusted_db else name
    try:
        allocator_id = brief.allocator_id if trusted_db else None
        if not allocator_id:
            allocator_id = _create_allocator(con, canonical_name, name, updates, session_id)
            logger.info("Gate persist: created allocator %s for %s", allocator_id, canonical_name)
        else:
            cols = _apply_enrichment_to_allocator(con, allocator_id, updates)
            logger.info("Gate persist: updated %d cols for %s", cols, allocator_id)

        payload = {
            "source": "gate",
            "session_id": session_id,
            "verdict": verdict,
            "lp_name": name,
            "canonical_name": canonical_name,
            "allocator_id": allocator_id,
            "updates": updates,
            "summary": explanation.summary,
            "online_evidence": explanation.online_evidence[:8],
            "web_context_excerpt": (web_context or "")[:2000],
            "core_gates": {
                "c1": {"status": explanation.c1_status, "evidence": explanation.c1_evidence},
                "c2": {"status": explanation.c2_status, "evidence": explanation.c2_evidence},
                "c3": {"status": explanation.c3_status, "evidence": explanation.c3_evidence},
                "c4": {"status": explanation.c4_status, "evidence": explanation.c4_evidence},
            },
        }
        _write_research_raw_record(con, allocator_id, 0, payload)

        # Extract contact channels (email/LinkedIn/X) from web research
        try:
            from contra.intelligence.contact_extract import extract_and_persist_gate_contacts
            contact_stats = extract_and_persist_gate_contacts(
                con,
                lp_name=name,
                allocator_id=allocator_id,
                web_context=web_context or "",
                source_urls=list(explanation.online_evidence or []),
            )
            if any(contact_stats.values()):
                logger.info("Gate contact extract for %s: %s", name, contact_stats)
                
            # If no contacts were found natively from the gate research context, run the dedicated contact hunter
            if not contact_stats.get("gate_emails"):
                from agents.research.contact_hunter import hunt_and_persist_contacts
                hunter_stats = hunt_and_persist_contacts(con, lp_name=name, allocator_id=allocator_id)
                logger.info(f"Gate contact hunter fallback for {name}: {hunter_stats}")

        except Exception as ce:
            logger.debug("Gate contact extract skipped for %s: %s", name, ce)

        return allocator_id
    except Exception as exc:
        logger.warning("Gate persist failed for %s: %s", name, exc)
        return brief.allocator_id


def persist_from_session(con, session) -> Optional[str]:
    """Re-persist from a gate session (e.g. Add to CRM). Idempotent COALESCE updates."""
    from contra.gate.models import GateExplanation, GateResult

    result = GateResult.model_validate(session.result_dict)
    verdict = result.assessment.recommendation
    if verdict not in ("yes", "review") and not result.yes and not result.is_review:
        return None

    explanation_dict = getattr(session, "explanation_dict", None) or {}
    if not explanation_dict:
        return None

    brief = IntelligenceBrief(**session.brief_dict)
    explanation = GateExplanation.model_validate(explanation_dict)
    v = "yes" if result.yes else ("review" if result.is_review else verdict)
    return persist_gate_findings(
        con,
        name=session.lp_name,
        brief=brief,
        explanation=explanation,
        web_context=session.web_context,
        verdict=v,
        session_id=session.session_id,
    )

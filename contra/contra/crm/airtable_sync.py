"""
Airtable push-only sync layer for the Contra CRM.

Pushes LP Leads, Outreach Drafts, and LP Dossiers to Airtable so the team
has a live, no-code view of the pipeline without touching the backend.

All functions are fire-and-forget: they log on failure and never raise, so a
broken Airtable token never takes down the main application.

Required env vars:
  AIRTABLE_API_KEY   — Personal Access Token (pat...)
  AIRTABLE_BASE_ID   — The base ID (app...)

Optional env vars (override default table names):
  AIRTABLE_LEADS_TABLE    — default "LP Leads"
  AIRTABLE_DRAFTS_TABLE   — default "Outreach Drafts"
  AIRTABLE_DOSSIERS_TABLE — default "LP Dossiers"

Table schemas are documented in AIRTABLE_SETUP.md at the project root.
"""

from __future__ import annotations

import json
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="airtable-sync")


# ─────────────────────────────────────────────────────────────────────────────
# Client bootstrap
# ─────────────────────────────────────────────────────────────────────────────

def _get_table(table_env_var: str, default_name: str):
    """
    Returns a pyairtable Table object, or None if credentials are missing.
    Import is deferred so the module loads even when pyairtable is not installed.
    """
    api_key = os.environ.get("AIRTABLE_API_KEY", "")
    base_id = os.environ.get("AIRTABLE_BASE_ID", "")
    if not api_key or not base_id:
        return None
    try:
        from pyairtable import Api  # type: ignore
        table_name = os.environ.get(table_env_var, default_name)
        api = Api(api_key)
        return api.table(base_id, table_name)
    except Exception as exc:
        logger.warning("airtable_sync: failed to init table '%s': %s", default_name, exc)
        return None


def _is_configured() -> bool:
    return bool(os.environ.get("AIRTABLE_API_KEY") and os.environ.get("AIRTABLE_BASE_ID"))


# ─────────────────────────────────────────────────────────────────────────────
# Field helpers
# ─────────────────────────────────────────────────────────────────────────────

def _str(val: Any, maxlen: int = 0) -> str:
    if val is None:
        return ""
    s = str(val)
    return s[:maxlen] if maxlen else s


def _json_str(val: Any, maxlen: int = 10000) -> str:
    if val is None:
        return ""
    if isinstance(val, str):
        return val[:maxlen]
    try:
        return json.dumps(val, ensure_ascii=False)[:maxlen]
    except Exception:
        return str(val)[:maxlen]


def _sources_text(sources: Any) -> str:
    if not sources:
        return ""
    if isinstance(sources, list):
        return "\n".join(str(s) for s in sources)[:10000]
    return _str(sources, 10000)


# ─────────────────────────────────────────────────────────────────────────────
# LP Leads
# ─────────────────────────────────────────────────────────────────────────────

def _build_lead_fields(lead: Dict[str, Any]) -> Dict[str, Any]:
    fields: Dict[str, Any] = {
        "Investor Name":   _str(lead.get("investor_name")),
        "Lead ID":         _str(lead.get("lead_id")),
        "Investor Type":   _str(lead.get("investor_type")),
        "Location":        _str(lead.get("investor_location")),
        "Pipeline Stage":  _str(lead.get("pipeline_stage") or "Prospect"),
        "Status":          _str(lead.get("status") or "active"),
        "Gate Verdict":    _str(lead.get("gate_verdict")),
        "Gate Confidence": _str(lead.get("gate_confidence")),
        "Gate Summary":    _str(lead.get("gate_summary"), 1000),
        "ICP Tier":        _str(lead.get("icp_tier")),
        "Contact Email":   _str(lead.get("contact_email")),
        "Needs Enrichment": bool(lead.get("needs_enrichment")),
    }
    if lead.get("fit_score") is not None:
        fields["Fit Score"] = float(lead["fit_score"])
    if lead.get("computed_score") is not None:
        fields["Computed Score"] = float(lead["computed_score"])
    # Strip empty strings to avoid Airtable validation noise
    return {k: v for k, v in fields.items() if v != ""}


def push_lead(lead: Dict[str, Any]) -> None:
    """
    Upsert an LP Lead row in Airtable. Matches on 'Investor Name'.
    Non-blocking — runs in a background thread.
    """
    if not _is_configured():
        return

    def _do():
        try:
            table = _get_table("AIRTABLE_LEADS_TABLE", "LP Leads")
            if table is None:
                return
            fields = _build_lead_fields(lead)
            if not fields.get("Investor Name"):
                return
            table.upsert(
                [{"fields": fields}],
                key_fields=["Investor Name"],
            )
            logger.debug("airtable_sync: upserted lead '%s'", lead.get("investor_name"))
        except Exception as exc:
            logger.warning("airtable_sync: push_lead failed for '%s': %s",
                           lead.get("investor_name"), exc)

    _executor.submit(_do)


def update_lead_latest_email(
    investor_name: str,
    subject: str,
    body: str,
    pipeline_stage: Optional[str] = None,
    status: Optional[str] = None,
    last_outreach_at: Optional[str] = None,
) -> None:
    """
    Update the Latest Email Subject/Body fields on a Lead row (upsert by name).
    Also advances pipeline stage and status if provided.
    Non-blocking.
    """
    if not _is_configured():
        return

    def _do():
        try:
            table = _get_table("AIRTABLE_LEADS_TABLE", "LP Leads")
            if table is None:
                return
            fields: Dict[str, Any] = {
                "Investor Name":       investor_name,
                "Latest Email Subject": _str(subject, 250),
                "Latest Email Body":   _str(body, 10000),
            }
            if pipeline_stage:
                fields["Pipeline Stage"] = pipeline_stage
            if status:
                fields["Status"] = status
            if last_outreach_at:
                fields["Last Outreach At"] = last_outreach_at[:10]  # ISO date only
            table.upsert(
                [{"fields": fields}],
                key_fields=["Investor Name"],
            )
        except Exception as exc:
            logger.warning("airtable_sync: update_lead_latest_email failed for '%s': %s",
                           investor_name, exc)

    _executor.submit(_do)


# ─────────────────────────────────────────────────────────────────────────────
# Outreach Drafts
# ─────────────────────────────────────────────────────────────────────────────

def push_outreach_draft(draft: Dict[str, Any]) -> None:
    """
    Upsert an Outreach Draft row in Airtable. Matches on 'Draft ID'.
    Non-blocking.
    """
    if not _is_configured():
        return

    def _do():
        try:
            table = _get_table("AIRTABLE_DRAFTS_TABLE", "Outreach Drafts")
            if table is None:
                return
            points = draft.get("personalization_points") or []
            points_text = "\n• ".join(str(p) for p in points) if points else ""
            if points_text:
                points_text = "• " + points_text

            fields: Dict[str, Any] = {
                "Draft ID":              _str(draft.get("draft_id")),
                "Investor Name":         _str(draft.get("investor_name")),
                "Subject":               _str(draft.get("subject"), 250),
                "Body":                  _str(draft.get("body"), 10000),
                "Status":                _str(draft.get("status") or "draft"),
                "Tone":                  _str(draft.get("tone")),
                "Archetype":             _str(draft.get("archetype")),
                "Model":                 _str(draft.get("model")),
                "Deep Research Used":    bool(draft.get("deep_research_used")),
                "Personalization Points": points_text,
            }
            fields = {k: v for k, v in fields.items() if v != "" and v is not None}
            if not fields.get("Draft ID"):
                return
            table.upsert(
                [{"fields": fields}],
                key_fields=["Draft ID"],
            )
            logger.debug("airtable_sync: upserted draft '%s'", draft.get("draft_id"))
        except Exception as exc:
            logger.warning("airtable_sync: push_outreach_draft failed for '%s': %s",
                           draft.get("draft_id"), exc)

    _executor.submit(_do)


def update_draft_status_airtable(draft_id: str, status: str) -> None:
    """
    Update just the Status field on an Outreach Draft row. Non-blocking.
    """
    if not _is_configured():
        return

    def _do():
        try:
            table = _get_table("AIRTABLE_DRAFTS_TABLE", "Outreach Drafts")
            if table is None:
                return
            table.upsert(
                [{"fields": {"Draft ID": draft_id, "Status": status}}],
                key_fields=["Draft ID"],
            )
        except Exception as exc:
            logger.warning("airtable_sync: update_draft_status failed for '%s': %s",
                           draft_id, exc)

    _executor.submit(_do)


# ─────────────────────────────────────────────────────────────────────────────
# LP Dossiers
# ─────────────────────────────────────────────────────────────────────────────

def push_dossier(dossier: Dict[str, Any]) -> None:
    """
    Upsert an LP Dossier row in Airtable. Matches on 'Name Key'.
    Non-blocking.
    """
    if not _is_configured():
        return

    def _do():
        try:
            table = _get_table("AIRTABLE_DOSSIERS_TABLE", "LP Dossiers")
            if table is None:
                return

            commitments = dossier.get("lp_commitments") or []
            if isinstance(commitments, list):
                commitments_text = "\n".join(str(c) for c in commitments)[:5000]
            else:
                commitments_text = _json_str(commitments, 5000)

            appetite = dossier.get("appetite") or {}
            appetite_text = _json_str(appetite, 3000)

            outreach_history = dossier.get("outreach_history") or []
            outreach_summary = _summarise_outreach_history(outreach_history)

            fields: Dict[str, Any] = {
                "Name Key":          _str(dossier.get("name_key")),
                "Investor Name":     _str(dossier.get("investor_name")),
                "Latest Verdict":    _str(dossier.get("latest_verdict")),
                "LP Commitments":    commitments_text,
                "Appetite":          appetite_text,
                "Sources":           _sources_text(dossier.get("sources")),
                "Research Notes":    _str(dossier.get("research_notes"), 10000),
                "Analyst Notes":     _str(dossier.get("analyst_notes"), 5000),
                "Outreach Summary":  outreach_summary,
                "Rejection Reason":  _str(dossier.get("rejection_reason")),
                "Revisit Date":      _str(dossier.get("revisit_date"), 10),
            }
            fields = {k: v for k, v in fields.items() if v not in ("", None)}
            if not fields.get("Name Key"):
                return
            table.upsert(
                [{"fields": fields}],
                key_fields=["Name Key"],
            )
            logger.debug("airtable_sync: upserted dossier '%s'", dossier.get("name_key"))
        except Exception as exc:
            logger.warning("airtable_sync: push_dossier failed for '%s': %s",
                           dossier.get("name_key"), exc)

    _executor.submit(_do)


def _summarise_outreach_history(history: List[Dict[str, Any]]) -> str:
    """Collapse outreach history into a human-readable summary string (<= 3000 chars)."""
    if not history:
        return ""
    lines = []
    for ev in history[-10:]:
        at = ev.get("at", "")[:10]
        event = ev.get("event", "")
        subject = ev.get("subject", "")
        reason = ev.get("reason", "")
        if event == "email_sent":
            lines.append(f"{at}  SENT — {subject}")
        elif event == "draft_generated":
            lines.append(f"{at}  DRAFT — {subject}")
        elif event == "rejection_tagged":
            lines.append(f"{at}  REJECTED ({reason})")
        else:
            lines.append(f"{at}  {event}")
    return "\n".join(lines)[:3000]


# ─────────────────────────────────────────────────────────────────────────────
# Bulk sync helpers (optional, for backfilling)
# ─────────────────────────────────────────────────────────────────────────────

def bulk_push_leads(leads: List[Dict[str, Any]]) -> None:
    """Push a list of leads. Runs in-thread (blocking) — use only for backfills."""
    for lead in leads:
        push_lead(lead)


def bulk_push_dossiers(dossiers: List[Dict[str, Any]]) -> None:
    """Push a list of dossiers. Runs in-thread (blocking) — use only for backfills."""
    for dossier in dossiers:
        push_dossier(dossier)

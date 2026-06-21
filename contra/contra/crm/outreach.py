"""
Outreach personalization agent — turns gate intelligence into first-touch emails.

For a CRM lead, gathers everything we know (dossier: verified LP commitments,
appetite, archetype, warm paths, sources; lead row: contacts, type, location)
and drafts a personalized outreach email with a strong model (default
claude-sonnet-4-5, override via OUTREACH_LLM_MODEL).

Doctrine baked into the prompt:
  - The hook must come from THEIR allocation behavior (a named fund they backed,
    their emerging-manager program, their stated thesis) — never generic flattery.
  - Never fabricate facts. If we have no verified commitment, lead with the
    thesis overlap instead.
  - 120–160 words, one specific CTA, no attachments mentioned, no jargon walls.

Drafts persist in crm_outreach_drafts (draft → approved → sent) and every
generation/send is appended to the LP's dossier outreach history.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from contra.crm.dossier import append_outreach_event, get_dossier

logger = logging.getLogger(__name__)

_FUND_CONTEXT = (
    "MyAsiaVC / Contra VC — AI-native Fund I, $30M target, pre-seed to Series A, "
    "investing in AI/robotics across Southeast Asia, North America, and the Middle East. "
    "Emerging manager (Fund I)."
)

_SYSTEM = f"""You write first-touch LP outreach emails for a VC fund GP.

FUND: {_FUND_CONTEXT}

NON-NEGOTIABLE RULES FOR SUCCESSFUL COLD OUTREACH:
1. NEVER fabricate facts about the recipient. Use ONLY the intelligence provided.
2. PATTERN INTERRUPT HOOK: The opening sentence MUST reference something highly specific about THEM — a verified fund commitment, their emerging-manager program, or their geography/sector thesis. This proves we did our homework. DO NOT use generic pleasantries ("I hope this finds you well").
3. EXTREME BREVITY: The email MUST be short (under 100-120 words). Use short, punchy sentences. Optimize for reading on a mobile device.
4. VALUE PROPOSITION: Focus on why this aligns with their thesis or past behavior, not just bragging about the fund.
5. FRICTIONLESS CTA: End with a low-friction question, not a demand for a 20-minute meeting block. Examples: "Open to a brief chat?", "Does this align with your current thesis?", or "Worth exploring?".
6. ZERO JARGON: No buzzword soup. Speak like a normal, high-level professional.
7. Subject line: under 6 words, lowercase (or sentence case), specific, no clickbait. It should look like an internal email.
8. If a warm path/intro source is provided, reference it naturally in sentence one.
9. Mention at most ONE fund metric. Do not list the whole deck.
10. Tone parameter: "warm" (default), "formal" (institutional LPs), or "concise" (busy execs).

Return JSON matching the schema you are given.
"""


class OutreachDraft(BaseModel):
    """Structured output schema for the outreach LLM call."""
    subject: str = Field(max_length=120)
    body: str = Field(max_length=2000)
    personalization_points: List[str] = Field(
        default_factory=list, max_length=5,
        description="Which specific facts from the intelligence were used as hooks",
    )


def _outreach_model() -> str:
    return os.environ.get("OUTREACH_LLM_MODEL", "").strip() or "claude-sonnet-4-5"


def _lead_row(con, lead_id: str) -> Optional[Dict[str, Any]]:
    cursor = con.execute(
        """
        SELECT lead_id, investor_name, investor_type, investor_location,
               investor_details, contacts_json, gate_summary, appetite_json,
               warm_path_count, pipeline_stage
        FROM crm_leads WHERE CAST(lead_id AS VARCHAR) = ?
        """,
        [lead_id],
    )
    row = cursor.fetchone()
    if not row:
        return None
    cols = [d[0].lower() for d in cursor.description]
    data = dict(zip(cols, row))
    for jf in ("contacts_json", "appetite_json"):
        if isinstance(data.get(jf), str):
            try:
                data[jf] = json.loads(data[jf])
            except Exception:
                data[jf] = None
    data["lead_id"] = str(data.get("lead_id", ""))
    return data


def _build_prompt(
    lead: Dict[str, Any],
    dossier: Optional[Dict[str, Any]],
    tone: str,
    sender_name: str,
    extra_instructions: str,
) -> str:
    contacts = lead.get("contacts_json") or {}
    first_contact = next(iter(contacts.values()), {}) if isinstance(contacts, dict) else {}
    appetite = (dossier or {}).get("appetite") or lead.get("appetite_json") or {}

    parts = [
        f"RECIPIENT: {lead['investor_name']}",
        f"Contact person: {first_contact.get('name') or '(unknown — address the organization)'}",
        f"Investor type: {lead.get('investor_type') or 'unknown'}",
        f"Location: {lead.get('investor_location') or 'unknown'}",
        f"Tone: {tone}",
        f"Sender (GP): {sender_name or 'the GP'}",
        "",
        "=== INTELLIGENCE (use ONLY this — do not invent) ===",
        f"Gate summary: {lead.get('gate_summary') or (dossier or {}).get('research_notes', '')[:400]}",
        f"Verified LP commitments: {json.dumps((dossier or {}).get('lp_commitments') or [])}",
        f"Appetite: {json.dumps({k: v for k, v in appetite.items() if isinstance(v, str)})[:600]}",
        f"Warm paths on record: {lead.get('warm_path_count') or 0}",
        f"Details: {(lead.get('investor_details') or '')[:600]}",
    ]
    if (dossier or {}).get("analyst_notes"):
        parts.append(f"Analyst notes: {dossier['analyst_notes'][:400]}")
    if extra_instructions:
        parts.append(f"\nADDITIONAL SENDER INSTRUCTIONS: {extra_instructions[:400]}")
    parts.append(
        "\nWrite the outreach email now. Return subject, body, personalization_points."
    )
    return "\n".join(parts)


def generate_outreach_draft(
    con,
    lead_id: str,
    tone: str = "warm",
    sender_name: str = "",
    extra_instructions: str = "",
) -> Dict[str, Any]:
    """Generate + persist a personalized outreach draft for a CRM lead."""
    from agents.research.llm_client import LLMUnavailable, get_llm_client

    lead = _lead_row(con, lead_id)
    if not lead:
        raise ValueError(f"Lead '{lead_id}' not found")

    dossier = get_dossier(con, lead["investor_name"])

    model = _outreach_model()
    try:
        llm = get_llm_client(provider="anthropic", model=model)
    except LLMUnavailable:
        # Fall back to the default configured provider (e.g. Haiku / OpenAI)
        llm = get_llm_client()
        model = getattr(llm, "model", "unknown")

    draft = llm.structured(
        prompt=_build_prompt(lead, dossier, tone, sender_name, extra_instructions),
        response_model=OutreachDraft,
        system=_SYSTEM,
        max_tokens=1500,
    )

    draft_id = str(uuid.uuid4())
    con.execute(
        """
        INSERT INTO crm_outreach_drafts
            (draft_id, lead_id, investor_name, subject, body, tone, model,
             personalization_json, status)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'draft')
        """,
        [
            draft_id, lead["lead_id"], lead["investor_name"],
            draft.subject, draft.body, tone, model,
            json.dumps(draft.personalization_points),
        ],
    )
    append_outreach_event(con, lead["investor_name"], {
        "event": "draft_generated",
        "draft_id": draft_id,
        "subject": draft.subject,
        "model": model,
    })
    return {
        "draft_id": draft_id,
        "lead_id": lead["lead_id"],
        "investor_name": lead["investor_name"],
        "subject": draft.subject,
        "body": draft.body,
        "tone": tone,
        "model": model,
        "personalization_points": draft.personalization_points,
        "status": "draft",
    }


def list_outreach_drafts(con, lead_id: str) -> List[Dict[str, Any]]:
    cursor = con.execute(
        """
        SELECT CAST(draft_id AS VARCHAR) AS draft_id, 
               CAST(lead_id AS VARCHAR) AS lead_id, 
               investor_name, subject, body, tone, model, 
               personalization_json, status,
               CAST(created_at AS VARCHAR) AS created_at
        FROM crm_outreach_drafts
        WHERE CAST(lead_id AS VARCHAR) = ?
        ORDER BY created_at DESC
        """,
        [lead_id],
    )
    rows = cursor.fetchall()
    cols = [d[0].lower() for d in cursor.description]
    out = []
    for r in rows:
        data = dict(zip(cols, r))
        points = data.get("personalization_json")
        if isinstance(points, str):
            try:
                points = json.loads(points)
            except Exception:
                points = []
        out.append({
            "draft_id": data.get("draft_id"), 
            "lead_id": data.get("lead_id"), 
            "investor_name": data.get("investor_name"),
            "subject": data.get("subject"), 
            "body": data.get("body"), 
            "tone": data.get("tone"), 
            "model": data.get("model"),
            "personalization_points": points or [], 
            "status": data.get("status"),
            "created_at": data.get("created_at"),
        })
    return out


def update_draft_status(con, draft_id: str, status: str) -> bool:
    """Move a draft through draft → approved → sent. Sent events hit the dossier."""
    if status not in ("draft", "approved", "sent", "discarded"):
        raise ValueError(f"Invalid status '{status}'")
    row = con.execute(
        "SELECT investor_name, subject FROM crm_outreach_drafts WHERE CAST(draft_id AS VARCHAR) = ?",
        [draft_id],
    ).fetchone()
    if not row:
        return False
    con.execute(
        "UPDATE crm_outreach_drafts SET status = ?, updated_at = NOW() WHERE CAST(draft_id AS VARCHAR) = ?",
        [status, draft_id],
    )
    if status == "sent":
        append_outreach_event(con, row[0], {
            "event": "email_sent", "draft_id": draft_id, "subject": row[1],
        })
        # Reflect in the lead pipeline
        con.execute(
            "UPDATE crm_leads SET status = 'contacted', updated_at = NOW() "
            "WHERE investor_name = ? AND status = 'active'",
            [row[0]],
        )
    elif status == "draft":
        # Allow undoing a mistaken "sent" mark
        con.execute(
            "UPDATE crm_leads SET status = 'active', updated_at = NOW() "
            "WHERE investor_name = ? AND status = 'contacted'",
            [row[0]],
        )
    return True

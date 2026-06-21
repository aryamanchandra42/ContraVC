"""
Outreach personalization agent — turns gate intelligence into first-touch emails.

For a CRM lead, gathers everything we know (dossier: verified LP commitments,
appetite, archetype, warm paths, sources; lead row: contacts, type, location)
and drafts a personalized outreach email with a strong model (default
claude-opus-4-5, override via OUTREACH_LLM_MODEL).

Doctrine baked into the prompt — grounded in 949 real emails (Jan–Jun 2026):
  - S1 hook MUST contain: org identity signal + named behavioral evidence
    + one-sentence bridge. Category descriptions ("activity across X, Y")
    are explicitly forbidden — only named fund/program/investment/thesis qualify.
  - No self-introduction in body ("I'm [Name], GP at…"). Signature covers it.
  - 4–5 sentences total. One metric or one portfolio signal, never both.
  - Subject line must contain org name or specific reference — never generic.
  - Follow-up tone/angle adapts to LP archetype (FoF vs FO vs asset manager).
  - Never fabricate facts. If no named signal is available, fall back to LP type
    + geography + sector combination — never block generation.

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

═══════════════════════════════════════════════════════
HOOK FORMULA — sentence 1 requires ALL THREE components:
  1. Org identity signal: name the org + what makes it specific
     (e.g. "the Woh Hup family office", "a dedicated built-environment CVC",
      "a Singapore-based FoF backing emerging managers across Asia")
  2. Distinguishing evidence — use the HIGHEST tier available from the intel:
     TIER A (best): Named fund they backed, program they run, or portfolio company
     TIER B:        Verbatim thesis quote or mandate language from analyst notes
     TIER C:        LP type + geography + sector combination that is specific to them
                    (e.g. "a Hong Kong multi-family office with a stated AI thesis",
                     not generic: "activity across technology and private funds")
     ── Only use Tier C when Tier A and B are absent from the intel.
     ── NEVER invent facts. Use ONLY what is in the intelligence section.
     ── NEVER use vague category lists ("activity across healthcare, technology…")
        as the distinguishing signal — they apply to any LP and prove nothing.
  3. One-sentence bridge: how that specific thing maps to what we're building.

EXAMPLE of a Tier A hook (strongest):
  "Industry Ventures has long backed shifts in venture structure; Contra VC is
   MyAsiaVC's move from syndicate access into a dedicated fund for global Asian
   AI founders."

EXAMPLE of a Tier C hook (acceptable when no named fund/program available):
  "A Singapore-based family office with a cross-border AI mandate is exactly the
   LP profile we had in mind when structuring Contra VC around technical Asian
   founders building global B2B companies."

FORBIDDEN opening phrases (any of these appearing in S1 = rewrite):
  "Over the last decade", "I hope", "I wanted to reach out", "My name is",
  "I'm writing to", "I came across", "Quick intro", "Just reaching out".
═══════════════════════════════════════════════════════

BODY STRUCTURE (4–5 sentences, no more):
  S1: Hook — 3 components above (no filler, no self-intro)
  S2: Bridge — why their signal maps to this fund specifically
  S3: ONE metric OR one portfolio signal (never both; bold the number)
  S4: CTA — one low-friction question
  P.S. (optional): only if a warm intro source is known in the intel

NO SELF-INTRODUCTION IN BODY. Never write "I'm [Name], General Partner at…"
or "My name is…". Your name and title appear in the signature — the body is
entirely about the recipient.

SUBJECT LINE FORMULA (pick the highest-signal option available):
  A. "[Org short name] / Contra VC"      — e.g. "Aurum / Contra VC"
  B. "[Fund they backed] → Contra VC"   — e.g. "Jungle Ventures → Contra VC"
  C. "via [Mutual name] / Contra VC"    — only if warm_paths > 0
  NEVER use: "Intro to Contra VC Fund I", questions in subject, exclamation
  marks, "quick question", or generic greetings.

PERSONALIZATION HIERARCHY (use highest signal available in the intel):
  1. Named fund commitment + vintage year  ← strongest — always use if present
  2. Named emerging-manager program they run
  3. Named portfolio company intersection with our fund
  4. Verbatim thesis/mandate quote from analyst notes
  5. LP type + geography + specific sector combination  ← use when 1–4 absent
  Always produce a draft using the highest available tier. Never block on missing
  data — a Tier 5 hook is better than no email.

ARCHETYPE-AWARE FOLLOW-UP ANGLE (use when drafting follow-up touch):
  FoF / fund platform    → lead with sourcing access and deal flow volume
  Family office / HNWI   → lead with co-invest / SPV priority access
  Asset manager          → lead with thesis alignment and portfolio proof points
  (LP archetype will be stated in the prompt)

ADDITIONAL NON-NEGOTIABLES:
- NEVER fabricate facts. Use ONLY the intelligence provided.
- Under 100 words in the body. Short punchy sentences. Mobile-readable.
- One fund metric maximum. Do not list the whole deck.
- Tone: "warm" (default), "formal" (institutional), "concise" (busy exec).
- If a warm path is provided, reference it naturally in sentence one.

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
    return os.environ.get("OUTREACH_LLM_MODEL", "").strip() or "claude-opus-4-5"


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
    prior_subjects: Optional[List[str]] = None,
) -> str:
    contacts = lead.get("contacts_json") or {}
    first_contact = next(iter(contacts.values()), {}) if isinstance(contacts, dict) else {}
    appetite = (dossier or {}).get("appetite") or lead.get("appetite_json") or {}

    # Format appetite as labeled signals rather than a raw JSON dump
    appetite_signal_keys = (
        "check_size", "stage_preference", "sector_focus",
        "geography", "emerging_manager_program",
    )
    appetite_lines = [
        f"  - {k}: {appetite[k]}"
        for k in appetite_signal_keys
        if appetite.get(k) and isinstance(appetite[k], str)
    ]
    appetite_block = "\n".join(appetite_lines) if appetite_lines else "  (no appetite signals on record)"

    # Contact name + title for formality calibration
    contact_name = first_contact.get("name") or "(unknown — address the organization)"
    contact_title = first_contact.get("title") or ""
    contact_label = f"{contact_name}" + (f" ({contact_title})" if contact_title else "")

    parts = [
        f"RECIPIENT: {lead['investor_name']}",
        f"Contact person: {contact_label}",
        f"LP archetype: {(dossier or {}).get('archetype') or lead.get('investor_type') or 'unknown'}",
        f"Location: {lead.get('investor_location') or 'unknown'}",
        f"Tone: {tone}",
        f"Sender (GP): {sender_name or 'the GP'}",
        "",
        "=== INTELLIGENCE (use ONLY this — do not invent) ===",
        f"Gate summary: {lead.get('gate_summary') or (dossier or {}).get('research_notes', '')[:400]}",
        f"Verified LP commitments: {json.dumps((dossier or {}).get('lp_commitments') or [])}",
        f"Appetite signals:\n{appetite_block}",
        f"Warm paths on record: {lead.get('warm_path_count') or 0}",
        f"Details: {(lead.get('investor_details') or '')[:600]}",
    ]

    if (dossier or {}).get("analyst_notes"):
        parts.append(f"Analyst notes: {dossier['analyst_notes'][:400]}")

    latest_event = (dossier or {}).get("latest_portfolio_event") or ""
    if latest_event:
        parts.append(f"Latest portfolio milestone (use if drafting follow-up): {latest_event[:200]}")

    if prior_subjects:
        parts.append(
            f"Prior outreach subjects already sent to this LP "
            f"(do NOT reuse the same hook or subject): {prior_subjects}"
        )

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

    # Fetch previous subjects to prevent hook repetition on re-generate
    prior_subjects: List[str] = []
    try:
        rows = con.execute(
            "SELECT subject FROM crm_outreach_drafts "
            "WHERE CAST(lead_id AS VARCHAR) = ? ORDER BY created_at DESC LIMIT 5",
            [lead_id],
        ).fetchall()
        prior_subjects = [r[0] for r in rows if r[0]]
    except Exception:
        pass

    model = _outreach_model()
    try:
        llm = get_llm_client(provider="anthropic", model=model)
    except LLMUnavailable:
        # Fall back to the default configured provider (e.g. Haiku / OpenAI)
        llm = get_llm_client()
        model = getattr(llm, "model", "unknown")

    draft = llm.structured(
        prompt=_build_prompt(
            lead, dossier, tone, sender_name, extra_instructions,
            prior_subjects=prior_subjects or None,
        ),
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

"""
Outreach personalization agent — turns gate intelligence into first-touch emails.

For a CRM lead, gathers everything we know (dossier: verified LP commitments,
appetite, archetype, warm paths, sources; lead row: contacts, type, location)
and drafts a personalized outreach email with a strong model (default
claude-opus-4-5, override via OUTREACH_LLM_MODEL).

Email structure:
  - Subject: "From [Org short name] to Contra - [specific bridge phrase]"
  - Personalized opening paragraph (3 sentences):
      S1: [Org]'s [thesis/focus] is a thesis we share + why reaching out.
      S2: Given your [specific evidence], I'd value your perspective.
      S3: (static) Factsheet link + call CTA.
  - Static pitch block (verbatim, 5 paragraphs): data point → fund thesis →
    GP track record → founder archetype → investment mechanics.
  - Static sign-off.

Only S1, S2, and the subject bridge phrase are personalized per recipient.
Everything else is copied verbatim from _STATIC_PITCH.

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
    "Contra VC — Fund I, $30M target, backing Global Asian founders building "
    "B2B AI companies. Pre-seed and seed, $500-750K tickets, ~30 companies. "
    "Institutional form of MyAsiaVC, which deployed $70M+ across 300+ companies "
    "alongside 6,000+ LPs over the past decade."
)

_STATIC_PITCH = """\
50% of new US tech founders are Asian. That share is rising every YC batch. Contra VC was built around that data point.

We're raising Fund I ($30M) to back Global Asian founders building B2B AI companies for the world. The contrarian insight: first-generation operators from Google, Meta, and OpenAI are founding the next generation of enterprise AI companies, and no institutional fund was purpose-built for them. That's the gap Contra VC was designed to fill.

My co-GP Sajid and I aren't new to this. Over the past decade, through MyAsiaVC, we've deployed $70M+ across 300+ companies alongside 6,000+ LPs. We've built one of the largest and most active Global Asian investor communities in the world. Contra VC is the institutional form of that edge, the fund infrastructure that lets us go deeper, move faster, and back founders at the moment it actually matters.

The founders we back are technical-first, often underestimated, and building in spaces like AI infrastructure, vertical automation, and enterprise software. They don't fit the archetype most institutional funds optimise for, which is precisely where we think the alpha is.

We invest $500-750K at pre-seed and seed, targeting ~30 companies with concentrated follow-on in our highest-conviction positions."""

_SYSTEM = f"""You write first-touch LP outreach emails for a VC fund GP.

FUND: {_FUND_CONTEXT}

═══════════════════════════════════════════════════════
EMAIL STRUCTURE — fixed template. Only the SUBJECT BRIDGE and the PERSONALIZED PARAGRAPH change per recipient. Everything else is static.

  Subject: From [Org short name] to Contra - [bridge]
    — "Org short name": shortest recognisable name for their firm/org (e.g. "Greylock", "Techstars", "Oyster")
    — "bridge": 3–6 words that name the SPECIFIC shared angle (thesis, program, focus, portfolio overlap).
      Derive it entirely from the intel — it must be unique to this recipient.
      BAD (generic): "backing great founders", "AI and venture"
      GOOD (specific): "backing immigrant operators in AI", "the emerging-manager program angle",
                       "Southeast Asia AI deal flow", "the fund-of-funds lens on Asian operators"
    — NEVER: "Intro to Contra VC", questions, exclamation marks, generic greetings.

  Hi [First Name],

  [PERSONALIZED PARAGRAPH — exactly 3 sentences. Write these fresh for every recipient.]:

    S1 — Org + specific angle + why you're writing.
      Do NOT use a fixed template. Write a natural sentence that:
        • Names their org by its specific identity (what it does / its thesis / its focus)
        • States the precise connection to what Contra VC is building
        • Ends with "…and it's why I'm reaching out." OR a similarly natural close
      Draw from: archetype_evidence, allocation_evidence, investor_details, analyst_notes.
      Each S1 must be different — never reuse language from prior drafts.

    S2 — Specific evidence signal + value framing.
      Open with "Given your…" and name the highest-signal fact available:
        TIER A (use if present): a named fund backed, program run, specific portfolio company,
                                 quantified track record (e.g. "150+ angel investments")
        TIER B: a verbatim thesis quote or mandate phrase from the intel
        TIER C: LP type + geography + sector combination unique to them
      End with "…I'd value your perspective on what we're building." OR equivalent.
      NEVER fabricate. Use ONLY what appears in the intelligence section.

    S3 — COPY THIS VERBATIM, unchanged:
      "Our Fund I factsheet is here: https://contravcfactsheet.netlify.app/ and I'd love to find time for a call if it sparks any questions."

  *Here's some more context on what we're building:*

  [STATIC PITCH — copy the following paragraphs verbatim, do not alter a single word]:
{_STATIC_PITCH}

  Would love to chat if you'd like to know more!

  [Sender name]

  General Partner, Contra VC

═══════════════════════════════════════════════════════

HARD RULES:
- S1 and S2 must feel freshly written for this specific recipient. If two drafts could plausibly
  swap S1/S2 with each other, rewrite until they cannot.
- NEVER start S1 with: "Over the last decade", "I hope", "I wanted to reach out", "My name is",
  "I'm writing to", "I came across", "Quick intro", "Just reaching out".
- If warm_paths > 0, reference the mutual connection naturally in S1 or S2.
- S3, the "*Here's some more context*" line, all five static paragraphs, the sign-off,
  and the sender title must be copied verbatim — do not paraphrase or summarise them.
- NEVER invent facts not present in the intelligence.

Return JSON matching the schema you are given.
"""


class OutreachDraft(BaseModel):
    """Structured output schema for the outreach LLM call."""
    subject: str = Field(max_length=120)
    body: str = Field(max_length=5000)
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

    # Pull the richest personalization signals into clearly labelled fields
    archetype_evidence = appetite.get("archetype_evidence") or ""
    allocation_evidence = appetite.get("allocation_evidence") or []
    if isinstance(allocation_evidence, str):
        try:
            allocation_evidence = json.loads(allocation_evidence)
        except Exception:
            allocation_evidence = [allocation_evidence]
    similarity_rationale = appetite.get("similarity_rationale") or ""

    appetite_signal_keys = (
        "check_size", "stage_preference", "sector_focus",
        "geography", "emerging_manager_program",
    )
    appetite_lines = [
        f"  - {k}: {appetite[k]}"
        for k in appetite_signal_keys
        if appetite.get(k) and isinstance(appetite[k], str)
    ]
    appetite_block = "\n".join(appetite_lines) if appetite_lines else "  (none on record)"

    # Contact name + title for formality calibration
    contact_name = first_contact.get("name") or "(unknown — address the organization)"
    contact_title = first_contact.get("title") or ""
    contact_label = f"{contact_name}" + (f" ({contact_title})" if contact_title else "")

    parts = [
        f"RECIPIENT: {lead['investor_name']}",
        f"Contact person: {contact_label}",
        f"Org / firm: {lead['investor_name']}",
        f"LP archetype: {(dossier or {}).get('archetype') or appetite.get('archetype') or lead.get('investor_type') or 'unknown'}",
        f"Location: {lead.get('investor_location') or 'unknown'}",
        f"Tone: {tone}",
        f"Sender (GP): {sender_name or 'the GP'}",
        "",
        "=== INTELLIGENCE — use ONLY these facts; do not invent ===",
        f"Investor profile / details: {(lead.get('investor_details') or '')[:800]}",
        f"Gate summary: {lead.get('gate_summary') or (dossier or {}).get('research_notes', '')[:400]}",
        f"Archetype evidence (why they fit): {archetype_evidence}",
        f"Known portfolio / allocation signals: {json.dumps(allocation_evidence)}",
        f"Similarity rationale: {similarity_rationale}",
        f"Verified LP commitments: {json.dumps((dossier or {}).get('lp_commitments') or [])}",
        f"Appetite signals:\n{appetite_block}",
        f"Warm paths on record: {lead.get('warm_path_count') or 0}",
    ]

    if (dossier or {}).get("analyst_notes"):
        parts.append(f"Analyst notes: {dossier['analyst_notes'][:400]}")

    latest_event = (dossier or {}).get("latest_portfolio_event") or ""
    if latest_event:
        parts.append(f"Latest portfolio milestone: {latest_event[:200]}")

    if prior_subjects:
        parts.append(
            f"Prior outreach subjects already sent to this LP "
            f"(do NOT reuse the same hook or subject): {prior_subjects}"
        )

    if extra_instructions:
        parts.append(f"\nADDITIONAL SENDER INSTRUCTIONS: {extra_instructions[:400]}")

    parts.append(
        f"\n=== STATIC PITCH — copy these five paragraphs verbatim into the body, word-for-word ===\n{_STATIC_PITCH}"
    )
    parts.append(
        "\n=== YOUR TASK ===\n"
        "Write S1 and S2 of the personalized paragraph using ONLY the intelligence above.\n"
        "S1 must name the recipient's org/fund and capture their SPECIFIC distinguishing characteristic "
        "(not a generic description). S2 must cite the highest-tier fact available — named portfolio, "
        "quantified track record, named program, or verbatim thesis language.\n"
        "If you cannot find a specific fact for S2, use LP type + location + sector — never leave it generic.\n"
        "Return subject, body (full email from 'Hi [First Name],' through the signature), "
        "and personalization_points listing which specific facts from the intel you used."
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
        max_tokens=3000,
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

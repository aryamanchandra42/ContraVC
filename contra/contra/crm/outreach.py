"""
Outreach personalization agent — turns gate intelligence into first-touch emails.

For a CRM lead, gathers everything we know (dossier: verified LP commitments,
appetite, archetype, warm paths, sources; lead row: contacts, type, location)
and drafts a personalized outreach email with a strong model (default
claude-opus-4-5, override via OUTREACH_LLM_MODEL).

Email structure:
  - Subject: chosen from three format strategies (specificity hook, observation/data
    lead, or bridge) based on which produces the most compelling and unique line for
    this specific recipient.
  - Personalized opening paragraph (2–3 sentences):
      - Leads with the highest-tier intel signal (named fund, portfolio co, track
        record, thesis quote). First sentence never starts with "I/We/Contra".
      - Bridges to Contra VC with the specific overlap point.
      - Optional warm-path / credibility sentence if signal warrants it.
      - (static) Factsheet link + call CTA.
  - Static pitch block (verbatim, 5 paragraphs): data point → fund thesis →
    GP track record → founder archetype → investment mechanics.
  - Static sign-off.

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
COLD EMAIL PRINCIPLES — internalize these before writing:

1. LEAD WITH THEIR WORLD, NOT YOURS. The first sentence must make the recipient feel
   seen and understood before you say a single word about Contra VC.
2. SPECIFICITY IS PROOF OF RESEARCH. Vague compliments signal a mass blast.
   Naming a specific fund they backed, a portfolio company, a thesis line they've
   published, or a program they run signals you actually looked them up.
3. PATTERN INTERRUPT. Don't open like every other cold email. Avoid "I hope this
   finds you well", "I wanted to reach out", "Quick intro". Start with a sharp
   observation, a striking data point, or a named reference to something specific.
4. SHORT OPENING WINS. The personalized paragraph is 2–3 sentences only.
   Busy allocators scan first — make every word earn its place.
5. ONE CLEAR ASK. The factsheet link + call CTA in S3 is the only ask.
   Don't add more.

═══════════════════════════════════════════════════════
SUBJECT LINE STRATEGY

Generate the single best subject line for this specific recipient using whichever
format scores highest for their archetype. Evaluate all three formats and pick the
winner — do NOT use format A just because it's listed first.

FORMAT A — Specificity hook (best when you have a named fund, portfolio co, or thesis phrase):
  "[Org short name] + [named thing from their world] → Contra"
  Examples:
    "Techstars + first-gen operator thesis → Contra"
    "Alumni Ventures + Global Asian deal flow → Contra"
    "Oyster + the emerging-manager angle → Contra"

FORMAT B — Observation/data lead (best when they have a distinctive mandate or track record):
  A 6–10 word statement of a sharp insight tied to THEIR specific focus area.
  Examples:
    "50% of YC is Asian — you already know this"
    "The operator archetype your LPs haven't seen funded yet"
    "Global Asian founders: the thesis hiding in plain sight"
    "Why first-gen operators are building the next B2B wave"

FORMAT C — Bridge (use when a mutual connection exists OR the overlap is unusually tight):
  "From [Org short name] to Contra — [3–6 word specific bridge]"
  Examples:
    "From Plug and Play to Contra — backing immigrant founders in AI"
    "From SVB Alumni Network to Contra — the Global Asian LP thesis"

SUBJECT LINE HARD RULES:
- NEVER: "Intro to Contra VC", generic phrases like "great founders" or "AI and venture",
  exclamation marks, questions, "reaching out", "just a note", "following up".
- The subject must be unique to this recipient. If it could be sent to 10 other people
  unchanged, rewrite it.
- Max 12 words.

═══════════════════════════════════════════════════════
EMAIL STRUCTURE

  [SUBJECT — chosen per above strategy]

  Hi [First Name],

  [PERSONALIZED OPENING — 2–3 sentences. NO rigid S1/S2/S3 labeling. Write it as a
   natural, flowing paragraph that a human would actually send.]

  OPENING GUIDANCE:
  • Sentence 1 — Hook with specificity. Open with the single most impressive or
    surprising specific fact you know about this recipient: a named fund they backed,
    a program they run, a portfolio company, a quantified track record, a verbatim
    thesis quote. Make it about THEM, not about you.
    Never start with: "I", "We", "Contra", "Over the last decade", "I hope",
    "I wanted to reach out", "My name is", "I came across", "Quick intro".
    Instead, try openings like:
      "[Fund name]'s bet on [thesis area] is one of the sharper theses I've seen in this space..."
      "[Specific portfolio company] caught my attention — it fits exactly the archetype..."
      "Backing [X] funds and running [program] puts [Org] in a rare position to..."
      "[Mutual connection] mentioned you'd been tracking [topic] — that's precisely why I'm writing."
  • Sentence 2 — Bridge to Contra VC. State the SPECIFIC reason why their world
    intersects with what Contra is building. Name the precise overlap. This sentence
    should be impossible to send to anyone else.
  • Sentence 3 (optional, use only if it adds signal) — Credibility or warm path hook.
    If warm_paths > 0, reference the mutual connection by name or role.
    If they have a specific program, quantified track record, or known LP commitment,
    lean on it here. Otherwise, skip this sentence and keep the opening to 2 sentences.

  SENTENCE 3 — COPY THIS VERBATIM, unchanged:
    "Our Fund I factsheet is here: https://contravcfactsheet.netlify.app/ and I'd love to find time for a call if it sparks any questions."

  *Here's some more context on what we're building:*

  [STATIC PITCH — copy the following paragraphs verbatim, do not alter a single word]:
{_STATIC_PITCH}

  Would love to chat if you'd like to know more!

  [Sender name]

  General Partner, Contra VC

═══════════════════════════════════════════════════════

HARD RULES:
- The opening paragraph must feel written FOR this specific person. If it could be
  copy-pasted to a different recipient unchanged, rewrite it.
- NEVER fabricate. Use ONLY facts that appear in the intelligence section.
- NEVER force "Given your…" as an opener — it signals a template.
- If warm_paths > 0, reference the mutual connection naturally.
- The factsheet sentence, the "*Here's some more context*" line, all five static
  paragraphs, the sign-off, and the sender title must be copied verbatim.
- Vary the opening structure: don't use the same grammatical pattern as prior drafts.

ARCHETYPAL TACTICS (apply based on LP type):
  Family office / UHNWI:  Lead with their portfolio or investment thesis. They care
    about strategic alignment with their family's legacy focus areas.
  Fund of funds:  Lead with their portfolio construction angle — diversity mandate,
    emerging manager program, or specific manager criteria they've published.
  Corporate VC / accelerator:  Lead with their portfolio overlap or the specific
    sector/geography where Contra's founders match their deal flow.
  Endowment / foundation:  Lead with their mission alignment or long-term allocation
    mandate. Reference their known alternative allocation percentage if available.
  Angel / individual:  Lead with their personal track record or a named company they
    backed. Shorter, more conversational tone.

Return JSON matching the schema you are given.
"""


class OutreachDraft(BaseModel):
    """Structured output schema for the outreach LLM call."""
    subject: str = Field(max_length=120)
    body: str = Field(max_length=5000)
    personalization_points: List[str] = Field(
        default_factory=list, max_length=8,
        description="Which specific facts from the intelligence were used as hooks",
    )
    subject_format: str = Field(
        default="",
        description="Which subject line format was used: 'A' (specificity hook), 'B' (observation/data), or 'C' (bridge)",
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

    lp_archetype = (
        (dossier or {}).get("archetype")
        or appetite.get("archetype")
        or lead.get("investor_type")
        or "unknown"
    )

    # Rank the available intel signals so the LLM knows what's highest-value
    signal_inventory: list[str] = []
    lp_commitments = (dossier or {}).get("lp_commitments") or []
    if lp_commitments:
        signal_inventory.append(f"TIER A — Named LP commitments: {json.dumps(lp_commitments)}")
    if allocation_evidence:
        signal_inventory.append(f"TIER A — Named portfolio / allocation signals: {json.dumps(allocation_evidence)}")
    if appetite.get("emerging_manager_program"):
        signal_inventory.append(f"TIER A — Emerging manager program: {appetite['emerging_manager_program']}")
    if archetype_evidence:
        signal_inventory.append(f"TIER B — Archetype evidence: {archetype_evidence}")
    if similarity_rationale:
        signal_inventory.append(f"TIER B — Similarity rationale: {similarity_rationale}")
    if appetite_lines:
        signal_inventory.append(f"TIER C — Appetite signals:\n" + "\n".join(appetite_lines))
    if not signal_inventory:
        signal_inventory.append("(No strong signals — use LP type + location + sector combo)")

    warm_count = lead.get("warm_path_count") or 0
    warm_note = (
        f"⚠ WARM PATH: {warm_count} mutual connection(s) on record — reference this naturally "
        f"in the opening (do not ignore it)"
        if warm_count > 0 else "No warm paths on record"
    )

    parts = [
        f"RECIPIENT: {lead['investor_name']}",
        f"Contact person: {contact_label}",
        f"LP archetype: {lp_archetype}",
        f"Location: {lead.get('investor_location') or 'unknown'}",
        f"Tone: {tone}",
        f"Sender (GP): {sender_name or 'the GP'}",
        f"Warm paths: {warm_note}",
        "",
        "=== INTELLIGENCE — use ONLY these facts; do not invent ===",
        f"Investor profile / details:\n{(lead.get('investor_details') or '')[:1000]}",
        f"Gate summary: {lead.get('gate_summary') or (dossier or {}).get('research_notes', '')[:600]}",
        "",
        "=== RANKED PERSONALIZATION SIGNALS (highest → lowest value) ===",
        "\n".join(signal_inventory),
    ]

    if (dossier or {}).get("analyst_notes"):
        parts.append(f"\nAnalyst notes: {dossier['analyst_notes'][:500]}")

    latest_event = (dossier or {}).get("latest_portfolio_event") or ""
    if latest_event:
        parts.append(f"Latest portfolio milestone: {latest_event[:200]}")

    if prior_subjects:
        parts.append(
            f"\n⚠ PRIOR SUBJECTS (do NOT reuse the same hook, angle, or grammatical "
            f"structure as any of these): {prior_subjects}"
        )

    if extra_instructions:
        parts.append(f"\nADDITIONAL SENDER INSTRUCTIONS: {extra_instructions[:400]}")

    parts.append(
        f"\n=== STATIC PITCH — copy these five paragraphs verbatim into the body, word-for-word ===\n{_STATIC_PITCH}"
    )
    parts.append(
        "\n=== YOUR TASK ===\n"
        "1. SUBJECT: Evaluate all three subject line formats (A, B, C) from the system prompt. "
        "Pick the format that produces the most specific and compelling subject for THIS recipient "
        "using only the ranked signals above. Justify your choice internally, then return only the "
        "winning subject.\n\n"
        "2. OPENING PARAGRAPH: Write 2–3 sentences using the HIGHEST-TIER signal available. "
        "The first sentence must NOT start with 'I', 'We', or 'Contra'. Lead with something "
        "specific about the recipient's world. Every sentence must be impossible to send "
        "to a different LP unchanged.\n\n"
        "3. FULL BODY: Assemble the complete email — opening paragraph, verbatim factsheet "
        "sentence, '*Here's some more context on what we're building:*' line, all five static "
        "paragraphs verbatim, then the sign-off.\n\n"
        "4. PERSONALIZATION POINTS: List the specific facts from the intel you used as hooks "
        "(be precise — e.g. 'Named fund: Sequoia Heritage' not 'portfolio signals').\n\n"
        "Return subject, body (full email from 'Hi [First Name],' through the signature), "
        "and personalization_points."
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
        max_tokens=4000,
    )

    draft_id = str(uuid.uuid4())
    personalization_payload = {
        "points": draft.personalization_points,
        "subject_format": draft.subject_format or "",
    }
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
            json.dumps(personalization_payload),
        ],
    )
    append_outreach_event(con, lead["investor_name"], {
        "event": "draft_generated",
        "draft_id": draft_id,
        "subject": draft.subject,
        "subject_format": draft.subject_format or "",
        "model": model,
    })
    return {
        "draft_id": draft_id,
        "lead_id": lead["lead_id"],
        "investor_name": lead["investor_name"],
        "subject": draft.subject,
        "subject_format": draft.subject_format or "",
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
        raw = data.get("personalization_json")
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except Exception:
                raw = {}
        # Support both old list format and new dict format
        if isinstance(raw, list):
            points, subject_format = raw, ""
        elif isinstance(raw, dict):
            points = raw.get("points") or []
            subject_format = raw.get("subject_format") or ""
        else:
            points, subject_format = [], ""
        out.append({
            "draft_id": data.get("draft_id"),
            "lead_id": data.get("lead_id"),
            "investor_name": data.get("investor_name"),
            "subject": data.get("subject"),
            "body": data.get("body"),
            "tone": data.get("tone"),
            "model": data.get("model"),
            "personalization_points": points,
            "subject_format": subject_format,
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

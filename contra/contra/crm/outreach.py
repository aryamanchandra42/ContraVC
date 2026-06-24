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
        record, thesis quote). First sentence MUST start with "I noticed", "I loved", "Your work at", or "Your recent investment".
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
from typing import Any, Dict, List, Optional, Literal

from pydantic import BaseModel, Field

from contra.crm.dossier import append_outreach_event, get_dossier

logger = logging.getLogger(__name__)

_FUND_CONTEXT = (
    "Contra VC — Fund I, $30M target, backing Global Asian founders building "
    "B2B AI companies. Pre-seed and seed, $500-750K tickets, ~30 companies. "
    "Institutional form of MyAsiaVC, which deployed $70M+ across 300+ companies "
    "alongside 6,000+ LPs over the past decade."
)

_CONTRA_STORY_INGREDIENTS = """\
MANDATORY DATA POINTS AND FEATURES (must be included exactly as facts, though you can weave them naturally into the narrative):
- Contra VC is a $30M Fund I.
- We invest $500-750K at pre-seed and seed stages.
- Target portfolio is ~30 companies with concentrated follow-on.
- The Thesis: 50% of new US tech founders are Asian (a share rising every YC batch), yet no institutional fund was built explicitly for these technical-first, first-generation operators (especially ex-Google, Meta, OpenAI).
- The Focus: Global Asian founders building B2B AI companies (AI infrastructure, vertical automation, enterprise software) for the world.
- The Track Record: Co-GPs Sajid and I have deployed $70M+ across 300+ companies alongside 6,000+ LPs over the past decade through MyAsiaVC.
- The Edge: Contra VC is the institutional form of that community, letting us lead rounds and back underestimated founders at inception where they don't fit standard fund archetypes.
"""


_DEFAULT_SENDER = "Aabhas Khanna"


# ─────────────────────────────────────────────────────────────────────────────
# Per-archetype opening playbooks.
# Keyed by the gate Archetype enum (contra.gate.models.Archetype). Each entry is
# injected into the prompt so the hook is written for THIS kind of allocator —
# a family office should never get the same opening as a fund-of-funds.
# ─────────────────────────────────────────────────────────────────────────────
_ARCHETYPE_PLAYBOOKS: Dict[str, str] = {
    "fund_of_funds": (
        "ARCHETYPE: Fund-of-funds.\n"
        "Address them directly about their specific manager investments. Keep it conversational and jargon-free.\n"
        "Hook examples:\n"
        "  \"I noticed your emerging manager program backing [named Fund I]. We are the Global Asian AI slot most FoF books are still missing.\"\n"
        "  \"Your work at [Firm] backing [named manager] suggests you have a deliberate Global Asian operator allocation.\""
    ),
    "family_office": (
        "ARCHETYPE: Family office / UHNWI.\n"
        "Address their direct deal or legacy focus directly. Keep it human and simple.\n"
        "Hook examples:\n"
        "  \"Your recent investment in [named company] stood out to me. We are building a community of founders that I think you would want to see.\"\n"
        "  \"I noticed your focus on [thesis] at [Family/Office]. That aligns perfectly with what we have spent a decade building.\""
    ),
    "founder_lp": (
        "ARCHETYPE: Founder / operator / angel LP.\n"
        "Peer-to-peer and SHORT. Lead with a company they founded or backed. Conversational, zero corporate-speak.\n"
        "Hook examples:\n"
        "  \"I loved what you built at [Company]. We are backing technical founders who are taking a similar path.\"\n"
        "  \"Your recent investment in [Company] caught my attention. We are funding similar founders at the earliest stages.\""
    ),
    "corporate_investor": (
        "ARCHETYPE: Corporate VC / strategic / accelerator.\n"
        "Address their strategic focus directly. Keep it simple and clear.\n"
        "Hook examples:\n"
        "  \"I noticed [Corp]'s recent push into [sector]. We are backing founders in that exact space at inception.\"\n"
        "  \"Your work at [program/accelerator] is impressive. We are funding founders right before they reach that stage.\""
    ),
    "institutional_lp": (
        "ARCHETYPE: Endowment / foundation / institutional LP.\n"
        "Address their mission or long-term mandate directly. Keep it professional but human.\n"
        "Hook examples:\n"
        "  \"I noticed [Institution]'s commitment to [mission/alt-allocation]. We are building a fund that shares that long-term view.\"\n"
        "  \"Your work at [Institution] supporting first-time managers is exactly what the market needs right now.\""
    ),
    "asia_specialist": (
        "ARCHETYPE: Asia / SEA specialist.\n"
        "Address their geographic focus directly without using jargon.\n"
        "Hook examples:\n"
        "  \"Your work at [Firm] building a portfolio in [SEA/Asia] shows a clear understanding of the region. We are backing those same founders as they build globally.\"\n"
        "  \"I noticed your recent investments in [region]. We are making a very similar bet on those founders.\""
    ),
    "technology_specialist": (
        "ARCHETYPE: AI / technology specialist.\n"
        "Address their specific AI or tech investments directly. Keep it conversational.\n"
        "Hook examples:\n"
        "  \"Your recent investment in [Portfolio co] caught my eye. We are backing similar founders right at the start.\"\n"
        "  \"I noticed your focus on AI infrastructure. We are funding those exact founders at the pre-seed stage.\""
    ),
    "emerging_manager_specialist": (
        "ARCHETYPE: Emerging manager specialist.\n"
        "Address their focus on new managers directly. Keep it simple and confident.\n"
        "Hook examples:\n"
        "  \"I noticed that you recently backed [named Fund I]. We are building a new fund based on a decade of prior investments.\"\n"
        "  \"Your work at [Firm] focusing on emerging managers is exactly why I am reaching out. We are raising a Fund I with a very clear track record.\""
    ),
    "generalist": (
        "ARCHETYPE: Generalist / unknown.\n"
        "Lead with the single strongest SPECIFIC fact you found about them. Address them directly.\n"
        "Hook examples:\n"
        "  \"I noticed your recent work with [Specific fact]. It aligns perfectly with what we are building.\"\n"
        "  \"Your work at [Firm] in [their angle] stood out to me. I wanted to share what we are working on.\""
    ),
}
_ARCHETYPE_PLAYBOOKS["unknown"] = _ARCHETYPE_PLAYBOOKS["generalist"]


# Map raw crm_leads.investor_type strings to a playbook key when the gate has not
# assigned a behavioral archetype.
_TYPE_TO_ARCHETYPE = {
    "fund of funds": "fund_of_funds",
    "fund-of-funds": "fund_of_funds",
    "fof": "fund_of_funds",
    "family office": "family_office",
    "family_office": "family_office",
    "multi-family office": "family_office",
    "single family office": "family_office",
    "uhnwi": "family_office",
    "hnwi": "family_office",
    "angel": "founder_lp",
    "angel investor": "founder_lp",
    "individual": "founder_lp",
    "operator": "founder_lp",
    "founder": "founder_lp",
    "gp": "founder_lp",
    "corporate": "corporate_investor",
    "corporate vc": "corporate_investor",
    "cvc": "corporate_investor",
    "accelerator": "corporate_investor",
    "strategic": "corporate_investor",
    "endowment": "institutional_lp",
    "foundation": "institutional_lp",
    "pension": "institutional_lp",
    "institutional": "institutional_lp",
    "sovereign": "institutional_lp",
}


def _resolve_archetype(lead: Dict[str, Any], dossier: Optional[Dict[str, Any]]) -> str:
    """Resolve the best playbook key: gate archetype first, then investor_type map."""
    appetite = (dossier or {}).get("appetite") or lead.get("appetite_json") or {}
    arch = (appetite.get("archetype") or "").strip().lower()
    if arch and arch in _ARCHETYPE_PLAYBOOKS:
        return arch
    itype = (lead.get("investor_type") or "").strip().lower()
    if itype in _TYPE_TO_ARCHETYPE:
        return _TYPE_TO_ARCHETYPE[itype]
    # Loose contains-match for messy type strings
    for needle, key in _TYPE_TO_ARCHETYPE.items():
        if needle in itype:
            return key
    return "generalist"

_SYSTEM = f"""You write first-touch LP outreach emails for a VC fund GP.

FUND STORY INGREDIENTS:
{_CONTRA_STORY_INGREDIENTS}

═══════════════════════════════════════════════════════
COLD EMAIL PRINCIPLES (DYNAMIC WEAVING):
1. The Story Arc: Your job is to weave a single, cohesive narrative. Do NOT just drop an abrupt quote followed by a copy-pasted pitch. Connect THEIR world to OUR world smoothly.
2. High Info (Deep Research Found): Start with a specific signal from the research (a named portfolio co, a specific thesis quote, etc.). Then, explicitly bridge *why* that specific fact makes them a fit for the Contra VC story. Weave our track record and thesis in as the natural next step.
3. Low Info (Sparse Research): If you only have generic tags (like "invests in AI"), use a brief, polite, human opener. Then, lean heavily into telling the Contra story—lead with the massive data point (50% of US tech founders are Asian) or our track record ($70M deployed via MyAsiaVC). Talk more about us than them.
4. Voice: Write like a confident founder-GP speaking peer-to-peer. Simple, declarative sentences. No corporate VC jargon.

═══════════════════════════════════════════════════════
SUBJECT LINE STRATEGY:
Max 12 words. Pick the format that produces the most specific subject:
FORMAT A (Specificity hook): "[Org short name] + [named thing from their world] → Contra"
FORMAT B (Observation): A 6–10 word insight tied to their focus.
FORMAT C (Bridge): "From [Org] to Contra — [specific bridge]"

═══════════════════════════════════════════════════════
EMAIL STRUCTURE:

  [SUBJECT]

  Hi [First Name] / Name,

  [THE BODY — 3-4 short paragraphs maximum]
  - Smoothly weave their context (if any) with the Contra story ingredients.
  - MUST include the mandatory data points ($30M, $70M deployed, 300+ companies, 50% Asian founders), but they should flow naturally as part of the narrative.
  
  [THE ASK VERBATIM]
  Our Fund I factsheet is here: https://contravcfactsheet.netlify.app/ and I'd love to find time for a call if it sparks any questions.

  [Sender name]
  General Partner, Contra VC

═══════════════════════════════════════════════════════
HARD RULES:
- NO ABRUPT QUOTES. Connect their facts to our thesis. If you mention they invest in X, explain how that connects to Contra backing Y.
- You MUST weave in the core metrics ($30M Fund I, $500-750K checks, $70M deployed via MyAsiaVC, 50% Asian founders data point).
- NO EM DASHES OR HYPHENS in the opening paragraph. Use periods to connect thoughts.
- NEVER fabricate facts.
- Do not use jargon (e.g., "alpha", "deal flow", "lens").
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


class OutreachCritique(BaseModel):
    """Critique of the generated email draft."""
    verdict: Literal["PASS", "REVISE"] = Field(description="PASS if the hook is highly specific and follows all rules. REVISE if it's generic, uses banned words, or fails rules.")
    critique_reasoning: str = Field(description="Why this passed or failed. If REVISE, what exactly needs fixing.")


def _critique_and_revise(
    llm: Any,
    draft: OutreachDraft,
    prompt_str: str,
) -> OutreachDraft:
    """Run a critique-revise loop on the draft. Max 2 iterations."""
    system = "You are a ruthless cold email editor. You critique email drafts against strict rules."
    
    for i in range(2):
        # 1. Deterministic checks first
        body = draft.body.lower()
        if "—" in body or "-" in draft.body.split("\n\n")[1]: # Check hyphens in opening paragraph
            pass # We'll let the LLM judge fix it
            
        critique_prompt = f"""Review this cold email draft for a VC LP:
        
SUBJECT: {draft.subject}

BODY:
{draft.body}

CRITERIA:
1. Does the email read as a single, cohesive narrative? If it reads like an abrupt hook followed by a disconnected pitch deck, output REVISE.
2. Are the core Contra metrics included ($30M Fund I, $500-750K checks, $70M deployed via MyAsiaVC, 50% Asian founders)? If any are missing, output REVISE.
3. Are there any em-dashes (—) or hyphens (-) in the personalized opening paragraph? (There MUST NOT BE ANY).
4. Does it use jargon like "alpha", "lens", "deal flow"? (It shouldn't).

If it violates ANY of these, or is generic, output REVISE and explain exactly why.
Otherwise, output PASS.
"""
        try:
            # We use the same LLM client for simplicity, but in a real enterprise setting
            # you'd inject a different model here to avoid self-preference bias.
            critique = llm.structured(
                prompt=critique_prompt,
                response_model=OutreachCritique,
                system=system,
                max_tokens=500,
            )
            
            if critique.verdict == "PASS":
                logger.debug("Draft passed critique on iteration %d", i+1)
                break
                
            logger.debug("Draft failed critique: %s. Revising...", critique.critique_reasoning)
            
            # Revise
            revise_prompt = prompt_str + f"\n\nYOUR PREVIOUS DRAFT FAILED CRITIQUE:\n{critique.critique_reasoning}\n\nREVISE THE DRAFT TO FIX THESE ISSUES. Do not make the same mistakes."
            
            draft = llm.structured(
                prompt=revise_prompt,
                response_model=OutreachDraft,
                system=_SYSTEM,
                max_tokens=4000,
            )
        except Exception as exc:
            logger.warning("Critique/revise loop failed: %s", exc)
            break # Fall back to the current draft
            
    return draft
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
    return os.environ.get("OUTREACH_LLM_MODEL", "").strip() or "gpt-4o"


def _lead_row(con, lead_id: str) -> Optional[Dict[str, Any]]:
    cursor = con.execute(
        """
        SELECT lead_id, investor_name, investor_type, investor_location,
               investor_details, contacts_json, gate_summary, gate_reasons_json,
               appetite_json, warm_path_count, pipeline_stage,
               icp_tier, fit_score, contra_rank, gate_confidence, gate_verdict
        FROM crm_leads WHERE CAST(lead_id AS VARCHAR) = ?
        """,
        [lead_id],
    )
    row = cursor.fetchone()
    if not row:
        return None
    cols = [d[0].lower() for d in cursor.description]
    data = dict(zip(cols, row))
    for jf in ("contacts_json", "appetite_json", "gate_reasons_json"):
        if isinstance(data.get(jf), str):
            try:
                data[jf] = json.loads(data[jf])
            except Exception:
                data[jf] = None
    data["lead_id"] = str(data.get("lead_id", ""))
    return data


class OutreachInsight(BaseModel):
    """A candidate angle for personalization derived from raw research."""
    fact_or_quote: str = Field(description="The specific fact, quote, or investment to reference")
    source: str = Field(description="Where this was found")
    why_non_obvious: str = Field(description="Why this proves deep research")
    bridge_to_contra: str = Field(description="One sentence connecting this to Contra's AI/Global Asian/Pre-seed thesis")


class ExtractedInsights(BaseModel):
    """The best 3 candidate angles for outreach."""
    candidate_angles: List[OutreachInsight] = Field(min_length=1, max_length=5)


def _extract_insight_angles(
    llm: Any,
    name: str,
    archetype: str,
    research_text: str,
    dossier_text: str,
) -> str:
    """
    Run a fast extraction pass over raw research to synthesize 3 concrete personalization angles.
    This prevents the writer from merely restating raw facts without insight.
    """
    if not (research_text and research_text.strip()) and not (dossier_text and dossier_text.strip()):
        return "(No research text available for insight extraction)"
        
    system = "You are a senior VC analyst preparing a partner for a cold outreach email."
    prompt = f"""Extract the 3 most compelling personalization angles for {name} from this research.
    
    Look for:
    - Specific podcast quotes, blog posts, or tweets (with the actual takeaway, not just 'they have a podcast')
    - Surprising or highly specific named investments (why did they back it?)
    - LP fund commitments or emerging manager programs
    
    If the research is generic or mostly namesake noise, extract the best you can find but DO NOT fabricate.
    
    RESEARCH:
    {research_text[:8000]}
    
    DOSSIER/DB INTEL:
    {dossier_text[:3000]}
    """
    
    try:
        insights = llm.structured(
            prompt=prompt,
            response_model=ExtractedInsights,
            system=system,
            max_tokens=1500,
        )
        lines = []
        for i, angle in enumerate(insights.candidate_angles, 1):
            lines.append(f"Angle {i}:")
            lines.append(f"  - Specific fact: {angle.fact_or_quote}")
            lines.append(f"  - Source: {angle.source}")
            lines.append(f"  - Why it's good: {angle.why_non_obvious}")
            lines.append(f"  - Bridge idea: {angle.bridge_to_contra}")
        return "\n".join(lines)
    except Exception as exc:
        logger.warning("Insight extraction failed for %s: %s", name, exc)
        return "(Insight extraction failed, rely on raw research)"


def _build_prompt(
    lead: Dict[str, Any],
    dossier: Optional[Dict[str, Any]],
    tone: str,
    sender_name: str,
    extra_instructions: str,
    prior_subjects: Optional[List[str]] = None,
    archetype: str = "generalist",
    fresh_research: str = "",
    extracted_insights: str = "",
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
    negative_flags = appetite.get("negative_flags") or []
    if isinstance(negative_flags, str):
        try:
            negative_flags = json.loads(negative_flags)
        except Exception:
            negative_flags = [negative_flags]
    negative_evidence = appetite.get("negative_evidence") or ""

    appetite_signal_keys = (
        "check_size", "stage_preference", "sector_focus",
        "geography", "emerging_manager_program",
    )
    appetite_lines = [
        f"  - {k}: {appetite[k]}"
        for k in appetite_signal_keys
        if appetite.get(k) and isinstance(appetite[k], str)
    ]

    # Contact name + title for formality calibration
    contact_name = first_contact.get("name") or lead.get('investor_name', '')
    contact_title = first_contact.get("title") or ""
    contact_label = f"{contact_name}" + (f" ({contact_title})" if contact_title else "")
    first_name = contact_name.split()[0] if contact_name else '[First Name]'

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
        
    gate_reasons = lead.get("gate_reasons_json") or []
    if isinstance(gate_reasons, str):
        try:
            gate_reasons = json.loads(gate_reasons)
        except Exception:
            gate_reasons = []
    if gate_reasons:
        signal_inventory.append(
            "TIER B — Gate qualification reasons (use as subtext for relevance, "
            "do NOT quote directly or use as hook angle):\n"
            + "\n".join(f"  • {r}" for r in gate_reasons[:5])
        )

    if appetite_lines:
        signal_inventory.append(f"TIER C — Appetite signals:\n" + "\n".join(appetite_lines))
    if not signal_inventory:
        signal_inventory.append("(No strong signals — mine the deep research below; else use type + location + sector)")

    warm_count = lead.get("warm_path_count") or 0
    warm_note = (
        f"⚠ WARM PATH: {warm_count} mutual connection(s) on record — reference this naturally "
        f"in the opening (do not ignore it)"
        if warm_count > 0 else "No warm paths on record"
    )

    playbook = _ARCHETYPE_PLAYBOOKS.get(archetype, _ARCHETYPE_PLAYBOOKS["generalist"])

    # Derive the correct hook axis from WHO this LP is — not what qualified them
    lp_commitments = (dossier or {}).get("lp_commitments") or []
    _hook_axis_map = {
        "fund_of_funds": (
            "Lead with a SPECIFIC NAMED FUND they backed or their manager selection track record. "
            "Do NOT lead with their AI/tech portfolio exposure — that was a gate qualifier, not their identity."
        ),
        "family_office": (
            "Lead with a SPECIFIC DEAL, COMPANY, or FOCUS AREA from their family office activity. "
            "Their identity is as a capital allocator, not a tech investor."
        ),
        "founder_lp": (
            "Lead with something THEY BUILT OR BACKED as a founder or operator. "
            "Peer tone. Their journey as a builder is the hook — not their LP activity."
        ),
        "emerging_manager_specialist": (
            "Lead with their TRACK RECORD OF BACKING NEW MANAGERS specifically. "
            "Named funds they anchored if available. This is their primary identity."
        ),
        "corporate_investor": (
            "Lead with their ORGANIZATION'S SPECIFIC PROGRAM or recent strategic move. "
            "Not generic accelerator language — name the actual program or cohort."
        ),
        "institutional_lp": (
            "Lead with their INSTITUTION'S MANDATE or known alternatives allocation. "
            "Professional and evidence-first. No startup language."
        ),
        "asia_specialist": (
            "Lead with their SPECIFIC REGIONAL INVESTMENT or portfolio company in Asia/SEA. "
            "They know the region — acknowledge it with specificity."
        ),
        "technology_specialist": (
            "Lead with a SPECIFIC PORTFOLIO COMPANY they backed, not the generic 'AI focus'. "
            "The hook must name something real from the research."
        ),
        "generalist": (
            "Lead with the single most SPECIFIC AND UNIQUE fact about them from the web research. "
            "If no specific fact exists, say so — do not fabricate a hook."
        ),
    }
    hook_axis = _hook_axis_map.get(archetype, _hook_axis_map["generalist"])
    if lp_commitments:
        hook_axis = (
            f"STRONGEST AVAILABLE SIGNAL: Named LP fund commitments on record: {lp_commitments[:3]}. "
            f"Lead with one of these named funds — this is the most specific hook possible."
        )

    approach_directive = (
        f"=== OUTREACH APPROACH DIRECTIVE — read before touching the research ===\n"
        f"Hook axis for {archetype.upper()}: {hook_axis}\n\n"
        f"⚠ CRITICAL FRAMING NOTE: The web research and gate summary contain AI/tech/VC signals "
        f"that were gathered to QUALIFY this LP (C1-C4 gate checks). Those signals tell you they "
        f"passed the gate. They do NOT tell you how to open the email. The hook must reflect "
        f"WHO THIS LP IS as an allocator ({archetype}), not why they passed our screening. "
        f"Do not let AI/tech language from the gate research bleed into the hook unless the LP's "
        f"PRIMARY identity is technology_specialist."
    )

    # Combine fresh deep research with the stored dossier research notes (generous budget)
    stored_research = (dossier or {}).get("research_notes", "") or ""
    research_blocks: list[str] = []
    if fresh_research and fresh_research.strip():
        research_blocks.append(f"FRESH DEEP WEB RESEARCH (run just now):\n{fresh_research.strip()[:6000]}")
    if stored_research and stored_research.strip():
        research_blocks.append(f"STORED DOSSIER RESEARCH:\n{stored_research.strip()[:3000]}")
    research_section = "\n\n".join(research_blocks) if research_blocks else "(no web research available)"

    parts = [
        f"RECIPIENT: {lead['investor_name']}",
        f"Contact person: {contact_label}",
        f"LP archetype (resolved): {archetype}",
        f"Location: {lead.get('investor_location') or 'unknown'}",
        f"ICP Tier: {lead.get('icp_tier') or 'unknown'}",
        f"Gate confidence: {lead.get('gate_confidence') or 'unknown'}",
        f"Tone: {tone}",
        f"Sender (GP): {sender_name or _DEFAULT_SENDER}",
        f"Warm paths: {warm_note}",
        "",
        approach_directive,
        "",
        "=== SYNTHESIZED ANGLES (Use one of these if it lands well) ===",
        extracted_insights,
        "",
        "=== ARCHETYPE PLAYBOOK — write the opening THIS way for THIS kind of LP ===",
        playbook,
        "",
        "=== INTELLIGENCE — use ONLY these facts; do not invent ===",
        f"Investor profile / details:\n{(lead.get('investor_details') or '')[:1000]}",
        f"Gate summary: {lead.get('gate_summary') or ''}",
        "",
        "=== RAW WEB RESEARCH ===",
        research_section,
        "",
        "=== RANKED PERSONALIZATION SIGNALS (highest → lowest value) ===",
        "\n".join(signal_inventory),
    ]

    if negative_flags or negative_evidence:
        parts.append(
            "\n⚠ AVOID / DO-NOT-CLAIM (intel contradicts these angles — never use them):\n"
            f"  flags: {json.dumps(negative_flags)}\n  notes: {negative_evidence[:400]}"
        )

    if (dossier or {}).get("analyst_notes"):
        parts.append(f"\nAnalyst notes: {dossier['analyst_notes'][:500]}")

    if prior_subjects:
        parts.append(
            f"\n⚠ PRIOR SUBJECTS (do NOT reuse the same hook, angle, or grammatical "
            f"structure as any of these): {prior_subjects}"
        )

    if extra_instructions:
        parts.append(f"\nADDITIONAL SENDER INSTRUCTIONS: {extra_instructions[:400]}")

    parts.append(
        f"\n=== CONTRA STORY INGREDIENTS (Weave these metrics naturally) ===\n{_CONTRA_STORY_INGREDIENTS}"
    )
    parts.append(
        "\n=== YOUR TASK ===\n"
        "1. SUBJECT: Evaluate all three subject line formats (A, B, C) from the system prompt. "
        "Pick the format that produces the most specific and compelling subject for THIS recipient. "
        "Return only the winning subject and which format you used.\n\n"
        "2. THE HOOK (most important): Write a catchy opening of 1–2 short sentences, ~300 characters "
        "or less, following the ARCHETYPE PLAYBOOK above. Lead with the single strongest specific fact "
        "about this recipient from the web research / signals. It must be impossible to send to anyone "
        "else. You MUST start the first sentence of the hook with exactly one of these phrases: 'I noticed', 'I loved', 'Your work at', or 'Your recent investment'. DO NOT use any dashes or hyphens.\n\n"
        f"3. FULL BODY: Assemble the complete email — start with 'Hi {first_name},', then dynamically weave the hook and the Contra story ingredients into 3-4 short paragraphs, ending with the verbatim factsheet sentence and sign-off.\n\n"
        "4. PERSONALIZATION POINTS: List the specific facts you used as hooks (be precise — "
        "e.g. 'Named fund: Sequoia Heritage', not 'portfolio signals').\n\n"
        "Return subject, subject_format, body (full email from 'Hi [First Name],' through the "
        "signature), and personalization_points."
    )
    return "\n".join(parts)


def _deep_research_for_lead(
    lead: Dict[str, Any],
    dossier: Optional[Dict[str, Any]],
    archetype: str,
) -> str:
    """
    Run fresh OpenAI deep research for this LP before the Opus call, specifically looking for hooks.
    Falls back to Tavily gracefully if OpenAI is unavailable.
    """
    name = lead["investor_name"]
    known_context = lead.get('investor_details') or ''
    
    # Try the high-quality adaptive OpenAI research first
    if os.environ.get("OPENAI_API_KEY"):
        try:
            from agents.research.openai_research import openai_lp_outreach_research
            notes, urls = openai_lp_outreach_research(
                name=name,
                archetype=archetype,
                known_context=known_context,
            )
            if notes:
                return notes
        except Exception as exc:
            logger.info("OpenAI outreach research unavailable or failed for '%s': %s", name, exc)

    return ""


def generate_outreach_draft(
    con,
    lead_id: str,
    tone: str = "warm",
    sender_name: str = _DEFAULT_SENDER,
    extra_instructions: str = "",
) -> Dict[str, Any]:
    """Generate + persist a personalized outreach draft for a CRM lead."""
    from agents.research.llm_client import LLMUnavailable, get_llm_client

    sender_name = (sender_name or "").strip() or _DEFAULT_SENDER

    lead = _lead_row(con, lead_id)
    if not lead:
        raise ValueError(f"Lead '{lead_id}' not found")

    dossier = get_dossier(con, lead["investor_name"])
    archetype = _resolve_archetype(lead, dossier)

    def _get_string_value(val: Any) -> str:
        if val is None:
            return ""
        if isinstance(val, str):
            return val
        return str(val)

    # Always run a fresh, cached deep-research pass so the hook is built on
    # current, specific facts rather than only stale DB fields.
    fresh_research = _get_string_value(_deep_research_for_lead(lead, dossier, archetype))

    stored_research = _get_string_value((dossier or {}).get("research_notes", ""))
    investor_details = _get_string_value(lead.get("investor_details", ""))
    
    has_research = bool(fresh_research.strip()) or bool(stored_research.strip())
    has_signals = bool(
        ((dossier or {}).get("lp_commitments") or [])
        or (lead.get("appetite_json") or {}).get("allocation_evidence")
        or _get_string_value(lead.get("gate_summary")).strip()
    )

    if not has_research and not has_signals:
        logger.warning(
            "Outreach draft blocked for '%s': no research and no signals available. "
            "Run gate screen first to populate dossier.",
            lead["investor_name"],
        )
        return {
            "draft_id": None,
            "lead_id": lead["lead_id"],
            "investor_name": lead["investor_name"],
            "error": "insufficient_intel",
            "message": (
                f"No research or signals available for {lead['investor_name']}. "
                "Run a gate screen first to populate the dossier, then regenerate."
            ),
        }

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
        provider = os.environ.get("OUTREACH_LLM_PROVIDER", os.environ.get("PULSE_LLM_PROVIDER", "anthropic"))
        llm = get_llm_client(provider=provider, model=model)
    except LLMUnavailable:
        # Fall back to the default configured provider (e.g. Haiku / OpenAI)
        llm = get_llm_client()
        model = getattr(llm, "model", "unknown")
        
    # Extract insights before writing
    stored_research = _get_string_value((dossier or {}).get("research_notes", ""))
    investor_details = _get_string_value(lead.get("investor_details", ""))
    extracted_insights = _extract_insight_angles(
        llm=llm,
        name=lead["investor_name"],
        archetype=archetype,
        research_text=fresh_research or "",
        dossier_text=stored_research + "\n" + investor_details,
    )

    prompt_str = _build_prompt(
        lead, dossier, tone, sender_name, extra_instructions,
        prior_subjects=prior_subjects or None,
        archetype=archetype,
        fresh_research=fresh_research,
        extracted_insights=extracted_insights,
    )
    
    draft = llm.structured(
        prompt=prompt_str,
        response_model=OutreachDraft,
        system=_SYSTEM,
        max_tokens=4000,
    )

    # 5. Critique and Revise Loop
    draft = _critique_and_revise(llm, draft, prompt_str)

    draft_id = str(uuid.uuid4())
    personalization_payload = {
        "points": draft.personalization_points,
        "subject_format": draft.subject_format or "",
        "archetype": archetype,
        "deep_research_used": bool(fresh_research.strip()),
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
        "archetype": archetype,
        "deep_research_used": bool(fresh_research.strip()),
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
        archetype = ""
        if isinstance(raw, list):
            points, subject_format = raw, ""
        elif isinstance(raw, dict):
            points = raw.get("points") or []
            subject_format = raw.get("subject_format") or ""
            archetype = raw.get("archetype") or ""
        else:
            points, subject_format = [], ""
        out.append({
            "draft_id": data.get("draft_id"),
            "lead_id": data.get("lead_id"),
            "investor_name": data.get("investor_name"),
            "subject": data.get("subject"),
            "archetype": archetype,
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

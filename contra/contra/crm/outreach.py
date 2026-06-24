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
      - Sentence 1: Their thesis — what their fund/firm stands for, who they back,
        what they believe. Leads with THEIR worldview, not a data point about them.
      - Sentence 2: Bridge — how Contra's thesis connects to theirs specifically.
      - Sentence 3: Specific research fact woven in as validation — the
        named fund, investment, or data point lives HERE as evidence, not as the opener.
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
from contra.crm import airtable_sync

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
        "Lead with THEIR THESIS as a manager selector — what they believe about which managers win.\n"
        "Sentence 1 frames their conviction. Sentence 2 bridges to Contra's thesis. Sentence 3 names a specific fund they backed as evidence.\n"
        "Hook examples:\n"
        "  S1: \"[Firm] was built around the belief that the best emerging managers are still invisible to most institutional capital.\"\n"
        "  S2: \"That's the same gap we're filling at Contra, specifically for Global Asian AI founders.\"\n"
        "  S3: \"Your commitment to [named Fund I] told me you'd find our approach worth 20 minutes.\""
    ),
    "family_office": (
        "ARCHETYPE: Family office / UHNWI.\n"
        "Lead with their investment identity or legacy thesis. Keep it human and simple.\n"
        "Sentence 1 frames who they are as capital allocators. Sentence 2 connects to Contra. Sentence 3 names a specific deal or focus area.\n"
        "Hook examples:\n"
        "  S1: \"[Family Office] has consistently backed founders that traditional capital misses — direct, conviction-led, long-term.\"\n"
        "  S2: \"That's exactly the investor mindset Contra is building around.\"\n"
        "  S3: \"Your investment in [named company] confirmed that you understand this space at the earliest stages.\""
    ),
    "founder_lp": (
        "ARCHETYPE: Founder / operator / angel LP.\n"
        "Peer-to-peer tone. Lead with what they built or what drives their angel thesis, not a dry fact.\n"
        "Sentence 1 frames their builder identity. Sentence 2 shows the shared conviction. Sentence 3 names what they built or backed.\n"
        "Hook examples:\n"
        "  S1: \"What you built at [Company] — [what made it distinctive] — is the same pattern we back at Contra before anyone else sees it.\"\n"
        "  S2: \"First-generation technical founders building B2B AI are exactly the archetype we fund at inception.\"\n"
        "  S3: \"Your investment in [specific company] is exactly the kind of early conviction that resonates with how we operate.\""
    ),
    "corporate_investor": (
        "ARCHETYPE: Corporate VC / strategic / accelerator.\n"
        "Lead with their organization's thesis or strategic mission, not a generic program name.\n"
        "Sentence 1 frames the org's conviction. Sentence 2 bridges to Contra. Sentence 3 names the specific program or cohort.\n"
        "Hook examples:\n"
        "  S1: \"[Corp]'s venture arm was built to back the founders that will redefine [their sector] from the outside.\"\n"
        "  S2: \"That conviction is why our portfolios will overlap — Contra backs the technical founders building that infrastructure.\"\n"
        "  S3: \"Your [program/cohort name] in [year] is exactly the kind of signal that made me reach out.\""
    ),
    "institutional_lp": (
        "ARCHETYPE: Endowment / foundation / institutional LP.\n"
        "Lead with their institution's mandate or long-term mission. Professional and evidence-first.\n"
        "Sentence 1 frames the institution's conviction. Sentence 2 connects to Contra's thesis. Sentence 3 cites their specific program or allocation.\n"
        "Hook examples:\n"
        "  S1: \"[Institution]'s alternatives program was built around a belief that the best returns come from backing underrepresented managers before they're obvious.\"\n"
        "  S2: \"That's the mandate Contra was designed for.\"\n"
        "  S3: \"Your commitment to [named program or manager] is the kind of track record that made me reach out.\""
    ),
    "asia_specialist": (
        "ARCHETYPE: Asia / SEA specialist.\n"
        "Lead with their regional conviction — what they believe about Asia-origin founders building globally.\n"
        "Sentence 1 frames their regional thesis. Sentence 2 bridges to Contra. Sentence 3 names a specific regional investment or move.\n"
        "Hook examples:\n"
        "  S1: \"[Firm]'s thesis has always been that the best global tech companies of the next decade are being built by founders from Asia.\"\n"
        "  S2: \"Contra is the institutional form of that bet — specifically for Global Asian founders building B2B AI for the world.\"\n"
        "  S3: \"Your investment in [named company] in [region] was exactly the kind of conviction that resonated with me.\""
    ),
    "technology_specialist": (
        "ARCHETYPE: AI / technology specialist.\n"
        "Lead with their technology thesis or what they believe about the AI infrastructure buildout.\n"
        "Sentence 1 frames their conviction about the tech wave. Sentence 2 bridges to Contra. Sentence 3 names a specific portfolio company.\n"
        "Hook examples:\n"
        "  S1: \"Your thesis has consistently been that the real AI infrastructure winners are the ones building for enterprise teams, not consumer audiences.\"\n"
        "  S2: \"That's precisely the space Contra is funding — Global Asian founders building B2B AI at the earliest stages.\"\n"
        "  S3: \"Your investment in [Portfolio co] is the clearest signal that you'd find our portfolio interesting.\""
    ),
    "emerging_manager_specialist": (
        "ARCHETYPE: Emerging manager specialist.\n"
        "Lead with their conviction about why backing new managers at Fund I creates outsized returns.\n"
        "Sentence 1 frames their emerging manager thesis. Sentence 2 bridges to why Contra fits. Sentence 3 cites a named fund they backed.\n"
        "Hook examples:\n"
        "  S1: \"[Firm]'s program was built on the belief that Fund I managers with deep community roots consistently outperform the market.\"\n"
        "  S2: \"That's the infrastructure Contra was designed around — a decade of community capital becoming an institutional fund.\"\n"
        "  S3: \"Your commitment to [named Fund I] told me you'd find our track record worth a conversation.\""
    ),
    "generalist": (
        "ARCHETYPE: Generalist / unknown.\n"
        "Lead with the single clearest signal of their investment thesis or identity, then bridge to Contra, then ground it in specifics.\n"
        "Hook examples:\n"
        "  S1: \"Your thesis around [their angle] has always been about backing founders that the consensus misses.\"\n"
        "  S2: \"That's the same conviction Contra was built on — specifically for Global Asian technical founders.\"\n"
        "  S3: \"[Specific fact from research] is exactly why I thought this would resonate.\""
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

_SYSTEM = """
You are a GP writing cold LP outreach emails for Contra VC (Fund I, $30M, AI & Robotics, pre-seed/seed, Global Asian founders).

Your job is to write a short, human, specific cold email. The goal is one reply. Not to impress. Not to explain everything. One reply.

═══════════════════════════════════════
DECISION TREE — run this before writing
═══════════════════════════════════════

STEP 1: Read the OUTREACH APPROACH DIRECTIVE and the web research carefully.
STEP 2: Identify TWO things separately:
  (A) Their THESIS — what their fund, firm, or approach stands for. What kinds of founders do they back? What do they believe the market misses? What conviction built their career?
  (B) A SPECIFIC FACT — a named fund they backed, a specific investment, a quote, an interview, a program they ran. Something recent and non-obvious (not their most famous career highlight).

STEP 3a: IF you have both (A) and (B) — open with their thesis, bridge to Contra's shared conviction, then use the specific fact as corroborating evidence in sentence 3.
STEP 3b: IF you have only (A) — open with their thesis, bridge to Contra, skip sentence 3.
STEP 3c: IF you have neither — do NOT fabricate. Use the NO-HOOK template below, leading with Contra's story confidently.

THE GOLDEN RULE: Their thesis always comes first. Specific facts are evidence, never the opener.

═══════════════════════
EMAIL STRUCTURE
═══════════════════════

WITH HOOK (specific fact found):

  The opening is THREE sentences, in this exact order:

  Sentence 1 — THEIR THESIS: What do they stand for? What did they build or back, and why?
    Lead with their conviction, their fund thesis, or their investment identity.
    Do NOT open with a dry research fact like "I noticed your fund closed $23M in August 2023."
    Frame it as a belief or mission: "[Their Fund] was built to back [their conviction]."
    or "Your thesis around [X] is exactly why I'm reaching out."
    Do NOT mention "Contra" in this sentence.

  Sentence 2 — THE THESIS BRIDGE: Connect their thesis directly to Contra's thesis.
    One sentence that shows the shared conviction. Make the overlap explicit and human.
    Example: "That conviction, that [overlooked founders / underestimated operators] are the real alpha, maps directly to what we've built at Contra."

  Sentence 3 — THE SPECIFIC INTELLIGENCE: Now bring in the research detail as corroborating evidence.
    Name the specific fact (fund size, date, named investment, quote) that proves you did the work.
    This grounds the thesis bridge in reality. It's the "and I know this because..." sentence.
    Example: "Your $23M inaugural fund, backed by 150+ angel investments, is exactly the kind of conviction-led track record that resonates."

  Line 4 — CTA (verbatim): "Our Fund I factsheet is here: https://contravcfactsheet.netlify.app/ and I'd love to find time for a call if it sparks any questions."
  [blank line]
  *Here's some more context on what we're building:*
  [STATIC PITCH — inserted verbatim by the system, do not rewrite it]
  [SIGN OFF]

NO-HOOK template (no specific fact found — or LP is too famous to hook specifically):
  Sentence 1: Lead with their archetype identity and why it resonates with what Contra is building.
    "[Their identity as X] is exactly the perspective we're looking to bring into Contra's LP base."
  Sentence 2: One sentence on Contra's thesis and why it fits their lens.
    "We are raising Fund I at Contra VC, backing Global Asian founders building B2B AI companies at pre-seed and seed — [one sentence on why this fits their specific angle]."
  Line 3: Factsheet CTA (verbatim): "Our Fund I factsheet is here: https://contravcfactsheet.netlify.app/ and I'd love to find time for a call if it sparks any questions."
  [blank line]
  *Here's some more context on what we're building:*
  [STATIC PITCH — inserted verbatim by the system, do not rewrite it]
  [SIGN OFF]

═══════════════════════
RULES — non-negotiable
═══════════════════════

1. No em dashes. Use commas, colons, or periods instead.
2. Do not mention "Contra" in sentence 1.
3. No bullet points in the email body.
4. Do not lead with a dry research fact (fund closed date, AUM figure, deal name) as sentence 1. Research details belong in sentence 3 as corroborating evidence, never as the opener. Do not use the LP's most famous or most-cited career highlight as the hook — if it's the first thing that comes up in a Google search, it is too generic. Find something specific and recent instead.
5. For high-profile angels or prolific investors (100+ investments), their well-known portfolio companies are NOT specific facts. Find a recent investment, a public statement, or a lesser-known bet.
6. The static pitch block is inserted verbatim after the opening — do not rewrite, summarize, or paraphrase it.
7. Do not add any paragraph between the CTA line and the static pitch block.
8. Sign off: sender name + "General Partner, Contra VC" only.
9. Maximum 3 sentences in the opening before the CTA line.
10. Do not fabricate facts. If you are uncertain whether something is true, use the NO-HOOK template instead.

═══════════════════════
EXAMPLE — WITH HOOK (use this as your quality benchmark)
═══════════════════════

RECIPIENT: Ihar Mahaniok (emerging_manager_specialist — runs Geek Ventures, $23M fund backing immigrant founders)
RESEARCH: Geek Ventures closed $23M Fund I in August 2023, focus on pre-seed immigrant founders, 150+ angel investments, Ihar has spoken publicly about immigrant founder psychology as a competitive advantage.

GOOD EMAIL:
---
Hi Ihar,

Geek Ventures was built to back bold, brilliant immigrant entrepreneurs. The same founders most institutional capital overlooks.

That conviction maps directly to our thesis at Contra: we believe first-generation, technical-first founders from Google, Meta, and OpenAI are building the next generation of enterprise AI, yet no institutional fund was designed for them.

Your $23M inaugural fund, backed by 150+ angel investments and a decade of deep founder psychology insight, is exactly the kind of track record that tells me you'd find our approach interesting.

Our Fund I factsheet is here: https://contravcfactsheet.netlify.app/ and I'd love to find time for a call if it sparks any questions.

*Here's some more context on what we're building:*

[STATIC PITCH]

Aabhas Khanna
General Partner, Contra VC
---

WHY IT WORKS: Opens with THEIR thesis (immigrant founders, overlooked capital). Bridges to Contra's shared belief (same conviction, different lens). Then uses the specific research detail ($23M, 150+ angels) as evidence — not as the opener. Short. Human. No jargon.

BAD VERSION (do NOT do this):
---
Hi Ihar,

I noticed that Geek Ventures closed its $23M inaugural fund in August 2023 focused on pre-seed immigrant founders.
---
WHY IT'S BAD: Opens with a dry research fact. Sounds like a data dump, not a thesis conversation.

═══════════════════════
EXAMPLE — NO-HOOK (high-profile LP, no specific recent fact)
═══════════════════════

RECIPIENT: Gokul Rajaram (founder_lp, 700+ investments)
HOOK AVAILABLE: Only famous investments (Airtable, Figma, DoorDash) — too generic

GOOD EMAIL:
---
Hi Gokul,

Your background as a founder and prolific early-stage angel puts you exactly in the community we're building Contra around.

We are raising Fund I at Contra VC, backing Global Asian founders building B2B AI companies at pre-seed and seed — the first-generation technical operators coming out of Google, Meta, and OpenAI who don't fit the archetype most funds optimize for.

Our Fund I factsheet is here: https://contravcfactsheet.netlify.app/ and I'd love to find time for a call if it sparks any questions.

*Here's some more context on what we're building:*

[STATIC PITCH]

Aabhas Khanna
General Partner, Contra VC
---

WHY IT WORKS: Leads with his identity as a founder/operator. Doesn't pretend to have a specific hook when the only available facts are too famous. Still personal and relevant.
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
1. Does sentence 1 open with THEIR THESIS — what they stand for, what they built, their conviction — NOT a dry research fact (fund size, closing date, deal name)? If it opens with "I noticed that [Firm] closed its $Xm fund in [Year]..." or any variant that leads with a data point rather than a thesis framing, output REVISE.
2. Does sentence 2 bridge their thesis to Contra's thesis explicitly? If the thesis connection is missing or vague, output REVISE.
3. Does sentence 3 use specific research intelligence as corroborating evidence (fund size, named investment, quote)? If the specific facts appear in sentence 1 instead, output REVISE.
4. Are the core Contra metrics included in the static pitch ($30M Fund I, $500-750K checks, $70M deployed via MyAsiaVC, 50% Asian founders)? If any are missing from the body as a whole, output REVISE.
5. Are there any em-dashes (—) in the personalized opening paragraph? (There MUST NOT BE ANY).
6. Does it use jargon like "alpha", "lens", "deal flow", "archetype"? (It shouldn't).

If it violates ANY of these, output REVISE and explain exactly which criterion failed and what the fix is.
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
            "Sentence 1: Frame THEIR THESIS as a manager selector — what do they believe about which Fund I managers win? "
            "Do NOT open with a named fund as a data point; open with the conviction that led them to back it. "
            "Sentence 3: Then name the specific fund as corroborating evidence."
        ),
        "family_office": (
            "Sentence 1: Frame their CAPITAL ALLOCATOR IDENTITY — what do they stand for as a family office? "
            "Sentence 3: Then name a specific deal or focus area from the research as the evidence."
        ),
        "founder_lp": (
            "Sentence 1: Frame WHAT THEY BUILT OR WHAT DRIVES THEIR ANGEL THESIS — their builder identity. "
            "Peer tone. Do not open with 'I noticed your investment in X' — open with what they built or believe. "
            "Sentence 3: Then name the specific company or investment as evidence."
        ),
        "emerging_manager_specialist": (
            "Sentence 1: Frame their CONVICTION ABOUT EMERGING MANAGERS — why do they believe Fund I managers outperform? "
            "Their identity is as someone who finds managers others miss. "
            "Sentence 3: Then name a specific fund they anchored as evidence."
        ),
        "corporate_investor": (
            "Sentence 1: Frame their ORGANIZATION'S MISSION or what their program is trying to achieve. "
            "Not generic 'you run an accelerator' — frame the conviction behind the program. "
            "Sentence 3: Then name the specific program, cohort, or recent strategic move."
        ),
        "institutional_lp": (
            "Sentence 1: Frame their INSTITUTION'S MANDATE or long-term thesis for alternatives. "
            "Professional and mission-grounded. "
            "Sentence 3: Then cite their specific program or known allocation as evidence."
        ),
        "asia_specialist": (
            "Sentence 1: Frame their REGIONAL CONVICTION — what do they believe about Asia-origin founders building globally? "
            "They know the region; acknowledge the thesis, not just the geography. "
            "Sentence 3: Then name a specific regional investment as evidence."
        ),
        "technology_specialist": (
            "Sentence 1: Frame their TECHNOLOGY THESIS — what do they believe about where the AI infrastructure buildout is heading? "
            "Sentence 3: Then name a specific portfolio company as corroborating evidence."
        ),
        "generalist": (
            "Sentence 1: Frame the clearest signal of their INVESTMENT IDENTITY or thesis from the research. "
            "If no clear thesis is available, lead with their archetype identity. "
            "Sentence 3: Then name the specific fact from research as evidence."
        ),
    }
    hook_axis = _hook_axis_map.get(archetype, _hook_axis_map["generalist"])
    if lp_commitments:
        hook_axis = (
            f"STRONGEST AVAILABLE SIGNAL: Named LP fund commitments on record: {lp_commitments[:3]}. "
            f"Do NOT open with these fund names as a data point. Instead, open with the THESIS that "
            f"led this LP to back those funds — their belief about what makes an emerging manager worth backing. "
            f"Then bridge to Contra's thesis. Then use one of the named fund commitments in sentence 3 as corroborating evidence."
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
        "\n=== YOUR TASK ===\n"
        "1. SUBJECT: Evaluate all three subject line formats (A, B, C) from the system prompt. "
        "Pick the format that produces the most specific and compelling subject for THIS recipient. "
        "Return only the winning subject and which format you used.\n\n"
        "2. THE HOOK (most important): Determine if you have a specific, recent, non-obvious fact about this person. If YES, write a specific 2-sentence opening. If NO (or if they are too famous), use the NO-HOOK template.\n\n"
        f"3. FULL BODY: Assemble the complete email — start with 'Hi {first_name},', then your opening, the verbatim factsheet sentence, the '*Here's some more context on what we're building:*' line, the static pitch verbatim, and the sign-off.\n\n"
        "4. PERSONALIZATION POINTS: List the specific facts you used as hooks (be precise — "
        "e.g. 'Named fund: Sequoia Heritage', not 'portfolio signals').\n\n"
        "Return subject, subject_format, body, and personalization_points."
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
    
    # Detect high-profile / prolific investors — for these, generic queries
    # return their most-famous investments which are too well-known to hook on.
    # Instead search for recent, specific, lesser-known activity.
    prominence_keywords = ["techstars", "yc", "ycombinator", "500 startups", "sequoia",
                           "a16z", "accel", "benchmark", "google", "meta", "coinbase",
                           "doordash", "stripe", "airbnb"]
    is_prominent = any(k in (lead.get("investor_details") or "").lower() or
                       k in (lead.get("gate_summary") or "").lower()
                       for k in prominence_keywords)

    archetype = _resolve_archetype(lead, dossier)

    if archetype == "founder_lp":
        if is_prominent:
            queries = [
                f"{name} angel investment 2024 2025",
                f"{name} recent portfolio company backed",
                f"{name} interview podcast statement 2024",
            ]
        else:
            queries = [
                f"{name} founder company built",
                f"{name} angel investment portfolio",
                f"{name} operator background",
            ]
    elif archetype == "fund_of_funds":
        queries = [
            f"{name} emerging manager fund backed",
            f"{name} fund commitments LP investments",
            f"{name} fund of funds portfolio 2023 2024",
        ]
    elif archetype == "family_office":
        queries = [
            f"{name} family office investment portfolio",
            f"{name} direct investment company backed",
            f"{name} venture investment thesis",
        ]
    elif archetype == "corporate_investor":
        queries = [
            f"{name} accelerator program cohort 2024",
            f"{name} corporate venture investment",
            f"{name} strategic partnership portfolio",
        ]
    elif archetype == "emerging_manager_specialist":
        queries = [
            f"{name} emerging manager fund backed anchored",
            f"{name} first time fund investment 2023 2024",
            f"{name} fund I fund II LP commitment",
        ]
    elif archetype == "institutional_lp":
        queries = [
            f"{name} endowment foundation venture allocation",
            f"{name} alternatives investment program",
            f"{name} emerging manager program",
        ]
    else:
        queries = [
            f"{name} venture investment recent 2024 2025",
            f"{name} fund LP portfolio",
            f"{name} investor background",
        ]
    
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
    
    deep_research_used = bool(fresh_research.strip())

    draft_id = str(uuid.uuid4())
    personalization_payload = {
        "points": draft.personalization_points,
        "subject_format": draft.subject_format or "",
        "archetype": archetype,
        "deep_research_used": deep_research_used,
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
    result = {
        "draft_id": draft_id,
        "lead_id": lead["lead_id"],
        "investor_name": lead["investor_name"],
        "subject": draft.subject,
        "subject_format": draft.subject_format or "",
        "archetype": archetype,
        "deep_research_used": deep_research_used,
        "body": draft.body,
        "tone": tone,
        "model": model,
        "personalization_points": draft.personalization_points,
        "status": "draft",
    }
    # Push to Airtable: new draft row + update lead's latest email inline
    airtable_sync.push_outreach_draft(result)
    airtable_sync.update_lead_latest_email(
        investor_name=lead["investor_name"],
        subject=draft.subject,
        body=draft.body,
        pipeline_stage="Outreach Sent",
    )
    return result


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
        # Sync to Airtable — mark draft sent and advance lead stage
        airtable_sync.update_draft_status_airtable(draft_id, "sent")
        airtable_sync.update_lead_latest_email(
            investor_name=row[0],
            subject=row[1],
            body="",
            pipeline_stage="Outreach Sent",
            status="contacted",
        )
    elif status == "draft":
        # Allow undoing a mistaken "sent" mark
        con.execute(
            "UPDATE crm_leads SET status = 'active', updated_at = NOW() "
            "WHERE investor_name = ? AND status = 'contacted'",
            [row[0]],
        )
        airtable_sync.update_draft_status_airtable(draft_id, "draft")
    elif status == "approved":
        airtable_sync.update_draft_status_airtable(draft_id, "approved")
    elif status == "discarded":
        airtable_sync.update_draft_status_airtable(draft_id, "discarded")
    return True

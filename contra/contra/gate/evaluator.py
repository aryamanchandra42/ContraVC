"""
Deterministic gate evaluator — Python decides, LLM only explains.

evaluate(brief, analyst_facts, appetite) → GateAssessment

Decision rules:
  Hard blocks  → immediate NO regardless of signals.
  Signals      → need ≥2 for YES.
  One signal OR syndicate upgrade with unknown core gates → REVIEW.
  Otherwise    → NO.

Appetite (filled after the LLM explain pass) adds graded appetite signals toward
the ≥2 bar and applies soft negative/archetype downgrades — never overriding hard
blocks and never forcing NO purely from an absence of data.
"""

from __future__ import annotations

from typing import List, Optional

from contra.gate.models import AppetiteProfile, CoreGateCheck, GateAssessment, GateSignal
from contra.intelligence.brief import IntelligenceBrief

# Negative-inference tags that justify pushing a verdict downward.
# Strong negatives are positive evidence of MISFIT (not mere absence of data).
# Synced with the negative_flags list in gate_explain.yaml.
_STRONG_NEGATIVES = {
    "pe_only", "direct_only", "no_venture",
    "no_fund_lp_history", "angel_only", "nfx_angel_only",
}
# Confirmed misfit — safe to push REVIEW→NO in institutional mode.
_CONFIRMED_MISFIT_FLAGS = {
    "pe_only", "direct_only", "no_venture", "angel_only", "nfx_angel_only",
}
# Absence-of-evidence flag — institutional mode keeps REVIEW (does not force NO).
_ABSENCE_FLAGS = {"no_fund_lp_history"}

# Behavioral archetypes that count against fit when no positive appetite exists.
_UNFAVORABLE_ARCHETYPES = {"corporate_investor"}

_MODERATE_OR_STRONGER = {"strong", "moderate"}

# Phrases in exclusion_reason that confirm a direct-only / PE-only block
_DIRECT_PE_PHRASES = (
    "direct-only", "direct only", "pe-only", "pe only",
    "private equity only", "no fund", "does not invest in funds",
)


def _is_direct_pe_block(exclusion_reason: Optional[str]) -> bool:
    if not exclusion_reason:
        return False
    low = exclusion_reason.lower()
    return any(p in low for p in _DIRECT_PE_PHRASES)


# ---------------------------------------------------------------------------
# DB allocation evidence (recency-weighted) — feeds the appetite prompt
# ---------------------------------------------------------------------------

def build_allocation_evidence(brief: IntelligenceBrief) -> str:
    """
    Summarize the allocator's database allocation history as a compact, recency-
    labeled text block for the LLM appetite prompt.

    Recency buckets (using investments.investment_date):
        <= 24 months  → PRIMARY signal of current appetite
        2 - 5 years   → SUPPORTING signal
        > 7 years     → CONTEXT only (stale; weight lightly)

    Pure facts — no scoring. Returns "" when there is no investment history so the
    prompt is not padded with empty data.
    """
    summary = brief.investment_summary or {}
    deal_count = int(summary.get("deal_count") or 0)
    if deal_count == 0:
        return ""

    fund_deals = int(summary.get("fund_deal_count") or 0)
    spv_deals = int(summary.get("spv_deal_count") or 0)
    total_usd = float(summary.get("total_usd") or 0)
    recent = int(summary.get("recent_24mo") or 0)
    mid = int(summary.get("window_2_5yr") or 0)
    old = int(summary.get("older_7yr") or 0)
    last_date = summary.get("last_investment_date") or "unknown"
    last_fund_date = summary.get("last_fund_deal_date") or "none recorded"

    lines = [
        f"Total recorded deals: {deal_count} "
        f"({fund_deals} fund commitment(s), {spv_deals} SPV/direct).",
        f"Total committed (recorded): ${total_usd:,.0f}.",
        f"Most recent deal: {last_date}; most recent fund commitment: {last_fund_date}.",
        "Recency distribution (weight recent activity most heavily):",
        f"  - last 24 months (PRIMARY): {recent} deal(s)",
        f"  - 2-5 years ago (SUPPORTING): {mid} deal(s)",
        f"  - older than 7 years (CONTEXT only — likely stale): {old} deal(s)",
    ]
    if recent == 0 and deal_count > 0:
        lines.append(
            "  NOTE: no activity in the last 24 months — appetite may have cooled; verify recency."
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Core gate evaluation
# ---------------------------------------------------------------------------

def _eval_core_gates(brief: IntelligenceBrief, analyst_facts: List[str]) -> List[CoreGateCheck]:
    """
    Derive pass/fail/unknown for C1–C4 from the IntelligenceBrief.

    C1 (VC fund LP): pass if ICP evidence passes OR syndicate is_fund_lp OR
                     analyst explicitly states they commit to VC funds.
    C2–C4:           pass/fail from ICP evidence text; unknown if no ICP score exists.
    """
    gates = []
    sp = brief.syndicate_profile or {}
    analyst_lower = " ".join(analyst_facts).lower()

    # ---- C1: VC fund LP ------------------------------------------------
    c1_ev = (brief.core_gates.get("c1") or "").strip()
    c1_analyst = any(k in analyst_lower for k in ("vc fund", "venture fund", "fund lp", "lp in", "invest in fund"))
    c1_syndicate = bool(sp.get("is_fund_lp"))

    if c1_syndicate:
        gates.append(CoreGateCheck(gate="c1", status="pass",
            evidence=f"Syndicate: {sp.get('fund_deal_count', 0)} fund deal(s) — behaviorally confirmed VC fund LP"))
    elif c1_analyst:
        gates.append(CoreGateCheck(gate="c1", status="pass",
            evidence="Analyst-confirmed: stated LP commits to VC funds"))
    elif c1_ev:
        status = "pass" if "pass" in c1_ev.lower() else ("fail" if "fail" in c1_ev.lower() else "unknown")
        gates.append(CoreGateCheck(gate="c1", status=status, evidence=c1_ev[:200]))
    else:
        gates.append(CoreGateCheck(gate="c1", status="unknown",
            evidence="No ICP score and no syndicate investment data"))

    # ---- C2–C4: from ICP evidence text ----------------------------------
    for gate_id, label in (("c2", "emerging manager"), ("c3", "AI/tech"), ("c4", "geography")):
        ev = (brief.core_gates.get(gate_id) or "").strip()
        analyst_hint = any(k in analyst_lower for k in (
            ("emerging manager", "fund i", "first-time") if gate_id == "c2"
            else ("ai", "tech", "deep tech") if gate_id == "c3"
            else ("asia", "north america", "middle east", "global")
        ))
        if analyst_hint and not ev:
            gates.append(CoreGateCheck(gate=gate_id, status="pass",  # type: ignore[arg-type]
                evidence=f"Analyst context suggests {label} pass"))
        elif ev:
            status = "pass" if "pass" in ev.lower() else ("fail" if "fail" in ev.lower() else "unknown")
            gates.append(CoreGateCheck(gate=gate_id, status=status, evidence=ev[:200]))  # type: ignore[arg-type]
        else:
            gates.append(CoreGateCheck(gate=gate_id, status="unknown",  # type: ignore[arg-type]
                evidence="No ICP score in database — gate web research may still assess this gate"))

    return gates


# ---------------------------------------------------------------------------
# Signal checklist
# ---------------------------------------------------------------------------

def _eval_signals(
    brief: IntelligenceBrief,
    analyst_facts: List[str],
    appetite: Optional[AppetiteProfile],
) -> List[GateSignal]:
    signals: List[GateSignal] = []
    sp = brief.syndicate_profile or {}
    gc = brief.graph_connectivity or {}

    # Treat as no record when match is untrusted (likely wrong person)
    no_db_record = not brief.allocator_id or brief.match_untrusted
    match_info = (
        f"Matched '{brief.matched_name}' (confidence {brief.match_confidence:.0%}, {brief.match_method})"
        if brief.matched_name else
        f"Not found in database (match confidence {brief.match_confidence:.0%}) — try full legal name or org name"
    )

    # Signal 1: ICP qualified (tier 1 or 2 + core_pass)
    icp_ok = (
        brief.icp_tier in ("tier_1", "tier_2")
        and brief.core_pass is True
    )
    if brief.match_untrusted and brief.allocator_id:
        icp_detail = (
            f"Match to '{brief.matched_name}' is unreliable (surname mismatch) — "
            f"ICP/syndicate data suppressed to avoid using wrong person's profile. {match_info}."
        )
    elif no_db_record:
        icp_detail = f"{match_info}. No ICP score possible without a database record."
    elif brief.icp_tier:
        icp_detail = f"ICP {brief.icp_tier}, fit={brief.icp_fit_score:.2f}, core_pass={brief.core_pass}. {match_info}."
    else:
        icp_detail = (
            f"Not in prospect sheets — no ICP score in database. {match_info}. "
            "Gate web research and LLM assessment still apply for screening."
        )
    signals.append(GateSignal(
        id="icp_qualified",
        label="ICP Qualified (Tier 1/2 + core pass)",
        met=icp_ok,
        source="backend",
        detail=icp_detail,
    ))

    # Signal 2: Syndicate fund LP behavior
    is_fund_lp = bool(sp.get("is_fund_lp"))
    if no_db_record:
        syndicate_detail = "No database record — cannot look up syndicate history."
    elif is_fund_lp:
        syndicate_detail = (
            f"{sp.get('fund_deal_count', 0)} fund deal(s), "
            f"ratio={sp.get('fund_lp_ratio', 0):.0%}, "
            f"total=${sp.get('total_committed_usd', 0):,.0f}"
        )
    else:
        syndicate_detail = "Not in syndicate roster, or 0 fund deals recorded. Check AngelList data."
    signals.append(GateSignal(
        id="syndicate_fund_lp",
        label="Syndicate Fund-LP Behavior",
        met=is_fund_lp,
        source="syndicate",
        detail=syndicate_detail,
    ))

    # Signal 3: Syndicate upgrade candidate (fund deal ≥1 + committed ≥$5k)
    is_upgrade = bool(sp.get("is_upgrade_candidate"))
    signals.append(GateSignal(
        id="syndicate_upgrade",
        label="Syndicate Upgrade Candidate",
        met=is_upgrade,
        source="syndicate",
        detail=(
            f"Fund deals ≥1 and committed ≥$5k (${sp.get('total_committed_usd', 0):,.0f} total)"
            if is_upgrade else "Does not meet upgrade criteria (fund deal + $5k minimum)"
        ),
    ))

    # Signal 4: Warm intro path
    warm_count = gc.get("warm_path_count", 0)
    if no_db_record:
        warm_detail = "No database record — warm path graph cannot be searched."
    elif warm_count > 0:
        warm_detail = f"{warm_count} warm path(s) via mutual connections in network graph."
    else:
        warm_detail = "No mutual connections found. Add LinkedIn contacts via Admin → Enrich to expand graph."
    signals.append(GateSignal(
        id="warm_path",
        label="Warm Intro Path",
        met=warm_count > 0,
        source="backend",
        detail=warm_detail,
    ))

    # Signal 5: Contra benchmark ranking
    if no_db_record:
        bench_detail = "No database record — not checked against benchmark list."
    elif brief.benchmark_rank:
        bench_detail = f"Ranked #{brief.benchmark_rank} on Contra Top-200 benchmark list."
    else:
        bench_detail = "Not on Contra Top-200 benchmark list."
    signals.append(GateSignal(
        id="benchmark_rank",
        label="Contra Top-200 Ranking",
        met=brief.benchmark_rank is not None,
        source="backend",
        detail=bench_detail,
    ))

    # Signals 6a-6c: graded APPETITE signals inferred from allocation behavior.
    # These replace the old single boolean web_em_ai_vc signal with per-dimension
    # evidence so partial appetite (e.g. clear EM appetite but implicit AI) still counts.
    em = appetite.em_appetite if appetite else "unknown"
    fund_i = appetite.fund_i_appetite if appetite else "unknown"
    ai_tech = appetite.ai_tech_appetite if appetite else "unknown"
    venture = appetite.venture_appetite if appetite else "unknown"
    cited = "; ".join((appetite.allocation_evidence or [])[:2]) if appetite else ""

    def _appetite_detail(met: bool, level: str, met_desc: str, miss_desc: str) -> str:
        if appetite is None:
            return "Appetite not yet inferred (pre-LLM pass)"
        if met:
            base = f"Inferred {level} {met_desc}"
            return f"{base} — e.g. {cited}" if cited else base
        return miss_desc

    em_met = em in _MODERATE_OR_STRONGER or fund_i in _MODERATE_OR_STRONGER
    signals.append(GateSignal(
        id="appetite_emerging_manager",
        label="Inferred Emerging-Manager / Fund-I Appetite",
        met=em_met,
        source="web",
        detail=_appetite_detail(
            em_met,
            em if em in _MODERATE_OR_STRONGER else fund_i,
            "appetite for emerging / first-time managers from allocation behavior",
            "No inferred emerging-manager or Fund-I appetite from allocation behavior",
        ),
    ))

    ai_met = ai_tech in _MODERATE_OR_STRONGER
    signals.append(GateSignal(
        id="appetite_ai_tech",
        label="Inferred AI / Tech Appetite",
        met=ai_met,
        source="web",
        detail=_appetite_detail(
            ai_met, ai_tech,
            "AI/tech appetite from backed managers or portfolio",
            "No inferred AI/tech appetite from backed managers or portfolio",
        ),
    ))

    venture_met = venture in _MODERATE_OR_STRONGER
    signals.append(GateSignal(
        id="appetite_venture_fit",
        label="Inferred Venture Fund-LP Appetite",
        met=venture_met,
        source="web",
        detail=_appetite_detail(
            venture_met, venture,
            "appetite for committing to VC funds as an LP",
            "No inferred appetite for committing to VC funds as an LP",
        ),
    ))

    # Signal 6d: Precedent LP pattern match — similar confirmed LPs in DB.
    # Fires only when ≥MIN_SIGNAL_COUNT anchors score above the MIN_SIGNAL_SCORE threshold,
    # ensuring the signal reflects genuine archetype similarity, not just LP volume.
    from contra.intelligence.lp_similarity import MIN_SIGNAL_COUNT, MIN_SIGNAL_SCORE

    similar_lps = brief.similar_confirmed_lps or []
    qualifying = [lp for lp in similar_lps if lp.get("similarity_score", 0) >= MIN_SIGNAL_SCORE]
    similar_met = len(qualifying) >= MIN_SIGNAL_COUNT

    if not similar_lps:
        similar_detail = "No comparable LP profiles found in database."
    elif not qualifying:
        names = ", ".join(lp.get("name", "?") for lp in similar_lps[:3])
        top_score = max((lp.get("similarity_score", 0) for lp in similar_lps), default=0)
        similar_detail = (
            f"{len(similar_lps)} candidate(s) found but none score ≥{MIN_SIGNAL_SCORE} "
            f"(best: {top_score}). Examples: {names}. "
            "Archetype precedent inconclusive — insufficient similarity for positive signal."
        )
    else:
        names = ", ".join(
            f"{lp['name']} ({lp.get('similarity_score', '?')}%)"
            for lp in sorted(qualifying, key=lambda x: -x.get("similarity_score", 0))[:3]
        )
        dims_example = ", ".join(qualifying[0].get("match_dimensions", [])[:3])
        similar_detail = (
            f"{len(qualifying)} high-similarity anchor(s) (≥{MIN_SIGNAL_SCORE} score) "
            f"with confirmed fund LP history. Top: {names}. "
            f"Matched on: {dims_example or 'multiple dimensions'}. "
            "Use as calibration baseline — does this LP match their archetype?"
        )
    signals.append(GateSignal(
        id="similar_lp_precedent",
        label="Similar Confirmed LP Precedent",
        met=similar_met,
        source="backend",
        detail=similar_detail,
    ))

    # Signal 7: Analyst-provided facts (capped at 2 signal points).
    # met=True ONLY when the fact explicitly confirms fund LP behavior.
    # Contextual metadata (NFX URL, firm name, location, check size) does NOT count —
    # those are passed via nfx_context_string() and do not flow through analyst_facts.
    _LP_CONFIRMING_KEYWORDS = (
        "lp in", "limited partner", "fund lp", "backed fund", "committed to",
        "invest in fund", "venture fund", "anchored", "first close", "fund i",
        "fund ii", "vc fund", "emerging manager", "fund commit",
    )

    def _fact_confirms_lp(fact: str) -> bool:
        low = fact.lower()
        return any(kw in low for kw in _LP_CONFIRMING_KEYWORDS)

    if not analyst_facts:
        signals.append(GateSignal(
            id="analyst_fact",
            label="Analyst Context",
            met=False,
            source="analyst",
            detail="No analyst facts provided — add context in chat to re-screen",
        ))
    else:
        for i, fact in enumerate(analyst_facts[:2]):
            met = _fact_confirms_lp(fact)
            signals.append(GateSignal(
                id=f"analyst_fact_{i + 1}",
                label=f"Analyst Context #{i + 1}",
                met=met,
                source="analyst",
                detail=fact[:200],
            ))

    return signals


# ---------------------------------------------------------------------------
# Main evaluation entry point
# ---------------------------------------------------------------------------

def evaluate(
    brief: IntelligenceBrief,
    analyst_facts: Optional[List[str]] = None,
    appetite: Optional[AppetiteProfile] = None,
    screening_mode: str = "institutional",
) -> GateAssessment:
    """
    Produce a GateAssessment from the IntelligenceBrief without calling the LLM.

    analyst_facts: plain-English facts the analyst provided via chat (e.g. "They backed Neon Fund I").
    appetite: inferred appetite from the LLM explain pass (None on the pre-LLM pass).
              Adds graded appetite signals and applies soft negative/archetype downgrades.
    screening_mode: "nfx_individual" or "institutional" — governs verdict strictness.
    """
    analyst_facts = analyst_facts or []
    hard_blocks: List[str] = []

    # ---- Hard blocks -------------------------------------------------------
    if brief.in_crm:
        crm_name = (brief.crm_row or {}).get("investor_name", brief.input_name)
        hard_blocks.append(f"Already in FundingStack CRM as '{crm_name}'")

    if brief.excluded and brief.exclusion_reason:
        hard_blocks.append(f"ICP excluded: {brief.exclusion_reason}")
    elif brief.excluded:
        hard_blocks.append("ICP excluded (no specific reason recorded)")

    if not brief.excluded and _is_direct_pe_block(brief.exclusion_reason):
        hard_blocks.append(f"Direct/PE-only investor: {brief.exclusion_reason}")

    # ---- Core gates --------------------------------------------------------
    core_gates = _eval_core_gates(brief, analyst_facts)

    # ---- Signals -----------------------------------------------------------
    signals = _eval_signals(brief, analyst_facts, appetite)
    signals_met = sum(1 for s in signals if s.met)

    # ---- Recommendation ----------------------------------------------------
    if hard_blocks:
        recommendation = "no"
    elif signals_met >= 2:
        recommendation = "yes"
    else:
        sp = brief.syndicate_profile or {}
        unknown_core_gates = sum(1 for g in core_gates if g.status == "unknown")
        if signals_met == 1 or (bool(sp.get("is_upgrade_candidate")) and unknown_core_gates >= 2):
            recommendation = "review"
        else:
            recommendation = "no"

    # ---- Soft appetite-based adjustments (never override hard blocks) -------
    if not hard_blocks:
        recommendation = apply_appetite_adjustments(recommendation, appetite, screening_mode)

    return GateAssessment(
        recommendation=recommendation,
        hard_blocks=hard_blocks,
        core_gates=core_gates,
        signals=signals,
        signals_met=signals_met,
        signals_required=2,
        appetite=appetite,
    )


# ---------------------------------------------------------------------------
# Soft appetite-based verdict adjustments
# ---------------------------------------------------------------------------

def apply_appetite_adjustments(
    recommendation: str,
    appetite: Optional[AppetiteProfile],
    screening_mode: str = "institutional",
) -> str:
    """
    Nudge a non-blocked recommendation using inferred appetite.

    Negative inference (positive evidence of misfit) pushes the verdict DOWN:
      - strong negatives: yes→review, review→no
      - nfx_individual + no_fund_lp_history: forces → no regardless of current level
      - soft negatives only: yes→review

    Archetype tie-breaker: a clearly unfavorable archetype (e.g. corporate_investor)
    with zero positive appetite nudges yes→review.

    This NEVER fires for already-"no" verdicts, NEVER touches hard blocks, and NEVER
    forces "no" purely from an absence of data — only from explicit negative evidence.
    """
    if appetite is None or recommendation == "no":
        return recommendation

    rec = recommendation
    flags = {f.strip().lower() for f in (appetite.negative_flags or [])}

    # In nfx_individual mode, any strong negative is conclusive — force NO directly.
    if screening_mode == "nfx_individual" and (flags & _STRONG_NEGATIVES):
        return "no"

    if flags & _CONFIRMED_MISFIT_FLAGS:
        rec = "review" if rec == "yes" else "no"
    elif flags & _ABSENCE_FLAGS:
        if rec == "yes":
            rec = "review"
        # institutional: keep REVIEW when only absence-of-evidence (no confirmed misfit)
    elif flags:
        if rec == "yes":
            rec = "review"

    if (
        rec == "yes"
        and appetite.archetype in _UNFAVORABLE_ARCHETYPES
        and appetite.appetite_signals_met() == 0
    ):
        rec = "review"

    return rec

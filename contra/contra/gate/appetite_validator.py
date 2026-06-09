"""
Post-LLM appetite validator.

Applies deterministic guardrails to the LLM's GateExplanation output BEFORE the
appetite profile is built and the post-LLM evaluate() pass runs.

Corrections made here (in order):
  1. GP title detected + no external LP commits → add no_fund_lp_history flag,
     cap em/fund_i appetites to 'unknown', clean employer-portfolio allocation_evidence.
  2. em/fund_i moderate/strong but allocation_evidence has no external LP commits → downgrade.
  3a. nfx_individual mode + any strong negative → force llm_recommendation='no'.
  3b. nfx_individual mode + zero evidence (REVIEW with no positive signals) → force 'no'.
  4. NO verdicts: strip hedge language ('further research needed', 'worth monitoring') from summary.
  5. REVIEW verdicts: ensure summary ends with a specific flip condition.

The validator never upgrades verdicts — it only caps/corrects downward.
"""

from __future__ import annotations

import re
from typing import List, Optional

from contra.gate.models import GateExplanation

# Hedge phrases that must not appear in NO verdict summaries
_HEDGE_PATTERNS = re.compile(
    r"further\s+research\s+(is\s+)?needed|further\s+review\s+(is\s+)?needed|"
    r"worth\s+monitoring|needs?\s+more\s+(research|investigation|review)|"
    r"additional\s+research\s+(is\s+)?needed|recommend\s+further\s+",
    re.IGNORECASE,
)

# Phrases that indicate a flip condition is already stated (for REVIEW summaries)
_FLIP_CONDITION_SIGNALS = (
    "flip", "confirm", "would change", "if ", "once ", "evidence of ", "proof of ",
    "lp commit", "fund commit", "would upgrade",
)

# Regex: detects GP/Principal title held *at* a fund/firm
_GP_TITLE_RE = re.compile(
    r"\b(principal|general\s+partner|managing\s+partner|co[\s-]?founder)\s+at\b",
    re.IGNORECASE,
)

# Words that signal an actual LP commitment (must appear in allocation_evidence text)
_LP_COMMIT_SIGNALS = (
    " lp in ", " lp at ", "limited partner in", "limited partner at",
    "committed to", "anchor lp", "first close", "fund lp",
    "backed fund", "invest in fund",
)

_STRONG_OR_MODERATE = {"strong", "moderate"}


def _has_external_lp_commits(evidence_list: List[str]) -> bool:
    """Return True if any allocation_evidence entry contains explicit LP commitment language."""
    for entry in evidence_list:
        low = f" {entry.lower()} "
        if any(sig in low for sig in _LP_COMMIT_SIGNALS):
            return True
    return False


def _extract_employer_firm(nfx_context: str) -> str:
    """Extract firm name from an NFX context string (produced by to_nfx_context_string())."""
    for line in (nfx_context or "").splitlines():
        stripped = line.strip()
        if stripped.startswith("Firm:"):
            return stripped.split(":", 1)[1].strip()
    return ""


def _entry_is_employer_portfolio(entry: str, employer_firm: str) -> bool:
    """
    Return True when an allocation_evidence entry refers to the employer fund's
    portfolio/investments rather than a personal LP commitment.

    Detects patterns like "Hustle Fund's investments in …" or "Hustle Fund portfolio".
    """
    if not employer_firm:
        return False
    entry_low = entry.lower()
    firm_low = employer_firm.lower()
    if firm_low not in entry_low:
        return False
    # If the entry also contains LP-commit language, it might still be real evidence
    low_padded = f" {entry_low} "
    if any(sig in low_padded for sig in _LP_COMMIT_SIGNALS):
        return False
    # Likely just the employer's portfolio description
    return True


def validate_and_patch(
    explanation: GateExplanation,
    nfx_context: Optional[str],
    web_context: str,
    screening_mode: str = "institutional",
) -> GateExplanation:
    """
    Return a (possibly patched) GateExplanation with invalid LLM inferences corrected.

    This is a pure function — the original explanation is never mutated.
    Returns the original unchanged if no corrections are needed.
    """
    updates: dict = {}
    conflicts = list(explanation.conflicts)
    negative_flags = [f.strip().lower() for f in (explanation.negative_flags or [])]

    employer_firm = _extract_employer_firm(nfx_context or "")
    combined_context = " ".join([
        nfx_context or "",
        explanation.archetype_evidence or "",
        web_context[:1000],
    ])
    has_gp_title = bool(_GP_TITLE_RE.search(combined_context))
    has_external_lp = _has_external_lp_commits(explanation.allocation_evidence or [])

    # --- Rule 1: GP title + no external LP commits --------------------------------
    if has_gp_title and not has_external_lp:
        if "no_fund_lp_history" not in negative_flags:
            negative_flags.append("no_fund_lp_history")
            conflicts.append(
                "GP/Principal title detected with no external LP fund commitments — "
                "employer fund portfolio is not this person's LP allocation evidence"
            )

        if (explanation.em_appetite in _STRONG_OR_MODERATE
                and "em_appetite" not in updates):
            updates["em_appetite"] = "unknown"
            updates["em_appetite_evidence"] = (
                "Capped: em_appetite requires a documented LP commitment to an external "
                "emerging-manager fund; GP role alone does not confirm EM appetite"
            )

        if (explanation.fund_i_appetite in _STRONG_OR_MODERATE
                and "fund_i_appetite" not in updates):
            updates["fund_i_appetite"] = "unknown"
            updates["fund_i_appetite_evidence"] = (
                "Capped: fund_i_appetite requires a documented LP commitment; GP role does not confirm"
            )

        # Venture appetite: running a fund ≠ LP-ing into someone else's fund.
        # Cap only when archetype_evidence references the employer firm exclusively.
        if (explanation.venture_appetite in _STRONG_OR_MODERATE and employer_firm
                and employer_firm.lower() in (explanation.archetype_evidence or "").lower()
                and "venture_appetite" not in updates):
            updates["venture_appetite"] = "unknown"
            updates["venture_appetite_evidence"] = (
                "Capped: running a fund ≠ committing LP capital to external VC funds"
            )

        # C2 gate: cap if solely derived from GP employer
        if explanation.c2_status == "pass" and employer_firm:
            c2_ev_low = (explanation.c2_evidence or "").lower()
            if employer_firm.lower() in c2_ev_low and not any(
                sig in f" {c2_ev_low} " for sig in _LP_COMMIT_SIGNALS
            ):
                updates["c2_status"] = "unknown"
                updates["c2_evidence"] = (
                    f"Capped: C2 requires LP commitment to an emerging-manager fund; "
                    f"GP at {employer_firm} alone does not confirm C2"
                )

    # --- Rule 2: EM/fund-I strong/moderate but no named external LP evidence ------
    current_em = updates.get("em_appetite", explanation.em_appetite)
    current_fund_i = updates.get("fund_i_appetite", explanation.fund_i_appetite)

    if current_em in _STRONG_OR_MODERATE and not has_external_lp:
        updates["em_appetite"] = "unknown"
        updates["em_appetite_evidence"] = (
            "Downgraded: no external fund LP commitment cited in allocation_evidence; "
            "em_appetite requires at minimum one named LP commitment"
        )
        if "no external lp" not in " ".join(conflicts).lower():
            conflicts.append(
                "em_appetite set to moderate/strong but allocation_evidence has no LP commit language"
            )

    if current_fund_i in _STRONG_OR_MODERATE and not has_external_lp:
        updates["fund_i_appetite"] = "unknown"
        updates["fund_i_appetite_evidence"] = (
            "Downgraded: no external Fund I LP commitment cited in allocation_evidence"
        )

    # --- Clean employer-portfolio entries from allocation_evidence ----------------
    if employer_firm and explanation.allocation_evidence:
        cleaned = [
            e for e in explanation.allocation_evidence
            if not _entry_is_employer_portfolio(e, employer_firm)
        ]
        if len(cleaned) != len(explanation.allocation_evidence):
            updates["allocation_evidence"] = cleaned
            conflicts.append(
                f"Removed employer-fund portfolio references from allocation_evidence — "
                f"{employer_firm}'s startup portfolio is not this person's LP activity"
            )

    # --- Rule 3a: nfx_individual + any strong negative → force NO ----------------
    _STRONG = {"no_fund_lp_history", "pe_only", "direct_only", "no_venture", "angel_only", "nfx_angel_only"}
    if any(f in _STRONG for f in negative_flags) and screening_mode == "nfx_individual":
        if explanation.llm_recommendation != "no":
            updates["llm_recommendation"] = "no"
            if not updates.get("primary_blocker") and not explanation.primary_blocker:
                employer_note = f" (GP at {employer_firm})" if employer_firm else ""
                updates["primary_blocker"] = (
                    f"No external VC fund LP commitments found{employer_note}"
                )
            conflicts.append(
                "Verdict overridden to 'no' — nfx_individual mode: no external fund LP history found"
            )

    # --- Rule 3b: nfx_individual + zero-evidence REVIEW → force NO ---------------
    # When the LLM says "review" purely because it found nothing (no positive appetite
    # signal, no LP commitments, all appetites unknown), that is not a genuinely
    # ambiguous case — it's an absence-of-evidence case that should default to NO
    # in the strict NFX individual screening mode.
    if (
        screening_mode == "nfx_individual"
        and explanation.llm_recommendation == "review"
        and not updates.get("llm_recommendation")
    ):
        all_unknown = all(
            getattr(explanation, f) in ("unknown", None)
            for f in ("em_appetite", "fund_i_appetite", "venture_appetite")
        )
        no_alloc_evidence = not (explanation.allocation_evidence or [])
        # GateExplanation has no positive_flags field; use allocation_evidence and
        # lp_commitments_found as proxy for any positive LP signal.
        no_positive_flags = not (explanation.lp_commitments_found or [])
        c1_unconfirmed = explanation.c1_status in ("unknown", "fail", None)
        if all_unknown and no_alloc_evidence and no_positive_flags and c1_unconfirmed:
            updates["llm_recommendation"] = "no"
            updates["primary_blocker"] = (
                "No fund LP evidence found — zero-evidence default to NO in NFX batch mode"
            )
            if "no_fund_lp_history" not in negative_flags:
                negative_flags.append("no_fund_lp_history")
            conflicts.append(
                "Verdict changed REVIEW→NO: nfx_individual mode, zero positive evidence, "
                "all appetite dimensions unknown, no LP commitments found"
            )

    # --- Apply primary_blocker if NO and not already set -------------------------
    final_rec = updates.get("llm_recommendation", explanation.llm_recommendation)
    if final_rec == "no" and not explanation.primary_blocker and "primary_blocker" not in updates:
        if "no_fund_lp_history" in negative_flags:
            employer_note = f" (GP at {employer_firm})" if employer_firm else ""
            updates["primary_blocker"] = f"No external VC fund LP commitments found{employer_note}"
        elif "angel_only" in negative_flags or "nfx_angel_only" in negative_flags:
            updates["primary_blocker"] = "Direct angel investor — no VC fund LP commitments found"
        elif "pe_only" in negative_flags:
            updates["primary_blocker"] = "PE-only investor — no venture fund LP history"
        elif "direct_only" in negative_flags:
            updates["primary_blocker"] = "Direct-only investor — does not commit to VC funds"

    # --- Rule 4: NO verdict — strip hedge language from summary -----------------
    # The LLM sometimes writes "further research needed" even for clear NO verdicts.
    # Deterministically remove it so the output is decisive and actionable.
    final_rec_for_summary = updates.get("llm_recommendation", explanation.llm_recommendation)
    current_summary = updates.get("summary", explanation.summary) or ""

    if final_rec_for_summary == "no" and _HEDGE_PATTERNS.search(current_summary):
        # Replace the hedge sentence(s); keep the rest of the summary.
        patched = _HEDGE_PATTERNS.sub("", current_summary).strip()
        # Clean up orphaned trailing connectors (".", "—", "-")
        patched = re.sub(r"[.—\-,;]+\s*$", ".", patched).strip()
        if len(patched) < 20:
            # If too much was removed, build a terse replacement from the blocker
            blocker = updates.get("primary_blocker", explanation.primary_blocker or "")
            flags_desc = ", ".join(negative_flags[:2]) if negative_flags else "no fund LP evidence"
            patched = (
                f"No — {blocker or flags_desc}. "
                f"Verdict is final; no additional research will change this unless "
                f"documented LP fund commitments emerge."
            )
        updates["summary"] = patched
        conflicts.append("Removed hedge language from NO verdict summary")

    # --- Rule 5: REVIEW verdict — ensure a specific flip condition is stated ----
    # "Further research is needed" is not a flip condition. Force the LLM to state
    # the ONE thing that would change this to YES.
    elif final_rec_for_summary == "review":
        review_summary = updates.get("summary", explanation.summary) or ""
        has_flip = any(sig in review_summary.lower() for sig in _FLIP_CONDITION_SIGNALS)
        has_hedge = _HEDGE_PATTERNS.search(review_summary)
        if not has_flip or has_hedge:
            # Replace/append with a flip condition drawn from what we know
            blocker = updates.get("primary_blocker", explanation.primary_blocker or "")
            em_ap = updates.get("em_appetite", explanation.em_appetite)
            c1 = explanation.c1_status
            # Determine the most specific flip condition
            if c1 == "unknown":
                flip = "Flip to YES if confirmed LP commitment to at least one VC fund is found."
            elif em_ap in ("unknown", "weak", "none"):
                flip = "Flip to YES if evidence of emerging-manager fund LP activity is confirmed."
            elif blocker:
                flip = f"Flip to YES if: {blocker} is resolved with documented LP evidence."
            else:
                flip = "Flip to YES if confirmed VC fund LP commitment (C1 pass) is documented."

            # If hedge language exists, replace it; otherwise append the flip condition
            if has_hedge:
                patched_review = _HEDGE_PATTERNS.sub("", review_summary).strip()
                patched_review = re.sub(r"[.—\-,;]+\s*$", ".", patched_review).strip()
                updates["summary"] = f"{patched_review} {flip}"
                conflicts.append("Replaced hedge language with specific flip condition in REVIEW summary")
            else:
                # Append if the last sentence doesn't already state a flip condition
                updates["summary"] = f"{review_summary.rstrip('.')}. {flip}"

    # --- Return early if nothing changed -----------------------------------------
    if not updates and negative_flags == [f.strip().lower() for f in (explanation.negative_flags or [])]:
        return explanation

    updates["negative_flags"] = negative_flags
    updates["conflicts"] = conflicts

    return explanation.model_copy(update=updates)

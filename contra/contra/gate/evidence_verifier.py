"""
Deterministic evidence verifier — the false-positive killer.

Runs AFTER the LLM explain pass and the appetite validator. Checks that every
LP commitment the LLM claims to have found is actually quotable from the
evidence it was given (web context + analyst/PitchBook facts). Models
sometimes assert "LP in X Fund" from priors or from misread GP-portfolio
snippets; those claims drive false-positive outreach.

Rules (downgrade-only — never upgrades a verdict):
  1. Every entry in lp_commitments_found must have its fund name present in
     the evidence corpus. Unquotable entries are removed (and mirrored out of
     allocation_evidence).
  2. A "yes" verdict with ZERO verified LP commitments and no LP-confirming
     analyst fact is downgraded to "review" (institutional) or "no"
     (nfx_individual), with confidence capped at "medium".
"""

from __future__ import annotations

import re
from typing import List, Tuple

from contra.gate.models import GateExplanation

# Generic tokens that don't identify a specific fund — never sufficient on
# their own to verify a claim.
_GENERIC_TOKENS = {
    "fund", "funds", "capital", "ventures", "venture", "partners", "partner",
    "the", "and", "of", "in", "lp", "ii", "iii", "iv", "v", "vi", "i",
    "group", "global", "management", "holdings", "company", "co", "llc",
    "anchor", "first", "close", "seed", "growth", "opportunity",
}

# Strip the claim down to the fund name: "LP in Hustle Fund (2022) — Crunchbase"
_CLAIM_PREFIX_RE = re.compile(
    r"^(anchor\s+lp\s+in|lp\s+in|lp\s+at|limited\s+partner\s+(?:in|at)|"
    r"committed\s+to|backed|invested\s+in)\s+",
    re.IGNORECASE,
)
_CLAIM_SUFFIX_RE = re.compile(r"\s*[\(—–\-].*$")

_LP_CONFIRMING_FACT = re.compile(
    r"lp in|limited partner|fund lp|confirmed lp|fund commitment", re.IGNORECASE
)


def _extract_fund_name(claim: str) -> str:
    name = _CLAIM_PREFIX_RE.sub("", claim.strip())
    name = _CLAIM_SUFFIX_RE.sub("", name)
    return name.strip()


def _distinctive_tokens(fund_name: str) -> List[str]:
    tokens = re.findall(r"[a-z0-9]{3,}", fund_name.lower())
    return [t for t in tokens if t not in _GENERIC_TOKENS]


def _claim_supported(claim: str, corpus: str) -> bool:
    """A claim is supported when its fund name (or its distinctive tokens) appears in the corpus."""
    fund_name = _extract_fund_name(claim)
    if not fund_name:
        return False
    low = fund_name.lower()
    if low in corpus:
        return True
    distinctive = _distinctive_tokens(fund_name)
    if not distinctive:
        # Fund name is all-generic ("Growth Fund II") — require the full phrase.
        return False
    # All distinctive tokens must appear somewhere in the evidence.
    return all(t in corpus for t in distinctive)


def verify_evidence(
    explanation: GateExplanation,
    web_context: str,
    analyst_facts: List[str],
    screening_mode: str = "institutional",
) -> Tuple[GateExplanation, List[str]]:
    """
    Verify LLM-claimed LP commitments against the evidence corpus.

    Returns (possibly-downgraded explanation, verification notes for the UI).
    """
    notes: List[str] = []
    corpus = (web_context + "\n" + "\n".join(analyst_facts or [])).lower()

    verified: List[str] = []
    dropped: List[str] = []
    for claim in explanation.lp_commitments_found or []:
        if _claim_supported(claim, corpus):
            verified.append(claim)
        else:
            dropped.append(claim)

    if dropped:
        notes.append(
            f"Removed {len(dropped)} unverifiable LP commitment claim(s) not quotable "
            f"from evidence: {'; '.join(_extract_fund_name(d) or d for d in dropped[:4])}"
        )

    update: dict = {}
    if dropped:
        update["lp_commitments_found"] = verified
        # Mirror the removal in allocation_evidence
        dropped_names = {_extract_fund_name(d).lower() for d in dropped}
        kept_alloc = [
            e for e in (explanation.allocation_evidence or [])
            if _extract_fund_name(e).lower() not in dropped_names
        ]
        if len(kept_alloc) != len(explanation.allocation_evidence or []):
            update["allocation_evidence"] = kept_alloc

    analyst_confirms_lp = any(
        _LP_CONFIRMING_FACT.search(f or "") for f in (analyst_facts or [])
    )

    if (
        explanation.llm_recommendation == "yes"
        and not verified
        and not analyst_confirms_lp
    ):
        downgraded_to = "no" if screening_mode == "nfx_individual" else "review"
        update["llm_recommendation"] = downgraded_to
        if explanation.confidence == "high":
            update["confidence"] = "medium"
        if downgraded_to == "review":
            update["summary"] = (
                "YES verdict downgraded by evidence verifier — no LP fund commitment in the "
                "claimed evidence could be verified against sources. "
                "Flip to YES if: at least one named external VC fund LP commitment is documented."
            )
        else:
            update["summary"] = (
                "YES verdict downgraded to NO by evidence verifier — no verifiable LP fund "
                "commitment found and NFX-batch posture requires concrete LP evidence. "
                "Re-screen with an analyst fact if an LP commitment is known."
            )
        notes.append(
            f"Verdict downgraded yes → {downgraded_to}: zero verifiable LP commitments in evidence."
        )

    if update:
        explanation = explanation.model_copy(update=update)
    return explanation, notes

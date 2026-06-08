"""Pydantic models for batch GATE processing (CSV upload)."""

from __future__ import annotations

from typing import List, Literal, Optional

from pydantic import BaseModel, Field


class NfxInvestorRecord(BaseModel):
    """One investor row parsed from an NFX Signal xlsx export."""

    investor_name: str
    firm_name: Optional[str] = None
    nfx_url: Optional[str] = None
    sweet_spot: Optional[str] = None
    check_min: Optional[str] = None
    check_max: Optional[str] = None
    locations: Optional[str] = None
    intro_source: Optional[str] = None
    intro_strength: Optional[str] = None

    def to_analyst_facts(self) -> List[str]:
        """
        Return ONLY facts that confirm LP-relevant behavior.

        NFX Signal metadata (URL, firm, check size, location) is contextual data
        about the person's DIRECT investment activity — NOT evidence they write
        LP checks into VC funds. Passing it as analyst_facts caused every NFX
        investor to auto-satisfy the 2-signal threshold and receive YES verdicts.

        Those fields are passed separately as nfx_context_string() so the LLM
        can use them without them inflating the signal count.
        """
        facts: List[str] = []
        # Only intro source/strength can hint at a warm LP relationship — include those.
        if self.intro_source and self.intro_source.upper() not in ("N/A", "NA", ""):
            facts.append(f"Intro source via: {self.intro_source}")
        if self.intro_strength and str(self.intro_strength).strip():
            facts.append(f"Intro strength: {self.intro_strength}")
        return facts

    def to_nfx_context_string(self) -> str:
        """
        Return NFX Signal metadata as a plain context string for the prompt.

        This is passed as nfx_context to run_gate() — it informs the LLM about
        the investor's background but does NOT count as a gate signal.

        IMPORTANT NOTE baked in: NFX Signal check sizes reflect DIRECT STARTUP
        investment sweet spots, not LP commitment sizes into VC funds.
        """
        lines = ["Source: NFX Signal (angel/early-stage investor network)"]
        if self.firm_name:
            lines.append(f"Firm: {self.firm_name}")
        if self.nfx_url:
            lines.append(f"NFX profile: {self.nfx_url}")
        if self.sweet_spot or (self.check_min and self.check_max):
            note = "(direct startup angel check — NOT an LP commitment size)"
            if self.sweet_spot and self.check_min and self.check_max:
                lines.append(
                    f"Angel sweet-spot: {self.sweet_spot} "
                    f"(range {self.check_min}–{self.check_max}) {note}"
                )
            elif self.sweet_spot:
                lines.append(f"Angel sweet-spot: {self.sweet_spot} {note}")
            else:
                lines.append(f"Angel check range: {self.check_min}–{self.check_max} {note}")
        if self.locations:
            lines.append(f"Investment locations listed: {self.locations}")
        return "\n".join(lines)


BatchVerdict = Literal["yes", "review", "no", "skipped", "error"]


class BatchGateItem(BaseModel):
    """Result for one investor in a batch gate run."""

    investor_name: str
    firm_name: Optional[str] = None
    nfx_url: Optional[str] = None
    verdict: BatchVerdict
    summary: str = ""
    reasons: List[str] = Field(default_factory=list)
    confidence: Literal["high", "medium", "low", "n/a"] = "n/a"
    session_id: Optional[str] = None
    crm_added: bool = False
    error_detail: Optional[str] = None


class BatchGateReport(BaseModel):
    """Full result of a batch gate run (partial while running)."""

    batch_id: str
    source_type: str
    total: int
    processed: int
    yes_count: int = 0
    review_count: int = 0
    no_count: int = 0
    skipped_count: int = 0
    error_count: int = 0
    running: bool = True
    results: List[BatchGateItem] = Field(default_factory=list)

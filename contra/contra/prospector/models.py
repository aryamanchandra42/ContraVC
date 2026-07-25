"""
The record that flows through the prospector cascade.

One `Candidate` per discovered name. Each stage fills in its own fields and sets
`stage` to the furthest point reached, so a candidate that dies at Stage 2 is
still fully explainable from the row alone — `drop_reason` says why, and no later
field is populated.

The invariant that matters: `span` is the VERBATIM text that surfaced this name,
never a paraphrase. Stages 3 and 4 both test against literal text, so a
model-rewritten span silently breaks quotability downstream.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

# Cascade stages, in order. `Candidate.stage` is the furthest one reached.
STAGES = ("harvest", "resolve", "prerank", "corroborate", "gate")


def domain_of(url: str) -> str:
    """Registrable-ish domain for independence checks. '' when not a real URL."""
    if not url:
        return ""
    try:
        parsed = urlparse(url)
    except ValueError:
        return ""
    # Only real web sources have domains. A provider synthesis pseudo-URL such as
    # "anthropic://web-search/ab12" otherwise parses to the host "web-search",
    # which would pose as an independent domain and inflate source_diversity.
    if parsed.scheme not in ("http", "https"):
        return ""
    host = (parsed.hostname or "").lower()
    if not host:
        return ""
    if host.startswith("www."):
        host = host[4:]
    return host


@dataclass
class Corroboration:
    """One independent confirmation of a fund-LP commitment."""

    quote: str
    url: str
    domain: str

    def as_analyst_fact(self, name: str) -> str:
        """
        Render as an analyst fact for the gate.

        The gate scores analyst facts as signals when the text contains
        LP-confirming language, so the commitment quote must survive verbatim.
        """
        return f"{name}: {self.quote} (source: {self.url})"


@dataclass
class Candidate:
    """A mined LP candidate, progressively enriched by each cascade stage."""

    # --- Stage 1: harvest -------------------------------------------------
    name: str
    span: str = ""                      # verbatim text that named this entity
    source_url: str = ""
    source_domain: str = ""
    doc_type: str = ""                  # fund_close | directory | program | profile | search
    entity_type: str = ""
    geography: str = ""
    confidence: str = "medium"          # extractor's own confidence
    seed: str = ""                      # which seed/query surfaced them
    # Every distinct domain that named this entity during harvest.
    domains: List[str] = field(default_factory=list)

    # --- Stage 2: resolve -------------------------------------------------
    name_key: str = ""

    # --- Stage 3: prerank -------------------------------------------------
    prerank_score: int = 0
    prerank_checks: Dict[str, Any] = field(default_factory=dict)

    # --- Stage 4: corroborate ---------------------------------------------
    corroborations: List[Corroboration] = field(default_factory=list)

    # --- Stage 5: adjudicate ----------------------------------------------
    gate_verdict: str = ""              # yes | review | no | error
    gate_session_id: str = ""
    gate_summary: str = ""
    lead_id: str = ""

    # --- Lifecycle --------------------------------------------------------
    stage: str = "harvest"
    drop_reason: str = ""               # set by whichever stage rejected it
    status: str = "review"              # prospector_candidates.status
    verdict_reason: str = ""
    revisit_date: Optional[str] = None

    @property
    def source_diversity(self) -> int:
        """Distinct real domains that named this entity during harvest."""
        return len({d for d in self.domains if d})

    @property
    def corroborated(self) -> bool:
        return bool(self.corroborations)

    def analyst_facts(self) -> List[str]:
        """
        Corroborated commitment quotes, as the gate's analyst-fact channel.

        The gate counts at most 2 analyst facts toward its signal bar, so there
        is no value in sending more.
        """
        return [c.as_analyst_fact(self.name) for c in self.corroborations[:2]]

    def drop(self, stage: str, reason: str) -> "Candidate":
        """Mark this candidate as rejected at `stage`, recording why."""
        self.stage = stage
        self.drop_reason = reason
        self.status = "rejected"
        self.verdict_reason = reason
        return self

    def advance(self, stage: str) -> "Candidate":
        self.stage = stage
        return self

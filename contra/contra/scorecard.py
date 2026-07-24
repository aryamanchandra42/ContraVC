"""
LP Scorecard — the visible evaluation layer.

Five checks, each pass / unknown / fail with a one-line evidence quote and a
source URL. The verdict rule is deliberately simple enough to print in the UI:

    QUALIFIED — Fund LP passes, No disqualifier passes, at least one of
                (Backs new managers / Thesis fit / Geography fit) passes,
                and nothing fails.
    REJECTED  — any check fails (the failing check + evidence is the reason).
    REVIEW    — everything else; the reason states exactly which fact would
                flip the verdict.

The scorecard is derived from the existing gate/ICP engines (which keep their
rigor underneath) but is the single surface users see. Every LP also carries a
yes-reason: the one checkable fact most likely to make them say yes, used for
queue ordering and the outreach hook.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Literal, Optional, Tuple

from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------

CheckId = Literal["fund_lp", "new_managers", "thesis_fit", "geography_fit", "no_disqualifier"]
CheckStatus = Literal["pass", "fail", "unknown"]
ScorecardVerdict = Literal["qualified", "review", "rejected"]
YesReason = Literal[
    "warm_path",        # we can get a warm intro through the syndicate graph
    "syndicate_alum",   # already invested as a fund LP inside MyAsiaVC
    "peer_fund_backer", # verified LP commitment into a peer / Fund I vehicle
    "stated_em_program",# documented emerging-manager / Fund I appetite
    "thesis_match",     # verified AI/tech + geography alignment
    "cold_fit",         # passes on paper only — needs research before outreach
]

CHECK_ORDER: Tuple[CheckId, ...] = (
    "fund_lp", "new_managers", "thesis_fit", "geography_fit", "no_disqualifier",
)

CHECK_LABELS: Dict[str, str] = {
    "fund_lp": "Fund LP",
    "new_managers": "Backs new managers",
    "thesis_fit": "Thesis fit (AI / deep tech)",
    "geography_fit": "Geography fit",
    "no_disqualifier": "No disqualifier",
}

YES_REASON_LABELS: Dict[str, str] = {
    "warm_path": "Warm intro path",
    "syndicate_alum": "Syndicate alum",
    "peer_fund_backer": "Backed a peer fund",
    "stated_em_program": "Stated EM program",
    "thesis_match": "Thesis match",
    "cold_fit": "Cold fit",
}


class ScorecardCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")

    check: CheckId
    status: CheckStatus = "unknown"
    # One line of checkable evidence — a quote or hard fact, never flattery.
    evidence: str = ""
    source_url: str = ""

    @property
    def label(self) -> str:
        return CHECK_LABELS[self.check]


class LpScorecard(BaseModel):
    model_config = ConfigDict(extra="forbid")

    investor_name: str
    name_key: str = ""
    checks: List[ScorecardCheck] = Field(default_factory=list)
    verdict: ScorecardVerdict = "review"
    verdict_reason: str = ""
    yes_reason: YesReason = "cold_fit"
    yes_evidence: str = ""
    source: str = "gate"  # gate | prospector | icp | syndicate
    updated_at: Optional[str] = None

    def check_map(self) -> Dict[str, ScorecardCheck]:
        return {c.check: c for c in self.checks}

    def to_api_dict(self) -> Dict[str, Any]:
        d = self.model_dump()
        for c in d["checks"]:
            c["label"] = CHECK_LABELS[c["check"]]
        d["yes_reason_label"] = YES_REASON_LABELS.get(self.yes_reason, self.yes_reason)
        return d


# ---------------------------------------------------------------------------
# Verdict rule (visible, no hidden weights)
# ---------------------------------------------------------------------------

def compute_verdict(checks: List[ScorecardCheck]) -> Tuple[ScorecardVerdict, str]:
    by = {c.check: c for c in checks}

    failed = [c for c in checks if c.status == "fail"]
    if failed:
        f = failed[0]
        why = f.evidence or "no evidence recorded"
        return "rejected", f"{CHECK_LABELS[f.check]} failed: {why}"

    fund_lp = by.get("fund_lp")
    disq = by.get("no_disqualifier")
    appetite_passes = [
        by[k] for k in ("new_managers", "thesis_fit", "geography_fit")
        if k in by and by[k].status == "pass"
    ]

    if fund_lp and fund_lp.status == "pass" and disq and disq.status == "pass" and appetite_passes:
        passed = " + ".join(CHECK_LABELS[c.check] for c in appetite_passes)
        return "qualified", f"Fund LP confirmed, no disqualifiers, {passed}."

    # Review — say exactly what would flip it.
    missing: List[str] = []
    if not fund_lp or fund_lp.status != "pass":
        missing.append("confirm one fund LP commitment (Fund LP is unverified)")
    if not disq or disq.status != "pass":
        missing.append("confirm there is no structural disqualifier")
    if not appetite_passes:
        missing.append("confirm EM appetite, thesis fit, or geography fit (none verified yet)")
    return "review", "Flip to qualified: " + "; ".join(missing)


# ---------------------------------------------------------------------------
# Yes-reason classifier (priority order = outreach queue order)
# ---------------------------------------------------------------------------

def classify_yes_reason(
    *,
    warm_path_count: int = 0,
    warm_bridge_name: str = "",
    syndicate_fund_deals: int = 0,
    lp_commitments: Optional[List[str]] = None,
    em_evidence: str = "",
    thesis_evidence: str = "",
) -> Tuple[YesReason, str]:
    """Pick the single strongest reason this LP would say yes, with its evidence."""
    lp_commitments = lp_commitments or []

    if warm_path_count > 0:
        via = f" via {warm_bridge_name}" if warm_bridge_name else ""
        return "warm_path", f"{warm_path_count} warm intro path(s){via} in the co-investment graph"
    if syndicate_fund_deals > 0:
        return "syndicate_alum", f"{syndicate_fund_deals} fund deal(s) as an LP inside the MyAsiaVC syndicate"
    if lp_commitments:
        return "peer_fund_backer", lp_commitments[0]
    if em_evidence:
        return "stated_em_program", em_evidence
    if thesis_evidence:
        return "thesis_match", thesis_evidence
    return "cold_fit", "Passes screening on paper; no verified hook yet"


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------

_GATE_TO_CHECK: Dict[str, CheckId] = {
    "c1": "fund_lp",
    "c2": "new_managers",
    "c3": "thesis_fit",
    "c4": "geography_fit",
}


def scorecard_from_gate(result: Any, *, name_key: str = "") -> LpScorecard:
    """
    Build a scorecard from a GateResult (contra.gate.models). Deterministic:
    reads the final merged core gates, hard blocks, negative flags, and signals.
    """
    from agents.normalization.crm_normalizer import norm_key as _norm

    checks: List[ScorecardCheck] = []
    assessment = result.assessment

    for g in assessment.core_gates:
        cid = _GATE_TO_CHECK.get(g.gate)
        if cid is None:
            continue
        checks.append(ScorecardCheck(
            check=cid,
            status=g.status,
            evidence=(g.evidence or "")[:280],
        ))

    # No-disqualifier: hard blocks and negative flags fail it; a completed LLM
    # pass with no flags passes it; otherwise unknown.
    appetite = result.appetite or assessment.appetite
    negative_flags = list(appetite.negative_flags) if appetite else []
    if assessment.hard_blocks:
        disq = ScorecardCheck(
            check="no_disqualifier", status="fail",
            evidence=assessment.hard_blocks[0][:280],
        )
    elif negative_flags:
        disq = ScorecardCheck(
            check="no_disqualifier", status="fail",
            evidence=(negative_flags[0] + ". " + (appetite.negative_evidence or ""))[:280].strip(),
        )
    elif appetite is not None:
        disq = ScorecardCheck(
            check="no_disqualifier", status="pass",
            evidence="No PE-only / direct-only / crypto-only / size-mismatch flags found in research",
        )
    else:
        disq = ScorecardCheck(check="no_disqualifier", status="unknown", evidence="Not yet researched")
    checks.append(disq)

    # Yes-reason from the gate signals + verified commitments.
    signal_map = {s.id: s for s in assessment.signals}
    warm = signal_map.get("warm_path")
    synd = signal_map.get("syndicate_fund_lp")
    em_ev = (appetite.archetype_evidence if appetite else "") or ""
    if appetite and appetite.em_appetite in ("strong", "moderate"):
        em_ev = next(
            (e for e in (appetite.allocation_evidence or []) if e), em_ev,
        )
    else:
        em_ev = ""
    thesis_ev = ""
    if appetite and appetite.ai_tech_appetite in ("strong", "moderate"):
        thesis_ev = next((e for e in (appetite.allocation_evidence or []) if e), "AI/tech appetite rated moderate+")

    yes_reason, yes_evidence = classify_yes_reason(
        warm_path_count=1 if (warm and warm.met) else 0,
        warm_bridge_name="",
        syndicate_fund_deals=1 if (synd and synd.met) else 0,
        lp_commitments=list(result.lp_commitments_found or []),
        em_evidence=em_ev,
        thesis_evidence=thesis_ev,
    )
    if warm and warm.met:
        yes_evidence = warm.detail[:280] or yes_evidence
    elif synd and synd.met:
        yes_evidence = synd.detail[:280] or yes_evidence

    verdict, reason = compute_verdict(checks)
    return LpScorecard(
        investor_name=result.lp_name,
        name_key=name_key or _norm(result.lp_name),
        checks=checks,
        verdict=verdict,
        verdict_reason=reason,
        yes_reason=yes_reason,
        yes_evidence=yes_evidence,
        source="gate",
    )


class ScorecardExtraction(BaseModel):
    """
    LLM structured-output schema for scoring an LP from research snippets
    (used by the Prospector). Every non-unknown status MUST carry evidence that
    is quotable from the provided snippets — the verifier strips the rest.
    """
    model_config = ConfigDict(extra="forbid")

    fund_lp_status: CheckStatus = "unknown"
    fund_lp_evidence: str = Field(default="", max_length=280)
    fund_lp_source_url: str = ""

    new_managers_status: CheckStatus = "unknown"
    new_managers_evidence: str = Field(default="", max_length=280)
    new_managers_source_url: str = ""

    thesis_fit_status: CheckStatus = "unknown"
    thesis_fit_evidence: str = Field(default="", max_length=280)
    thesis_fit_source_url: str = ""

    geography_fit_status: CheckStatus = "unknown"
    geography_fit_evidence: str = Field(default="", max_length=280)
    geography_fit_source_url: str = ""

    no_disqualifier_status: CheckStatus = "unknown"
    no_disqualifier_evidence: str = Field(default="", max_length=280)
    no_disqualifier_source_url: str = ""

    def to_checks(self) -> List[ScorecardCheck]:
        out: List[ScorecardCheck] = []
        for cid in CHECK_ORDER:
            out.append(ScorecardCheck(
                check=cid,
                status=getattr(self, f"{cid}_status"),
                evidence=getattr(self, f"{cid}_evidence") or "",
                source_url=getattr(self, f"{cid}_source_url") or "",
            ))
        return out


def scorecard_from_extraction(
    investor_name: str,
    extraction: ScorecardExtraction,
    *,
    source: str = "prospector",
    lp_commitments: Optional[List[str]] = None,
) -> LpScorecard:
    from agents.normalization.crm_normalizer import norm_key as _norm

    checks = extraction.to_checks()
    verdict, reason = compute_verdict(checks)
    by = {c.check: c for c in checks}
    yes_reason, yes_evidence = classify_yes_reason(
        lp_commitments=lp_commitments or (
            [by["fund_lp"].evidence] if by["fund_lp"].status == "pass" else []
        ),
        em_evidence=by["new_managers"].evidence if by["new_managers"].status == "pass" else "",
        thesis_evidence=by["thesis_fit"].evidence if by["thesis_fit"].status == "pass" else "",
    )
    return LpScorecard(
        investor_name=investor_name,
        name_key=_norm(investor_name),
        checks=checks,
        verdict=verdict,
        verdict_reason=reason,
        yes_reason=yes_reason,
        yes_evidence=yes_evidence,
        source=source,
    )


# ---------------------------------------------------------------------------
# Persistence — lead_scorecards (one row per name_key, latest wins).
# Table is created by agents.db_migrations.migrate_lead_scorecards.
# ---------------------------------------------------------------------------

def upsert_scorecard(con, sc: LpScorecard) -> None:
    con.execute(
        """
        INSERT INTO lead_scorecards
            (name_key, investor_name, verdict, verdict_reason,
             yes_reason, yes_evidence, checks_json, source, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, NOW())
        ON CONFLICT (name_key) DO UPDATE SET
            investor_name  = EXCLUDED.investor_name,
            verdict        = EXCLUDED.verdict,
            verdict_reason = EXCLUDED.verdict_reason,
            yes_reason     = EXCLUDED.yes_reason,
            yes_evidence   = EXCLUDED.yes_evidence,
            checks_json    = EXCLUDED.checks_json,
            source         = EXCLUDED.source,
            updated_at     = NOW()
        """,
        [
            sc.name_key, sc.investor_name, sc.verdict, sc.verdict_reason,
            sc.yes_reason, sc.yes_evidence,
            json.dumps([c.model_dump() for c in sc.checks]),
            sc.source,
        ],
    )


def _row_to_scorecard(row: tuple, cols: List[str]) -> LpScorecard:
    data = dict(zip(cols, row))
    raw_checks = data.get("checks_json") or "[]"
    if isinstance(raw_checks, str):
        try:
            raw_checks = json.loads(raw_checks)
        except json.JSONDecodeError:
            raw_checks = []
    return LpScorecard(
        investor_name=data["investor_name"],
        name_key=data["name_key"],
        checks=[ScorecardCheck(**c) for c in raw_checks],
        verdict=data.get("verdict") or "review",
        verdict_reason=data.get("verdict_reason") or "",
        yes_reason=data.get("yes_reason") or "cold_fit",
        yes_evidence=data.get("yes_evidence") or "",
        source=data.get("source") or "gate",
        updated_at=str(data["updated_at"]) if data.get("updated_at") else None,
    )


def get_scorecard(con, name_key: str) -> Optional[LpScorecard]:
    row = con.execute(
        "SELECT * FROM lead_scorecards WHERE name_key = ? LIMIT 1", [name_key],
    ).fetchone()
    if not row:
        return None
    cols = [d[0] for d in con.description]
    return _row_to_scorecard(row, cols)


def scorecards_for_keys(con, name_keys: List[str]) -> Dict[str, LpScorecard]:
    """Batch fetch — one query for a whole lead list."""
    if not name_keys:
        return {}
    placeholders = ",".join("?" for _ in name_keys)
    rows = con.execute(
        f"SELECT * FROM lead_scorecards WHERE name_key IN ({placeholders})",
        list(name_keys),
    ).fetchall()
    cols = [d[0] for d in con.description]
    out: Dict[str, LpScorecard] = {}
    for row in rows:
        sc = _row_to_scorecard(row, cols)
        out[sc.name_key] = sc
    return out


def save_scorecard_from_gate(con, result: Any) -> Optional[LpScorecard]:
    """Build + persist a scorecard after a gate run. Non-fatal on failure."""
    try:
        sc = scorecard_from_gate(result)
        upsert_scorecard(con, sc)
        return sc
    except Exception as exc:
        logger.warning("Scorecard persist failed for %s: %s", getattr(result, "lp_name", "?"), exc)
        return None

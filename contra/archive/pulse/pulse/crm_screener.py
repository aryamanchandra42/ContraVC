"""
CRM pre-screener — should this person go into FundingStack?

Uses three reference datasets (read directly from raw_data/):
  1. ContraVC Top 200 LP Rankings — external benchmark / gold outreach list
  2. Syndicate LPs (AngelList export, e.g. …1777431165.xlsx) — roster + investments
  3. Fund_Rating_Guide.xlsx — LP fund-evaluation rubric (high-weight dimensions)

Verdicts: add | review | skip
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
from rapidfuzz import fuzz, process

from agents.normalization.taxonomies import normalize_geography, normalize_lp_type_label, parse_usd
from agents.scoring.icp_scorer import (
    _compute_fit_score,
    _compute_tier,
    _score_c1_vc_fund,
    _score_c2_emerging_manager,
    _score_c3_ai_tech,
    _score_c4_geography,
    _score_exclusions,
    _score_s1_ai_signal,
    _score_s2_emerging_manager,
    _score_s3_lp_type,
    _score_s4_decision_speed,
    _score_s5_stage,
    _score_s6_clean_profile,
    _score_s7_proxy_fund,
)
from pulse.exports.outreach_pack import _norm_key

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "raw_data"

_LEGAL_SUFFIX_RE = re.compile(
    r"\s+(ltd|limited|llc|inc|corp|plc|pte|sa|bv|gmbh|lp|llp|co)\.?$", re.IGNORECASE
)
_TIER_RE = re.compile(r"tier\s*([1-4])", re.IGNORECASE)
_FUZZY_AUTO = 92
_FUZZY_REVIEW = 85


@dataclass
class ScreenResult:
    name: str
    verdict: str  # add | review | skip
    score: float
    confidence: str  # high | medium | low
    reasons: List[str] = field(default_factory=list)
    signals: Dict[str, Any] = field(default_factory=dict)
    checklist: List[str] = field(default_factory=list)
    matched_name: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class ReferenceIndex:
    """Lazy-loaded lookup tables from Contra, syndicate, fund-rating rubric, CRM export."""

    def __init__(self, raw_dir: Optional[Path] = None) -> None:
        self.raw_dir = raw_dir or RAW
        self._loaded = False
        self.contra: Dict[str, Dict[str, Any]] = {}
        self.contra_names: List[str] = []
        self.syndicate: Dict[str, Dict[str, Any]] = {}
        self.syndicate_names: List[str] = []
        self.investment_stats: Dict[str, Dict[str, Any]] = {}
        self.crm_names: set[str] = set()
        self.fund_rating_checklist: List[str] = []

    def load(self) -> None:
        if self._loaded:
            return
        self._load_contra()
        self._load_syndicate()
        self._load_fund_rating_rubric()
        self._load_crm_export()
        self._loaded = True

    def _load_contra(self) -> None:
        paths = sorted(self.raw_dir.glob("ContraVC*.xlsx"))
        if not paths:
            return
        df = pd.read_excel(paths[0], sheet_name="Top 200 LP Rankings")
        for _, row in df.iterrows():
            name = str(row.get("Name") or "").strip()
            if not name or name.lower() == "name":
                continue
            key = _norm_key(name)
            tier_raw = str(row.get("Tier") or "")
            tier_m = _TIER_RE.search(tier_raw)
            prior_raw = str(row.get("Prior Fund LP?") or "").strip().lower()
            rec = {
                "name": name,
                "rank": _safe_int(row.get("Rank")),
                "priority_score": _safe_float(row.get("Priority Score")),
                "tier": f"tier_{tier_m.group(1)}" if tier_m else None,
                "tier_label": tier_raw.strip(),
                "prior_fund_lp": prior_raw.startswith("yes"),
                "spvs_backed": _safe_int(row.get("SPVs Backed")),
                "funds_backed": _safe_int(row.get("Funds Backed")),
                "median_check_usd": parse_usd(str(row.get("Median Check") or "")),
                "total_invested_usd": _safe_float(row.get("Total Invested (Syndicate)")),
                "al_activity_usd": _safe_float(row.get("AL Activity (Last 12m)")),
                "linkedin": str(row.get("LinkedIn URL") or "").strip() or None,
                "source": "contravc_top200",
            }
            self.contra[key] = rec
            self.contra_names.append(name)

    def _load_syndicate(self) -> None:
        paths = sorted(self.raw_dir.glob("Syndicate LPs*.xlsx"))
        if not paths:
            return
        path = paths[0]
        lp_df = pd.read_excel(path, sheet_name="Syndicate LPs")
        inv_df = pd.read_excel(path, sheet_name="LP investments")

        for _, row in lp_df.iterrows():
            name = str(row.get("Name") or "").strip()
            if not name or name.lower() == "name":
                continue
            key = _norm_key(name)
            total = parse_usd(str(row.get("Total amount invested with your syndicate") or "")) or 0.0
            median = parse_usd(str(row.get("Median check with your syndicate") or ""))
            rec = {
                "name": name,
                "email": str(row.get("Email") or "").strip() or None,
                "total_invested_usd": total,
                "median_check_usd": median,
                "spvs_invested": _safe_int(row.get("Num SPVs invested with your syndicate")),
                "funds_invested": _safe_int(row.get("Num funds invested with your syndicate")),
                "spvs_invited": _safe_int(row.get("Num SPVs invited with your syndicate")),
                "al_activity_12m": parse_usd(
                    str(row.get("Total amount invested with AngelList (last 12m)") or "")
                ),
                "linkedin": str(row.get("Linkedin profile URL") or "").strip() or None,
                "tags": str(row.get("Tags") or "").strip() or None,
                "source_file": path.name,
                "source": "syndicate_lp",
            }
            self.syndicate[key] = rec
            self.syndicate_names.append(name)

        if not inv_df.empty:
            inv_df = inv_df.copy()
            inv_df["_key"] = inv_df["Partner name"].astype(str).map(_norm_key)
            for key, grp in inv_df.groupby("_key"):
                types = grp["Type"].astype(str).str.upper()
                amounts = grp["Investment amount"].map(lambda x: parse_usd(str(x)) or 0.0)
                self.investment_stats[key] = {
                    "deal_count": len(grp),
                    "spv_count": int((types == "SPV").sum()),
                    "fund_count": int((types == "FUND").sum()),
                    "total_invested_usd": float(amounts.sum()),
                    "sample_deals": grp["Investment name"].astype(str).head(3).tolist(),
                }

    def _load_fund_rating_rubric(self) -> None:
        path = self.raw_dir / "Fund_Rating_Guide.xlsx"
        if not path.exists():
            return
        df = pd.read_excel(path, sheet_name="Fund_Rating_Guide", header=None)
        # Row 3 is header: Area, Dimension, Question, Typical LP weight, Benchmark
        for i in range(4, len(df)):
            row = df.iloc[i]
            area = str(row.iloc[0] or "").strip() if pd.notna(row.iloc[0]) else ""
            dimension = str(row.iloc[1] or "").strip() if len(row) > 1 and pd.notna(row.iloc[1]) else ""
            weight = str(row.iloc[3] or "").strip().lower() if len(row) > 3 and pd.notna(row.iloc[3]) else ""
            if not dimension:
                continue
            if weight in ("high", "very high"):
                self.fund_rating_checklist.append(f"{area}: {dimension} ({weight} LP weight)")

    def _load_crm_export(self) -> None:
        path = self.raw_dir / "export.csv"
        if not path.exists():
            return
        df = pd.read_csv(path)
        col = "Investor Name" if "Investor Name" in df.columns else df.columns[0]
        for name in df[col].astype(str):
            n = name.strip()
            if n and n.lower() not in ("investor name", "nan"):
                self.crm_names.add(_norm_key(n))


def _safe_int(val: Any) -> Optional[int]:
    try:
        if pd.isna(val):
            return None
        return int(float(str(val).replace(",", "").strip()))
    except (ValueError, TypeError):
        return None


def _safe_float(val: Any) -> Optional[float]:
    try:
        if pd.isna(val):
            return None
        return float(str(val).replace(",", "").replace("$", "").strip())
    except (ValueError, TypeError):
        return None


def _fuzzy_lookup(name: str, choices: List[str]) -> Tuple[Optional[str], int]:
    if not choices:
        return None, 0
    hit = process.extractOne(name, choices, scorer=fuzz.token_sort_ratio)
    if not hit:
        return None, 0
    matched, score, _ = hit
    return matched, int(score)


def _score_contra(rec: Dict[str, Any]) -> Tuple[float, List[str]]:
    pts = 0.0
    reasons: List[str] = []
    rank = rec.get("rank") or 200
    pts += max(0, 30 - (rank - 1) * 0.12)
    reasons.append(f"Contra Top 200 #{rank} (priority {rec.get('priority_score', '—')})")

    tier = rec.get("tier") or ""
    if tier == "tier_1":
        pts += 25
        reasons.append("Contra Tier 1 — highest external conviction")
    elif tier == "tier_2":
        pts += 15
        reasons.append("Contra Tier 2")

    if rec.get("prior_fund_lp"):
        pts += 20
        reasons.append("Prior fund LP (backs VC funds, not SPV-only)")

    funds = rec.get("funds_backed") or 0
    if funds >= 2:
        pts += 10
        reasons.append(f"Backed {funds} syndicate funds")
    elif funds == 1:
        pts += 5

    median = rec.get("median_check_usd")
    if median and 5_000 <= median <= 500_000:
        pts += 8
        reasons.append(f"Median check ${median:,.0f} — in target LP range")

    al12 = rec.get("al_activity_usd") or 0
    if al12 >= 10_000:
        pts += 5
        reasons.append("Active on AngelList in last 12 months")

    return pts, reasons


def _score_syndicate(rec: Dict[str, Any], inv: Optional[Dict[str, Any]]) -> Tuple[float, List[str]]:
    pts = 0.0
    reasons: List[str] = []
    reasons.append(f"On your syndicate roster ({rec.get('source_file', 'syndicate')})")

    total = rec.get("total_invested_usd") or 0.0
    if total >= 25_000:
        pts += 20
        reasons.append(f"${total:,.0f} invested in your syndicate")
    elif total >= 5_000:
        pts += 12
        reasons.append(f"${total:,.0f} syndicate investment history")
    elif total > 0:
        pts += 8
        reasons.append(f"${total:,.0f} syndicate investment history")
    elif (rec.get("spvs_invested") or 0) >= 1:
        pts += 6
        reasons.append(f"{rec['spvs_invested']} SPV investment(s) — syndicate participant")

    funds_inv = rec.get("funds_invested") or 0
    if funds_inv >= 1:
        pts += 18
        reasons.append(f"Invested in {funds_inv} syndicate fund(s) — fund-LP behaviour")
    elif (rec.get("spvs_invested") or 0) >= 5:
        pts += 8
        reasons.append(f"{rec['spvs_invested']} SPV investments — active angel, review fund appetite")

    if inv:
        fc = inv.get("fund_count") or 0
        if fc >= 1:
            pts += 12
            reasons.append(f"{fc} fund vehicle investment(s) in deal history")
        dc = inv.get("deal_count") or 0
        if dc >= 10:
            pts += 5
            reasons.append(f"{dc} total syndicate deals")

    median = rec.get("median_check_usd")
    if median and median >= 2_500:
        pts += 5

    tags = (rec.get("tags") or "").lower()
    if "not invested yet" in tags and total == 0 and funds_inv == 0:
        pts -= 15
        reasons.append("Syndicate tag: not invested yet — low engagement")

    return pts, reasons


def _score_icp_text(
    text: str,
    investor_type: str,
    location: str,
) -> Tuple[float, List[str], Dict[str, Any]]:
    if not text.strip():
        return 0.0, [], {}

    alloc_type = normalize_lp_type_label(investor_type) or "unknown"
    geo = normalize_geography(location)
    scoring = text

    c1, _ = _score_c1_vc_fund(scoring)
    c2, c2_ev = _score_c2_emerging_manager(scoring, "")
    c3, _ = _score_c3_ai_tech(scoring)
    c4, _ = _score_c4_geography(scoring)
    core_pass = c1 and c2 and c3 and c4
    excluded, excl_reason = _score_exclusions("", scoring, "", location)

    s1 = _score_s1_ai_signal(scoring)
    s2 = _score_s2_emerging_manager(scoring, "")
    s3 = _score_s3_lp_type(alloc_type)
    s4 = _score_s4_decision_speed(alloc_type)
    s5 = _score_s5_stage(scoring)
    s6 = _score_s6_clean_profile(scoring, "")
    s7 = _score_s7_proxy_fund(scoring)
    fit = _compute_fit_score(s1, s2, s3, s4, s5, s6, s7)
    tier = _compute_tier(core_pass, excluded, fit, "pending")

    pts = 0.0
    reasons: List[str] = []
    if excluded:
        pts -= 40
        reasons.append(f"ICP exclusion: {excl_reason}")
    elif core_pass:
        pts += min(25, fit * 30)
        reasons.append(f"ICP core gates pass (fit {fit:.2f}, {tier})")
    else:
        fails = []
        if not c1:
            fails.append("C1 VC-fund LP")
        if not c2:
            fails.append(f"C2 EM ({c2_ev[:60]})")
        if not c3:
            fails.append("C3 AI/tech")
        if not c4:
            fails.append("C4 geography")
        pts -= 10
        reasons.append(f"ICP core gaps: {', '.join(fails)}")

    meta = {
        "core_pass": core_pass,
        "excluded": excluded,
        "fit_score": fit,
        "tier": tier,
        "allocator_type": alloc_type,
        "geography": geo,
    }
    return pts, reasons, meta


def screen_prospect(
    name: str,
    *,
    details: str = "",
    investor_type: str = "",
    location: str = "",
    email: str = "",
    linkedin: str = "",
    index: Optional[ReferenceIndex] = None,
    skip_if_in_crm: bool = True,
) -> ScreenResult:
    """
    Classify whether to add *name* to FundingStack CRM.

    Primary signals: Contra Top 200, syndicate LP roster/investments, optional ICP text.
    """
    idx = index or ReferenceIndex()
    idx.load()

    key = _norm_key(name)
    in_crm = key in idx.crm_names
    reasons: List[str] = []
    signals: Dict[str, Any] = {"input_name": name}
    if in_crm:
        signals["in_crm"] = True
    score = 0.0
    matched_name: Optional[str] = None

    if skip_if_in_crm and in_crm:
        return ScreenResult(
            name=name,
            verdict="skip",
            score=0.0,
            confidence="high",
            reasons=["Already in FundingStack CRM export"],
            signals={"in_crm": True},
        )

    contra_rec = idx.contra.get(key)
    syndicate_rec = idx.syndicate.get(key)
    inv_stats = idx.investment_stats.get(key)

    if not contra_rec:
        m, sim = _fuzzy_lookup(name, idx.contra_names)
        if m and sim >= _FUZZY_AUTO:
            contra_rec = idx.contra.get(_norm_key(m))
            matched_name = m
            signals["contra_fuzzy_score"] = sim
        elif m and sim >= _FUZZY_REVIEW:
            signals["contra_fuzzy_candidate"] = {"name": m, "score": sim}

    if not syndicate_rec:
        m, sim = _fuzzy_lookup(name, idx.syndicate_names)
        if m and sim >= _FUZZY_AUTO:
            syndicate_rec = idx.syndicate.get(_norm_key(m))
            matched_name = matched_name or m
            signals["syndicate_fuzzy_score"] = sim
        elif m and sim >= _FUZZY_REVIEW:
            signals["syndicate_fuzzy_candidate"] = {"name": m, "score": sim}

    if contra_rec:
        pts, rs = _score_contra(contra_rec)
        score += pts
        reasons.extend(rs)
        signals["contra"] = contra_rec

    if syndicate_rec:
        pts, rs = _score_syndicate(syndicate_rec, inv_stats)
        score += pts
        reasons.extend(rs)
        signals["syndicate"] = syndicate_rec
        if inv_stats:
            signals["syndicate_investments"] = inv_stats

    if contra_rec and syndicate_rec:
        score += 15
        reasons.append("Dual signal: Contra Top 200 + your syndicate roster")

    if details or investor_type or location:
        pts, rs, icp_meta = _score_icp_text(
            details or name,
            investor_type,
            location,
        )
        score += pts
        reasons.extend(rs)
        if icp_meta:
            signals["icp"] = icp_meta

    # Fund Rating Guide rubric — checklist for review, not a numeric score
    checklist = list(idx.fund_rating_checklist[:6])
    if not contra_rec and not syndicate_rec:
        checklist.insert(0, "Not in Contra Top 200 or syndicate — verify fund-LP vs direct-only")

    if score >= 55:
        verdict = "add"
        confidence = "high" if score >= 75 else "medium"
    elif score >= 25 or signals.get("contra_fuzzy_candidate") or signals.get("syndicate_fuzzy_candidate"):
        verdict = "review"
        confidence = "medium" if score >= 25 else "low"
    else:
        verdict = "skip"
        confidence = "medium" if syndicate_rec else "low"

    if signals.get("icp", {}).get("excluded"):
        verdict = "skip"
        confidence = "high"

    return ScreenResult(
        name=name,
        verdict=verdict,
        score=round(score, 1),
        confidence=confidence,
        reasons=reasons,
        signals=signals,
        checklist=checklist,
        matched_name=matched_name,
    )


def screen_batch_csv(
    input_path: Path,
    output_path: Optional[Path] = None,
    *,
    name_col: str = "Investor Name",
    details_col: str = "Investor Details",
    type_col: str = "Investor Type",
    location_col: str = "Investor Location",
) -> Path:
    """Screen every row in a FundingStack CSV export."""
    idx = ReferenceIndex()
    idx.load()

    df = pd.read_csv(input_path)
    if name_col not in df.columns:
        raise ValueError(f"Column {name_col!r} not found in {input_path}")

    rows: List[Dict[str, Any]] = []
    for _, row in df.iterrows():
        name = str(row.get(name_col) or "").strip()
        if not name or name.lower() == "investor name":
            continue
        result = screen_prospect(
            name,
            details=str(row.get(details_col) or ""),
            investor_type=str(row.get(type_col) or ""),
            location=str(row.get(location_col) or ""),
            index=idx,
            skip_if_in_crm=False,
        )
        rows.append({
            "investor_name": name,
            "already_in_crm": result.signals.get("in_crm", False),
            "crm_verdict": result.verdict,
            "crm_score": result.score,
            "confidence": result.confidence,
            "matched_name": result.matched_name or "",
            "top_reason": result.reasons[0] if result.reasons else "",
            "reason_count": len(result.reasons),
            "in_contra_top200": "contra" in result.signals,
            "in_syndicate": "syndicate" in result.signals,
            "prior_fund_lp": (result.signals.get("contra") or {}).get("prior_fund_lp", ""),
        })

    out = output_path or (ROOT / "processed_data" / "crm_screen_results.csv")
    out.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_csv(out, index=False)
    return out

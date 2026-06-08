"""
ICP v4.1 specification — derived from two sources:
  1. IIP sheet of MyAsiaVC_ICP_4.0_Prospect_List_External.xlsx
  2. MyAsiaVC LP Scoping.xlsx (Core Filters, Exclusion Filters, Soft Filters, LP Type Priority)

The LP Scoping doc is the authoritative source. IIP sheet is secondary.

Scoring model:
  CORE filters (C1–C4)  — ALL must pass. Fail any → core_pass=False → Tier 4.
  EXCLUSION rules (E1–E12) — ANY match → excluded=True → Tier 4.
  SOFT signals (S1–S7)  — weighted sum → fit_score (0–1).
  TIER logic:
    Tier 1 = core_pass AND NOT excluded AND fit_score >= 0.65 AND client approved
    Tier 2 = core_pass AND NOT excluded AND fit_score >= 0.40
    Tier 3 = core_pass AND NOT excluded (weak signals or pending)
    Tier 4 = excluded OR NOT core_pass

Version bump history:
  4.0 — initial build from IIP sheet only
  4.1 — full alignment with LP Scoping doc (C1-C6, E1-E12, S1-S10, LP type priority tiers)
"""

from __future__ import annotations

from pathlib import Path
from typing import Tuple

import yaml

ROOT = Path(__file__).resolve().parent.parent.parent
CALIBRATION_YAML = ROOT / "prompts" / "icp_calibration.yaml"

ICP_VERSION = "4.1"

# ---------------------------------------------------------------------------
# CORE FILTERS (C1–C4) — ALL must be TRUE to qualify
# Source: LP Scoping doc, "Core Filters" sheet
# ---------------------------------------------------------------------------

# C1: LP must invest in VC funds as an LP (primary commitment)
# NOT direct-only, NOT secondaries-only, NOT PE-only
C1_KEYWORDS = [
    "fund", "vc", "venture capital", "venture fund", "lp investment",
    "invest in vc", "invest in venture", "fund manager", "lp in",
    "fund investments", "venture fund investments", "backs funds",
]
C1_REQUIRED_ANY = ["fund", "vc", "venture"]  # at least one must hit

# C2: Emerging Manager Appetite
# Must have backed at least one first-, second-, or third-time fund manager
C2_EMERGING_MANAGER_POSITIVE = [
    "emerging manager", "emerging managers", "first-time fund", "first time fund",
    "fund i", "fund 1", "fund ii", "fund 2", "fund iii", "fund 3",
    "emerging fund", "first time manager", "new manager", "inaugural fund",
    "fund one", "fund two", "emerging gp", "first-time gp",
    "dedicated emerging", "emerging manager program", "ilp program",
    "backs emerging", "back emerging", "invests in emerging",
]

# C3: AI / Tech Thesis Alignment
# Direct mention of AI/ML or portfolio in AI ecosystem
C3_SECTORS = [
    "artificial intelligence", " ai ", "ai,", "ai.", "ai/", "ai-", "ai+",
    "machine learning", "deep learning", "robotics", "automation",
    "deep tech", "deeptech", "defence tech", "defense tech",
    "cybersecurity", "cyber security", "saas", "software as a service",
    "energy tech", "energytech", "hr tech", "industrial tech",
    "space tech", "mobility tech", "dual-use", "fintech",
    "semiconductor", "computer vision", "generative ai", "llm",
    "openai", "anthropic", "cohere", "xai", "gemini",  # known AI cos
]

# C4: Geographic Fit
# Must invest in at least one of: North America, Asia, Middle East
# Global mandates always qualify
C4_REGIONS = [
    "asia", "southeast asia", "south asia", "east asia", "asia pacific",
    "apac", "asean", "singapore", "india", "china", "japan", "korea",
    "taiwan", "hong kong", "vietnam", "indonesia", "thailand",
    "north america", "united states", "usa", " us ", "canada",
    "middle east", "gcc", "uae", "saudi", "dubai", "qatar",
    "global", "worldwide", "international",
]

# ---------------------------------------------------------------------------
# EXCLUSION RULES (E1–E12) — ANY match = immediate reject
# Source: LP Scoping doc, "Exclusion Filters" sheet
# ---------------------------------------------------------------------------

# E1: PE/Buyout Primary Focus
E1_PE_PHRASES = [
    "pe focus", "private equity focus", "pe primary", "buyout focus",
    "buyout only", "pe/buyout", "private equity only", "pe only",
    "private equity is the dominant", "primarily pe",
]

# E2: VC Secondaries Only
E2_SECONDARIES_PHRASES = [
    "vc secondaries", "secondaries only", "secondary focus",
    "secondary vc", "vc secondary", "secondaries per website",
]

# E3: Real Estate Primary
E3_REAL_ESTATE_PHRASES = [
    "real estate focus", "real estate primary", "real estate only",
    "primarily real estate", "real estate is the dominant",
    "real estate investment trust", "reit focus",
]

# E4: Web3/Crypto Only
E4_WEB3_PHRASES = [
    "web3 focus", "blockchain focus", "crypto focus", "crypto only",
    "crypto-native", "web3-native", "nft focus", "defi focus",
    "blockchain primary", "crypto primary",
]

# E5: Healthcare/Life Sciences Only
E5_HEALTHCARE_PHRASES = [
    "healthcare only", "healthcare focus", "life sciences only",
    "lifesciences only", "life science focus", "biotech only",
    "biotech focus", "medical only", "pharma focus",
]

# E6: Geography-Locked Non-Qualifying
E6_LOCKED_GEO_PHRASES = [
    "alberta only", "alberta-only", "hk only", "hong kong only",
    "dc only", "domestic only", "latam only", "latin america only",
    "europe only", "africa only", "australia only",
    "single region mandate", "local mandate",
]

# E7: Impact/Philanthropy Primary
E7_IMPACT_PHRASES = [
    "impact investing focus", "philanthropy focus", "philanthropic mandate",
    "esg-screened", "impact only", "blended finance only",
    "social impact primary", "non-profit focus",
]

# E8: Does Not Back Emerging Managers (explicit evidence)
E8_NO_EM_PHRASES = [
    "does not invest in emerging", "no emerging manager",
    "only established managers", "only tier 1 managers",
    "sequoia only", "andreessen only", "top-tier only",
    "dont see any emerging managers", "not emerging managers",
    "they dont seem to invest in emerging managers",
    "no evidence of emerging manager", "established track record only",
    "proven track record required",
]

# E9: Check Size Mismatch (min ticket > $30M or max < $250K)
E9_OVERLARGE_PHRASES = [
    "write larger checks", "larger checks", "minimum ticket",
    "we do not fit bucket", "not fit bucket", "check size too small",
    "below minimum", "ticket too small",
]

# E10: No VC Fund Investments (Direct Only)
E10_DIRECT_ONLY_PHRASES = [
    "does not invest in funds", "direct investments only",
    "no fund investments", "exclusively direct",
    "direct investments vs fund", "does not take lp positions",
    "does not invest in vc funds",
]

# E11: Blacklist / Prior Contact — handled by client_status flag

# E12: Prop Trading / Non-VC Financial
E12_PROP_TRADING_PHRASES = [
    "prop trading firm", "proprietary trading", "hedge fund without vc",
    "broker-dealer", "market maker", "algo trading",
]

# Combined hard exclusion set for fast scanning
ALL_HARD_EXCLUSION_PHRASES: list[str] = (
    E1_PE_PHRASES + E2_SECONDARIES_PHRASES + E3_REAL_ESTATE_PHRASES
    + E4_WEB3_PHRASES + E5_HEALTHCARE_PHRASES + E6_LOCKED_GEO_PHRASES
    + E7_IMPACT_PHRASES + E8_NO_EM_PHRASES + E10_DIRECT_ONLY_PHRASES
    + E12_PROP_TRADING_PHRASES
)

# Sanctioned countries (OFAC / MAS)
SANCTIONED_COUNTRIES = {
    "iran", "north korea", "dprk", "myanmar", "burma", "cuba",
    "venezuela", "belarus", "russia", "syria", "sudan",
}

# Client status strings that trigger exclusion
CLIENT_STATUS_BLACKLIST = "rejected - blacklist"
CLIENT_STATUS_CONFLICT  = "rejected - seems to conflict"

# ---------------------------------------------------------------------------
# LP TYPE PRIORITY TIERS
# Source: LP Scoping doc, "LP Type Priority" sheet
# Maps allocator_type → (priority_tier, base_score, decision_speed_score)
# decision_speed: 1.0 = fastest (HNWI weeks), 0.1 = slowest (Pension 18 months)
# ---------------------------------------------------------------------------

LP_TYPE_PRIORITY: dict[str, tuple[str, float, float]] = {
    # (scoping_tier, lp_type_score, decision_speed_score)
    "fund_of_funds":        ("lp_tier_1", 1.00, 0.65),  # $1M-$30M, 3-6 months
    "family_office_multi":  ("lp_tier_1", 0.90, 0.75),  # $500K-$10M, 2-5 months
    "family_office_single": ("lp_tier_2", 0.75, 0.90),  # $250K-$5M, 1-3 months (fast)
    "high_net_worth":       ("lp_tier_2", 0.80, 1.00),  # $100K-$2M, weeks (fastest)
    "angel":                ("lp_tier_2", 0.70, 0.95),  # similar to HNWI
    "asset_manager":        ("lp_tier_3", 0.45, 0.30),  # $2M-$30M, 6-12 months
    "corporate":            ("lp_tier_3", 0.40, 0.30),  # $1M-$20M, 6-12 months
    "pension_fund":         ("lp_tier_4", 0.15, 0.10),  # $10M-$100M+, 12-18 months
    "endowment":            ("lp_tier_4", 0.20, 0.20),  # $5M-$50M, 9-15 months
    "foundation":           ("lp_tier_4", 0.20, 0.20),
    "sovereign_wealth":     ("lp_tier_4", 0.15, 0.10),
    "insurance":            ("lp_tier_3", 0.35, 0.25),
    "bank":                 ("lp_tier_3", 0.35, 0.25),
    "development_finance":  ("lp_tier_4", 0.15, 0.10),
    "unknown":              ("lp_tier_?", 0.35, 0.40),
}

# ---------------------------------------------------------------------------
# SOFT SIGNALS (S1–S7) — weighted conviction boosters
# Source: LP Scoping doc, "Soft Filters" sheet + LP Type Priority sheet
# Weights must sum to 1.0
# ---------------------------------------------------------------------------

# S1: AI Investment Signal (HIGH — 0.4× fit_score per scoping doc)
# Direct portfolio mention > thesis mention > sector generalism
S1_AI_PHRASES = [
    "artificial intelligence", " ai ", "ai,", "ai.", "ai/",
    "machine learning", "robotics", "automation", "deep learning",
    "generative ai", "gen ai", "llm", "computer vision",
    "openai", "anthropic", "cohere", "xai", "gemini",
    "ai/robotics", "ai and robotics", "ai-native",
]
S1_WEIGHT = 0.25

# S2: Emerging Manager Depth (HIGH — 0.3× fit_score per scoping doc)
# Multiple Fund I/II investments, active EM program, or dedicated EM fund
S2_EM_PHRASES = C2_EMERGING_MANAGER_POSITIVE  # reuse C2 keywords
S2_WEIGHT = 0.20

# S3: LP Type Priority (from LP Type Priority sheet)
# Score derived from LP_TYPE_PRIORITY table above
S3_WEIGHT = 0.20

# S4: Decision Speed (derived from LP type — urgency for first close June 2026)
# Score derived from LP_TYPE_PRIORITY table above
S4_WEIGHT = 0.15

# S5: Stage Alignment (MEDIUM — 0.4× fit_score per scoping doc)
S5_STAGE_PHRASES = [
    "pre-seed", "preseed", "seed", "series a", "early stage", "early-stage",
    "venture", "emerging manager", "fund i", "fund 1", "fund one",
]
S5_WEIGHT = 0.10

# S6: No Conflict / Clean Profile (absence of conflict phrases → 1.0)
S6_CONFLICT_PHRASES = (
    E4_WEB3_PHRASES[:4]      # web3 only
    + E1_PE_PHRASES[:3]       # pe focus
    + E5_HEALTHCARE_PHRASES[:3]  # healthcare only
    + E10_DIRECT_ONLY_PHRASES[:3]  # direct only
)
S6_WEIGHT = 0.05

# S7: Proxy Fund Portfolio Overlap
# Has invested in MyAsiaVC peer/proxy funds → strongest thesis alignment signal
S7_PROXY_FUNDS = [
    "neon fund", "better capital", "mana ventures", "afore capital",
    "20vc", "lumikai", "pi ventures", "golden gate ventures",
    "jungle ventures", "gilgamesh ventures", "strive vc",
    "emergent ventures", "better tomorrow ventures",
]
S7_WEIGHT = 0.05

# Verify weights sum to 1.0
_WEIGHT_SUM = S1_WEIGHT + S2_WEIGHT + S3_WEIGHT + S4_WEIGHT + S5_WEIGHT + S6_WEIGHT + S7_WEIGHT
assert abs(_WEIGHT_SUM - 1.0) < 1e-6, f"Signal weights must sum to 1.0, got {_WEIGHT_SUM}"

# ---------------------------------------------------------------------------
# TIER THRESHOLDS — defaults; overridden by prompts/icp_calibration.yaml when present
# ---------------------------------------------------------------------------

DEFAULT_TIER_1_FIT_MIN = 0.60   # core + approved + strong signals
DEFAULT_TIER_2_FIT_MIN = 0.38   # core + moderate signals
# Tier 3 = core pass but weak; Tier 4 = excluded or core fail


def get_tier_thresholds() -> Tuple[float, float]:
    """Load calibrated tier mins from yaml, falling back to hardcoded defaults."""
    if CALIBRATION_YAML.exists():
        try:
            with open(CALIBRATION_YAML, encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            winners = data.get("winning_thresholds") or {}
            t1 = winners.get("TIER_1_FIT_MIN", DEFAULT_TIER_1_FIT_MIN)
            t2 = winners.get("TIER_2_FIT_MIN", DEFAULT_TIER_2_FIT_MIN)
            return float(t1), float(t2)
        except (OSError, yaml.YAMLError, TypeError, ValueError):
            pass
    return DEFAULT_TIER_1_FIT_MIN, DEFAULT_TIER_2_FIT_MIN


TIER_1_FIT_MIN, TIER_2_FIT_MIN = get_tier_thresholds()

# ---------------------------------------------------------------------------
# PROSPECT SHEET COLUMN CONSTANTS
# ---------------------------------------------------------------------------

PROSPECTS_HEADER_ROW = 9
COL_NR              = "Prospects"
COL_NAME            = "Unnamed: 1"
COL_TYPE            = "Unnamed: 2"
COL_WEBSITE         = "Unnamed: 3"
COL_COUNTRY         = "Unnamed: 4"
COL_DATA_SOURCES    = "Unnamed: 5"
COL_SCORING         = "Unnamed: 6"
COL_QA_STATUS       = "Unnamed: 7"
COL_CLIENT_STATUS   = "Unnamed: 8"
COL_CLIENT_COMMENTS = "Unnamed: 9"
COL_MINER_COMMENTS  = "Unnamed: 10"
COL_CONTACT_TITLE   = "Unnamed: 12"
COL_CONTACT_NAME    = "Unnamed: 13"
COL_EMAIL           = "Unnamed: 16"
COL_LINKEDIN        = "Unnamed: 17"

PROSPECT_SHEETS = [
    "Prospects_m1", "Prospects_m2", "Prospects_m3",
    "Prospects_Hong Kong", "Prospects_London",
]

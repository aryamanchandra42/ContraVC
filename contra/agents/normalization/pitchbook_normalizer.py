"""
PitchBook vocabulary → PULSE canonical taxonomy mappings.

Used by both the Selenium scraper (pitchbook_scraper.py) and the manual
XLSX adapter (pitchbook_xlsx_adapter.py).
"""

from __future__ import annotations

import re
from typing import Optional

# ---------------------------------------------------------------------------
# LP / Investor Type
# ---------------------------------------------------------------------------

_PB_LP_TYPE_MAP: dict[str, str] = {
    # Family offices
    "family office":                "family_office_multi",
    "multi-family office":          "family_office_multi",
    "multi family office":          "family_office_multi",
    "single family office":         "family_office_single",
    "single-family office":         "family_office_single",
    # Fund vehicles
    "fund of funds":                "fund_of_funds",
    "fund-of-funds":                "fund_of_funds",
    "fof":                          "fund_of_funds",
    # Institutional
    "endowment":                    "endowment",
    "university endowment":         "endowment",
    "pension fund":                 "pension_fund",
    "pension":                      "pension_fund",
    "public pension":               "pension_fund",
    "corporate pension":            "pension_fund",
    "sovereign wealth fund":        "sovereign_wealth",
    "sovereign wealth":             "sovereign_wealth",
    "swf":                          "sovereign_wealth",
    "foundation":                   "foundation",
    "charitable foundation":        "foundation",
    "private foundation":           "foundation",
    "insurance company":            "insurance",
    "insurance":                    "insurance",
    # Asset managers
    "asset manager":                "asset_manager",
    "investment manager":           "asset_manager",
    "wealth manager":               "asset_manager",
    "registered investment advisor": "asset_manager",
    "ria":                          "asset_manager",
    "bank":                         "bank",
    "commercial bank":              "bank",
    "investment bank":              "bank",
    # Development / government
    "development finance institution": "development_finance",
    "development finance":          "development_finance",
    "dfi":                          "development_finance",
    "government":                   "development_finance",
    # Corporate / strategic
    "corporate":                    "corporate",
    "corporate venture capital":    "corporate",
    "corporate investor":           "corporate",
    "cvc":                          "corporate",
    # Individuals
    "high net worth individual":    "high_net_worth",
    "high net worth":               "high_net_worth",
    "hnwi":                         "high_net_worth",
    "hnw":                          "high_net_worth",
    "angel investor":               "angel",
    "angel":                        "angel",
    "individual":                   "high_net_worth",
}


def normalize_pb_lp_type(raw: Optional[str]) -> str:
    """
    Map a PitchBook 'Investor Type' label to a PULSE canonical allocator_type.
    Returns 'unknown' if no match found.
    """
    if not raw:
        return "unknown"
    key = raw.strip().lower()
    if key in _PB_LP_TYPE_MAP:
        return _PB_LP_TYPE_MAP[key]
    # Partial match fallback
    for pb_label, pulse_type in _PB_LP_TYPE_MAP.items():
        if pb_label in key or key in pb_label:
            return pulse_type
    return "unknown"


# ---------------------------------------------------------------------------
# Fund Type → VC / PE classification
# ---------------------------------------------------------------------------

_VC_FUND_TYPES = {
    "venture capital",
    "vc",
    "early stage vc",
    "late stage vc",
    "seed",
    "micro vc",
    "corporate venture capital",
    "venture lending",
}

_PE_FUND_TYPES = {
    "private equity",
    "pe",
    "buyout",
    "growth equity",
    "growth",
    "mezzanine",
    "secondaries",
    "real estate",
    "real assets",
    "infrastructure",
    "credit",
    "debt",
    "hedge fund",
}


def is_vc_fund(fund_type: Optional[str]) -> bool:
    """True when the PitchBook fund type maps to a VC vehicle (C1 pass evidence)."""
    if not fund_type:
        return False
    key = fund_type.strip().lower()
    return key in _VC_FUND_TYPES or any(vt in key for vt in _VC_FUND_TYPES)


def is_pe_only_fund(fund_type: Optional[str]) -> bool:
    """True when the fund type is clearly PE/buyout (E1 exclusion flag)."""
    if not fund_type:
        return False
    key = fund_type.strip().lower()
    if is_vc_fund(fund_type):
        return False
    return key in _PE_FUND_TYPES or any(pt in key for pt in _PE_FUND_TYPES)


# ---------------------------------------------------------------------------
# Emerging-manager fund heuristic
# ---------------------------------------------------------------------------

_EM_FUND_NAME_PATTERNS = re.compile(
    r"\b(fund\s*(i|ii|iii|1|2|3|one|two|three)|inaugural|debut|first fund|"
    r"fund i\b|fund ii\b|fund iii\b)\b",
    re.IGNORECASE,
)

# Fund size below this threshold is a strong signal of a first/emerging manager
_EM_FUND_SIZE_THRESHOLD_USD = 150_000_000  # $150M

# Vintage years within this many years of today are "recent enough" for Fund I/II
_EM_VINTAGE_LOOKBACK_YEARS = 5


def is_emerging_manager_fund(
    fund_name: Optional[str],
    vintage_year: Optional[int],
    fund_size_usd: Optional[float],
) -> bool:
    """
    Heuristic: True when the fund was likely raised by a first- or second-time manager.

    Criteria (any one of):
    1. Fund name contains "Fund I", "Fund II", "Fund III", "Fund 1", etc.
    2. Vintage year is within the last _EM_VINTAGE_LOOKBACK_YEARS AND fund size < $150M
    3. Fund size < $75M (very small fund regardless of vintage)
    """
    import datetime

    current_year = datetime.date.today().year

    if fund_name and _EM_FUND_NAME_PATTERNS.search(fund_name):
        return True

    if fund_size_usd is not None and fund_size_usd < 75_000_000:
        return True

    if (
        vintage_year is not None
        and fund_size_usd is not None
        and current_year - vintage_year <= _EM_VINTAGE_LOOKBACK_YEARS
        and fund_size_usd < _EM_FUND_SIZE_THRESHOLD_USD
    ):
        return True

    return False


# ---------------------------------------------------------------------------
# Geography normalizer (PitchBook region labels → PULSE geography enum)
# ---------------------------------------------------------------------------

_PB_GEO_MAP: dict[str, str] = {
    "southeast asia":           "southeast_asia",
    "sea":                      "southeast_asia",
    "south asia":               "south_asia",
    "india":                    "south_asia",
    "east asia":                "east_asia",
    "asia pacific":             "asia_pacific",
    "apac":                     "asia_pacific",
    "asia":                     "asia_pacific",
    "middle east":              "middle_east",
    "mena":                     "middle_east",
    "gcc":                      "middle_east",
    "north america":            "north_america",
    "united states":            "north_america",
    "usa":                      "north_america",
    "us":                       "north_america",
    "canada":                   "north_america",
    "europe":                   "europe",
    "africa":                   "africa",
    "sub-saharan africa":       "africa",
    "latin america":            "latin_america",
    "latam":                    "latin_america",
    "global":                   "global",
    "worldwide":                "global",
    "international":            "global",
    "emerging markets":         "emerging_markets",
}


def normalize_pb_geography(raw: Optional[str]) -> Optional[str]:
    """Map a PitchBook geography string to a PULSE canonical geography value."""
    if not raw:
        return None
    key = raw.strip().lower()
    if key in _PB_GEO_MAP:
        return _PB_GEO_MAP[key]
    for pb_label, pulse_geo in _PB_GEO_MAP.items():
        if pb_label in key:
            return pulse_geo
    return None

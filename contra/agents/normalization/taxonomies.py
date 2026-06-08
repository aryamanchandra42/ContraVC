"""
PULSE canonical taxonomies.

Hand-curated enums derived from the substrate audit of the 6 source files.
Version-pinned: changing a value here is a schema-migration event; document in decision_archive.md.
"""

from __future__ import annotations

from enum import Enum


class AllocatorType(str, Enum):
    PENSION_FUND = "pension_fund"
    SOVEREIGN_WEALTH = "sovereign_wealth"
    ENDOWMENT = "endowment"
    FOUNDATION = "foundation"
    FAMILY_OFFICE_SINGLE = "family_office_single"
    FAMILY_OFFICE_MULTI = "family_office_multi"
    FUND_OF_FUNDS = "fund_of_funds"
    INSURANCE = "insurance"
    BANK = "bank"
    ASSET_MANAGER = "asset_manager"
    DEVELOPMENT_FINANCE = "development_finance"
    CORPORATE = "corporate"
    HIGH_NET_WORTH = "high_net_worth"
    ANGEL = "angel"
    UNKNOWN = "unknown"


# Common synonyms / patterns for fuzzy taxonomy mapping
ALLOCATOR_TYPE_PATTERNS: dict[str, list[str]] = {
    AllocatorType.PENSION_FUND: ["pension", "superannuation", "retirement fund", "provident fund"],
    AllocatorType.SOVEREIGN_WEALTH: ["sovereign wealth", "swf", "government investment", "national wealth"],
    AllocatorType.ENDOWMENT: ["endowment", "university fund", "college fund"],
    AllocatorType.FOUNDATION: ["foundation", "charitable", "philanthropic"],
    AllocatorType.FAMILY_OFFICE_SINGLE: ["single family office", "SFO", "family office"],
    AllocatorType.FAMILY_OFFICE_MULTI: ["multi family office", "MFO", "multi-family"],
    AllocatorType.FUND_OF_FUNDS: ["fund of funds", "FoF", "fund-of-funds", "FOF"],
    AllocatorType.INSURANCE: ["insurance", "insurer", "life insurance", "reinsurance"],
    AllocatorType.BANK: ["bank", "banking", "commercial bank", "investment bank"],
    AllocatorType.ASSET_MANAGER: ["asset manager", "asset management", "investment manager"],
    AllocatorType.DEVELOPMENT_FINANCE: ["DFI", "development finance", "IFC", "ADB", "development bank"],
    AllocatorType.CORPORATE: ["corporate", "company", "conglomerate"],
    AllocatorType.HIGH_NET_WORTH: ["HNWI", "high net worth", "ultra high net worth", "UHNWI"],
}


class Geography(str, Enum):
    SOUTHEAST_ASIA = "southeast_asia"
    SOUTH_ASIA = "south_asia"
    EAST_ASIA = "east_asia"
    ASIA_PACIFIC = "asia_pacific"
    MIDDLE_EAST = "middle_east"
    AFRICA = "africa"
    NORTH_AMERICA = "north_america"
    EUROPE = "europe"
    LATIN_AMERICA = "latin_america"
    GLOBAL = "global"
    EMERGING_MARKETS = "emerging_markets"
    UNKNOWN = "unknown"


GEOGRAPHY_PATTERNS: dict[str, list[str]] = {
    Geography.SOUTHEAST_ASIA: ["SEA", "Southeast Asia", "ASEAN", "Singapore", "Indonesia", "Vietnam", "Thailand", "Philippines", "Malaysia"],
    Geography.SOUTH_ASIA: ["India", "South Asia", "SAARC", "Bangladesh", "Pakistan", "Sri Lanka"],
    Geography.EAST_ASIA: ["China", "Japan", "Korea", "Taiwan", "Hong Kong", "East Asia"],
    Geography.ASIA_PACIFIC: ["APAC", "Asia Pacific", "Asia ex-Japan", "Asia"],
    Geography.MIDDLE_EAST: ["Middle East", "GCC", "UAE", "Saudi", "Kuwait", "Qatar", "Bahrain"],
    Geography.AFRICA: ["Africa", "Sub-Saharan", "SSA", "Nigeria", "Kenya", "South Africa"],
    Geography.NORTH_AMERICA: ["US", "USA", "United States", "Canada", "North America"],
    Geography.EUROPE: ["Europe", "EU", "UK", "United Kingdom", "Germany", "France", "Nordics"],
    Geography.LATIN_AMERICA: ["LATAM", "Latin America", "Brazil", "Mexico", "Colombia"],
    Geography.GLOBAL: ["global", "worldwide", "international"],
    Geography.EMERGING_MARKETS: ["EM", "emerging market", "emerging markets", "frontier"],
}


class CheckSizeBucket(str, Enum):
    BELOW_1M = "below_1m"
    ONE_TO_5M = "1m_to_5m"
    FIVE_TO_25M = "5m_to_25m"
    TWENTY_FIVE_TO_100M = "25m_to_100m"
    ONE_HUNDRED_TO_500M = "100m_to_500m"
    ABOVE_500M = "above_500m"
    UNKNOWN = "unknown"


def classify_check_size(usd_amount: float | None) -> str:
    """Classify a USD check size into a canonical bucket."""
    if usd_amount is None:
        return CheckSizeBucket.UNKNOWN
    m = usd_amount / 1_000_000
    if m < 1:
        return CheckSizeBucket.BELOW_1M
    elif m < 5:
        return CheckSizeBucket.ONE_TO_5M
    elif m < 25:
        return CheckSizeBucket.FIVE_TO_25M
    elif m < 100:
        return CheckSizeBucket.TWENTY_FIVE_TO_100M
    elif m < 500:
        return CheckSizeBucket.ONE_HUNDRED_TO_500M
    else:
        return CheckSizeBucket.ABOVE_500M


class StagePreference(str, Enum):
    SEED = "seed"
    SERIES_A = "series_a"
    SERIES_B = "series_b"
    GROWTH = "growth"
    LATE = "late"
    BUYOUT = "buyout"
    MULTI_STAGE = "multi_stage"
    FUND_LEVEL = "fund_level"
    UNKNOWN = "unknown"


class Appetite(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    NONE = "none"
    UNKNOWN = "unknown"


class Flexibility(str, Enum):
    HIGH = "high"           # can move fast, few committee layers
    MEDIUM = "medium"
    LOW = "low"             # rigid mandate, multiple committee sign-offs
    UNKNOWN = "unknown"


class ProgressionStage(str, Enum):
    COLD = "cold"
    INITIAL_OUTREACH = "initial_outreach"
    FIRST_MEETING = "first_meeting"
    FOLLOW_UP = "follow_up"
    DUE_DILIGENCE = "due_diligence"
    TERM_SHEET = "term_sheet"
    COMMITTED = "committed"
    REJECTED = "rejected"
    DORMANT = "dormant"


def normalize_allocator_type(raw: str | None) -> str:
    """Map a raw string to a canonical AllocatorType. Returns 'unknown' if unmatched."""
    if not raw:
        return AllocatorType.UNKNOWN
    raw_lower = raw.lower().strip()
    for atype, patterns in ALLOCATOR_TYPE_PATTERNS.items():
        for pat in patterns:
            if pat.lower() in raw_lower:
                return atype
    return AllocatorType.UNKNOWN


# Additional patterns keyed specifically on LP Type Priority taxonomy labels
# (exact column values from the LP Scoping xlsx)
_LP_TYPE_PRIORITY_MAP: dict[str, str] = {
    "fund of funds": AllocatorType.FUND_OF_FUNDS,
    "multi-family office": AllocatorType.FAMILY_OFFICE_MULTI,
    "multi family office": AllocatorType.FAMILY_OFFICE_MULTI,
    "family office": AllocatorType.FAMILY_OFFICE_SINGLE,
    "single family office": AllocatorType.FAMILY_OFFICE_SINGLE,
    "hnwi": AllocatorType.HIGH_NET_WORTH,
    "high net worth": AllocatorType.HIGH_NET_WORTH,
    "asset manager": AllocatorType.ASSET_MANAGER,
    "investment manager": AllocatorType.ASSET_MANAGER,
    "corporate venture capital": AllocatorType.CORPORATE,
    "cvc": AllocatorType.CORPORATE,
    "pension fund": AllocatorType.PENSION_FUND,
    "pension": AllocatorType.PENSION_FUND,
    "endowment": AllocatorType.ENDOWMENT,
    "sovereign wealth": AllocatorType.SOVEREIGN_WEALTH,
    "bank": AllocatorType.BANK,
    "insurance": AllocatorType.INSURANCE,
    "foundation": AllocatorType.FOUNDATION,
    "development finance": AllocatorType.DEVELOPMENT_FINANCE,
}


def normalize_lp_type_label(raw: str | None) -> str:
    """
    Map LP Type Priority taxonomy labels directly to AllocatorType.
    More precise than normalize_allocator_type — use when source is a
    structured 'Investor Type' or 'LP Type' column.
    """
    if not raw:
        return AllocatorType.UNKNOWN
    raw_lower = raw.lower().strip()
    for label, atype in _LP_TYPE_PRIORITY_MAP.items():
        if label in raw_lower:
            return atype
    # Fall back to the general mapper
    return normalize_allocator_type(raw)


def infer_type_from_name(name: str | None) -> str:
    """
    Heuristic type inference from the canonical entity name itself.
    Used as a last-resort fallback when no explicit 'type' column is available.
    """
    if not name:
        return AllocatorType.UNKNOWN
    name_lower = name.lower()

    if "single family office" in name_lower or "single-family office" in name_lower:
        return AllocatorType.FAMILY_OFFICE_SINGLE
    if "multi family office" in name_lower or "multi-family office" in name_lower:
        return AllocatorType.FAMILY_OFFICE_MULTI
    if "family office" in name_lower:
        return AllocatorType.FAMILY_OFFICE_SINGLE
    if "fund of funds" in name_lower or "fund-of-funds" in name_lower:
        return AllocatorType.FUND_OF_FUNDS
    if "pension" in name_lower:
        return AllocatorType.PENSION_FUND
    if "endowment" in name_lower:
        return AllocatorType.ENDOWMENT
    if "foundation" in name_lower:
        return AllocatorType.FOUNDATION
    if "sovereign wealth" in name_lower:
        return AllocatorType.SOVEREIGN_WEALTH
    if any(w in name_lower for w in ["insurance", "assurance", "life assurance"]):
        return AllocatorType.INSURANCE
    if any(w in name_lower for w in ["hnwi", "high net worth", "ultra high net worth"]):
        return AllocatorType.HIGH_NET_WORTH
    if any(w in name_lower for w in [
        " holdings", "holdings ", "group ", " group", "conglomerate",
        " corp", " corporation", "enterprises",
    ]):
        return AllocatorType.CORPORATE
    # "Equities", "Securities", "Asset Management", "Capital Management" → asset manager
    if any(w in name_lower for w in [
        "asset management", "capital management", "investment management",
        " equities", " securities",
    ]):
        return AllocatorType.ASSET_MANAGER
    return AllocatorType.UNKNOWN


def normalize_geography(raw: str | None) -> str:
    """Map a raw geography string to a canonical Geography."""
    if not raw:
        return Geography.UNKNOWN
    raw_lower = raw.lower()
    for geo, patterns in GEOGRAPHY_PATTERNS.items():
        for pat in patterns:
            if pat.lower() in raw_lower:
                return geo
    return Geography.UNKNOWN


def parse_usd(raw: str | None) -> float | None:
    """Parse a USD string like '$5M', '5,000,000', '5m' to float."""
    if not raw:
        return None
    raw = str(raw).strip().replace(",", "").replace("$", "").replace(" ", "")
    multiplier = 1.0
    if raw.upper().endswith("M"):
        multiplier = 1_000_000
        raw = raw[:-1]
    elif raw.upper().endswith("B"):
        multiplier = 1_000_000_000
        raw = raw[:-1]
    elif raw.upper().endswith("K"):
        multiplier = 1_000
        raw = raw[:-1]
    try:
        return float(raw) * multiplier
    except ValueError:
        return None

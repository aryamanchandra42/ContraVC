"""
Fund normalizer — creates fund records from raw data.

Two sources:
  1. Contra VC (MyAsiaVC AI & Robotics Fund I) — the fund being raised.
     Derived from AI_Native_VC_Fund_Strategy.docx + LP Side Plan Draft 1.pdf.
  2. Proxy Funds — the 17 peer/benchmark funds listed in the
     'Proxy Funds/Companies' sheet of the ICP Prospect List.
     These are funds LPs have backed; LP→ProxyFund investment history
     is the strongest thesis alignment signal (S8 in LP Scoping doc).

Run: `pulse normalize` (called automatically in the normalize stage).
"""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Dict, Optional

from agents.normalization.taxonomies import normalize_geography, parse_usd


# ---------------------------------------------------------------------------
# Contra VC (the fund being raised) — derived from source docs
# ---------------------------------------------------------------------------

CONTRA_VC_FUND = {
    "canonical_name":   "Contra VC - AI & Robotics Fund I",
    "aliases":          ["MyAsiaVC AI & Robotics Fund I", "Contra VC", "MyAsiaVC Fund I"],
    "fund_type":        "venture_capital",
    "manager_name":     "MyAsiaVC / Contra VC",
    "vintage_year":     2026,
    "geography_focus":  "asia_pacific",   # primary; also NA and ME
    "strategy":         "AI-native VC, Pre-seed to Series A, Asia + North America + Middle East",
    "target_size_usd":  30_000_000,
    "source_file":      "AI_Native_VC_Fund_Strategy.docx",
}

# ---------------------------------------------------------------------------
# Proxy funds — from 'Proxy Funds/Companies' sheet of ICP 4.0 file.
# These are peer/benchmark funds used for LP prospecting.
# Geography enriched from public knowledge of each fund.
# ---------------------------------------------------------------------------

PROXY_FUNDS: list[Dict] = [
    {"canonical_name": "Neon Fund",           "geography_focus": "emerging_markets", "fund_type": "venture_capital"},
    {"canonical_name": "Better Capital",      "geography_focus": "south_asia",        "fund_type": "venture_capital"},
    {"canonical_name": "Mana Ventures",       "geography_focus": "global",             "fund_type": "venture_capital"},
    {"canonical_name": "Afore Capital",       "geography_focus": "north_america",      "fund_type": "venture_capital"},
    {"canonical_name": "20VC",                "geography_focus": "global",             "fund_type": "venture_capital"},
    {"canonical_name": "Operator Studio",     "geography_focus": "global",             "fund_type": "venture_capital"},
    {"canonical_name": "Anti Fund",           "geography_focus": "north_america",      "fund_type": "venture_capital"},
    {"canonical_name": "Baobab Ventures",     "geography_focus": "emerging_markets",   "fund_type": "venture_capital"},
    {"canonical_name": "Verb Ventures",       "geography_focus": "global",             "fund_type": "venture_capital"},
    {"canonical_name": "Firebrand Ventures",  "geography_focus": "north_america",      "fund_type": "venture_capital"},
    {"canonical_name": "Lumikai",             "geography_focus": "south_asia",         "fund_type": "venture_capital"},
    {"canonical_name": "Gilgamesh Ventures",  "geography_focus": "global",             "fund_type": "venture_capital"},
    {"canonical_name": "Origgin",             "geography_focus": "emerging_markets",   "fund_type": "venture_capital"},
    {"canonical_name": "Magic Fund",          "geography_focus": "global",             "fund_type": "venture_capital"},
    {"canonical_name": "Pi Ventures",         "geography_focus": "south_asia",         "fund_type": "venture_capital"},
    {"canonical_name": "Golden Gate Ventures","geography_focus": "southeast_asia",     "fund_type": "venture_capital"},
    {"canonical_name": "Jungle Ventures",     "geography_focus": "southeast_asia",     "fund_type": "venture_capital"},
]


def _stable_hash(name: str) -> str:
    return hashlib.sha256(f"fund:{name}".encode()).hexdigest()


def upsert_fund(
    canonical_name: str,
    source_record_id: str,
    source_file: str,
    content_hash: str,
    con,
    fund_type: Optional[str] = None,
    manager_name: Optional[str] = None,
    vintage_year: Optional[int] = None,
    geography_focus: Optional[str] = None,
    strategy: Optional[str] = None,
    target_size_usd: Optional[float] = None,
    aliases: Optional[list] = None,
) -> str:
    """Upsert a fund record. Returns fund_id as string."""
    existing = con.execute(
        "SELECT CAST(fund_id AS VARCHAR) FROM funds WHERE canonical_name = ?",
        [canonical_name],
    ).fetchone()
    if existing:
        return str(existing[0])

    fund_id = str(uuid.uuid4())
    con.execute(
        """
        INSERT INTO funds (
            fund_id, canonical_name, aliases, fund_type, manager_name,
            vintage_year, geography_focus, strategy, target_size_usd,
            source_record_id, source_file, content_hash
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            fund_id,
            canonical_name,
            json.dumps(aliases or []),
            fund_type,
            manager_name,
            vintage_year,
            geography_focus,
            strategy,
            target_size_usd,
            source_record_id,
            source_file,
            content_hash,
        ],
    )
    return fund_id


def ingest_fund_rows(con) -> int:
    """
    Create fund records from two sources:
      1. Contra VC (the fund being raised) — from known constants.
      2. Proxy funds — from 'Proxy Funds/Companies' sheet in ICP file.

    Idempotent: upsert_fund skips existing canonical names.
    Returns count of new fund records created.
    """
    created = 0

    # --- 1. Contra VC (self-fund) ---
    f = CONTRA_VC_FUND
    # Use the strategy doc source_record_id if available
    strategy_row = con.execute(
        "SELECT source_record_id, content_hash FROM entities_raw "
        "WHERE source_file LIKE '%Strategy%' LIMIT 1"
    ).fetchone()
    src_id  = strategy_row[0] if strategy_row else _stable_hash(f["canonical_name"])
    src_ch  = strategy_row[1] if strategy_row else _stable_hash(f["canonical_name"])

    prev = con.execute(
        "SELECT COUNT(*) FROM funds WHERE canonical_name = ?", [f["canonical_name"]]
    ).fetchone()[0]
    upsert_fund(
        canonical_name  = f["canonical_name"],
        source_record_id= src_id,
        source_file     = f["source_file"],
        content_hash    = src_ch,
        con             = con,
        fund_type       = f["fund_type"],
        manager_name    = f["manager_name"],
        vintage_year    = f["vintage_year"],
        geography_focus = f["geography_focus"],
        strategy        = f["strategy"],
        target_size_usd = f["target_size_usd"],
        aliases         = f["aliases"],
    )
    if con.execute(
        "SELECT COUNT(*) FROM funds WHERE canonical_name = ?", [f["canonical_name"]]
    ).fetchone()[0] > prev:
        created += 1

    # --- 2. Proxy funds from ICP Prospect List ---
    proxy_rows = con.execute(
        """
        SELECT source_record_id, source_file, content_hash, raw_content
        FROM entities_raw
        WHERE source_file LIKE '%ICP%'
          AND json_extract_string(raw_content, '$._sheet') LIKE '%Proxy%'
          AND json_extract_string(raw_content, '$."Proxy Funds/Companies"') IS NOT NULL
          AND json_extract_string(raw_content, '$."Proxy Funds/Companies"') NOT IN
              ('', 'Name', 'all AI fund that are below $250M', 'all Asia focused fund')
        """
    ).fetchall()

    # Build a lookup of known proxy fund data by canonical name
    proxy_lookup = {p["canonical_name"].lower(): p for p in PROXY_FUNDS}
    # Also partial matches (e.g. "Firebrand" → "Firebrand Ventures")
    for p in PROXY_FUNDS:
        short = p["canonical_name"].split()[0].lower()
        if short not in proxy_lookup:
            proxy_lookup[short] = p

    for src_id, src_file, src_ch, raw_content in proxy_rows:
        if isinstance(raw_content, str):
            raw_content = json.loads(raw_content)
        raw_name = raw_content.get("Proxy Funds/Companies", "").strip()
        if not raw_name:
            continue

        # Look up enriched metadata if we have it
        known = proxy_lookup.get(raw_name.lower()) or proxy_lookup.get(raw_name.split()[0].lower())
        geo   = known["geography_focus"] if known else "global"
        ftype = known["fund_type"] if known else "venture_capital"
        canon = known["canonical_name"] if known else raw_name

        prev = con.execute(
            "SELECT COUNT(*) FROM funds WHERE canonical_name = ?", [canon]
        ).fetchone()[0]
        upsert_fund(
            canonical_name  = canon,
            source_record_id= src_id,
            source_file     = src_file,
            content_hash    = src_ch,
            con             = con,
            fund_type       = ftype,
            geography_focus = geo,
        )
        if con.execute(
            "SELECT COUNT(*) FROM funds WHERE canonical_name = ?", [canon]
        ).fetchone()[0] > prev:
            created += 1

    return created

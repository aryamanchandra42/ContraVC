"""
Prospector seed registry.

A seed is something worth searching FROM:
  peer_fund      — a fund whose disclosed LPs are near-perfect ICP matches
                   (someone who backed Neon Fund I will recognise this pitch)
  confirmed_lp   — a known LP whose co-investors / co-LPs are good candidates
  query_template — a standing search pattern ({geo} is substituted)

Seeds rotate by last_mined_at so consecutive runs cover different ground.
Every confirmed LP found by the agent becomes a new confirmed_lp seed, and
every peer fund named in verified evidence becomes a new peer_fund seed —
this is how the agent keeps finding NEW leads instead of re-finding old ones.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Peer/proxy funds from the ICP spec (S7) — LPs of these funds are the
# strongest thesis-alignment candidates we can find on the open web.
DEFAULT_PEER_FUNDS = [
    "Neon Fund",
    "Better Capital",
    "Mana Ventures",
    "Afore Capital",
    "Lumikai",
    "Pi Ventures",
    "Golden Gate Ventures",
    "Jungle Ventures",
    "Gilgamesh Ventures",
    "Emergent Ventures",
    "Better Tomorrow Ventures",
    "Hustle Fund",
    "iSeed SEA",
]

DEFAULT_GEOGRAPHIES = ["Southeast Asia", "Singapore", "India", "Middle East", "Hong Kong"]

# {geo} is substituted with a rotating geography.
DEFAULT_QUERY_TEMPLATES = [
    '{geo} family office "limited partner" venture fund commitment',
    '{geo} venture "fund I" close anchor LP announcement',
    '{geo} fund of funds emerging manager program venture',
    '{geo} "backed the fund" OR "committed to the fund" venture capital LP',
    'first-time fund manager {geo} anchor investor family office',
]


def ensure_default_seeds(con) -> int:
    """Insert default seeds if missing. Returns number inserted."""
    inserted = 0
    rows: List[tuple] = []
    for fund in DEFAULT_PEER_FUNDS:
        rows.append(("peer_fund", fund, None))
    for tpl in DEFAULT_QUERY_TEMPLATES:
        rows.append(("query_template", tpl, None))
    for seed_type, value, geo in rows:
        try:
            existing = con.execute(
                "SELECT 1 FROM prospector_seeds WHERE seed_type = ? AND value = ?",
                [seed_type, value],
            ).fetchone()
            if existing:
                continue
            con.execute(
                "INSERT INTO prospector_seeds (seed_type, value, geography, origin) "
                "VALUES (?, ?, ?, 'default')",
                [seed_type, value, geo],
            )
            inserted += 1
        except Exception as exc:
            logger.debug("Seed insert skipped (%s %s): %s", seed_type, value, exc)
    return inserted


def pick_seeds(con, limit: int = 6) -> List[Dict[str, Any]]:
    """Least-recently-mined enabled seeds first (never-mined win)."""
    rows = con.execute(
        """
        SELECT CAST(seed_id AS VARCHAR), seed_type, value, geography
        FROM prospector_seeds
        WHERE enabled
        ORDER BY last_mined_at ASC NULLS FIRST, created_at ASC
        LIMIT ?
        """,
        [limit],
    ).fetchall()
    return [
        {"seed_id": r[0], "seed_type": r[1], "value": r[2], "geography": r[3]}
        for r in rows
    ]


def mark_seeds_mined(con, seed_ids: List[str]) -> None:
    for sid in seed_ids:
        con.execute(
            "UPDATE prospector_seeds SET last_mined_at = NOW() WHERE CAST(seed_id AS VARCHAR) = ?",
            [sid],
        )


def add_seed(
    con,
    seed_type: str,
    value: str,
    *,
    geography: Optional[str] = None,
    origin: str = "expansion",
) -> bool:
    """Insert a seed if new. Returns True when inserted."""
    value = (value or "").strip()
    if not value or len(value) < 3:
        return False
    existing = con.execute(
        "SELECT 1 FROM prospector_seeds WHERE seed_type = ? AND LOWER(value) = LOWER(?)",
        [seed_type, value],
    ).fetchone()
    if existing:
        return False
    con.execute(
        "INSERT INTO prospector_seeds (seed_type, value, geography, origin) VALUES (?, ?, ?, ?)",
        [seed_type, value, geography, origin],
    )
    return True


def queries_for_seed(seed: Dict[str, Any], geographies: Optional[List[str]] = None) -> List[str]:
    """Turn one seed into 2-3 concrete search queries."""
    geos = geographies or DEFAULT_GEOGRAPHIES
    stype, value = seed["seed_type"], seed["value"]
    if stype == "peer_fund":
        return [
            f'"{value}" limited partners fund close',
            f'"{value}" anchor investor LP backed',
        ]
    if stype == "confirmed_lp":
        return [
            f'"{value}" limited partner venture fund also backed',
            f'"{value}" co-investor family office fund commitment',
        ]
    if stype == "query_template":
        geo = seed.get("geography") or geos[hash(value) % len(geos)]
        return [value.replace("{geo}", geo)]
    return []

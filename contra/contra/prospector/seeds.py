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
import zlib
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

DEFAULT_GEOGRAPHIES = [
    "Southeast Asia", "Singapore", "India", "Indonesia",
    "Middle East", "UAE", "Hong Kong", "Japan",
]

# {geo} is substituted with a rotating geography.
#
# These target DOCUMENTS that name many LPs at once — fund-close announcements and
# emerging-manager programme pages — rather than asking about one LP at a time. A
# single close announcement typically names 3-10 LPs, so the yield per search is an
# order of magnitude better than per-entity querying.
#
# Cheque size is the constraint that does the real work here. Unqualified LP queries
# return the largest institutions on the web — $225B public pensions, $17B
# fund-of-funds — whose minimum commitment is bigger than this entire fund, so they
# are unreachable no matter how well they gate. Anchoring on small closes and
# first-time managers surfaces LPs whose cheque actually fits.
#
# Deliberately absent: directory and "investors list" queries. Those rank listicles,
# which the harvest stage now penalises, so generating them wastes a search slot.
DEFAULT_QUERY_TEMPLATES = [
    # Small-fund close announcements — the densest source of reachable LP names.
    '{geo} venture fund "first close" "limited partners include" family office',
    '{geo} "$20 million" OR "$30 million" OR "$50 million" venture fund close anchor LP',
    '{geo} micro VC OR "pre-seed fund" close "backed by" family offices angel investors',
    'first-time fund manager {geo} anchor investor family office commitment',
    '{geo} venture fund announces close "with participation from" investors',
    # Mid-size institutions and fund-of-funds that allocate to first-time managers.
    '{geo} "emerging manager program" venture capital allocation first-time fund',
    '{geo} fund of funds "emerging managers" venture fund commitment',
    # Single family offices and operator-LPs, who appear in no LP database.
    '{geo} entrepreneur family office "limited partner" venture funds interview',
]


def retire_stale_default_templates(con) -> int:
    """
    Disable default query_template seeds that are no longer in DEFAULT_QUERY_TEMPLATES.

    Without this, retargeting the templates has no effect on a database that has
    already been seeded: `ensure_default_seeds` only ever inserts, so the retired
    queries stay enabled and keep winning search slots on last_mined_at ordering.
    Disabled rather than deleted so per-template yield history survives. Seeds added
    by hand or by expansion (origin != 'default') are never touched.
    """
    try:
        placeholders = ", ".join("?" for _ in DEFAULT_QUERY_TEMPLATES)
        result = con.execute(
            f"""
            UPDATE prospector_seeds SET enabled = FALSE
            WHERE seed_type = 'query_template' AND origin = 'default'
              AND enabled AND value NOT IN ({placeholders})
            """,
            list(DEFAULT_QUERY_TEMPLATES),
        )
        retired = result.fetchone()
        count = int(retired[0]) if retired else 0
        if count:
            logger.info("Retired %d stale default query templates", count)
        return count
    except Exception as exc:
        logger.warning("Could not retire stale default templates: %s", exc)
        return 0


def ensure_default_seeds(con) -> int:
    """Insert default seeds if missing. Returns number inserted."""
    retire_stale_default_templates(con)
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


def pick_seeds(con, limit: int = 6, *, min_templates: int = 2) -> List[Dict[str, Any]]:
    """
    Least-recently-mined enabled seeds first (never-mined win), but reserve
    `min_templates` slots for query_template seeds.

    Without the reservation, peer_fund seeds monopolise the budget forever: there
    are 13 of them against 5 templates, they were inserted first, and the
    ordering is purely by last_mined_at — so the templates had never been mined
    once. Templates are the only seed type that discovers LPs outside the orbit
    of funds we already know about, which makes them the seeds that matter most
    for finding genuinely new names.
    """
    order = "ORDER BY last_mined_at ASC NULLS FIRST, created_at ASC"

    def _fetch(where: str, lim: int) -> List[Dict[str, Any]]:
        if lim <= 0:
            return []
        rows = con.execute(
            f"""
            SELECT CAST(seed_id AS VARCHAR), seed_type, value, geography
            FROM prospector_seeds
            WHERE enabled {where}
            {order}
            LIMIT ?
            """,
            [lim],
        ).fetchall()
        return [
            {"seed_id": r[0], "seed_type": r[1], "value": r[2], "geography": r[3]}
            for r in rows
        ]

    reserved = min(min_templates, max(0, limit - 1))
    templates = _fetch("AND seed_type = 'query_template'", reserved)
    rest = _fetch("AND seed_type != 'query_template'", limit - len(templates))

    picked = templates + rest
    if len(picked) < limit:  # not enough non-template seeds — backfill with templates
        have = {s["seed_id"] for s in picked}
        for seed in _fetch("AND seed_type = 'query_template'", limit):
            if seed["seed_id"] not in have and len(picked) < limit:
                picked.append(seed)
    return picked[:limit]


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


def geo_rotation(con) -> int:
    """
    Rotation offset for template geographies, advancing once per run.

    Keyed on the number of runs recorded so far, so successive runs point the same
    template at a different geography.
    """
    try:
        row = con.execute("SELECT COUNT(*) FROM prospector_runs").fetchone()
        return int(row[0]) if row else 0
    except Exception:
        return 0


def queries_for_seed(
    seed: Dict[str, Any],
    geographies: Optional[List[str]] = None,
    *,
    rotation: int = 0,
) -> List[str]:
    """Turn one seed into 2-3 concrete search queries."""
    geos = geographies or DEFAULT_GEOGRAPHIES
    stype, value = seed["seed_type"], seed["value"]
    if stype == "peer_fund":
        # Both queries demand LP-DISCLOSING language. Without it, a peer-fund query
        # returns coverage of the fund's own fundraise — "Better Tomorrow Ventures
        # Raises $140M" from TechCrunch, Yahoo and PitchBook — which announces the
        # size and the thesis but names no LP. A measured run spent three of five
        # query slots on those articles and harvested nothing from them.
        return [
            f'"{value}" "limited partners include" OR "investors include"',
            f'"{value}" fund "anchor investor" OR "backed by" family office LP',
        ]
    if stype == "confirmed_lp":
        return [
            f'"{value}" limited partner venture fund also backed',
            f'"{value}" co-investor family office fund commitment',
        ]
    if stype == "query_template":
        # crc32, not hash(): Python randomizes string hashing per process, so
        # hash() picked a different geography on every restart and no run was
        # reproducible — which makes measuring per-template yield meaningless.
        #
        # `rotation` advances once per run. Keyed on the template text alone, each
        # template was pinned to one geography forever, so the miner could only ever
        # reach len(templates) geography pairs no matter how often it ran — a live run
        # returned nothing but Japanese and Korean names for exactly this reason. The
        # offset keeps templates spread across different geos within a run while
        # moving all of them to fresh ground on the next one.
        idx = (zlib.crc32(value.encode()) + rotation) % len(geos)
        geo = seed.get("geography") or geos[idx]
        return [value.replace("{geo}", geo)]
    return []

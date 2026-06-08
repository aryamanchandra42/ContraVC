"""
Syndicate normalizer — integrates the AngelList syndicate datasets into PULSE.

Handles two raw files that are structurally different from the institutional ICP
prospect list (clean headers at row 0, individual-angel population, real transactions):

  1. "Syndicate LPs - MyAsiaVC - ...xlsx"
       - sheet "Syndicate LPs"   → ~5.9k LP roster   → allocators (population='syndicate_lp')
       - sheet "LP investments"  → ~16.8k LP→deal txns → funds + investments table
  2. "ContraVC_Top200_LP_Outreach copy.xlsx"
       - sheet "Top 200 LP Rankings" → external benchmark → benchmark_rankings

These LPs are deliberately kept OUT of the generic fuzzy resolver and the
column/scoring-text enrichment passes (which are O(n) per-name table scans and would
not scale to thousands of individuals). Instead they are resolved here by exact /
cleaned-name matching, which also captures overlap with institutional prospects.

The co-investment graph (`co_invested` edges) is derived from shared deal participation:
two LPs who both backed the same SPV/fund get an edge, gated by a minimum number of
shared deals to keep the graph signal-rich rather than dense noise.

Idempotent: each step clears its own prior output (scoped by source_file / edge_type)
before rewriting.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections import Counter, defaultdict
from datetime import datetime, timezone
from itertools import combinations
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from agents.normalization.fund_normalizer import upsert_fund
from agents.normalization.taxonomies import (
    infer_type_from_name, parse_usd, classify_check_size, AllocatorType,
)


def _bulk_insert(con, table: str, columns: List[str], rows: List[tuple]) -> int:
    """
    Bulk-insert rows via a registered DataFrame. DuckDB is columnar: row-by-row
    INSERT/executemany over tens of thousands of rows is pathologically slow
    (minutes), while a single INSERT...SELECT from a DataFrame is seconds.
    NaN (from None in numeric columns) is coerced back to NULL.
    """
    if not rows:
        return 0
    df = pd.DataFrame(rows).astype(object)
    df.columns = columns
    df = df.where(pd.notnull(df), None)
    con.register("_bulk_df", df)
    collist = ", ".join(columns)
    con.execute(f"INSERT INTO {table} ({collist}) SELECT {collist} FROM _bulk_df")
    con.unregister("_bulk_df")
    return len(rows)

# Source-file match patterns (files live at raw_data/ root → source_file == filename)
SYNDICATE_FILE_LIKE = "%Syndicate LPs%"
CONTRA_FILE_LIKE = "%ContraVC%"

# Co-investment edge: minimum number of shared deals before two LPs get an edge.
# All-pairs co-presence is ~485k pairs (noise); >=3 shared deals keeps ~28k high-signal edges.
MIN_SHARED_DEALS = 3
# Cap evidence rows per edge to bound the relationship_evidence table.
EVIDENCE_PER_EDGE_CAP = 2
# Evidence self-reported certainty for a single shared-deal observation.
CO_INVEST_EVIDENCE_STRENGTH = 0.6

_LEGAL_SUFFIX_RE = re.compile(
    r"\s+(ltd|limited|llc|inc|corp|plc|pte|sa|bv|gmbh|lp|llp|co)\.?$", re.IGNORECASE
)


# ---------------------------------------------------------------------------
# Name helpers
# ---------------------------------------------------------------------------

def _norm_key(name: str) -> str:
    """Lowercased, suffix-stripped key for exact/cleaned name matching."""
    key = (name or "").strip().lower()
    key = _LEGAL_SUFFIX_RE.sub("", key).strip()
    return key


def _stable_hash(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode()).hexdigest()


def _build_name_index(con) -> Dict[str, str]:
    """Map normalized name -> allocator_id; institutional_prospect wins over syndicate_lp."""
    index: Dict[str, str] = {}
    for aid, name, pop in con.execute(
        """
        SELECT CAST(allocator_id AS VARCHAR), canonical_name, population
        FROM allocators
        ORDER BY CASE population
            WHEN 'institutional_prospect' THEN 0
            WHEN 'benchmark_target' THEN 1
            ELSE 2
        END
        """
    ).fetchall():
        if name:
            index.setdefault(_norm_key(name), aid)
    for alias_text, canonical_id in con.execute(
        "SELECT alias_text, canonical_id FROM entity_aliases WHERE entity_type = 'allocator'"
    ).fetchall():
        if alias_text:
            index.setdefault(_norm_key(alias_text), canonical_id)
    return index


def _find_value(row: Dict[str, Any], *keywords: str) -> str:
    """Find the first value whose column header contains all given keyword tokens."""
    for k, v in row.items():
        kl = str(k).lower()
        if all(tok in kl for tok in keywords):
            return str(v) if v is not None else ""
    return ""


def _looks_like_firm(name: str) -> bool:
    nl = name.lower()
    return any(w in nl for w in (
        "capital", "ventures", "venture", "partners", "fund", "office",
        "group", "holdings", "management", "asset", "advisors", "investments",
        "llc", "ltd", "inc", "associates", "trust", " vc", "labs",
    ))


def _default_syndicate_type(name: str) -> str:
    """Most syndicate roster entries are individual angels; firms get inferred."""
    inferred = infer_type_from_name(name)
    if inferred != AllocatorType.UNKNOWN:
        return inferred
    return AllocatorType.HIGH_NET_WORTH if not _looks_like_firm(name) else AllocatorType.UNKNOWN


def _create_allocator(
    con, name: str, population: str, allocator_type: str,
    check_usd: Optional[float], src_id: str, src_file: str, content_hash: str,
) -> str:
    """Insert a new allocator row and return its id."""
    allocator_id = str(uuid.uuid4())
    bucket = classify_check_size(check_usd) if check_usd else None
    con.execute(
        """
        INSERT INTO allocators (
            allocator_id, canonical_name, allocator_type, geography,
            check_size_min_usd, check_size_max_usd, check_size_bucket,
            population, source_record_id, source_file, content_hash
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            allocator_id, name, allocator_type, "unknown",
            check_usd, check_usd, bucket,
            population, src_id, src_file, content_hash,
        ],
    )
    return allocator_id


def _write_alias(con, canonical_id: str, alias_text: str, source_file: str, confidence: float) -> None:
    existing = con.execute(
        "SELECT 1 FROM entity_aliases WHERE canonical_id = ? AND alias_text = ? AND source_file = ?",
        [canonical_id, alias_text, source_file],
    ).fetchone()
    if existing:
        return
    con.execute(
        """
        INSERT INTO entity_aliases
            (alias_id, canonical_id, entity_type, alias_text, source_file, confidence, resolver_method)
        VALUES (?, ?, 'allocator', ?, ?, ?, 'exact_name_match')
        """,
        [str(uuid.uuid4()), canonical_id, alias_text, source_file, confidence],
    )


# ---------------------------------------------------------------------------
# Step 1 — syndicate LP roster → allocators
# ---------------------------------------------------------------------------

def ingest_syndicate_lps(con) -> Dict[str, int]:
    """Create allocators for the AngelList syndicate roster. Returns counts."""
    rows = con.execute(
        """
        SELECT source_record_id, source_file, content_hash, raw_content
        FROM entities_raw
        WHERE source_file LIKE ?
          AND json_extract_string(raw_content, '$._sheet') = 'Syndicate LPs'
        """,
        [SYNDICATE_FILE_LIKE],
    ).fetchall()

    index = _build_name_index(con)
    matched = 0
    seen_this_run: set[str] = set()
    alloc_rows: List[tuple] = []
    alias_rows: List[tuple] = []

    for src_id, src_file, content_hash, raw in rows:
        if isinstance(raw, str):
            raw = json.loads(raw)
        name = (raw.get("Name") or "").strip()
        if not name or name.lower() in ("name", "nan"):
            continue

        key = _norm_key(name)
        median_check = parse_usd(_find_value(raw, "median", "check"))
        ch = content_hash or _stable_hash(name)

        if key in index:
            alias_rows.append((str(uuid.uuid4()), index[key], "allocator", name, src_file, 0.95, "exact_name_match"))
            matched += 1
            continue
        if key in seen_this_run:
            continue
        seen_this_run.add(key)

        allocator_id = str(uuid.uuid4())
        index[key] = allocator_id
        bucket = classify_check_size(median_check) if median_check else None
        alloc_rows.append((
            allocator_id, name, _default_syndicate_type(name), "unknown",
            median_check, median_check, bucket, "syndicate_lp", src_id, src_file, ch,
        ))
        alias_rows.append((str(uuid.uuid4()), allocator_id, "allocator", name, src_file, 1.0, "exact_name_match"))

    # Idempotent: clear prior syndicate-sourced aliases, then bulk insert.
    con.execute("DELETE FROM entity_aliases WHERE source_file LIKE ?", [SYNDICATE_FILE_LIKE])
    _bulk_insert(con, "allocators",
                 ["allocator_id", "canonical_name", "allocator_type", "geography",
                  "check_size_min_usd", "check_size_max_usd", "check_size_bucket",
                  "population", "source_record_id", "source_file", "content_hash"],
                 alloc_rows)
    _bulk_insert(con, "entity_aliases",
                 ["alias_id", "canonical_id", "entity_type", "alias_text",
                  "source_file", "confidence", "resolver_method"],
                 alias_rows)

    # Backfill population for the original institutional allocators.
    con.execute(
        "UPDATE allocators SET population = 'institutional_prospect' WHERE population IS NULL"
    )

    return {"syndicate_lps_created": len(alloc_rows), "matched_existing": matched,
            "aliases_created": len(alias_rows)}


# ---------------------------------------------------------------------------
# Step 2 — LP investments → funds + investments table
# ---------------------------------------------------------------------------

def _parse_invest_date(raw: str) -> Optional[str]:
    if not raw or str(raw).strip() in ("", "nan", "None"):
        return None
    from dateutil import parser as dparser
    try:
        return dparser.parse(str(raw)).date().isoformat()
    except Exception:
        return None


def ingest_syndicate_investments(con) -> Dict[str, int]:
    """
    Populate funds (one per distinct deal) and the investments table from the
    'LP investments' sheet. Idempotent: clears prior syndicate-sourced investments.
    """
    rows = con.execute(
        """
        SELECT source_record_id, source_file, content_hash, raw_content
        FROM entities_raw
        WHERE source_file LIKE ?
          AND json_extract_string(raw_content, '$._sheet') = 'LP investments'
        """,
        [SYNDICATE_FILE_LIKE],
    ).fetchall()

    index = _build_name_index(con)

    deal_fund: Dict[str, str] = {}
    inv_rows: List[tuple] = []
    new_alloc_rows: List[tuple] = []
    new_alias_rows: List[tuple] = []

    for src_id, src_file, content_hash, raw in rows:
        if isinstance(raw, str):
            raw = json.loads(raw)
        partner = (raw.get("Partner name") or "").strip()
        deal = (raw.get("Investment name") or "").strip()
        if not partner or not deal or partner.lower() in ("partner name", "nan"):
            continue

        dtype = (raw.get("Type") or "").strip().lower()
        fund_type = "spv" if "spv" in dtype else "venture_capital"

        if deal not in deal_fund:
            deal_fund[deal] = upsert_fund(
                canonical_name=deal,
                source_record_id=src_id,
                source_file=src_file,
                content_hash=content_hash or _stable_hash(deal),
                con=con,
                fund_type=fund_type,
                manager_name="AngelList Syndicate",
            )
        fund_id = deal_fund[deal]

        pkey = _norm_key(partner)
        lp_id = index.get(pkey)
        if not lp_id:
            lp_id = str(uuid.uuid4())
            index[pkey] = lp_id
            new_alloc_rows.append((
                lp_id, partner, _default_syndicate_type(partner), "unknown",
                None, None, None, "syndicate_lp", src_id, src_file,
                content_hash or _stable_hash(partner),
            ))
            new_alias_rows.append((
                str(uuid.uuid4()), lp_id, "allocator", partner, src_file, 1.0, "exact_name_match",
            ))

        inv_rows.append((
            str(uuid.uuid4()), lp_id, fund_id,
            _parse_invest_date(raw.get("Investment date", "")),
            parse_usd(raw.get("Investment amount", "")),
            True, True, (raw.get("Type") or "").strip(),
            src_id, src_file, content_hash or _stable_hash(partner, deal, src_id),
        ))

    # New partners discovered only in the investment ledger.
    _bulk_insert(con, "allocators",
                 ["allocator_id", "canonical_name", "allocator_type", "geography",
                  "check_size_min_usd", "check_size_max_usd", "check_size_bucket",
                  "population", "source_record_id", "source_file", "content_hash"],
                 new_alloc_rows)
    _bulk_insert(con, "entity_aliases",
                 ["alias_id", "canonical_id", "entity_type", "alias_text",
                  "source_file", "confidence", "resolver_method"],
                 new_alias_rows)

    # Idempotent clear + bulk insert of investments.
    con.execute("DELETE FROM investments WHERE source_file LIKE ?", [SYNDICATE_FILE_LIKE])
    _bulk_insert(con, "investments",
                 ["investment_id", "lp_id", "fund_id", "investment_date", "commitment_usd",
                  "syndicate_overlap", "co_investment_flag", "notes",
                  "source_record_id", "source_file", "content_hash"],
                 inv_rows)

    return {
        "funds_created": len(deal_fund),
        "investments_created": len(inv_rows),
        "partners_created": len(new_alloc_rows),
    }


# ---------------------------------------------------------------------------
# Step 3 — co-investment edges from shared deal participation
# ---------------------------------------------------------------------------

def build_coinvestment_edges(con, min_shared: int = MIN_SHARED_DEALS) -> Dict[str, int]:
    """
    Derive `co_invested` LP↔LP edges from shared deal participation.
    Two LPs sharing >= min_shared deals get one edge (weight = shared count),
    backed by up to EVIDENCE_PER_EDGE_CAP `co_investment_pattern` evidence rows.
    Idempotent: clears prior co_invested edges + their evidence.
    """
    inv_rows = con.execute(
        """
        SELECT CAST(lp_id AS VARCHAR), CAST(fund_id AS VARCHAR), source_record_id
        FROM investments
        WHERE co_investment_flag = TRUE
        """
    ).fetchall()

    # deal -> list of (lp_id, src_id)
    deal_members: Dict[str, List[Tuple[str, str]]] = defaultdict(list)
    for lp_id, fund_id, src_id in inv_rows:
        deal_members[fund_id].append((lp_id, src_id))

    # Pass 1: count shared deals per LP pair (sorted tuple).
    pair_count: Counter = Counter()
    for members in deal_members.values():
        lps = sorted({m[0] for m in members})
        if len(lps) < 2:
            continue
        for a, b in combinations(lps, 2):
            pair_count[(a, b)] += 1

    qualifying = {pair: cnt for pair, cnt in pair_count.items() if cnt >= min_shared}
    if not qualifying:
        return {"co_invested_edges": 0, "co_invested_evidence": 0}

    edge_ids = {pair: str(uuid.uuid4()) for pair in qualifying}

    # Pass 2: collect up to EVIDENCE_PER_EDGE_CAP source ids per qualifying pair.
    evidence_srcs: Dict[Tuple[str, str], List[str]] = defaultdict(list)
    for members in deal_members.values():
        # representative src per lp in this deal
        lp_src = {}
        for lp_id, src_id in members:
            lp_src.setdefault(lp_id, src_id)
        lps = sorted(lp_src.keys())
        if len(lps) < 2:
            continue
        for a, b in combinations(lps, 2):
            if (a, b) in qualifying and len(evidence_srcs[(a, b)]) < EVIDENCE_PER_EDGE_CAP:
                evidence_srcs[(a, b)].append(lp_src[a])

    # Idempotent clear of prior co-investment artifacts.
    con.execute(
        "DELETE FROM relationship_evidence WHERE evidence_type = 'co_investment_pattern'"
    )
    con.execute("DELETE FROM relationships WHERE edge_type = 'co_invested'")

    now = datetime.now(timezone.utc).isoformat()
    edge_batch: List[tuple] = []
    ev_batch: List[tuple] = []

    for (a, b), cnt in qualifying.items():
        srcs = evidence_srcs.get((a, b)) or []
        if not srcs:
            continue
        edge_id = edge_ids[(a, b)]
        edge_batch.append((edge_id, a, "lp", b, "lp", "co_invested", float(cnt), now, now))
        for src_id in srcs:
            ev_batch.append((
                str(uuid.uuid4()), edge_id, src_id, "co_investment_pattern",
                CO_INVEST_EVIDENCE_STRENGTH, CO_INVEST_EVIDENCE_STRENGTH,
                json.dumps({
                    "source_file": "Syndicate LPs - MyAsiaVC",
                    "shared_deals": cnt,
                    "lp_a": a, "lp_b": b,
                }),
            ))

    _bulk_insert(con, "relationships",
                 ["edge_id", "source_node_id", "source_node_type",
                  "target_node_id", "target_node_type", "edge_type", "weight",
                  "first_seen", "last_seen"],
                 edge_batch)
    _bulk_insert(con, "relationship_evidence",
                 ["evidence_id", "edge_id", "source_record_id", "evidence_type",
                  "evidence_strength", "confidence", "provenance_pointer"],
                 ev_batch)

    return {"co_invested_edges": len(edge_batch), "co_invested_evidence": len(ev_batch)}


# ---------------------------------------------------------------------------
# Step 4 — ContraVC Top 200 benchmark
# ---------------------------------------------------------------------------

def _parse_tier(raw: str) -> Optional[str]:
    m = re.search(r"tier\s*([1-4])", (raw or "").lower())
    return f"tier_{m.group(1)}" if m else None


def _parse_int(raw: str) -> Optional[int]:
    try:
        return int(float(str(raw).replace(",", "").strip()))
    except (ValueError, TypeError):
        return None


def _parse_float(raw: str) -> Optional[float]:
    try:
        return float(str(raw).replace(",", "").replace("$", "").strip())
    except (ValueError, TypeError):
        return None


def ingest_contra_benchmark(con) -> Dict[str, int]:
    """Ingest the ContraVC Top-200 ranking into benchmark_rankings (calibration gold set)."""
    rows = con.execute(
        """
        SELECT source_record_id, source_file, content_hash, raw_content
        FROM entities_raw
        WHERE source_file LIKE ?
          AND json_extract_string(raw_content, '$._sheet') = 'Top 200 LP Rankings'
        """,
        [CONTRA_FILE_LIKE],
    ).fetchall()

    index = _build_name_index(con)
    con.execute("DELETE FROM benchmark_rankings WHERE ranking_source = 'contravc_top200'")

    batch: List[tuple] = []
    matched = created = 0

    for src_id, src_file, content_hash, raw in rows:
        if isinstance(raw, str):
            raw = json.loads(raw)
        name = (raw.get("Name") or "").strip()
        rank = _parse_int(raw.get("Rank", ""))
        if not name or name.lower() in ("name", "nan") or rank is None:
            continue

        key = _norm_key(name)
        allocator_id = index.get(key)
        if allocator_id:
            matched += 1
        else:
            allocator_id = _create_allocator(
                con, name, "syndicate_lp", _default_syndicate_type(name),
                _parse_float(raw.get("Median Check", "")),
                src_id, src_file, content_hash or _stable_hash(name),
            )
            index[key] = allocator_id
            _write_alias(con, allocator_id, name, src_file, 1.0)
            created += 1

        prior_raw = (raw.get("Prior Fund LP?") or "").strip().lower()
        prior = True if prior_raw.startswith("yes") else (False if prior_raw else None)

        batch.append((
            str(uuid.uuid4()), allocator_id, name, "contravc_top200", rank,
            _parse_float(raw.get("Priority Score", "")),
            _parse_tier(raw.get("Tier", "")),
            prior,
            _parse_int(raw.get("SPVs Backed", "")),
            _parse_int(raw.get("Funds Backed", "")),
            _parse_float(raw.get("Median Check", "")),
            _parse_float(raw.get("Total Invested (Syndicate)", "")),
            _parse_float(raw.get("AL Activity (Last 12m)", "")),
            (raw.get("LinkedIn URL") or "").strip() or None,
            src_id, src_file, content_hash or _stable_hash(name, str(rank)),
        ))

    _bulk_insert(con, "benchmark_rankings",
                 ["benchmark_id", "allocator_id", "external_name", "ranking_source", "rank",
                  "priority_score", "tier", "prior_fund_lp", "spvs_backed", "funds_backed",
                  "median_check_usd", "total_invested_usd", "al_activity_usd", "linkedin_url",
                  "source_record_id", "source_file", "content_hash"],
                 batch)

    return {"benchmark_rows": len(batch), "matched_existing": matched, "created_new": created}


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def run_syndicate_integration(con) -> Dict[str, Any]:
    """Run all syndicate + benchmark integration steps in dependency order."""
    out: Dict[str, Any] = {}
    out["roster"] = ingest_syndicate_lps(con)
    out["investments"] = ingest_syndicate_investments(con)
    out["coinvest"] = build_coinvestment_edges(con)
    out["benchmark"] = ingest_contra_benchmark(con)
    return out

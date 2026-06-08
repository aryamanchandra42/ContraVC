"""
Syndicate intelligence signal extractor.

Mines the investments table (16k+ rows) to surface which syndicate LPs
behave like real fund LPs vs SPV-only angels.

Three new signals written per eligible allocator:
  fund_lp_behavior   — 0–1; driven by fund_deal_count and fund_lp_ratio
  syndicate_depth    — 0–1; log-scaled total committed USD
  syndicate_recency  — 0–1; exp-decay from most recent investment date
"""

from __future__ import annotations

import hashlib
import math
import uuid
from collections import defaultdict
from datetime import date
from typing import Any, Dict, List, Tuple

from agents.scoring.signal_evidence_writer import (
    delete_signals_cascade,
    insert_signals_batch,
    make_evidence_row,
)

SOURCE_FILE = "agents/scoring/syndicate_signal_extractor.py"
FUND_NOTES = {"venture fund", "fund"}   # normalised notes values that indicate a fund commitment
MAX_USD_CAP = 1_000_000.0              # log-scale reference point (~$1M cap for normalization)
DECAY_HALF_LIFE = 365.0                # days


def _stable_hash(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode()).hexdigest()


def _recency(d: date | None, today: date | None = None) -> float:
    if d is None:
        return 0.2
    days = max(0, ((today or date.today()) - d).days)
    return round(math.exp(-days / DECAY_HALF_LIFE), 4)


def _log_normalize(usd: float, cap: float = MAX_USD_CAP) -> float:
    if usd <= 0:
        return 0.0
    return round(min(1.0, math.log1p(usd) / math.log1p(cap)), 4)


def run_syndicate_signal_extraction(con) -> Dict[str, Any]:
    """Extract and write syndicate intelligence signals. Idempotent."""
    for sig_type in ("fund_lp_behavior", "syndicate_depth", "syndicate_recency"):
        delete_signals_cascade(
            con,
            "signal_type = ? AND source_file = ?",
            [sig_type, SOURCE_FILE],
        )

    rows = con.execute(
        """
        SELECT
            CAST(lp_id AS VARCHAR),
            notes,
            commitment_usd,
            investment_date,
            source_record_id
        FROM investments
        """
    ).fetchall()

    by_lp: Dict[str, List[Tuple]] = defaultdict(list)
    for lp_id, notes, usd, inv_date, src_rec in rows:
        by_lp[lp_id].append((notes or "", float(usd or 0), inv_date, src_rec))

    if not by_lp:
        return {"fund_lp_behavior": 0, "syndicate_depth": 0, "syndicate_recency": 0}

    today = date.today()
    signal_rows: List[Tuple] = []
    evidence_rows: List[Tuple] = []
    counts = {"fund_lp_behavior": 0, "syndicate_depth": 0, "syndicate_recency": 0}

    for lp_id, deals in by_lp.items():
        fund_deals = [d for d in deals if d[0].strip().lower() in FUND_NOTES]
        fund_count = len(fund_deals)
        total_count = len(deals)
        total_usd = sum(d[1] for d in deals)
        dates = [d[2] for d in deals if d[2] is not None]
        latest_date = max(dates, default=None)
        src_rec = next((d[3] for d in deals if d[3]), _stable_hash(lp_id))

        # fund_lp_behavior: blend of ratio and count cap
        fund_ratio = fund_count / total_count if total_count else 0.0
        fund_score = round(min(1.0, 0.6 * fund_ratio + 0.4 * min(1.0, fund_count / 3.0)), 4)

        # syndicate_depth: log-scaled total committed
        depth_score = _log_normalize(total_usd)

        # syndicate_recency: exp-decay from latest investment
        recency_score = _recency(latest_date, today)

        raw_note = f"fund_deals={fund_count}/{total_count}, total_usd={total_usd:.0f}"

        for sig_type, val, note in (
            ("fund_lp_behavior", fund_score, f"fund_count={fund_count}, fund_ratio={fund_ratio:.2f}"),
            ("syndicate_depth", depth_score, f"total_usd={total_usd:.0f}"),
            ("syndicate_recency", recency_score, f"latest_date={latest_date}"),
        ):
            sig_id = str(uuid.uuid4())
            signal_rows.append((
                sig_id, lp_id, sig_type, raw_note, val,
                src_rec, SOURCE_FILE, _stable_hash(lp_id, sig_type, src_rec),
            ))
            evidence_rows.append(make_evidence_row(
                sig_id, src_rec, "signal_investment_pattern", val, SOURCE_FILE, note,
            ))
            counts[sig_type] += 1

    insert_signals_batch(con, signal_rows, evidence_rows)
    return counts

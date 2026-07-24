"""
Mine data Contra already owns — the highest-signal, zero-cost lead source.

1. Syndicate fund-LPs: people who already invested in MyAsiaVC *funds*
   (not just SPVs) but are not in the CRM. They have already said yes to this
   exact product once; they get a scorecard built from internal evidence and
   auto-promote as leads (source='syndicate').

2. past_outreach backfill: parse the .eml archive of every cold email ever
   sent, log each recipient in outreach_log, and flip matching CRM leads to
   'contacted'. This stops the agent re-mining people we already emailed and
   makes the funnel's contacted count real.
"""

from __future__ import annotations

import email
import glob
import logging
import os
import re
from email import policy
from email.utils import getaddresses, parsedate_to_datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from contra.scorecard import (
    LpScorecard,
    ScorecardCheck,
    classify_yes_reason,
    compute_verdict,
    upsert_scorecard,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 1. Syndicate fund-LP promotion
# ---------------------------------------------------------------------------

def promote_syndicate_fund_lps(con, limit: int = 25) -> Dict[str, Any]:
    """Promote syndicate members with real fund-deal history into the CRM."""
    from agents.normalization.crm_normalizer import norm_key
    from contra.crm.writer import _insert_lead

    rows = con.execute(
        """
        SELECT canonical_name, fund_deal_count, spv_deal_count,
               total_committed_usd, geography, allocator_type,
               fund_lp_behavior_score
        FROM v_syndicate_profile
        WHERE is_fund_lp AND NOT in_crm
        ORDER BY fund_lp_behavior_score DESC NULLS LAST, fund_deal_count DESC
        LIMIT ?
        """,
        [limit],
    ).fetchall()

    dismissed = {
        r[0] for r in con.execute("SELECT name_key FROM crm_dismissed").fetchall()
    }

    promoted: List[str] = []
    skipped = 0
    for name, fund_deals, spv_deals, committed, geo, atype, score in rows:
        key = norm_key(name)
        if key in dismissed:
            skipped += 1
            continue

        evidence = (
            f"{int(fund_deals or 0)} fund deal(s) as LP inside the MyAsiaVC syndicate"
            + (f", ${float(committed):,.0f} total committed" if committed else "")
        )
        checks = [
            ScorecardCheck(check="fund_lp", status="pass", evidence=evidence,
                           source_url="internal://syndicate"),
            ScorecardCheck(check="new_managers", status="pass",
                           evidence="Backed MyAsiaVC syndicate funds — emerging-manager vehicles by definition",
                           source_url="internal://syndicate"),
            ScorecardCheck(check="thesis_fit", status="unknown"),
            ScorecardCheck(check="geography_fit",
                           status="pass" if geo else "unknown",
                           evidence=f"Recorded geography: {geo}" if geo else "",
                           source_url="internal://syndicate" if geo else ""),
            ScorecardCheck(check="no_disqualifier", status="pass",
                           evidence="Already an LP in this exact fund family — no structural blocker",
                           source_url="internal://syndicate"),
        ]
        verdict, reason = compute_verdict(checks)
        yes_reason, yes_evidence = classify_yes_reason(
            syndicate_fund_deals=int(fund_deals or 0),
        )
        sc = LpScorecard(
            investor_name=name, name_key=key, checks=checks,
            verdict=verdict, verdict_reason=reason,
            yes_reason=yes_reason, yes_evidence=yes_evidence,
            source="syndicate",
        )
        try:
            _insert_lead(con, {
                "investor_name": name,
                "investor_type": atype,
                "investor_location": geo,
                "investor_details": f"Syndicate fund-LP auto-promoted. {evidence}.",
                "pipeline_stage": "Prospect",
                "status": "active",
                "syndicate_score": float(score) if score is not None else None,
            }, source="syndicate")
            upsert_scorecard(con, sc)
            promoted.append(name)
        except ValueError:
            skipped += 1  # already in CRM under another key
        except Exception as exc:
            logger.warning("Syndicate promote failed for %s: %s", name, exc)
            skipped += 1

    return {"promoted": len(promoted), "skipped": skipped, "names": promoted[:25]}


# ---------------------------------------------------------------------------
# 2. past_outreach .eml backfill
# ---------------------------------------------------------------------------

def _default_eml_dir() -> Path:
    configured = os.environ.get("PAST_OUTREACH_DIR", "").strip()
    if configured:
        return Path(configured)
    # contra/ package root → sibling past_outreach/
    return Path(__file__).resolve().parents[2].parent / "past_outreach"


_FREE_MAIL = {
    "gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "icloud.com",
    "me.com", "protonmail.com", "aol.com", "live.com", "msn.com",
}


def backfill_past_outreach(con, eml_dir: Optional[str] = None) -> Dict[str, Any]:
    """
    Parse every .eml in the archive into outreach_log (idempotent) and flip
    matching active CRM leads to 'contacted'.
    """
    from agents.normalization.crm_normalizer import norm_key

    directory = Path(eml_dir) if eml_dir else _default_eml_dir()
    files = sorted(glob.glob(str(directory / "*.eml")))
    if not files:
        return {"files": 0, "recipients_logged": 0, "leads_marked_contacted": 0,
                "error": f"No .eml files found in {directory}"}

    existing = {
        (r[0] or "", r[1] or "") for r in con.execute(
            "SELECT recipient_email, source_file FROM outreach_log WHERE source = 'eml_backfill'"
        ).fetchall()
    }

    logged = 0
    parse_errors = 0
    contacted_keys: set = set()

    for path in files:
        try:
            with open(path, encoding="utf-8", errors="ignore") as fh:
                msg = email.message_from_file(fh, policy=policy.default)
        except Exception:
            parse_errors += 1
            continue

        subject = str(msg.get("Subject") or "")[:300]
        sent_at = None
        try:
            if msg.get("Date"):
                sent_at = parsedate_to_datetime(msg.get("Date"))
        except Exception:
            sent_at = None

        source_file = os.path.basename(path)[:200]
        recipients = getaddresses([
            str(msg.get("To") or ""), str(msg.get("Cc") or ""),
        ])
        for display_name, addr in recipients:
            addr = (addr or "").strip().lower()
            if not addr or "@" not in addr or "contravc.com" in addr:
                continue
            dedupe_key = (addr, source_file)
            if dedupe_key in existing:
                continue
            existing.add(dedupe_key)

            display_name = re.sub(r"\s+", " ", display_name or "").strip()
            key = norm_key(display_name) if display_name else None
            domain = addr.split("@")[1]
            con.execute(
                """
                INSERT INTO outreach_log
                    (recipient_email, recipient_name, name_key, company_domain,
                     subject, sent_at, source, source_file)
                VALUES (?, ?, ?, ?, ?, ?, 'eml_backfill', ?)
                """,
                [addr, display_name or None, key,
                 None if domain in _FREE_MAIL else domain,
                 subject, sent_at, source_file],
            )
            logged += 1
            if key:
                contacted_keys.add(key)

    # Flip matching active leads to contacted (never downgrade later stages).
    marked = 0
    if contacted_keys:
        placeholders = ",".join("?" for _ in contacted_keys)
        result = con.execute(
            f"""
            UPDATE crm_leads
            SET status = 'contacted', updated_at = NOW()
            WHERE name_key IN ({placeholders}) AND status = 'active'
            """,
            list(contacted_keys),
        )
        try:
            marked = result.fetchall()[0][0] if result else 0
        except Exception:
            marked = 0

    return {
        "files": len(files),
        "parse_errors": parse_errors,
        "recipients_logged": logged,
        "unique_recipient_names": len(contacted_keys),
        "leads_marked_contacted": marked,
    }


def contacted_name_keys(con) -> set:
    """Name keys of everyone we have already emailed — used by dedupe."""
    try:
        return {
            r[0] for r in con.execute(
                "SELECT DISTINCT name_key FROM outreach_log WHERE name_key IS NOT NULL"
            ).fetchall()
        }
    except Exception:
        return set()

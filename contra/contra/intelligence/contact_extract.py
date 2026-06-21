"""
Gate contact extraction.

After a YES/REVIEW gate run, parses the web_context and source_urls for:
  - Email addresses belonging to the screened LP
  - LinkedIn profile URLs
  - X/Twitter profile URLs

Results are upserted into allocator_contacts with source='gate_research'.

Also parses analyst_facts (analyst-provided facts pasted during gate chat)
for explicit contact overrides (source='analyst').

This module is called by persist_gate_findings in contra/gate/persist.py.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Regex patterns
# ---------------------------------------------------------------------------

_EMAIL_RE = re.compile(
    r"\b[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}\b"
)

_EMAIL_NOISE = re.compile(
    r"(noreply|no-reply|donotreply|support|info|hello|contact|privacy|legal|"
    r"press|media|unsubscribe|bounce|mailer|postmaster|example|test)\b",
    re.IGNORECASE,
)
_EMAIL_NOISE_DOMAINS = {
    "example.com", "test.com", "sentry.io", "cloudinary.com",
    "amazonaws.com", "googleusercontent.com", "akamai.com",
}

_LINKEDIN_RE = re.compile(
    r"https?://(?:www\.)?linkedin\.com/in/([a-zA-Z0-9\-_%]+)/?",
    re.IGNORECASE,
)

_TWITTER_RE = re.compile(
    r"https?://(?:www\.)?(?:twitter|x)\.com/(?!share|intent|search|hashtag|home)"
    r"([a-zA-Z0-9_]{1,50})(?:/.*)?",
    re.IGNORECASE,
)

_ANALYST_EMAIL_RE = re.compile(
    r"(?:email|e-mail|mail)\s*[:\-=]\s*([a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,})",
    re.IGNORECASE,
)

_ANALYST_LINKEDIN_RE = re.compile(
    r"linkedin\s*[:\-=]\s*(https?://[^\s]+)",
    re.IGNORECASE,
)

_ANALYST_TWITTER_RE = re.compile(
    r"(?:twitter|x\.com)\s*[:\-=]\s*(https?://[^\s]+)",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Extraction helpers
# ---------------------------------------------------------------------------

def _clean_email(email: str) -> Optional[str]:
    email = email.strip().lower()
    if _EMAIL_NOISE.search(email.split("@")[0]):
        return None
    domain = email.split("@")[-1] if "@" in email else ""
    if domain in _EMAIL_NOISE_DOMAINS:
        return None
    return email


def _clean_linkedin(match: re.Match) -> Optional[str]:
    slug = match.group(1).lower().rstrip("/")
    if len(slug) < 3 or slug in {"in", "company", "search", "pub"}:
        return None
    return f"https://www.linkedin.com/in/{slug}"


def _clean_twitter(match: re.Match) -> Optional[str]:
    handle = match.group(1).lower().rstrip("/")
    # Skip obvious non-person handles
    if handle in {"share", "intent", "search", "hashtag", "home", "explore", "i"}:
        return None
    if len(handle) < 2:
        return None
    return f"https://x.com/{handle}"


def _extract_from_text(text: str) -> Tuple[List[str], List[str], List[str]]:
    """Return (emails, linkedin_urls, twitter_urls) extracted from free text."""
    emails: List[str] = []
    linkedin: List[str] = []
    twitter: List[str] = []

    seen_emails: set = set()
    seen_li: set = set()
    seen_tw: set = set()

    for m in _EMAIL_RE.finditer(text):
        e = _clean_email(m.group())
        if e and e not in seen_emails:
            seen_emails.add(e)
            emails.append(e)

    for m in _LINKEDIN_RE.finditer(text):
        u = _clean_linkedin(m)
        if u and u not in seen_li:
            seen_li.add(u)
            linkedin.append(u)

    for m in _TWITTER_RE.finditer(text):
        u = _clean_twitter(m)
        if u and u not in seen_tw:
            seen_tw.add(u)
            twitter.append(u)

    return emails, linkedin, twitter


def _extract_from_analyst_facts(facts: List[str]) -> Tuple[List[str], List[str], List[str]]:
    """Parse analyst-supplied facts for explicitly stated contact details."""
    emails, linkedin, twitter = [], [], []
    for fact in facts:
        for m in _ANALYST_EMAIL_RE.finditer(fact):
            e = _clean_email(m.group(1))
            if e:
                emails.append(e)
        for m in _ANALYST_LINKEDIN_RE.finditer(fact):
            linkedin.append(m.group(1).strip())
        for m in _ANALYST_TWITTER_RE.finditer(fact):
            twitter.append(m.group(1).strip())
        # Also catch bare URLs in the fact text
        e_list, li_list, tw_list = _extract_from_text(fact)
        emails.extend(e_list)
        linkedin.extend(li_list)
        twitter.extend(tw_list)
    return emails, linkedin, twitter


# ---------------------------------------------------------------------------
# Channel list builders
# ---------------------------------------------------------------------------

def _dedupe_channels(channels: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Remove duplicates keeping highest-confidence entry per (type, value)."""
    seen: Dict[Tuple[str, str], int] = {}
    result: List[Dict[str, Any]] = []
    for i, ch in enumerate(channels):
        key = (ch["type"], ch["value"])
        if key not in seen or ch["confidence"] > channels[seen[key]]["confidence"]:
            seen[key] = i
    used = set(seen.values())
    for i, ch in enumerate(channels):
        if i in used:
            result.append(ch)
    return result


def build_channels(
    emails: List[str],
    linkedin_urls: List[str],
    twitter_urls: List[str],
    source: str,
    confidence: float,
) -> List[Dict[str, Any]]:
    channels: List[Dict[str, Any]] = []
    for e in emails:
        channels.append({"type": "email", "value": e, "source": source, "confidence": confidence})
    for u in linkedin_urls:
        channels.append({"type": "linkedin", "value": u, "source": source, "confidence": confidence})
    for u in twitter_urls:
        channels.append({"type": "twitter", "value": u, "source": source, "confidence": confidence})
    return channels


# ---------------------------------------------------------------------------
# DB write
# ---------------------------------------------------------------------------

def _upsert_gate_contact(
    con,
    allocator_id: str,
    lp_name: str,
    channels: List[Dict[str, Any]],
    source: str,
) -> None:
    """Upsert one allocator_contacts row per distinct person (source key) with channels_json."""
    if not channels:
        return

    # Extract the best email and linkedin_url for the flat columns
    email = next((c["value"] for c in channels if c["type"] == "email"), None)
    linkedin_url = next((c["value"] for c in channels if c["type"] == "linkedin"), None)
    twitter_url = next((c["value"] for c in channels if c["type"] == "twitter"), None)
    channels_json = json.dumps(channels)

    try:
        existing = con.execute(
            """
            SELECT contact_id FROM allocator_contacts
            WHERE allocator_id = ? AND source = ?
            LIMIT 1
            """,
            [allocator_id, source],
        ).fetchone()

        if existing:
            con.execute(
                """
                UPDATE allocator_contacts SET
                    email        = COALESCE(?, email),
                    linkedin_url = COALESCE(?, linkedin_url),
                    twitter_url  = COALESCE(?, twitter_url),
                    channels_json = ?,
                    ingested_at  = NOW()
                WHERE contact_id = ?
                """,
                [email, linkedin_url, twitter_url, channels_json, str(existing[0])],
            )
        else:
            con.execute(
                """
                INSERT INTO allocator_contacts
                    (contact_id, allocator_id, source, full_name,
                     email, linkedin_url, twitter_url, channels_json,
                     match_confidence)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    str(uuid.uuid4()), allocator_id, source, lp_name,
                    email, linkedin_url, twitter_url, channels_json, 0.7,
                ],
            )
    except Exception as exc:
        logger.warning("gate contact upsert failed for %s: %s", allocator_id, exc)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def extract_and_persist_gate_contacts(
    con,
    *,
    lp_name: str,
    allocator_id: str,
    web_context: str,
    source_urls: Optional[List[str]] = None,
    analyst_facts: Optional[List[str]] = None,
) -> Dict[str, int]:
    """
    Extract email/LinkedIn/X contacts from gate web research and analyst facts,
    then upsert to allocator_contacts.

    Called by persist_gate_findings after YES/REVIEW verdicts.
    Returns counts: {gate_emails, gate_linkedin, gate_twitter, analyst_overrides}.
    """
    # --- Gate web research extraction (source='gate_research', conf=0.70) ---
    combined_text = web_context or ""
    for url in (source_urls or []):
        combined_text += f"\n{url}"

    gate_emails, gate_li, gate_tw = _extract_from_text(combined_text)

    # --- Analyst facts (higher confidence, source='analyst') ---
    analyst_emails, analyst_li, analyst_tw = _extract_from_analyst_facts(analyst_facts or [])

    # Write analyst channels first (priority source)
    if analyst_emails or analyst_li or analyst_tw:
        analyst_channels = build_channels(analyst_emails, analyst_li, analyst_tw, "analyst", 0.95)
        analyst_channels = _dedupe_channels(analyst_channels)
        _upsert_gate_contact(con, allocator_id, lp_name, analyst_channels, "analyst")

    # Write gate_research channels
    if gate_emails or gate_li or gate_tw:
        gate_channels = build_channels(gate_emails, gate_li, gate_tw, "gate_research", 0.70)
        gate_channels = _dedupe_channels(gate_channels)
        _upsert_gate_contact(con, allocator_id, lp_name, gate_channels, "gate_research")

    return {
        "gate_emails": len(gate_emails),
        "gate_linkedin": len(gate_li),
        "gate_twitter": len(gate_tw),
        "analyst_overrides": len(analyst_emails) + len(analyst_li) + len(analyst_tw),
    }

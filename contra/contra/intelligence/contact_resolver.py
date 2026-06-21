"""
Contact resolver — merges all sources for an LP into one deduplicated
ContactProfile with ranked channels.

Sources and their priority (highest first):
    analyst > gate_research > linkedin_export > crm_import

Usage:
    from contra.intelligence.contact_resolver import resolve_contacts
    profile = resolve_contacts(con, name="Ihar Mahniok")
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Source priority: lower index = higher authority.
_SOURCE_PRIORITY = ["analyst", "gate_research", "linkedin_export", "crm_import"]


def _source_rank(source: str) -> int:
    try:
        return _SOURCE_PRIORITY.index(source)
    except ValueError:
        return len(_SOURCE_PRIORITY)


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class ContactChannel:
    type: str             # "email" | "linkedin" | "twitter"
    value: str
    source: str
    confidence: float


@dataclass
class ContactPerson:
    full_name: Optional[str]
    title: Optional[str]
    company: Optional[str]
    location: Optional[str]
    channels: List[ContactChannel] = field(default_factory=list)


@dataclass
class ContactProfile:
    investor_name: str
    allocator_id: Optional[str]
    contacts: List[ContactPerson] = field(default_factory=list)
    recommended_channel: str = ""         # "email" | "linkedin" | "twitter" | "warm_intro"
    recommendation_rationale: str = ""
    confidence: float = 0.0

    def best_email(self) -> Optional[str]:
        for ch in self._all_channels("email"):
            return ch.value
        return None

    def best_linkedin(self) -> Optional[str]:
        for ch in self._all_channels("linkedin"):
            return ch.value
        return None

    def best_twitter(self) -> Optional[str]:
        for ch in self._all_channels("twitter"):
            return ch.value
        return None

    def _all_channels(self, type_: str) -> List[ContactChannel]:
        result = []
        for person in self.contacts:
            for ch in person.channels:
                if ch.type == type_:
                    result.append(ch)
        result.sort(key=lambda c: (_source_rank(c.source), -c.confidence))
        return result

    def to_api_dict(self) -> Dict[str, Any]:
        return {
            "investor_name": self.investor_name,
            "allocator_id": self.allocator_id,
            "recommended_channel": self.recommended_channel,
            "recommendation_rationale": self.recommendation_rationale,
            "confidence": self.confidence,
            "recommended_value": self._recommended_value(),
            "channels": [
                {
                    "type": ch.type,
                    "value": ch.value,
                    "source": ch.source,
                    "confidence": ch.confidence,
                }
                for person in self.contacts
                for ch in person.channels
            ],
            "contacts": [
                {
                    "full_name": p.full_name,
                    "title": p.title,
                    "company": p.company,
                    "location": p.location,
                    "email": next((c.value for c in p.channels if c.type == "email"), None),
                    "linkedin_url": next((c.value for c in p.channels if c.type == "linkedin"), None),
                    "twitter_url": next((c.value for c in p.channels if c.type == "twitter"), None),
                }
                for p in self.contacts
            ],
        }

    def _recommended_value(self) -> Optional[str]:
        if self.recommended_channel == "email":
            return self.best_email()
        if self.recommended_channel == "linkedin":
            return self.best_linkedin()
        if self.recommended_channel == "twitter":
            return self.best_twitter()
        return None


# ---------------------------------------------------------------------------
# DB queries
# ---------------------------------------------------------------------------

def _fetch_allocator_contacts(con, allocator_id: str) -> List[Dict[str, Any]]:
    """All allocator_contacts rows for this allocator, ordered by source priority."""
    try:
        rows = con.execute(
            """
            SELECT full_name, title, company, location, email, linkedin_url,
                   twitter_url, channels_json, source, match_confidence
            FROM allocator_contacts
            WHERE allocator_id = ?
            ORDER BY match_confidence DESC NULLS LAST
            """,
            [allocator_id],
        ).fetchall()
        cols = ["full_name", "title", "company", "location", "email",
                "linkedin_url", "twitter_url", "channels_json", "source", "match_confidence"]
        return [dict(zip(cols, r)) for r in rows]
    except Exception as exc:
        logger.debug("fetch allocator_contacts failed: %s", exc)
        return []


def _fetch_crm_contacts(con, name_key: str) -> List[Dict[str, Any]]:
    """crm_contacts.contacts_json for this name_key (legacy CRM import)."""
    try:
        rows = con.execute(
            """
            SELECT investor_name, contacts_json
            FROM crm_contacts WHERE name_key = ? LIMIT 5
            """,
            [name_key],
        ).fetchall()
        results = []
        for investor_name, contacts_json in rows:
            if not contacts_json:
                continue
            parsed = json.loads(contacts_json) if isinstance(contacts_json, str) else contacts_json
            if isinstance(parsed, list):
                for c in parsed:
                    c["_source"] = "crm_import"
                    results.extend([c])
            elif isinstance(parsed, dict):
                parsed["_source"] = "crm_import"
                results.append(parsed)
        return results
    except Exception as exc:
        logger.debug("fetch crm_contacts failed: %s", exc)
        return []


def _fetch_crm_leads_contacts(con, name_key: str) -> List[Dict[str, Any]]:
    """contacts_json snapshot from crm_leads (gate add-to-CRM path)."""
    try:
        rows = con.execute(
            """
            SELECT contacts_json FROM crm_leads
            WHERE lower(replace(investor_name, ' ', '_')) = ?
            ORDER BY created_at DESC LIMIT 3
            """,
            [name_key],
        ).fetchall()
        results = []
        for (contacts_json,) in rows:
            if not contacts_json:
                continue
            parsed = json.loads(contacts_json) if isinstance(contacts_json, str) else contacts_json
            if isinstance(parsed, list):
                for c in parsed:
                    c["_source"] = "crm_import"
                results.extend(parsed)
            elif isinstance(parsed, dict):
                parsed["_source"] = "crm_import"
                results.append(parsed)
        return results
    except Exception as exc:
        logger.debug("fetch crm_leads contacts failed: %s", exc)
        return []


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------

def _channels_from_row(row: Dict[str, Any]) -> List[ContactChannel]:
    """Build ContactChannel list from a flat allocator_contacts row."""
    channels: List[ContactChannel] = []
    source = row.get("source", "unknown")
    conf = float(row.get("match_confidence") or 0.7)

    # Prefer channels_json when it exists (richer, already deduplicated at write time)
    cj = row.get("channels_json")
    if cj:
        raw_channels = json.loads(cj) if isinstance(cj, str) else cj
        if isinstance(raw_channels, list):
            for c in raw_channels:
                channels.append(ContactChannel(
                    type=c.get("type", ""),
                    value=c.get("value", ""),
                    source=c.get("source", source),
                    confidence=float(c.get("confidence", conf)),
                ))
            return channels

    # Fall back to flat columns
    if row.get("email"):
        channels.append(ContactChannel(type="email", value=row["email"], source=source, confidence=conf))
    if row.get("linkedin_url"):
        channels.append(ContactChannel(type="linkedin", value=row["linkedin_url"], source=source, confidence=conf))
    if row.get("twitter_url"):
        channels.append(ContactChannel(type="twitter", value=row["twitter_url"], source=source, confidence=conf))
    return channels


def _channels_from_crm_row(row: Dict[str, Any]) -> List[ContactChannel]:
    """Build ContactChannel list from a crm_contacts / crm_leads contacts_json entry."""
    channels: List[ContactChannel] = []
    source = row.get("_source", "crm_import")
    conf = 0.6

    email = row.get("email") or row.get("contact_email")
    linkedin = row.get("linkedin_url") or row.get("linkedin") or row.get("contact_linkedin")
    twitter = row.get("twitter_url") or row.get("twitter")

    if email:
        channels.append(ContactChannel(type="email", value=email, source=source, confidence=conf))
    if linkedin:
        channels.append(ContactChannel(type="linkedin", value=linkedin, source=source, confidence=conf))
    if twitter:
        channels.append(ContactChannel(type="twitter", value=twitter, source=source, confidence=conf))
    return channels


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------

def _dedupe_channels(channels: List[ContactChannel]) -> List[ContactChannel]:
    """Keep highest-priority (source + confidence) entry per (type, normalized_value)."""
    best: Dict[Tuple[str, str], ContactChannel] = {}
    for ch in channels:
        key = (ch.type, ch.value.lower().strip("/"))
        existing = best.get(key)
        if existing is None:
            best[key] = ch
        elif (_source_rank(ch.source), -ch.confidence) < (_source_rank(existing.source), -existing.confidence):
            best[key] = ch
    return list(best.values())


# ---------------------------------------------------------------------------
# Main resolver
# ---------------------------------------------------------------------------

def _norm_key(name: str) -> str:
    return name.lower().replace(" ", "_").strip()


def resolve_contacts(con, name: str, allocator_id: Optional[str] = None) -> ContactProfile:
    """
    Merge all contact sources for an LP into one deduplicated ContactProfile.

    If allocator_id is not provided, performs a name-based lookup.
    """
    # Resolve allocator_id from name if not given
    resolved_id = allocator_id
    matched_name = name
    if not resolved_id:
        try:
            from contra.intelligence.resolver import resolve as _resolve
            match = _resolve(con, name)
            resolved_id = match.allocator_id
            matched_name = match.matched_name or name
        except Exception:
            pass

    name_key = _norm_key(name)
    all_channels: List[ContactChannel] = []
    person_meta: Dict[str, Any] = {}

    # Source 1: allocator_contacts (LinkedIn export + gate research + analyst)
    if resolved_id:
        for row in _fetch_allocator_contacts(con, resolved_id):
            all_channels.extend(_channels_from_row(row))
            # Pick richest person metadata
            for field_name in ("full_name", "title", "company", "location"):
                if row.get(field_name) and not person_meta.get(field_name):
                    person_meta[field_name] = row[field_name]

    # Source 2: crm_contacts (legacy CRM import)
    for row in _fetch_crm_contacts(con, name_key):
        all_channels.extend(_channels_from_crm_row(row))

    # Source 3: crm_leads contacts_json snapshot
    for row in _fetch_crm_leads_contacts(con, name_key):
        all_channels.extend(_channels_from_crm_row(row))

    # Deduplicate
    deduped = _dedupe_channels(all_channels)

    person = ContactPerson(
        full_name=person_meta.get("full_name") or matched_name,
        title=person_meta.get("title"),
        company=person_meta.get("company"),
        location=person_meta.get("location"),
        channels=sorted(deduped, key=lambda c: (_source_rank(c.source), -c.confidence)),
    )

    profile = ContactProfile(
        investor_name=matched_name,
        allocator_id=resolved_id,
        contacts=[person] if deduped else [],
    )

    return profile

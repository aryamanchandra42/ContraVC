"""
Contact Hunter using Tavily web search and Verifalia email verification.

Replaces the Phantombuster integration. Searches the web for an LP's contact details,
extracts them via regex, and verifies emails before persisting.

Each extracted contact value is associated with the source URL it was found on,
stored as `context_url` inside the channels_json blob so incorrect finds can be traced.

Provider selection:
  CONTACT_HUNTER_SEARCH_PROVIDER env var (default: "anthropic").
  Uses Claude's built-in web search — no extra API key needed if ANTHROPIC_API_KEY
  is already set. Falls back to Tavily on quota/rate-limit errors.
"""

from __future__ import annotations

import logging
import os
from typing import Dict, List, Optional

from agents.research.web_search import (
    get_search_provider, SearchUnavailable, TavilyProvider, AnthropicWebSearchProvider,
)
from agents.research.verifalia_client import verify_emails
from contra.intelligence.contact_extract import _extract_from_text, _dedupe_channels, _upsert_gate_contact

logger = logging.getLogger(__name__)

_QUOTA_PHRASES = ("insufficient_quota", "rate_limit", "429", "quota exceeded", "billing")


def _is_quota_error(exc: Exception) -> bool:
    return any(p in str(exc).lower() for p in _QUOTA_PHRASES)


def _get_contact_provider():
    """
    Return the best available search provider for contact hunting.

    Prefers CONTACT_HUNTER_SEARCH_PROVIDER (default: tavily) over the global
    PULSE_SEARCH_PROVIDER setting so an exhausted OpenAI quota doesn't block
    contact searches.
    """
    pref = os.environ.get("CONTACT_HUNTER_SEARCH_PROVIDER", "anthropic").lower().strip()
    try:
        return get_search_provider(pref)
    except SearchUnavailable:
        # If preferred provider isn't configured, fall back to auto
        return get_search_provider("auto")


def hunt_and_persist_contacts(
    con,
    lp_name: str,
    allocator_id: str,
    max_results: int = 10
) -> Dict[str, int]:
    """
    Search web for contact info, verify emails via Verifalia, and persist to allocator_contacts.

    Each email / LinkedIn / Twitter value is tagged with the URL of the search result it
    came from (`context_url` inside channels_json) so incorrect results can be traced.
    """
    try:
        provider = _get_contact_provider()
    except SearchUnavailable:
        logger.warning(f"Contact Hunter: Web search unavailable for {lp_name}")
        return {"emails": 0, "linkedin": 0, "twitter": 0}

    query = f'"{lp_name}" email address OR contact OR linkedin'
    logger.info(f"Contact Hunter: Searching web for {lp_name} contacts...")

    try:
        resp = provider.search(query, max_results=max_results)
    except Exception as exc:
        if _is_quota_error(exc):
            # Primary provider hit a quota/rate-limit — retry with Anthropic, then Tavily
            logger.warning(
                f"Contact Hunter: Search failed for {lp_name} (quota/rate-limit), "
                f"retrying with fallback provider: {exc}"
            )
            fallback_providers = [AnthropicWebSearchProvider, TavilyProvider]
            resp = None
            for fb_cls in fallback_providers:
                try:
                    fb = fb_cls()
                    resp = fb.search(query, max_results=max_results)
                    break
                except SearchUnavailable:
                    continue
                except Exception as retry_exc:
                    logger.warning(
                        f"Contact Hunter: {fb_cls.__name__} retry also failed for {lp_name}: {retry_exc}"
                    )
            if resp is None:
                logger.error(f"Contact Hunter: All fallback providers failed for {lp_name}")
                return {"emails": 0, "linkedin": 0, "twitter": 0}
        else:
            logger.error(f"Contact Hunter: Search failed for {lp_name}: {exc}")
            return {"emails": 0, "linkedin": 0, "twitter": 0}

    # Maps value → first source URL where it was found (keeps the most-specific origin).
    email_source_url: Dict[str, str] = {}
    linkedin_source_url: Dict[str, str] = {}
    twitter_source_url: Dict[str, str] = {}

    for r in resp.results:
        result_url = r.url or ""
        text = f"{r.title}\n{r.url}\n{r.snippet}\n"
        if r.raw_content:
            # First 5000 chars of raw content to avoid overflowing regex
            text += r.raw_content[:5000]

        emails, linkedin_urls, twitter_urls = _extract_from_text(text)

        for e in emails:
            if e not in email_source_url:
                email_source_url[e] = result_url
        for u in linkedin_urls:
            if u not in linkedin_source_url:
                linkedin_source_url[u] = result_url
        for u in twitter_urls:
            if u not in twitter_source_url:
                twitter_source_url[u] = result_url

    raw_emails = list(email_source_url.keys())

    verified_emails: List[str] = []
    unverified_emails: List[str] = []

    if raw_emails:
        logger.info(f"Contact Hunter: Found {len(raw_emails)} raw emails for {lp_name}. Verifying...")
        verify_results = verify_emails(raw_emails)

        for email, status in verify_results.items():
            if status == "Deliverable":
                verified_emails.append(email)
                logger.info(f"Contact Hunter: Email {email} is Deliverable.")
            elif status == "Unknown":
                unverified_emails.append(email)
                logger.info(f"Contact Hunter: Email {email} verification skipped/unknown.")
            else:
                logger.info(f"Contact Hunter: Email {email} rejected (Status: {status}).")

    linkedin_urls = list(linkedin_source_url.keys())
    twitter_urls = list(twitter_source_url.keys())

    if not verified_emails and not unverified_emails and not linkedin_urls and not twitter_urls:
        logger.info(f"Contact Hunter: No valid contacts found for {lp_name}.")
        return {"emails": 0, "linkedin": 0, "twitter": 0}

    # Build channels list manually so we can attach the context_url for each value.
    channels: List[Dict] = []
    for e in verified_emails:
        channels.append({
            "type": "email", "value": e,
            "source": "web_hunter", "confidence": 0.85,
            "context_url": email_source_url.get(e, ""),
        })
    for e in unverified_emails:
        channels.append({
            "type": "email", "value": e,
            "source": "web_hunter", "confidence": 0.60,
            "context_url": email_source_url.get(e, ""),
        })
    for u in linkedin_urls:
        channels.append({
            "type": "linkedin", "value": u,
            "source": "web_hunter", "confidence": 0.85,
            "context_url": linkedin_source_url.get(u, ""),
        })
    for u in twitter_urls:
        channels.append({
            "type": "twitter", "value": u,
            "source": "web_hunter", "confidence": 0.85,
            "context_url": twitter_source_url.get(u, ""),
        })

    channels = _dedupe_channels(channels)

    _upsert_gate_contact(con, allocator_id, lp_name, channels, "web_hunter")
    logger.info(
        f"Contact Hunter: Persisted {len(verified_emails) + len(unverified_emails)} emails, "
        f"{len(linkedin_urls)} LI, {len(twitter_urls)} TW for {lp_name}"
    )

    return {
        "emails": len(verified_emails) + len(unverified_emails),
        "linkedin": len(linkedin_urls),
        "twitter": len(twitter_urls),
    }

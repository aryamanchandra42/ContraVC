"""
Contact Hunter using Tavily web search and Verifalia email verification.

Replaces the Phantombuster integration. Searches the web for an LP's contact details,
extracts them via regex, and verifies emails before persisting.
"""

from __future__ import annotations

import logging
from typing import Dict, List, Optional, Set

from agents.research.web_search import get_search_provider, SearchUnavailable
from agents.research.verifalia_client import verify_emails
from contra.intelligence.contact_extract import _extract_from_text, build_channels, _dedupe_channels, _upsert_gate_contact

logger = logging.getLogger(__name__)


def hunt_and_persist_contacts(
    con,
    lp_name: str,
    allocator_id: str,
    max_results: int = 10
) -> Dict[str, int]:
    """
    Search web for contact info, verify emails via Verifalia, and persist to allocator_contacts.
    """
    try:
        provider = get_search_provider()
    except SearchUnavailable:
        logger.warning(f"Contact Hunter: Web search unavailable for {lp_name}")
        return {"emails": 0, "linkedin": 0, "twitter": 0}
        
    query = f'"{lp_name}" email address OR contact OR linkedin'
    logger.info(f"Contact Hunter: Searching web for {lp_name} contacts...")
    
    try:
        resp = provider.search(query, max_results=max_results)
    except Exception as exc:
        logger.error(f"Contact Hunter: Search failed for {lp_name}: {exc}")
        return {"emails": 0, "linkedin": 0, "twitter": 0}
        
    combined_text = ""
    for r in resp.results:
        combined_text += f"{r.title}\n{r.url}\n{r.snippet}\n"
        if r.raw_content:
            # First 5000 chars of raw content to avoid overflowing regex
            combined_text += f"{r.raw_content[:5000]}\n"
            
    raw_emails, linkedin, twitter = _extract_from_text(combined_text)
    
    verified_emails: List[str] = []
    unverified_emails: List[str] = []
    
    if raw_emails:
        # Dedupe before verifying to save credits
        unique_emails = list(set(raw_emails))
        logger.info(f"Contact Hunter: Found {len(unique_emails)} raw emails for {lp_name}. Verifying...")
        
        verify_results = verify_emails(unique_emails)
        
        for email, status in verify_results.items():
            if status == "Deliverable":
                verified_emails.append(email)
                logger.info(f"Contact Hunter: Email {email} is Deliverable.")
            elif status == "Unknown":
                unverified_emails.append(email)
                logger.info(f"Contact Hunter: Email {email} verification skipped/unknown.")
            else:
                logger.info(f"Contact Hunter: Email {email} rejected (Status: {status}).")
                
    if not verified_emails and not unverified_emails and not linkedin and not twitter:
        logger.info(f"Contact Hunter: No valid contacts found for {lp_name}.")
        return {"emails": 0, "linkedin": 0, "twitter": 0}
        
    # Build channels dicts using confidence 0.85 for verified emails
    channels = build_channels(verified_emails, linkedin, twitter, "web_hunter", 0.85)
    # Add unverified emails with lower confidence
    channels.extend(build_channels(unverified_emails, [], [], "web_hunter", 0.60))
    
    channels = _dedupe_channels(channels)
    
    _upsert_gate_contact(con, allocator_id, lp_name, channels, "web_hunter")
    logger.info(f"Contact Hunter: Persisted {len(verified_emails) + len(unverified_emails)} emails, {len(linkedin)} LI, {len(twitter)} TW for {lp_name}")
    
    return {
        "emails": len(verified_emails) + len(unverified_emails),
        "linkedin": len(linkedin),
        "twitter": len(twitter)
    }

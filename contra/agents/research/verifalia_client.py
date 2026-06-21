"""
Verifalia API client for email deliverability verification.

Checks SMTP handshake/inbox status via the Verifalia REST API.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import time
import urllib.request
import urllib.error
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# https://verifalia.com/developers#api-endpoints-base-urls
_BASE_URL = "https://api.verifalia.com/v2.4"

class VerifaliaError(Exception):
    pass


def _get_auth_header() -> str:
    username = os.environ.get("VERIFALIA_USERNAME", "").strip()
    password = os.environ.get("VERIFALIA_PASSWORD", "").strip()
    
    if not username or not password:
        logger.warning("Verifalia credentials not set (VERIFALIA_USERNAME / VERIFALIA_PASSWORD). Verification will be skipped.")
        return ""
        
    auth_str = f"{username}:{password}"
    b64_auth = base64.b64encode(auth_str.encode("utf-8")).decode("ascii")
    return f"Basic {b64_auth}"


def verify_emails(emails: List[str]) -> Dict[str, str]:
    """
    Submits a batch of emails to Verifalia and waits for results.
    Returns a dict mapping {email: status} (e.g. "Deliverable", "Undeliverable", "Risky", "Unknown").
    If credentials aren't set or an error occurs, returns {e: "Unknown" for e in emails}.
    """
    if not emails:
        return {}
        
    auth_header = _get_auth_header()
    if not auth_header:
        return {e: "Unknown" for e in emails}
        
    # 1. Submit the job
    # https://verifalia.com/developers#email-verifications
    submit_url = f"{_BASE_URL}/email-validations"
    payload = {
        "entries": [{"inputData": e} for e in emails]
    }
    
    req = urllib.request.Request(
        submit_url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": auth_header,
            "Content-Type": "application/json",
            "Accept": "application/json"
        },
        method="POST"
    )
    
    try:
        with urllib.request.urlopen(req) as resp:
            data = json.loads(resp.read().decode())
            job_id = data.get("overview", {}).get("id")
            if not job_id:
                raise VerifaliaError(f"No job ID returned: {data}")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")
        logger.error(f"Verifalia submit error {exc.code}: {body}")
        return {e: "Unknown" for e in emails}
    except Exception as exc:
        logger.error(f"Verifalia network error: {exc}")
        return {e: "Unknown" for e in emails}
        
    # 2. Poll for completion
    poll_url = f"{_BASE_URL}/email-validations/{job_id}"
    req_poll = urllib.request.Request(
        poll_url,
        headers={"Authorization": auth_header, "Accept": "application/json"},
        method="GET"
    )
    
    max_attempts = 15
    for attempt in range(max_attempts):
        try:
            with urllib.request.urlopen(req_poll) as resp:
                data = json.loads(resp.read().decode())
                overview = data.get("overview", {})
                status = overview.get("status")
                
                if status == "Completed":
                    results = {}
                    entries = data.get("entries", {}).get("data", [])
                    for entry in entries:
                        email = entry.get("inputData")
                        # e.g., Deliverable, Undeliverable, Risky, Unknown
                        classification = entry.get("classification")
                        if email and classification:
                            results[email] = classification
                            
                    # Delete job to clean up / anonymize right away
                    _delete_job(job_id, auth_header)
                    return results
                    
                if status in ("Expired", "Deleted"):
                    logger.warning(f"Verifalia job {job_id} ended with status {status}")
                    return {e: "Unknown" for e in emails}
                    
        except urllib.error.HTTPError as exc:
            body = exc.read().decode(errors="replace")
            logger.error(f"Verifalia poll error {exc.code}: {body}")
            return {e: "Unknown" for e in emails}
        except Exception as exc:
            logger.error(f"Verifalia network error during poll: {exc}")
            return {e: "Unknown" for e in emails}
            
        time.sleep(2.0)
        
    logger.warning(f"Verifalia poll timed out for job {job_id}")
    return {e: "Unknown" for e in emails}


def _delete_job(job_id: str, auth_header: str):
    """Clean up job to remove PII from Verifalia servers."""
    url = f"{_BASE_URL}/email-validations/{job_id}"
    req = urllib.request.Request(
        url,
        headers={"Authorization": auth_header},
        method="DELETE"
    )
    try:
        with urllib.request.urlopen(req):
            pass
    except Exception as exc:
        logger.debug(f"Failed to delete Verifalia job {job_id}: {exc}")

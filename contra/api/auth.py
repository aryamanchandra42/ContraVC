"""Simple shared-password auth for Contra Console.

Set CONSOLE_PASSWORD in the environment. When unset or empty, auth is disabled
(local-dev convenience). When set, every /api/* call except /api/health and
/api/auth/login requires Authorization: Bearer <token>.

Tokens are HMAC-signed, expire after CONSOLE_TOKEN_HOURS (default 168 = 7 days).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import time
from typing import Optional

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

_bearer = HTTPBearer(auto_error=False)


def _password() -> str:
    return os.environ.get("CONSOLE_PASSWORD", "").strip()


def auth_enabled() -> bool:
    return bool(_password())


def _secret() -> bytes:
    # Derive a signing key from the password so rotating the password invalidates tokens.
    pw = _password() or "dev-insecure"
    return hashlib.sha256(f"contra-console:{pw}".encode()).digest()


def _token_ttl_seconds() -> int:
    try:
        hours = float(os.environ.get("CONSOLE_TOKEN_HOURS", "168") or 168)
    except ValueError:
        hours = 168.0
    return max(1, int(hours * 3600))


def issue_token() -> str:
    """Return a signed token: base64(exp).sig"""
    exp = int(time.time()) + _token_ttl_seconds()
    payload = str(exp).encode()
    sig = hmac.new(_secret(), payload, hashlib.sha256).digest()
    return (
        base64.urlsafe_b64encode(payload).decode().rstrip("=")
        + "."
        + base64.urlsafe_b64encode(sig).decode().rstrip("=")
    )


def _b64decode(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def verify_token(token: str) -> bool:
    try:
        payload_b64, sig_b64 = token.split(".", 1)
        payload = _b64decode(payload_b64)
        sig = _b64decode(sig_b64)
        expected = hmac.new(_secret(), payload, hashlib.sha256).digest()
        if not hmac.compare_digest(sig, expected):
            return False
        exp = int(payload.decode())
        return time.time() < exp
    except Exception:
        return False


def check_password(password: str) -> bool:
    expected = _password()
    if not expected:
        return True
    return hmac.compare_digest(password.strip(), expected)


async def require_auth(
    request: Request,
    creds: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
) -> None:
    """FastAPI dependency — no-op when CONSOLE_PASSWORD is unset."""
    if not auth_enabled():
        return
    path = request.url.path
    if path in ("/api/health", "/api/auth/login", "/api/auth/status"):
        return
    if creds is None or not verify_token(creds.credentials):
        raise HTTPException(status_code=401, detail="Authentication required")

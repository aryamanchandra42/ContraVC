"""Auth endpoints — shared-password login for Contra Console."""

from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from api.auth import auth_enabled, check_password, issue_token, _token_ttl_seconds

router = APIRouter()


class LoginRequest(BaseModel):
    password: str = Field(min_length=1, max_length=200)


class LoginResponse(BaseModel):
    token: str
    expires_in: int


@router.get("/auth/status")
def auth_status() -> Dict[str, Any]:
    return {"auth_required": auth_enabled()}


@router.post("/auth/login", response_model=LoginResponse)
def login(body: LoginRequest) -> LoginResponse:
    if not auth_enabled():
        # Dev mode: issue a token anyway so the client can store something.
        return LoginResponse(token=issue_token(), expires_in=_token_ttl_seconds())
    if not check_password(body.password):
        raise HTTPException(status_code=401, detail="Invalid password")
    return LoginResponse(token=issue_token(), expires_in=_token_ttl_seconds())

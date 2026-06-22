"""Anonymous teaser session minting (gated by Cloudflare Turnstile).

POST /session/anon  → { session_token, expires_in, sid }

The frontend solves a Turnstile challenge on load and posts the token here; the
backend verifies it with Cloudflare and returns a short-lived session token the
teaser endpoints accept via the `X-EdgeLane-Session` header. In dev
(AUTH_ENABLED=false) it skips verification but still issues a token, so the
frontend flow is identical locally.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from ..config import get_settings
from .. import session_auth

router = APIRouter()


class AnonSessionRequest(BaseModel):
    turnstile_token: str | None = None


@router.post("/session/anon")
async def create_anon_session(req: AnonSessionRequest, request: Request):
    settings = get_settings()
    if settings.auth_enabled:
        if not req.turnstile_token:
            raise HTTPException(400, "turnstile_token required")
        ip = request.headers.get("cf-connecting-ip") or (
            request.client.host if request.client else None)
        if not await session_auth.verify_turnstile(req.turnstile_token, ip):
            raise HTTPException(403, "Turnstile verification failed")
    return session_auth.mint_anon_session()

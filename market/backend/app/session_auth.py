"""Anonymous teaser sessions, gated by Cloudflare Turnstile.

Anonymous visitors may see only the teaser chips (spot / bias / walls). To stop
casual curl/bot scraping of even that, the flow is:

  1. The page loads a Turnstile widget; the user solves it (usually invisibly).
  2. Frontend POSTs the Turnstile token to /session/anon.
  3. Backend verifies it with Cloudflare, then mints a SHORT-LIVED signed
     session token (HS256, ANON_SESSION_SECRET). The teaser endpoints accept it
     via the `X-EdgeLane-Session` header.

This replaces a static shared key (which would be readable in the browser's
Network tab) with a per-session, expiring token a bot can't pre-fabricate.

Full product data (picks/scoring/strategies/lookup/orders) requires a real
Supabase login — see auth.py.
"""
from __future__ import annotations

import logging
import secrets
import time

import httpx
import jwt
from fastapi import Depends, Header, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from . import auth
from .config import get_settings

log = logging.getLogger("edgelane.market.session")

_bearer = HTTPBearer(auto_error=False)
_TURNSTILE_VERIFY = "https://challenges.cloudflare.com/turnstile/v0/siteverify"
_ALG = "HS256"
_KIND = "anon"


async def verify_turnstile(token: str, remote_ip: str | None = None) -> bool:
    """Verify a Turnstile token with Cloudflare. Returns True on success."""
    settings = get_settings()
    if not settings.turnstile_secret:
        # Misconfiguration in prod; in dev we never reach here (auth disabled).
        log.error("[session] TURNSTILE_SECRET not configured")
        return False
    data = {"secret": settings.turnstile_secret, "response": token}
    if remote_ip:
        data["remoteip"] = remote_ip
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post(_TURNSTILE_VERIFY, data=data)
            body = r.json()
            if not body.get("success"):
                log.warning("[session] Turnstile failed: %s", body.get("error-codes"))
            return bool(body.get("success"))
    except Exception as exc:
        log.error("[session] Turnstile verify error: %s", exc)
        return False


def mint_anon_session() -> dict:
    """Mint a short-lived anonymous teaser session token."""
    settings = get_settings()
    if not settings.anon_session_secret:
        raise HTTPException(500, "ANON_SESSION_SECRET is not configured")
    now = int(time.time())
    ttl = int(settings.anon_session_ttl_sec)
    sid = secrets.token_urlsafe(16)
    payload = {"sub": sid, "kind": _KIND, "iat": now, "exp": now + ttl}
    token = jwt.encode(payload, settings.anon_session_secret, algorithm=_ALG)
    return {"session_token": token, "expires_in": ttl, "sid": sid}


def _decode_anon(token: str) -> dict:
    settings = get_settings()
    if not settings.anon_session_secret:
        raise HTTPException(500, "ANON_SESSION_SECRET is not configured")
    payload = jwt.decode(token, settings.anon_session_secret, algorithms=[_ALG])
    if payload.get("kind") != _KIND:
        raise HTTPException(401, "not an anon session token")
    return payload


def require_teaser_session(
    request: Request,
    creds: HTTPAuthorizationCredentials = Depends(_bearer),
    x_edgelane_session: str | None = Header(default=None),
) -> dict:
    """Gate the teaser endpoints AND signal teaser-vs-full in one dependency.

    Returns {"full": bool, "kind": str, "id": str}:
      * full=True  → logged-in user / admin / dev → return the complete payload
      * full=False → valid anon session            → return the redacted teaser

    Raises 401 when AUTH_ENABLED and the caller has neither a valid Supabase
    JWT/admin token nor a valid anon session token.
    """
    settings = get_settings()

    # Admin bypass (server secret) — full access.
    if auth._admin_token_ok(request):
        return {"full": True, "kind": "admin", "id": "admin"}

    # Dev: gates open, full payload.
    if not settings.auth_enabled:
        return {"full": True, "kind": "dev", "id": "dev-local"}

    # Logged-in user → full.
    if creds is not None:
        try:
            user = auth._user_from_payload(auth._decode(creds.credentials))
            return {"full": True, "kind": "user", "id": user["id"]}
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(401, "Invalid or expired session")

    # Anonymous → must present a valid anon session token (teaser only).
    if x_edgelane_session:
        try:
            payload = _decode_anon(x_edgelane_session)
            return {"full": False, "kind": "anon", "id": payload.get("sub", "")}
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(401, "Invalid or expired teaser session")

    raise HTTPException(401, "Teaser session required — solve the challenge and call /session/anon")

"""Auth proxy — the browser talks ONLY to this API, never to Supabase.

Architecture decision (2026-08-16, superseding the Matrix pattern): Supabase
remains the identity provider, but its endpoints are reached exclusively
server-side. The client surface is these five routes; the Supabase URL and keys
never ship to the browser, and the platform's single public surface is this API
(one place for rate limiting, lockouts, and observability).

Trade-offs accepted knowingly:
  * This backend now sees raw passwords in transit (TLS-terminated at the
    tunnel edge, never logged, never persisted) — a liability Supabase's
    browser SDK otherwise carries.
  * The refresh flow is ours to run; the client schedules refresh before
    expiry and retries once on 401.
  * Confirmation / reset emails are still SENT by Supabase either way.

All five endpoints are deliberately unauthenticated (they ARE the auth) and sit
behind the existing per-session rate-limit middleware. Error passthrough keeps
Supabase's human-readable messages ("Invalid login credentials", "User already
registered") without leaking anything else.
"""
from __future__ import annotations

import logging

import httpx
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, field_validator

from ..config import get_settings

log = logging.getLogger("edgelane.market.authproxy")
router = APIRouter()

_TIMEOUT = 15.0


def _client_ip(request: Request) -> str | None:
    return request.headers.get("cf-connecting-ip") or (
        request.client.host if request.client else None)


def _gotrue():
    s = get_settings()
    if not (s.supabase_url and s.supabase_service_key):
        raise HTTPException(503, "auth backend not configured")
    base = s.supabase_url.rstrip("/") + "/auth/v1"
    # The service key is a valid apikey for GoTrue and never leaves the server.
    headers = {"apikey": s.supabase_service_key,
               "Content-Type": "application/json"}
    return base, headers


def _session(payload: dict) -> dict | None:
    """Normalize a GoTrue token response into the client's session shape."""
    if not payload or not payload.get("access_token"):
        return None
    return {
        "access_token": payload["access_token"],
        "refresh_token": payload.get("refresh_token"),
        "expires_in": payload.get("expires_in"),
        "expires_at": payload.get("expires_at"),
        "token_type": payload.get("token_type", "bearer"),
        "user": {
            "id": (payload.get("user") or {}).get("id"),
            "email": (payload.get("user") or {}).get("email"),
        },
    }


def _passthrough_error(r: httpx.Response) -> HTTPException:
    """Surface Supabase's human-readable auth message, nothing more."""
    try:
        body = r.json()
        msg = body.get("msg") or body.get("message") or body.get(
            "error_description") or body.get("error") or "authentication failed"
    except ValueError:
        msg = "authentication failed"
    status = r.status_code if r.status_code in (400, 401, 403, 422, 429) else 502
    return HTTPException(status, str(msg)[:300])


async def _post(path: str, json_body: dict, params: dict | None = None,
                client_ip: str | None = None) -> dict:
    base, headers = _gotrue()
    if client_ip:
        headers = {**headers, "X-Forwarded-For": client_ip,
                   "X-Real-IP": client_ip}
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
            r = await c.post(f"{base}{path}", headers=headers,
                             params=params or {}, json=json_body)
    except httpx.HTTPError as e:
        log.warning("auth proxy upstream error on %s: %s", path, e)
        raise HTTPException(502, "auth service unreachable")
    if r.status_code >= 400:
        raise _passthrough_error(r)
    try:
        return r.json()
    except ValueError:
        return {}


class Credentials(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=6, max_length=200)

    @field_validator("email")
    @classmethod
    def _email_shape(cls, v: str) -> str:
        # Light shape check only — Supabase is the authority on validity, and
        # EmailStr would add an email-validator dependency for no gain.
        v = v.strip().lower()
        if "@" not in v or v.startswith("@") or v.endswith("@") or " " in v:
            raise ValueError("invalid email")
        return v


class RefreshBody(BaseModel):
    refresh_token: str = Field(min_length=8, max_length=2000)


class EmailBody(BaseModel):
    email: str = Field(min_length=3, max_length=254)

    _shape = field_validator("email")(Credentials._email_shape.__func__)


@router.post("/auth/signup")
async def signup(body: Credentials, request: Request):
    data = await _post("/signup", {"email": body.email, "password": body.password},
                       client_ip=_client_ip(request))
    sess = _session(data)
    # With email confirmation ON, GoTrue returns a user but no session.
    return {"session": sess, "confirmation_required": sess is None}


@router.post("/auth/login")
async def login(body: Credentials, request: Request):
    data = await _post("/token", {"email": body.email, "password": body.password},
                       params={"grant_type": "password"},
                       client_ip=_client_ip(request))
    sess = _session(data)
    if sess is None:
        raise HTTPException(401, "authentication failed")
    return {"session": sess}


@router.post("/auth/refresh")
async def refresh(body: RefreshBody):
    data = await _post("/token", {"refresh_token": body.refresh_token},
                       params={"grant_type": "refresh_token"})
    sess = _session(data)
    if sess is None:
        raise HTTPException(401, "refresh failed")
    return {"session": sess}


@router.post("/auth/logout")
async def logout(body: RefreshBody | None = None):
    """Best-effort revocation; always 200 so a client can clear local state
    unconditionally."""
    if body and body.refresh_token:
        try:
            base, headers = _gotrue()
            async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
                await c.post(f"{base}/logout", headers=headers,
                             json={"refresh_token": body.refresh_token})
        except (httpx.HTTPError, HTTPException):
            pass
    return {"ok": True}


@router.post("/auth/resend")
async def resend(body: EmailBody, request: Request):
    await _post("/resend", {"type": "signup", "email": body.email},
                client_ip=_client_ip(request))
    return {"ok": True}

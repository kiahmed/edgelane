"""Per-session request rate limiting (in-memory token bucket).

Bounds requests so a single caller can't outrun the number of live sessions.
The bucket key prefers the caller's session identity (Supabase bearer token or
anon session token) and falls back to the real client IP (read from
`CF-Connecting-IP` when behind the Cloudflare tunnel).

This is the app-level fairness layer. The real DDoS shield is Cloudflare's edge
(WAF + rate-limiting rules + Turnstile) in front of the tunnel — a single
home-PC origin can't absorb a volumetric flood on its own.

In-memory + single-process: fine for one uvicorn worker on a local host. Move to
Redis if the backend is ever horizontally scaled.
"""
from __future__ import annotations

import hashlib
import time

from fastapi.responses import JSONResponse

from . import auth
from .config import get_settings

# key -> (count, window_start_monotonic)
_buckets: dict[str, tuple[int, float]] = {}
_MAX_KEYS = 50_000  # safety cap; prune stale entries past this


def _client_ip(request) -> str:
    return request.headers.get("cf-connecting-ip") or (
        request.client.host if request.client else "unknown")


def _client_key(request) -> str:
    # /auth/* is UNAUTHENTICATED (it IS the auth). Keying on the caller-supplied
    # bearer would let a brute-forcer rotate a random token per request and dodge
    # the limiter entirely, and DoS nothing but themselves. Key on IP so password
    # guessing is actually bounded.
    if request.url.path.startswith("/auth/"):
        return "auth-ip:" + _client_ip(request)
    authz = request.headers.get("authorization") or ""
    if authz.lower().startswith("bearer "):
        return "u:" + hashlib.sha256(authz[7:].encode()).hexdigest()[:16]
    sess = request.headers.get("x-edgelane-session")
    if sess:
        return "s:" + hashlib.sha256(sess.encode()).hexdigest()[:16]
    return "ip:" + _client_ip(request)


def _prune(now: float, window: int) -> None:
    if len(_buckets) <= _MAX_KEYS:
        return
    stale = [k for k, (_, start) in _buckets.items() if now - start >= window]
    for k in stale:
        _buckets.pop(k, None)


async def rate_limit_middleware(request, call_next):
    settings = get_settings()
    # Off in dev; admin token is exempt (server-side curl/testing).
    if not settings.auth_enabled or auth._admin_token_ok(request):
        return await call_next(request)

    is_auth = request.url.path.startswith("/auth/")
    limit = (settings.rate_limit_auth_per_min if is_auth
             else settings.rate_limit_per_min)
    window = settings.rate_limit_window_sec
    now = time.monotonic()
    key = _client_key(request)

    count, start = _buckets.get(key, (0, now))
    if now - start >= window:
        count, start = 0, now
    count += 1
    _buckets[key] = (count, start)
    _prune(now, window)

    if count > limit:
        retry = max(1, int(window - (now - start)))
        return JSONResponse(
            status_code=429,
            content={"detail": "rate limit exceeded"},
            headers={"Retry-After": str(retry)},
        )
    return await call_next(request)

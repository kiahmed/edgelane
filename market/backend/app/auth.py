"""Authentication — verify Supabase-issued JWTs, with server-side bypasses.

The Vercel frontend handles signup / login / sessions against Supabase directly;
the backend's only job is to *verify* the access token (a JWT) the frontend
attaches as `Authorization: Bearer <token>` and resolve it to a user.

Supabase signs tokens either way, picked from each token's `alg` header:
  * HS256        — legacy symmetric signing (SUPABASE_JWT_SECRET)
  * ES256/RS256  — modern asymmetric; public keys from the project's JWKS endpoint

Two server-side bypasses (neither is ever shipped to a browser):
  * AUTH_ENABLED=false (dev / parity / e2e tests) → dependencies return a
    synthetic dev user; no token required. Production sets AUTH_ENABLED=true.
  * `X-Admin-Token: <ADMIN_API_TOKEN>` → curl/testing bypass (a server secret).
"""
from __future__ import annotations

import jwt
from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .config import get_settings

_bearer = HTTPBearer(auto_error=False)
_AUDIENCE = "authenticated"

_DEV_USER = {"id": "dev-local", "email": "dev@local", "fullName": "Dev",
             "plan": "free", "auth": "dev"}
_ADMIN_USER = {"id": "admin", "email": "admin@local", "fullName": "Admin",
               "plan": "pro", "auth": "admin"}

# JWKS client for asymmetric tokens — built lazily, cached (PyJWT caches keys
# for ~5 min internally).
_jwks_client = None
_jwks_url: str | None = None


def _get_jwks_client():
    global _jwks_client, _jwks_url
    settings = get_settings()
    if not settings.supabase_url:
        return None
    url = settings.supabase_url.rstrip("/") + "/auth/v1/.well-known/jwks.json"
    if _jwks_client is None or _jwks_url != url:
        _jwks_client = jwt.PyJWKClient(url)
        _jwks_url = url
    return _jwks_client


# Clock-skew tolerance for iat/exp. Supabase (GoTrue) stamps iat at issue time;
# a backend clock a few seconds behind — routine on WSL2/Docker-Desktop VMs after
# sleep/resume, and normal across any distributed pair — otherwise rejects a
# perfectly valid fresh token with ImmatureSignatureError ("not yet valid").
# 30s is the conventional allowance and far below any token lifetime.
_LEEWAY = 30


def _decode(token: str) -> dict:
    """Verify a Supabase JWT, choosing HS256 vs ES256/RS256 from its header."""
    settings = get_settings()
    alg = jwt.get_unverified_header(token).get("alg", "")

    if alg == "HS256":
        if not settings.supabase_jwt_secret:
            raise HTTPException(500, "SUPABASE_JWT_SECRET is not configured")
        return jwt.decode(token, settings.supabase_jwt_secret,
                          algorithms=["HS256"], audience=_AUDIENCE,
                          leeway=_LEEWAY)

    if alg in ("ES256", "RS256"):
        client = _get_jwks_client()
        if client is None:
            raise HTTPException(500, "SUPABASE_URL is not configured")
        signing_key = client.get_signing_key_from_jwt(token)
        return jwt.decode(token, signing_key.key,
                          algorithms=["ES256", "RS256"], audience=_AUDIENCE,
                          leeway=_LEEWAY)

    raise HTTPException(401, f"Unsupported token algorithm: {alg or 'none'}")


def _user_from_payload(payload: dict) -> dict:
    md = payload.get("user_metadata") or {}
    return {
        "id": payload.get("sub"),
        "email": payload.get("email", ""),
        "fullName": md.get("full_name", ""),
        "plan": "free",            # authoritative plan lives in profiles table
        "auth": "supabase",
    }


def _admin_token_ok(request: Request) -> bool:
    settings = get_settings()
    # Accept the admin secret via the X-Admin-Token header (curl/API) OR a
    # ?token= query param (browser navigation — same convenience the Torque page
    # already grants, now shared by every admin-gated route).
    tok = (request.headers.get("x-admin-token")
           or request.query_params.get("token")
           or "")
    return bool(settings.admin_api_token) and tok == settings.admin_api_token


def get_current_user(request: Request,
                     creds: HTTPAuthorizationCredentials = Depends(_bearer)) -> dict:
    """Require an authenticated user (Supabase JWT or admin token), else 401.

    When AUTH_ENABLED=false, returns a synthetic dev user (honoring a real
    token/admin header if one happens to be present).
    """
    if _admin_token_ok(request):
        return dict(_ADMIN_USER)

    settings = get_settings()
    if not settings.auth_enabled:
        if creds is not None:
            try:
                return _user_from_payload(_decode(creds.credentials))
            except Exception:
                pass
        return dict(_DEV_USER)

    if creds is None:
        raise HTTPException(401, "Not authenticated")
    try:
        return _user_from_payload(_decode(creds.credentials))
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(401, "Invalid or expired session")


def require_admin(request: Request,
                  user: dict = Depends(get_current_user)) -> dict:
    """Admin-ONLY gate (stricter than get_current_user, which lets any signed-in
    Supabase user through). Grants access only to the server admin token
    (`X-Admin-Token` / `?token=`), so server-owner tools — strike profiles,
    etc. — are invisible to regular users.

    When AUTH_ENABLED=false it's a no-op (dev convenience), matching the rest of
    the auth layer."""
    settings = get_settings()
    if not settings.auth_enabled:
        return user
    if user.get("auth") == "admin":
        return user
    raise HTTPException(403, "Admin only")


def get_optional_user(request: Request,
                      creds: HTTPAuthorizationCredentials = Depends(_bearer)) -> dict | None:
    """Return the user if a valid token/admin header is present, else None.
    Never raises — used to decide teaser-vs-full payloads."""
    try:
        if _admin_token_ok(request):
            return dict(_ADMIN_USER)
        if creds is not None:
            return _user_from_payload(_decode(creds.credentials))
    except Exception:
        return None
    return None

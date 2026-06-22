"""Server-side Supabase access via the service_role key (bypasses RLS).

ONLY the backend uses the service_role key — it must never reach the browser.
Used to read a user's plan (profiles) and their own broker credentials
(broker_configs) for the per-user order path.
"""
from __future__ import annotations

import logging
from typing import Optional

import httpx

from .config import get_settings

log = logging.getLogger("edgelane.market.supabase")


def _rest(settings):
    base = settings.supabase_url.rstrip("/") + "/rest/v1"
    headers = {
        "apikey": settings.supabase_service_key,
        "Authorization": f"Bearer {settings.supabase_service_key}",
    }
    return base, headers


async def _rpc(fn: str, payload: dict) -> Optional[list]:
    """Call a Postgres function via PostgREST RPC using the service_role key."""
    settings = get_settings()
    if not (settings.supabase_url and settings.supabase_service_key):
        log.debug("[supabase] URL/service key not configured; skipping rpc %s", fn)
        return None
    base, headers = _rest(settings)
    headers = {**headers, "Content-Type": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post(f"{base}/rpc/{fn}", headers=headers, json=payload)
            if r.status_code != 200:
                log.error("[supabase] rpc %s failed (%s): %s", fn, r.status_code, r.text[:200])
                return None
            return r.json()
    except Exception as exc:
        log.error("[supabase] rpc %s error: %s", fn, exc)
        return None


async def _select_one(table: str, key: str, value: str, select: str) -> Optional[dict]:
    settings = get_settings()
    if not (settings.supabase_url and settings.supabase_service_key):
        log.debug("[supabase] URL/service key not configured; skipping %s read", table)
        return None
    base, headers = _rest(settings)
    params = {key: f"eq.{value}", "select": select, "limit": "1"}
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.get(f"{base}/{table}", headers=headers, params=params)
            if r.status_code != 200:
                log.error("[supabase] %s read failed (%s): %s", table, r.status_code, r.text[:200])
                return None
            rows = r.json()
            return rows[0] if rows else None
    except Exception as exc:
        log.error("[supabase] %s read error (%s=%s): %s", table, key, value, exc)
        return None


async def get_profile_plan(user_id: str) -> Optional[str]:
    """Read a user's current plan from public.profiles (authoritative)."""
    row = await _select_one("profiles", "id", user_id, "plan")
    return row.get("plan") if row else None


async def insert_row(table: str, row: dict) -> bool:
    """Insert one row via PostgREST using the service_role key (bypasses RLS)."""
    settings = get_settings()
    if not (settings.supabase_url and settings.supabase_service_key):
        log.warning("[supabase] URL/service key not configured; cannot insert into %s", table)
        return False
    base, headers = _rest(settings)
    headers = {**headers, "Content-Type": "application/json", "Prefer": "return=minimal"}
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post(f"{base}/{table}", headers=headers, json=row)
            if r.status_code >= 300:
                log.error("[supabase] insert %s failed (%s): %s", table, r.status_code, r.text[:200])
                return False
            return True
    except Exception as exc:
        log.error("[supabase] insert %s error: %s", table, exc)
        return False


async def upload_object(bucket: str, path: str, content: bytes, content_type: str) -> bool:
    """Upload bytes to a Supabase Storage bucket via the service_role key."""
    settings = get_settings()
    if not (settings.supabase_url and settings.supabase_service_key):
        log.warning("[supabase] URL/service key not configured; cannot upload %s", path)
        return False
    url = f"{settings.supabase_url.rstrip('/')}/storage/v1/object/{bucket}/{path}"
    headers = {
        "apikey": settings.supabase_service_key,
        "Authorization": f"Bearer {settings.supabase_service_key}",
        "Content-Type": content_type or "application/octet-stream",
        "x-upsert": "true",
    }
    try:
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post(url, headers=headers, content=content)
            if r.status_code >= 300:
                log.error("[supabase] upload %s failed (%s): %s", path, r.status_code, r.text[:200])
                return False
            return True
    except Exception as exc:
        log.error("[supabase] upload %s error: %s", path, exc)
        return False


async def get_broker_config(user_id: str, config_id: Optional[str] = None) -> Optional[dict]:
    """Read a user's broker config with the token DECRYPTED (server-side only).

    The token is encrypted at rest (Vault key → broker_configs.tradier_token_enc;
    the plaintext column is always NULL). We decrypt via the service_role-only
    RPC `get_broker_secret`, never by selecting the raw column.

    config_id=None → the user's active connection (order path). A specific id →
    that connection (the UI "Test" button). Returns
    {id, broker, tradier_token, tradier_account_id, tradier_env} or None.
    """
    rows = await _rpc("get_broker_secret", {"p_user_id": user_id, "p_id": config_id})
    return rows[0] if rows else None

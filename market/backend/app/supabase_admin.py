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


async def get_user_email(user_id: str) -> Optional[str]:
    """Authoritative email for a user via the GoTrue admin API (auth.users is
    not exposed through PostgREST). Service-role only. Returns None on any
    failure / missing config so callers degrade to 'not emailed'."""
    settings = get_settings()
    if not (settings.supabase_url and settings.supabase_service_key):
        return None
    url = settings.supabase_url.rstrip("/") + f"/auth/v1/admin/users/{user_id}"
    headers = {
        "apikey": settings.supabase_service_key,
        "Authorization": f"Bearer {settings.supabase_service_key}",
    }
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.get(url, headers=headers)
            if r.status_code != 200:
                log.error("[supabase] admin user read failed (%s): %s",
                          r.status_code, r.text[:200])
                return None
            email = (r.json() or {}).get("email")
            return email or None
    except Exception as exc:
        log.error("[supabase] admin user read error for %s: %s", user_id, exc)
        return None


async def get_user_tools(user_id: str) -> list[str]:
    """Tool keys this user is entitled to (public.profiles.tools_enabled),
    authoritative server-side read. Returns [] if none / not found."""
    row = await _select_one("profiles", "id", user_id, "tools_enabled")
    tools = (row or {}).get("tools_enabled") or []
    return list(tools) if isinstance(tools, list) else []


async def get_users_with_tool(tool: str) -> list[str]:
    """Every profile id currently entitled to `tool` (tools_enabled contains
    it), read live from Supabase — the database is the source of truth for
    "who can use this," not a separately-maintained allowlist. Used by the
    news-signal webhook's fan-out: granting or revoking a user's
    news-reactor entitlement takes effect on the very next signal, no
    backend restart needed, and no TORQUE_OPERATOR_UIDS entry to remember to
    add. Returns [] if Supabase isn't configured or the query fails — the
    caller drops the signal rather than guessing who's entitled."""
    settings = get_settings()
    if not (settings.supabase_url and settings.supabase_service_key):
        log.debug("[supabase] URL/service key not configured; skipping tools-by-tool query")
        return []
    base, headers = _rest(settings)
    # PostgREST "contains" filter on a text[] column: tools_enabled=cs.{tool}
    params = {"tools_enabled": f"cs.{{{tool}}}", "select": "id"}
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.get(f"{base}/profiles", headers=headers, params=params)
            if r.status_code != 200:
                log.error("[supabase] profiles-by-tool query failed (%s): %s",
                          r.status_code, r.text[:200])
                return []
            rows = r.json()
            return [str(row["id"]) for row in rows if row.get("id")]
    except Exception as exc:
        log.error("[supabase] profiles-by-tool query error (tool=%s): %s", tool, exc)
        return []


async def grant_user_tools(user_id: str, tools: list[str]) -> bool:
    """Idempotently union `tools` into a user's profiles.tools_enabled, written
    via the service_role key (bypasses RLS). Used to seed the server operator(s)
    at startup from config — never grants from the browser. No-op (returns True)
    when the user already has every tool, or when Supabase isn't configured."""
    settings = get_settings()
    if not (settings.supabase_url and settings.supabase_service_key):
        log.debug("[supabase] URL/service key not configured; skipping tool grant")
        return False
    current = set(await get_user_tools(user_id))
    merged = sorted(current | set(tools))
    if merged == sorted(current):
        return True  # already entitled — nothing to write
    base, headers = _rest(settings)
    headers = {**headers, "Content-Type": "application/json", "Prefer": "return=minimal"}
    params = {"id": f"eq.{user_id}"}
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.patch(f"{base}/profiles", headers=headers, params=params,
                              json={"tools_enabled": merged})
            if r.status_code >= 300:
                log.error("[supabase] grant tools failed (%s): %s", r.status_code, r.text[:200])
                return False
            return True
    except Exception as exc:
        log.error("[supabase] grant tools error (uid=%s): %s", user_id, exc)
        return False


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


# ── Simmer additions (new helpers only — nothing above is modified) ─────────

async def select_many(table: str, select: str = "*",
                      filters: Optional[dict] = None,
                      order: Optional[str] = None,
                      limit: Optional[int] = None,
                      offset: Optional[int] = None) -> Optional[list]:
    """Read many rows via PostgREST with the service_role key (bypasses RLS).

    `filters` values carry their PostgREST operator (e.g. {"active": "eq.true",
    "symbol": "eq.NVDA"}). Returns the row list, or None when Supabase is not
    configured / the read failed — callers must treat None as "unknown", never
    as an empty table (the watcher falls back to config tickers on None).
    """
    settings = get_settings()
    if not (settings.supabase_url and settings.supabase_service_key):
        log.debug("[supabase] URL/service key not configured; skipping %s read", table)
        return None
    base, headers = _rest(settings)
    params: dict = {"select": select}
    for k, v in (filters or {}).items():
        params[k] = v
    if order:
        params["order"] = order
    if limit is not None:
        params["limit"] = str(int(limit))
    if offset is not None:
        params["offset"] = str(int(offset))
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.get(f"{base}/{table}", headers=headers, params=params)
            if r.status_code != 200:
                log.error("[supabase] %s list failed (%s): %s", table, r.status_code, r.text[:200])
                return None
            rows = r.json()
            return rows if isinstance(rows, list) else None
    except Exception as exc:
        log.error("[supabase] %s list error: %s", table, exc)
        return None


async def update_rows(table: str, filters: dict, values: dict) -> bool:
    """PATCH rows matching `filters` (PostgREST operator syntax) via the
    service_role key. Refuses to run without at least one filter — an
    unfiltered PATCH would rewrite the whole table."""
    settings = get_settings()
    if not (settings.supabase_url and settings.supabase_service_key):
        log.warning("[supabase] URL/service key not configured; cannot update %s", table)
        return False
    if not filters:
        log.error("[supabase] update_rows(%s) refused: empty filter", table)
        return False
    base, headers = _rest(settings)
    headers = {**headers, "Content-Type": "application/json", "Prefer": "return=minimal"}
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.patch(f"{base}/{table}", headers=headers, params=filters, json=values)
            if r.status_code >= 300:
                log.error("[supabase] update %s failed (%s): %s", table, r.status_code, r.text[:200])
                return False
            return True
    except Exception as exc:
        log.error("[supabase] update %s error: %s", table, exc)
        return False


async def delete_rows(table: str, filters: dict) -> bool:
    """DELETE rows matching PostgREST filters (service_role). REFUSES an empty
    filter — an unfiltered DELETE would wipe the table."""
    if not filters:
        log.error("[supabase] delete_rows(%s) called with no filters — refused", table)
        return False
    settings = get_settings()
    if not (settings.supabase_url and settings.supabase_service_key):
        log.warning("[supabase] URL/service key not configured; cannot delete from %s", table)
        return False
    base, headers = _rest(settings)
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.delete(f"{base}/{table}", headers=headers, params=filters)
        if r.status_code in (200, 204):
            return True
        log.error("[supabase] %s delete failed (%s): %s", table, r.status_code, r.text[:300])
    except Exception as e:
        log.error("[supabase] %s delete error: %s", table, e)
    return False


async def upsert_row(table: str, row: dict, on_conflict: str) -> bool:
    """Insert-or-merge one row via PostgREST upsert (service_role key)."""
    settings = get_settings()
    if not (settings.supabase_url and settings.supabase_service_key):
        log.warning("[supabase] URL/service key not configured; cannot upsert into %s", table)
        return False
    base, headers = _rest(settings)
    headers = {**headers, "Content-Type": "application/json",
               "Prefer": "return=minimal,resolution=merge-duplicates"}
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post(f"{base}/{table}", headers=headers,
                             params={"on_conflict": on_conflict}, json=row)
            if r.status_code >= 300:
                log.error("[supabase] upsert %s failed (%s): %s", table, r.status_code, r.text[:200])
                return False
            return True
    except Exception as exc:
        log.error("[supabase] upsert %s error: %s", table, exc)
        return False


# ── Torque news-signal persistence (see docs/torque.md) ─────────────────────

async def claim_news_signal(source_event_id: str, row: Optional[dict] = None) -> bool:
    """Atomically claim a source_event_id for POST /webhook/news_signal —
    True the first time (safe to place the order), False if already claimed
    (a replay, or the sender's own retry). `row` carries the rest of the
    signal log (full payload + key fields) so it lands in the SAME insert as
    the claim — the table is the first place a signal is persisted, and a
    second write could be lost. Deliberately NOT built on insert_row: a
    duplicate-key conflict here is the expected, normal idempotency case, not
    an error worth logging loudly."""
    settings = get_settings()
    if not (settings.supabase_url and settings.supabase_service_key):
        log.warning("[supabase] URL/service key not configured; cannot claim news signal")
        return False
    base, headers = _rest(settings)
    headers = {**headers, "Content-Type": "application/json", "Prefer": "return=minimal"}
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post(f"{base}/news_reactor_signals", headers=headers,
                             json={**(row or {}), "source_event_id": str(source_event_id)})
            if r.status_code in (200, 201):
                return True
            if r.status_code == 409:
                return False   # already claimed — expected, not an error
            log.error("[supabase] claim_news_signal failed (%s): %s", r.status_code, r.text[:200])
            return False
    except Exception as exc:
        log.error("[supabase] claim_news_signal error: %s", exc)
        return False


async def record_news_signal_outcome(source_event_id: str, outcome: str, reason: Optional[str]) -> bool:
    """Stamp what Torque decided (placed / dropped / cancel_processed, and
    why) onto a claimed signal's log row."""
    return await update_rows("news_reactor_signals",
                             {"source_event_id": f"eq.{source_event_id}"},
                             {"outcome": outcome, "reason": reason})


async def insert_torque_order(row: dict) -> bool:
    """Record which order, on which account, resulted from which news-signal
    event — one row per (signal, qualified account). Correlation index for a
    future CANCEL signal to find and act on exactly the right order(s)."""
    return await insert_row("torque_orders", row)


async def get_torque_orders_by_event(source_event_id: str) -> list[dict]:
    """Every (account, order) pair a prior news-signal fanned out to — the
    correlation a CANCEL signal (state="cancel") needs to find and act on
    exactly the right order(s) across every account that received the
    original signal."""
    rows = await select_many("torque_orders", filters={"source_event_id": f"eq.{source_event_id}"})
    return rows or []


async def update_torque_order_status(entry_order_id: str, status: str,
                                     reason: Optional[str] = None) -> bool:
    """Record a signal order's latest state (broker status or the Torque
    action taken on it). A no-op for an order id that isn't a signal order."""
    from datetime import datetime, timezone
    return await update_rows("torque_orders", {"entry_order_id": f"eq.{entry_order_id}"},
                             {"status": status, "status_reason": reason,
                              "status_at": datetime.now(timezone.utc).isoformat()})


async def get_recent_torque_orders(hours: int = 24) -> Optional[list[dict]]:
    """Signal orders placed in the last `hours` — lets the orders panel
    recognise them (Tradier drops the `tag` on OTOCO brackets, so the tag
    alone can't) and keep their status in sync. None = unknown (read failed)."""
    from datetime import datetime, timedelta, timezone
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    return await select_many("torque_orders", select="entry_order_id,status",
                             filters={"placed_at": f"gte.{cutoff}"})


async def upsert_torque_watcher(row: dict) -> bool:
    """Checkpoint a watcher's current state (see docs/torque.md 'Multi-user
    order persistence'). Called at state TRANSITIONS, not every price tick —
    the in-memory watcher stays authoritative for a running process; this is
    the recovery path for when that process didn't stay running."""
    return await upsert_row("torque_watchers", row, on_conflict="entry_order_id")


async def get_active_torque_watchers() -> list[dict]:
    """Every not-yet-done watcher, read once at backend startup to resume
    monitoring positions a restart would otherwise have silently dropped."""
    rows = await select_many("torque_watchers", filters={"done": "eq.false"})
    return rows or []


async def purge_stale_torque_records(older_than_hours: int = 24) -> None:
    """Delete claimed news signals, their order-correlation rows, and
    completed watcher rows past the retention window. Hooked into
    poller.py's own open->closed transition — not a standalone scheduler."""
    from datetime import datetime, timedelta, timezone
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=older_than_hours)).isoformat()
    await delete_rows("news_reactor_signals", {"received_at": f"lt.{cutoff}"})
    await delete_rows("torque_orders", {"placed_at": f"lt.{cutoff}"})
    await delete_rows("torque_watchers", {"done": "eq.true", "updated_at": f"lt.{cutoff}"})


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

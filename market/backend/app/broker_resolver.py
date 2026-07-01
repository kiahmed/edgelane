"""Shared broker resolution for user-initiated trade execution.

Used by BOTH the market /orders routes and Torque so there is a single source
of truth for "whose broker account does this order hit?".

Trade execution ALWAYS routes through the authenticated user's OWN broker
credentials (broker_configs in Supabase) — Tradier or Webull, decrypted
server-side via get_broker_secret. A regular signed-in user has NO access to the
house/admin Tradier connection for trading: that client exists only for
market-data reads (chains, quotes). A signed-in user without an active, usable
broker connection is rejected (403).

The house client is reachable for execution ONLY via the server-owner bypasses
— the admin token or AUTH_ENABLED=false dev mode — which back owner tooling.
"""
from __future__ import annotations

from typing import Any

from fastapi import HTTPException, Request

from . import supabase_admin
from .tradier_client import TradierClient
from .webull_client import WebullClient


def house_client(request: Request):
    """The shared house/admin Tradier client wired onto app.state at startup."""
    client = getattr(request.app.state, "tradier", None)
    if client is None:
        raise HTTPException(500, "tradier client not initialized on app.state")
    return client


async def resolve_broker(request: Request, user: dict) -> tuple[str, Any, str | None, bool]:
    """Resolve (broker, client, account_override_or_None, is_per_user) for an order.

    is_per_user=True  → a per-request client built from the user's own creds;
                        the caller MUST close it when done.
    is_per_user=False → the shared house client (admin/dev only); do NOT close.
    """
    uid = user.get("id")
    auth_kind = user.get("auth")
    cfg = None
    if uid and auth_kind == "supabase":
        cfg = await supabase_admin.get_broker_config(uid)
    if cfg:
        broker = (cfg.get("broker") or "tradier").lower()
        if broker == "webull" and cfg.get("webull_app_key") and cfg.get("webull_app_secret"):
            client = WebullClient(
                app_key=cfg["webull_app_key"], app_secret=cfg["webull_app_secret"],
                region=cfg.get("webull_region") or "us", env=cfg.get("webull_env") or "production",
            )
            account = (cfg.get("webull_account_id") or "").strip() or None
            return "webull", client, account, True
        if cfg.get("tradier_token"):
            env = (cfg.get("tradier_env") or "production").lower()
            base = "https://sandbox.tradier.com" if env == "sandbox" else "https://api.tradier.com"
            client = TradierClient(base_url=base, token=cfg["tradier_token"])
            account = (cfg.get("tradier_account_id") or "").strip() or None
            return "tradier", client, account, True
        # cfg row exists but carries no usable credentials → treat as "no
        # connection" and fall through to the rejection below.

    # No usable per-user broker connection resolved.
    if auth_kind == "supabase":
        raise HTTPException(
            403,
            "No active brokerage connection for your account. Add and activate "
            "your own broker connection in Settings before placing trades — the "
            "house connection is read-only and cannot execute orders.",
        )
    # Server-owner bypass only (admin token / dev mode): the house client backs
    # owner-only tooling.
    return "tradier", house_client(request), None, False

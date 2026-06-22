"""Per-user broker utilities.

The broker token is encrypted at rest (Vault) and write-only from the browser,
so the UI can no longer validate a saved connection by calling Tradier directly.
`POST /broker/test` does it server-side: it decrypts the user's own token via the
service_role RPC and probes Tradier's /v1/user/profile — never returning the
token itself.
"""
from __future__ import annotations

import logging

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..auth import get_current_user
from ..webull_client import WebullClient, WebullError, WEBULL_AUTH_HINT
from .. import supabase_admin

log = logging.getLogger("edgelane.market.broker")
router = APIRouter()


class BrokerTestRequest(BaseModel):
    id: str | None = None  # specific saved connection; None → the active one


def _tradier_base(env: str) -> str:
    return "https://sandbox.tradier.com" if (env or "").lower() == "sandbox" else "https://api.tradier.com"


@router.post("/broker/test")
async def test_broker(req: BrokerTestRequest, user: dict = Depends(get_current_user)):
    uid = user.get("id")
    if not uid or user.get("auth") != "supabase":
        raise HTTPException(403, "broker test requires a signed-in user")
    cfg = await supabase_admin.get_broker_config(uid, req.id)
    if not cfg:
        raise HTTPException(404, "no broker connection found for this user")
    broker = (cfg.get("broker") or "tradier").lower()

    # ── Webull: validate via the SDK's account list (proves the signed creds work).
    if broker == "webull":
        if not (cfg.get("webull_app_key") and cfg.get("webull_app_secret")):
            raise HTTPException(404, "no Webull credentials found for this connection")
        client = WebullClient(
            app_key=cfg["webull_app_key"], app_secret=cfg["webull_app_secret"],
            region=cfg.get("webull_region") or "us", env=cfg.get("webull_env") or "production",
        )
        try:
            accts = await client.list_accounts()
        except WebullError as e:
            # hint covers the most common cause (app not yet authorized for trading)
            return {"ok": False, "broker": "webull", "env": cfg.get("webull_env"),
                    "error": str(e)[:200], "hint": WEBULL_AUTH_HINT}
        finally:
            await client.close()
        account_id = accts[0].get("account_id") if accts else None
        return {"ok": True, "broker": "webull", "env": cfg.get("webull_env"),
                "account_id": account_id, "hint": WEBULL_AUTH_HINT}

    # ── Tradier: validate via /v1/user/profile.
    if not cfg.get("tradier_token"):
        raise HTTPException(404, "no Tradier token found for this connection")
    base = _tradier_base(cfg.get("tradier_env"))
    headers = {"Authorization": f"Bearer {cfg['tradier_token']}", "Accept": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=12) as c:
            r = await c.get(f"{base}/v1/user/profile", headers=headers)
    except httpx.RequestError as e:
        raise HTTPException(502, f"Tradier unreachable: {e}")
    if r.status_code != 200:
        return {"ok": False, "broker": "tradier", "status": r.status_code, "env": cfg.get("tradier_env")}
    try:
        acct = (r.json().get("profile") or {}).get("account")
    except Exception:
        acct = None
    if isinstance(acct, list):
        acct = acct[0] if acct else None
    account_id = (acct or {}).get("account_number") if isinstance(acct, dict) else None
    return {"ok": True, "broker": "tradier", "env": cfg.get("tradier_env"), "account_id": account_id}

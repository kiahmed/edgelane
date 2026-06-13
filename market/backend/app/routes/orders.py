"""Order submission routes. Two-stage: /orders/preview always runs first,
/orders/submit requires explicit confirm flag in the body."""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from ..poller import state as poller_state
from ..config import get_settings
from ..order_builder import (
    Leg,
    build_legs_from_candidate,
    build_tradier_order_payload,
    resolve_leg_symbols,
)

log = logging.getLogger("edgelane.market.orders")
router = APIRouter()


class OrderRequest(BaseModel):
    symbol: str
    candidate_label: str
    strategy: str
    quantity: int = Field(default=1, ge=1, le=100)
    limit_price: float | None = None
    duration: str = Field(default="day")
    account_id: str | None = None
    order_type: str | None = None
    confirm: bool = False


def _lookup_candidate(symbol: str, strategy: str, label: str) -> tuple[dict, dict]:
    sym = symbol.upper()
    snap = poller_state.latest_by_symbol.get(sym)
    if not snap:
        raise HTTPException(404, f"no snapshot for {sym} (poller hasn't run yet)")
    strats = snap.get("strategies") or {}
    bucket = strats.get(strategy)
    if not bucket:
        raise HTTPException(404, f"no {strategy} candidates for {sym}")
    cands = list(bucket.get("all_widths") or [])
    if bucket.get("best"):
        cands.append(bucket["best"])
    cand = next((c for c in cands if c and c.get("label") == label), None)
    if not cand:
        labels = sorted({c.get("label") for c in cands if c}) if cands else []
        raise HTTPException(404, f"no '{label}' variant of {strategy} for {sym}; available labels: {labels}")
    return snap, cand


def _resolve_account_id(req: OrderRequest) -> str:
    """Priority: request.account_id > settings.active_account_id > main._tradier_account_id (auto-resolved)."""
    settings = get_settings()
    from .. import main as _main
    aid = (
        req.account_id
        or settings.active_account_id
        or getattr(_main, "_tradier_account_id", "")
        or ""
    ).strip()
    if not aid:
        raise HTTPException(
            400,
            "account_id not provided in request, not set in config "
            "(TRADIER_ACCOUNT_ID / TRADIER_ACCOUNT_ID_SANDBOX), and "
            "auto-resolve from /v1/user/profile did not return a usable account. "
            "Check the backend startup log for the resolve attempt.",
        )
    return aid


def _resolve_order_type(req: OrderRequest, cand: dict) -> str:
    if req.order_type:
        ot = req.order_type.lower()
        if ot not in ("market", "credit", "debit"):
            raise HTTPException(400, f"bad order_type: {req.order_type!r}")
        return ot
    t = (cand.get("type") or "").lower()
    if t in ("credit", "debit"):
        return t
    return "credit"


def _resolve_limit_price(req: OrderRequest, cand: dict, order_type: str) -> float | None:
    if order_type == "market":
        return None
    if req.limit_price is not None:
        return float(req.limit_price)
    lp = cand.get("limit_price")
    if lp is not None:
        try:
            return float(lp)
        except (TypeError, ValueError):
            pass
    net = cand.get("net_premium")
    if net is not None:
        try:
            return float(net)
        except (TypeError, ValueError):
            pass
    raise HTTPException(400, "no limit_price provided and candidate has no net_premium")


async def _ensure_leg_symbols(legs: list[Leg], symbol: str, expiration: str,
                              tradier_client) -> list[Leg]:
    if all(l.occ_symbol for l in legs):
        return legs
    try:
        chain = await tradier_client.options_chain(symbol, expiration)
    except Exception as e:
        raise HTTPException(502, f"failed to fetch chain for symbol resolution: {e}")
    try:
        return resolve_leg_symbols(legs, chain, expiration=expiration)
    except ValueError as e:
        raise HTTPException(400, str(e))


def _tradier_client(request: Request):
    client = getattr(request.app.state, "tradier", None)
    if client is None:
        raise HTTPException(500, "tradier client not initialized on app.state")
    return client


async def _submit_order(req: OrderRequest, tradier_client, preview: bool) -> dict:
    snap, cand = _lookup_candidate(req.symbol, req.strategy, req.candidate_label)
    account_id = _resolve_account_id(req)
    order_type = _resolve_order_type(req, cand)
    limit_price = _resolve_limit_price(req, cand, order_type)
    expiration = snap.get("expiration")
    if not expiration:
        raise HTTPException(500, f"snapshot missing expiration for {req.symbol}")

    try:
        legs = build_legs_from_candidate(cand, base_quantity=int(req.quantity))
    except ValueError as e:
        raise HTTPException(400, f"candidate has invalid legs: {e}")

    legs = await _ensure_leg_symbols(legs, req.symbol.upper(), expiration, tradier_client)

    try:
        payload = build_tradier_order_payload(
            account_id=account_id, symbol=req.symbol, legs=legs,
            order_type=order_type, limit_price=limit_price, duration=req.duration,
            preview=preview, tag=f"edgelane{cand.get('label') or req.strategy}",
        )
    except ValueError as e:
        raise HTTPException(400, str(e))

    response = await _post_to_tradier(tradier_client, account_id, payload)
    return {
        "preview": preview, "symbol": req.symbol.upper(),
        "strategy": req.strategy, "label": req.candidate_label,
        "account_id": account_id, "expiration": expiration,
        "quantity": int(req.quantity), "order_type": order_type,
        "limit_price": limit_price, "duration": req.duration,
        "payload": payload, "tradier_response": response,
    }


async def _post_to_tradier(tradier_client, account_id: str, payload: dict) -> dict:
    place_order = getattr(tradier_client, "place_order", None)
    if callable(place_order):
        try:
            return await place_order(account_id=account_id, payload=payload)
        except TypeError:
            return await place_order(account_id, payload)
    import httpx
    try:
        client = await tradier_client._get_client()
    except Exception as e:
        raise HTTPException(500, f"tradier client unusable: {e}")
    url = f"{tradier_client.base_url}/v1/accounts/{account_id}/orders"
    headers = {
        "Authorization": f"Bearer {tradier_client.token}",
        "Accept": "application/json",
        "Content-Type": "application/x-www-form-urlencoded",
    }
    try:
        resp = await client.post(url, data=payload, headers=headers)
    except httpx.TimeoutException as e:
        raise HTTPException(504, f"Tradier order POST timed out: {e}")
    except httpx.RequestError as e:
        raise HTTPException(502, f"Tradier order POST failed: {e}")
    if resp.status_code >= 400:
        body = resp.text[:500]
        raise HTTPException(resp.status_code, f"Tradier rejected order: {body}")
    try:
        return resp.json()
    except Exception:
        return {"raw": resp.text[:1000]}


@router.post("/orders/preview")
async def preview_order(req: OrderRequest, request: Request):
    tradier_client = _tradier_client(request)
    return await _submit_order(req, tradier_client, preview=True)


@router.post("/orders/submit")
async def submit_order(req: OrderRequest, request: Request):
    if not req.confirm:
        raise HTTPException(400, "submit requires confirm=true in the request body")
    tradier_client = _tradier_client(request)
    result = await _submit_order(req, tradier_client, preview=False)
    tradier_resp = result.get("tradier_response") or {}
    order = tradier_resp.get("order") or {}
    result["order_id"] = order.get("id")
    result["order_status"] = order.get("status")
    log.info(
        "live order: symbol=%s strategy=%s label=%s qty=%s order_id=%s status=%s",
        req.symbol, req.strategy, req.candidate_label, req.quantity,
        result.get("order_id"), result.get("order_status"),
    )
    return result

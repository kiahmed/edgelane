"""Order submission routes. Two-stage: /orders/preview always runs first,
/orders/submit requires explicit confirm flag in the body."""
from __future__ import annotations

import asyncio
import logging
import time

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from .. import level_pick
from ..poller import state as poller_state
from ..config import get_settings
from ..auth import get_current_user
from ..broker_resolver import resolve_broker
from ..entitlements import ensure_tool
from ..webull_client import WebullClient, WebullError
from ..webull_order_builder import build_webull_option_order
from ..order_builder import (
    Leg,
    build_legs_from_candidate,
    build_tradier_order_payload,
    resolve_leg_symbols,
)

log = logging.getLogger("edgelane.market.orders")
from ..logsetup import TXN  # noqa: E402
router = APIRouter()

# --- Order-path deadlines --------------------------------------------------
#
# A user-initiated order request must ALWAYS come back with an answer. When a
# stage stalls with no bound, the browser sits on "Previewing…" forever AND the
# stall leaves no trace in the access log -- uvicorn writes its line only once a
# response is produced, so a request that never finishes is simply invisible.
# That combination is what makes "the Preview button hangs" so hard to diagnose.
#
# So: every remote stage is bounded, every stage is logged, and the timeout
# error names the stage that stalled. Budgets are sized to land comfortably
# inside the frontend's own abort so the server -- not the browser -- is what
# reports the failure, with a reason attached.
BROKER_RESOLVE_TIMEOUT_SEC = 20.0    # Supabase RPC that decrypts the user's broker creds
ACCOUNT_RESOLVE_TIMEOUT_SEC = 15.0   # Tradier /user/profile -> account_number
CHAIN_RESOLVE_TIMEOUT_SEC = 25.0     # options chain fetch (only when OCC symbols are missing)
PREVIEW_DEADLINE_SEC = 45.0          # whole-request ceiling for preview (places nothing)


async def _stage(name: str, coro, timeout: float):
    """Await one order stage under a hard deadline, logging how long it took.

    On timeout the partial work is cancelled and the caller gets a 504 naming
    the stage, instead of an open-ended wait the browser has to guess about.
    """
    t0 = time.monotonic()
    try:
        return await asyncio.wait_for(coro, timeout=timeout)
    except asyncio.TimeoutError:
        log.error("order stage %s TIMED OUT after %.0fs", name, timeout)
        raise HTTPException(
            504,
            f"broker step '{name}' timed out after {timeout:.0f}s — your broker "
            f"did not respond. Nothing was placed; try again in a moment.",
        )
    finally:
        log.info("order stage %s took %.2fs", name, time.monotonic() - t0)


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
    # Level-engine pick (docs/level_engine.md §3.4): the Matrix tile sends the
    # pick_id it shows (and source="levels"); resolved from the level engine,
    # never from the legacy strategies.
    pick_id: str | None = None
    source: str | None = None


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


_STALE_LEVEL_PICK = "level pick no longer current — refresh"


def _is_level_request(req: OrderRequest) -> bool:
    return bool(req.pick_id) or (req.source or "").strip().lower() == "levels"


async def _lookup_level_candidate(req: OrderRequest) -> tuple[dict, dict]:
    """Resolve a level pick by pick_id from a FRESH level-engine decision —
    the tile's pick or any grid card (decision.strategies[*].best / all_widths).
    Stale / flipped / unknown / not-orderable → 409. Never falls back to a
    legacy candidate."""
    if getattr(get_settings(), "matrix_decision_source", "engine") != "levels":
        raise HTTPException(409, "level picks are not enabled (MATRIX_DECISION_SOURCE) — refresh")
    if not req.pick_id:
        raise HTTPException(409, f"level order without a pick_id — {_STALE_LEVEL_PICK}")
    sym = req.symbol.upper()
    decision = await level_pick.fetch_decision(get_settings(), sym, max_age=0)
    if decision is None:
        raise HTTPException(503, "level engine unavailable — nothing was placed; try again")
    pick = level_pick.find_level_pick(decision, req.pick_id)
    if pick is None:
        raise HTTPException(409, _STALE_LEVEL_PICK)
    if not pick.get("legs"):
        raise HTTPException(409, f"level candidate {req.pick_id} has no orderable legs — "
                                 f"{_STALE_LEVEL_PICK}")
    # The tile sends the pick's strategy, a grid card its grid key (contract C1)
    # — either names this candidate; anything else is a stale / mismatched card.
    if req.strategy not in level_pick.level_strategy_names(decision, req.pick_id):
        raise HTTPException(409, _STALE_LEVEL_PICK)
    cand = dict(pick)
    # Level picks carry the legacy engine_pick key `spread_type`; the order
    # path reads the candidate key `type` (credit/debit).
    if not cand.get("type") and cand.get("spread_type"):
        cand["type"] = cand["spread_type"]
    legacy = poller_state.latest_by_symbol.get(sym) or {}
    snap = {"symbol": sym, "expiration": pick.get("expiration") or legacy.get("expiration")}
    return snap, cand


async def _resolve_candidate(req: OrderRequest) -> tuple[dict, dict]:
    if _is_level_request(req):
        return await _lookup_level_candidate(req)
    if (getattr(get_settings(), "matrix_decision_source", "engine") == "levels"
            and req.symbol.upper() == "SPX"):
        # Levels mode shows nothing legacy (docs/level_engine.md §3.4) — an SPX
        # order must name the level pick / card it came from.
        raise HTTPException(409, f"levels mode: SPX orders need the level pick_id — {_STALE_LEVEL_PICK}")
    return _lookup_candidate(req.symbol, req.strategy, req.candidate_label)


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
        chain = await _stage("chain-fetch",
                             tradier_client.options_chain(symbol, expiration),
                             CHAIN_RESOLVE_TIMEOUT_SEC)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, f"failed to fetch chain for symbol resolution: {e}")
    try:
        return resolve_leg_symbols(legs, chain, expiration=expiration)
    except ValueError as e:
        raise HTTPException(400, str(e))


async def _submit_webull(req: OrderRequest, client: WebullClient, preview: bool,
                         account_override: str | None = None) -> dict:
    snap, cand = await _resolve_candidate(req)
    expiration = snap.get("expiration")
    if not expiration:
        raise HTTPException(500, f"snapshot missing expiration for {req.symbol}")
    try:
        account_id = (account_override or "").strip() or await _stage(
            "account-resolve", client.first_account_id(), ACCOUNT_RESOLVE_TIMEOUT_SEC)
    except WebullError as e:
        raise HTTPException(502, f"Webull account lookup failed: {e}")
    if not account_id:
        raise HTTPException(400, "no Webull account_id (set one in the broker config or ensure the account is reachable)")

    order_type = _resolve_order_type(req, cand)
    limit_price = _resolve_limit_price(req, cand, order_type)
    try:
        legs = build_legs_from_candidate(cand, base_quantity=int(req.quantity))
        orders = build_webull_option_order(
            strategy=req.strategy, legs=legs, expiration=expiration,
            underlying=req.symbol.upper(), quantity=int(req.quantity),
            order_type=order_type, limit_price=limit_price,
        )
    except ValueError as e:
        raise HTTPException(400, f"could not build Webull order: {e}")

    try:
        resp = await (client.preview_option if preview else client.place_option)(account_id, orders)
    except WebullError as e:
        raise HTTPException(502, str(e))
    return {
        "preview": preview, "broker": "webull", "symbol": req.symbol.upper(),
        "strategy": req.strategy, "label": req.candidate_label,
        "account_id": account_id, "expiration": expiration,
        "quantity": int(req.quantity), "order_type": order_type,
        "limit_price": limit_price, "duration": req.duration,
        "payload": orders, "webull_response": resp,
    }


async def _submit_order(req: OrderRequest, tradier_client, preview: bool,
                        account_override: str | None = None,
                        per_user: bool = False) -> dict:
    snap, cand = await _resolve_candidate(req)
    if account_override and account_override.strip():
        account_id = account_override.strip()
    elif per_user:
        # Per-user orders must target the USER's own account, resolved from their
        # own broker token — never the house-config account fallback.
        account_id = (req.account_id or "").strip()
        # A real account id is never an email — ignore an autofilled/mis-entered
        # email and resolve the real account_number from the user's token instead.
        if "@" in account_id:
            log.warning("order: ignoring email-shaped account_id on the request; "
                        "resolving the real account from the user's broker token")
            account_id = ""
        if not account_id:
            account_id = await _stage("account-resolve",
                                      tradier_client.resolve_account_id(),
                                      ACCOUNT_RESOLVE_TIMEOUT_SEC)
        if not account_id:
            raise HTTPException(
                400,
                "could not determine an account from your broker connection; set "
                "the account id on your broker connection in Settings.",
            )
    else:
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
    if len(legs) == 1:
        payload = _single_leg_payload(payload, order_type)

    response = await _post_to_tradier(tradier_client, account_id, payload)
    return {
        "preview": preview, "symbol": req.symbol.upper(),
        "strategy": req.strategy, "label": req.candidate_label,
        "account_id": account_id, "expiration": expiration,
        "quantity": int(req.quantity), "order_type": order_type,
        "limit_price": limit_price, "duration": req.duration,
        "payload": payload, "tradier_response": response,
    }


def _single_leg_payload(multileg: dict, order_type: str) -> dict:
    """Tradier's `multileg` class needs 2+ legs; a one-leg structure (the level
    engine's long call / long put) must go as `class=option` with an un-indexed
    leg and a market|limit type — same shape Torque's single-option orders use.
    Built from the already-validated multileg params, so nothing else changes."""
    p = {k: v for k, v in multileg.items() if not k.endswith("[0]")}
    p.update({
        "class": "option",
        "option_symbol": multileg["option_symbol[0]"],
        "side": multileg["side[0]"],
        "quantity": multileg["quantity[0]"],
        "type": "market" if order_type == "market" else "limit",
    })
    return p


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
async def preview_order(req: OrderRequest, request: Request,
                        user: dict = Depends(get_current_user)):
    # Log on ENTRY, not just on completion: a stalled order request never
    # reaches uvicorn's access log, so without this line a hang is invisible.
    t0 = time.monotonic()
    log.info("order preview: START user=%s symbol=%s strategy=%s label=%s qty=%s",
             str(user.get("id"))[:8], req.symbol, req.strategy,
             req.candidate_label, req.quantity)
    await ensure_tool(user, "market")
    broker, client, account_override, per_user = await _stage(
        "broker-resolve", resolve_broker(request, user), BROKER_RESOLVE_TIMEOUT_SEC)
    log.info("order preview: broker=%s per_user=%s", broker, per_user)
    try:
        if broker == "webull":
            coro = _submit_webull(req, client, preview=True,
                                  account_override=account_override)
        else:
            coro = _submit_order(req, client, preview=True,
                                 account_override=account_override,
                                 per_user=per_user)
        # A preview places nothing, so cancelling it mid-flight is always safe.
        result = await _stage("preview", coro, PREVIEW_DEADLINE_SEC)
        log.info("order preview: DONE in %.2fs", time.monotonic() - t0)
        return result
    except HTTPException as e:
        log.warning("order preview: FAILED in %.2fs — HTTP %s: %s",
                    time.monotonic() - t0, e.status_code, e.detail)
        raise
    except Exception as e:
        log.exception("order preview: ERROR in %.2fs — %s", time.monotonic() - t0, e)
        raise
    finally:
        if per_user:
            await client.close()


@router.post("/orders/submit")
async def submit_order(req: OrderRequest, request: Request,
                       user: dict = Depends(get_current_user)):
    if not req.confirm:
        raise HTTPException(400, "submit requires confirm=true in the request body")
    t0 = time.monotonic()
    TXN.info("order submit: START user=%s symbol=%s strategy=%s label=%s qty=%s",
             str(user.get("id"))[:8], req.symbol, req.strategy,
             req.candidate_label, req.quantity)
    await ensure_tool(user, "market")
    broker, client, account_override, per_user = await _stage(
        "broker-resolve", resolve_broker(request, user), BROKER_RESOLVE_TIMEOUT_SEC)
    # NOTE: deliberately NO whole-request deadline here. The preparation stages
    # are individually bounded, but the live order POST must never be cancelled
    # mid-flight — a cancelled-but-delivered order would be reported as failed
    # while actually resting at the broker, inviting a duplicate resubmit. That
    # call is bounded by the Tradier/Webull client's own socket timeout instead.
    try:
        if broker == "webull":
            result = await _submit_webull(req, client, preview=False,
                                          account_override=account_override)
        else:
            result = await _submit_order(req, client, preview=False,
                                         account_override=account_override,
                                         per_user=per_user)
    finally:
        if per_user:
            await client.close()
    if result.get("broker") == "webull":
        wb = result.get("webull_response") or {}
        # Webull preview/place response shapes vary; surface what's present.
        result["order_id"] = wb.get("order_id") or wb.get("client_order_id")
        result["order_status"] = wb.get("order_status") or wb.get("status")
        TXN.info("live webull order: symbol=%s strategy=%s label=%s qty=%s order_id=%s",
                 req.symbol, req.strategy, req.candidate_label, req.quantity, result.get("order_id"))
        return result
    tradier_resp = result.get("tradier_response") or {}
    order = tradier_resp.get("order") or {}
    result["order_id"] = order.get("id")
    result["order_status"] = order.get("status")
    TXN.info(
        "live order: symbol=%s strategy=%s label=%s qty=%s order_id=%s status=%s",
        req.symbol, req.strategy, req.candidate_label, req.quantity,
        result.get("order_id"), result.get("order_status"),
    )
    return result

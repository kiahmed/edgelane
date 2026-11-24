"""Level Engine data feed — `GET /internal/feed/{symbol}` (docs/level_engine.md §3.2).

The level-engine container never calls Tradier; it reads what this backend's
poller already fetched. Port 8789 is what the Cloudflare tunnel serves (and is
published on the host), so this route IS internet-reachable and is locked down
here, in code:

  * header `X-Internal-Token` must match `INTERNAL_FEED_TOKEN`
    (`hmac.compare_digest` on bytes);
  * **fails closed** — a blank `INTERNAL_FEED_TOKEN` 404s every request;
  * any request carrying `Cf-Connecting-Ip` or `Cf-Ray` (i.e. it came through
    the tunnel) is refused even with a valid token;
  * every refusal is a plain `404 {"detail": "Not Found"}`, indistinguishable
    from a route that doesn't exist; not in the OpenAPI schema.

No user-session dependency (no JWT / entitlement), and the payload is market
data only — no account, user or broker fields, no `_`-prefixed internals.

Not ready (auth passed, but no snapshot yet, or LEVEL_FEED_ENABLED is off so
no chain is cached): `503 {"error": "no snapshot yet"}` — a distinct status
the level engine treats as "stale, skip this tick" without confusing it with
a refused request.
"""
from __future__ import annotations

import hmac

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from ..config import get_settings
from ..poller import state as poller_state

router = APIRouter()

_TUNNEL_HEADERS = ("cf-connecting-ip", "cf-ray")


def _not_found() -> HTTPException:
    # Same body FastAPI returns for an unknown path.
    return HTTPException(status_code=404, detail="Not Found")


def _authorised(request: Request) -> bool:
    token = (get_settings().internal_feed_token or "").strip()
    if not token:
        return False
    if any(h in request.headers for h in _TUNNEL_HEADERS):
        return False
    presented = request.headers.get("x-internal-token") or ""
    if not presented:
        return False
    return hmac.compare_digest(presented.encode("utf-8"), token.encode("utf-8"))


def _with_mid(c: dict) -> dict:
    out = dict(c)
    try:
        bid = float(c.get("bid") or 0.0)
        ask = float(c.get("ask") or 0.0)
    except (TypeError, ValueError):
        bid = ask = 0.0
    out["mid"] = (bid + ask) / 2.0 if bid > 0 and ask > 0 else None
    return out


def _prevclose(sym: str) -> float | None:
    return (poller_state.latest_quote_by_symbol.get(sym) or {}).get("prevclose")


def build_feed(symbol: str) -> dict | None:
    """The feed payload for `symbol`, or None when nothing is cached yet.
    Read synchronously (no await) so every field comes from the same poll."""
    snap = poller_state.latest_by_symbol.get(symbol)
    contracts = poller_state.latest_contracts_by_symbol.get(symbol)
    if not snap or contracts is None:
        return None
    return {
        "symbol": symbol,
        "ts": snap.get("ts"),
        "spot": snap.get("spot"),
        "expiration": snap.get("expiration"),
        "dte": snap.get("dte"),
        "expected_move": snap.get("expected_move"),
        "contracts": [_with_mid(c) for c in contracts],
        "bias": snap.get("bias"),
        "engine_pick": snap.get("engine_pick"),
        "spy_spot": poller_state.latest_spy_spot,
        "spx_prevclose": _prevclose("SPX"),
        "spy_prevclose": _prevclose("SPY"),
        "market_open": poller_state.market_open,
        "market_reason": poller_state.market_reason,
    }


@router.get("/internal/feed/{symbol}", include_in_schema=False)
async def internal_feed(symbol: str, request: Request):
    if not _authorised(request):
        raise _not_found()
    payload = build_feed((symbol or "").upper())
    if payload is None:
        return JSONResponse(status_code=503, content={"error": "no snapshot yet"})
    return payload

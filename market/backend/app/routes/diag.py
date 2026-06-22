"""Tradier diagnostics — one-shot smoke tests for the operator."""
from __future__ import annotations

import time
from datetime import date
from fastapi import APIRouter, Request

from ..tradier_client import (
    TradierAuthError,
    TradierError,
    TradierRateLimitError,
    TradierTimeoutError,
    get_rate_limit_state,
)

router = APIRouter()


@router.get("/diag/walls/{symbol}")
async def diag_walls(symbol: str, request: Request, window: float = 0.015,
                     source: str = "oi"):
    """Per-strike GEX for a symbol, shown in the provider sign convention
    (negative = PUT, positive = CALL), plus the dominant put/call walls. Lets
    the operator compare Tradier walls against what the provider displays
    without running tools/wall_compare.py by hand.

    ?source=oi (gamma x open_interest, default) or ?source=volume (gamma x
    today's traded volume) — call both to diff resting vs flow walls live."""
    gex_weight = "volume" if (source or "oi").lower() == "volume" else "oi"
    from ..poller import _normalize_tradier_contract
    from ..dealer_exposures import compute_dealer_exposures

    sym = symbol.upper()
    client = request.app.state.tradier
    quote = await client.stock_quote(sym)
    spot = 0.0
    for k in ("last", "close", "prevclose"):
        try:
            spot = float((quote or {}).get(k) or 0)
        except (TypeError, ValueError):
            spot = 0.0
        if spot > 0:
            break
    if not spot:
        return {"error": f"no spot for {sym}"}

    exps = sorted(str(e) for e in (await client.option_expirations(sym)))
    today = date.today().isoformat()
    exp = today if today in exps else next((e for e in exps if e >= today), exps[0] if exps else None)
    if not exp:
        return {"error": f"no expirations for {sym}"}
    raw = await client.options_chain(sym, exp)
    contracts = [_normalize_tradier_contract(o) for o in raw if o.get("strike") is not None]
    rows = (compute_dealer_exposures(contracts, spot, gex_weight=gex_weight).get("exposures_by_date", {}).get(exp) or {}).get("by_strike", [])

    lo, hi = spot * (1 - window), spot * (1 + window)
    # provider convention: gex_tt = call_gex - put_gex (negative = PUT, positive = CALL)
    strikes = []
    for r in sorted(rows, key=lambda r: r["strike"]):
        if not (lo <= r["strike"] <= hi):
            continue
        gex_tt = float(r.get("call_gex") or 0) - float(r.get("put_gex") or 0)
        strikes.append({
            "strike": r["strike"],
            "pos": "above" if r["strike"] > spot else "below" if r["strike"] < spot else "at",
            "gex_tt": gex_tt,
            "label": "put" if gex_tt < 0 else "call" if gex_tt > 0 else "flat",
        })

    tt = [(r["strike"], float(r.get("call_gex") or 0) - float(r.get("put_gex") or 0)) for r in rows]
    put_wall = min(tt, key=lambda x: x[1]) if tt else (None, 0)   # most negative
    call_wall = max(tt, key=lambda x: x[1]) if tt else (None, 0)  # most positive
    return {
        "symbol": sym,
        "spot": spot,
        "expiration": exp,
        "convention": "edgelane_provider: negative=PUT, positive=CALL",
        "source": ("tradier_volume (gamma x volume)" if gex_weight == "volume"
                   else "tradier_oi (gamma x open_interest)"),
        "put_wall": {"strike": put_wall[0], "gex_tt": put_wall[1],
                     "pos": "above" if (put_wall[0] or 0) > spot else "below"},
        "call_wall": {"strike": call_wall[0], "gex_tt": call_wall[1],
                      "pos": "above" if (call_wall[0] or 0) > spot else "below"},
        "strikes": strikes,
    }


@router.get("/diag/tradier")
async def tradier_smoke(request: Request):
    s = request.app.state.settings
    client = request.app.state.tradier
    using_mock = bool(getattr(request.app.state, "using_mock", False))
    mode = s.tradier_mode

    symbol = "SPY"
    t0 = time.perf_counter()
    quote: dict | None = None
    error: str | None = None
    error_type: str | None = None

    try:
        quote = await client.stock_quote(symbol)
    except TradierAuthError as e:
        error = str(e); error_type = "auth"
    except TradierRateLimitError as e:
        error = f"{e} (retry_after={getattr(e, 'retry_after_sec', 0):.1f}s)"
        error_type = "rate_limit"
    except TradierTimeoutError as e:
        error = str(e); error_type = "timeout"
    except TradierError as e:
        error = str(e); error_type = "other"
    except Exception as e:
        error = f"{type(e).__name__}: {e}"; error_type = "other"

    latency_ms = int((time.perf_counter() - t0) * 1000)
    shape_ok = bool(quote and isinstance(quote, dict) and quote.get("symbol"))
    resolved_account_id = (
        getattr(request.app.state, "tradier_account_id", "")
        or s.active_account_id
        or ""
    )

    return {
        "ok": error is None and shape_ok,
        "mode": mode,
        "tradier_env": s.tradier_env,
        "tradier_account_id": resolved_account_id,
        "using_mock": using_mock,
        "symbol": symbol,
        "latency_ms": latency_ms,
        "quote": quote,
        "shape_ok": shape_ok,
        "rate_limit": get_rate_limit_state() or None,
        "error": error,
        "error_type": error_type,
    }

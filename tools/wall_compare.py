#!/usr/bin/env python3
"""Wall-source comparison + sign-convention dump.

Computes per-strike GEX from the Tradier chain (gamma x open_interest — the
OI-based dealer GEX, same construct EdgelaneProvider uses) and prints every strike
around spot with its call/put dominance, so the displayed walls can be checked
against what EdgelaneProvider shows on screen.

Also pulls the latest EdgelaneProvider-extension payload from the running backend's
/webhook/debug (flow-based, gamma x volume) so the two sources sit side by side.

Run from repo root:
    EDGELANE_MARKET_CONFIG=edgelane_market.config \
      market/backend/.venv/bin/python3 tools/wall_compare.py [SYMBOL] [WINDOW_PCT]

SIGN CONVENTIONS (the whole point of this dump):
  • Our engine : net_gex = put_gex - call_gex  → puts POSITIVE.
                 put wall  = most-POSITIVE strike, call wall = most-NEGATIVE.
  • EdgelaneProvider  : SqueezeMetrics standard, puts NEGATIVE.
                 put wall (magnet) = most-NEGATIVE strike.
  Same physical "put-dominated" strike, opposite sign label. This dump shows
  raw call_gex vs put_gex per strike so dominance is unambiguous regardless of
  whose sign you use.
"""
from __future__ import annotations
import asyncio
import json
import os
import sys
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "market", "backend"))

from app.config import get_settings
from app.tradier_client import TradierClient
from app.mock_tradier import MockTradierClient
from app.poller import _normalize_tradier_contract
from app.dealer_exposures import compute_dealer_exposures
from datetime import date


def _fetch_edgelane_provider(base_url: str, symbol: str) -> dict | None:
    try:
        with urllib.request.urlopen(base_url.rstrip("/") + "/webhook/debug", timeout=4) as r:
            d = json.loads(r.read().decode())
        entry = (d.get("latest_per_symbol") or {}).get(symbol.upper())
        return entry.get("payload") if entry else None
    except Exception as e:
        print(f"(no EdgelaneProvider payload: {e})")
        return None


async def main() -> None:
    symbol = (sys.argv[1] if len(sys.argv) > 1 else "SPX").upper()
    window = float(sys.argv[2]) if len(sys.argv) > 2 else 0.02  # ±2% around spot

    settings = get_settings()
    if settings.active_tradier_token:
        client = TradierClient(settings.tradier_base_url, settings.active_tradier_token)
        src = f"Tradier {settings.tradier_mode}"
    else:
        client = MockTradierClient()
        src = "MOCK"

    quote = await client.stock_quote(symbol)
    spot = 0.0
    for k in ("last", "close", "prevclose"):
        try:
            spot = float(quote.get(k) or 0)
        except (TypeError, ValueError):
            spot = 0.0
        if spot > 0:
            break

    exps = await client.option_expirations(symbol)
    today = date.today().isoformat()
    exps_sorted = sorted(str(e) for e in exps)
    chosen = today if today in exps_sorted else next((e for e in exps_sorted if e >= today), exps_sorted[0])
    raw = await client.options_chain(symbol, chosen)
    contracts = [_normalize_tradier_contract(o) for o in raw if o.get("strike") is not None]
    de = compute_dealer_exposures(contracts, spot)
    rows = (de.get("exposures_by_date", {}).get(chosen) or {}).get("by_strike", [])

    lo, hi = spot * (1 - window), spot * (1 + window)
    near = sorted((r for r in rows if lo <= r["strike"] <= hi), key=lambda r: r["strike"])

    # EDGELANE_PROVIDER CONVENTION: gex_tt = call_gex - put_gex  → negative = PUT, positive = CALL.
    print(f"\n=== {symbol}  spot={spot:.2f}  exp={chosen}  source={src}  (OI-based: gamma x open_interest) ===")
    print("    sign convention = EDGELANE_PROVIDER: negative = PUT,  positive = CALL")
    print(f"{'strike':>8} {'pos':>6} {'gex(tt)':>14}  label")
    for r in near:
        gex_tt = r["call_gex"] - r["put_gex"]
        pos = "ABOVE" if r["strike"] > spot else "below" if r["strike"] < spot else "=spot"
        label = "PUT" if gex_tt < 0 else "CALL" if gex_tt > 0 else "—"
        print(f"{r['strike']:>8.0f} {pos:>6} {gex_tt:>14.3e}  {label}")

    if rows:
        ttvals = [(r["strike"], r["call_gex"] - r["put_gex"]) for r in rows]
        put_wall = min(ttvals, key=lambda x: x[1])   # most negative = strongest PUT
        call_wall = max(ttvals, key=lambda x: x[1])  # most positive = strongest CALL
        print(f"\n  strongest PUT wall  (most negative) : {put_wall[0]:.0f}  "
              f"({'ABOVE' if put_wall[0]>spot else 'below'} spot)  gex(tt)={put_wall[1]:.2e}")
        print(f"  strongest CALL wall (most positive) : {call_wall[0]:.0f}  "
              f"({'ABOVE' if call_wall[0]>spot else 'below'} spot)  gex(tt)={call_wall[1]:.2e}")

    # EdgelaneProvider (flow) side, if the backend has a fresh payload
    payload = _fetch_edgelane_provider(settings.cors_base if hasattr(settings, "cors_base") else "http://127.0.0.1:8789", symbol)
    if not payload:
        payload = _fetch_edgelane_provider("http://127.0.0.1:8789", symbol)
    if payload:
        print(f"\n=== {symbol}  EdgelaneProvider extension (flow-based: gamma x volume) ===")
        print(f"  probe put_wall  = {payload.get('put_wall_strike')}  "
              f"call_wall = {payload.get('call_wall_strike')}  gex_wall = {payload.get('gex_wall_strike')}  "
              f"spot = {payload.get('spot')}")
        ts, tg = payload.get("strikes") or [], payload.get("net_gex_by_strike") or []
        prows = sorted(((float(s), float(g)) for s, g in zip(ts, tg)), key=lambda x: x[0])
        pnear = [(s, g) for s, g in prows if lo <= s <= hi]
        # Negate to EdgelaneProvider convention (probe sends puts-positive internally).
        print("    sign convention = EDGELANE_PROVIDER: negative = PUT,  positive = CALL")
        print(f"  {'strike':>8} {'pos':>6} {'gex(tt, flow)':>16}  label")
        for s, g in pnear:
            gex_tt = -g
            pos = "ABOVE" if s > spot else "below" if s < spot else "=spot"
            print(f"  {s:>8.0f} {pos:>6} {gex_tt:>16.3e}  {'PUT' if gex_tt<0 else 'CALL'}")
    else:
        print("\n(no fresh EdgelaneProvider payload on the backend — reopen the tab during market hours to populate)")


if __name__ == "__main__":
    asyncio.run(main())

"""Torque ticker + strategy configuration.

Everything the frontend needs to know about *which* strikes to auto-fill lives
here, server-side — the page never hardcodes offsets. A JSON file at the repo
root (`torque_tickers.json`, or $TORQUE_TICKERS_CONFIG) is deep-merged over
these defaults so the offsets can be tuned without touching code.

Anchoring model (all distances are in index/underlying POINTS, snapped to the
live chain grid by the engine):

  single     long single option, `offset` points OTM from spot (0 = ATM).
  vertical   near leg `anchor` points from spot (OTM direction of the side);
             far leg `width` points beyond the near leg. For DEBIT verticals
             the near leg is the LONG; for CREDIT verticals it's the SHORT.
  condor     short put `short_offset` below spot / short call above; each
             protected by a `width`-point wing.
  iron_fly   short put + short call at ATM; wings `width` points out.
  butterfly  body at spot+`body_offset`; wings `wing` points on each side
             (buy 1 / sell 2 / buy 1).
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

# ── Strategy registry ──────────────────────────────────────────────────────
# Order here is the display order in the strategy selector. Default = bull_call.
STRATEGY_DEFS: dict[str, dict[str, Any]] = {
    "long_call":   {"name": "Long Call",   "short": "Long Call",   "kind": "single",    "type": "debit",  "side": "call", "legs": 1},
    "long_put":    {"name": "Long Put",    "short": "Long Put",    "kind": "single",    "type": "debit",  "side": "put",  "legs": 1},
    "bull_call":   {"name": "Bull Call",   "short": "Bull Call",   "kind": "vertical",  "type": "debit",  "side": "call", "dir": "bull", "legs": 2},
    "bear_put":    {"name": "Bear Put",    "short": "Bear Put",    "kind": "vertical",  "type": "debit",  "side": "put",  "dir": "bear", "legs": 2},
    "bull_put":    {"name": "Bull Put",    "short": "Bull Put",    "kind": "vertical",  "type": "credit", "side": "put",  "dir": "bull", "legs": 2},
    "bear_call":   {"name": "Bear Call",   "short": "Bear Call",   "kind": "vertical",  "type": "credit", "side": "call", "dir": "bear", "legs": 2},
    "iron_condor": {"name": "Iron Condor", "short": "Iron Condor", "kind": "condor",    "type": "credit", "side": "both", "legs": 4},
    "iron_fly":    {"name": "Iron Fly",    "short": "Iron Fly",    "kind": "iron_fly",  "type": "credit", "side": "both", "legs": 4},
    "call_fly":    {"name": "Call Butterfly", "short": "Call Fly", "kind": "butterfly", "type": "debit",  "side": "call", "legs": 3},
    "put_fly":     {"name": "Put Butterfly",  "short": "Put Fly",  "kind": "butterfly", "type": "debit",  "side": "put",  "legs": 3},
}

DEFAULT_STRATEGY = "bull_call"

# ── Per-ticker offsets ─────────────────────────────────────────────────────
# `tick` is the option price increment used for rounding limit prices.
_BASE_OFFSETS: dict[str, Any] = {
    "tick": 0.05,
    "single":    {"offset": 0},
    "vertical":  {"anchor": 0,  "width": 50},
    "condor":    {"short_offset": 50, "width": 50},
    "iron_fly":  {"width": 50},
    "butterfly": {"body_offset": 0, "wing": 50},
}

# First entry = the default ticker (NDX), pre-selected on load.
TICKERS: list[str] = ["NDX", "SPX", "RUT", "SPY", "QQQ"]

# Yahoo Finance chart symbols for the live spot quote (indices need the ^ form;
# ETFs are the bare ticker). Used as the primary spot source, with Tradier
# put-call parity as fallback. A ticker absent here just uses the fallback.
YAHOO_SYMBOLS: dict[str, str] = {
    "NDX": "^NDX", "SPX": "^GSPC", "RUT": "^RUT", "SPY": "SPY", "QQQ": "QQQ",
}


def yahoo_symbol(ticker: str) -> str | None:
    return YAHOO_SYMBOLS.get((ticker or "").upper())

_TICKER_OVERRIDES: dict[str, dict[str, Any]] = {
    "NDX": {
        "tick": 0.05,
        "vertical":  {"anchor": 20, "width": 100},   # long 20pts off spot, short 100 beyond
        "condor":    {"short_offset": 100, "width": 100},
        "iron_fly":  {"width": 100},
        "butterfly": {"body_offset": 0, "wing": 100},
    },
    "SPX": {   # ~7490, 5pt grid
        "tick": 0.05,
        "vertical":  {"anchor": 5, "width": 25},
        "condor":    {"short_offset": 25, "width": 25},
        "iron_fly":  {"width": 25},
        "butterfly": {"body_offset": 0, "wing": 25},
    },
    "RUT": {   # ~2960, 5pt grid
        "tick": 0.05,
        "vertical":  {"anchor": 5, "width": 20},
        "condor":    {"short_offset": 20, "width": 20},
        "iron_fly":  {"width": 20},
        "butterfly": {"body_offset": 0, "wing": 20},
    },
    "SPY": {   # ~746, 1pt grid
        "tick": 0.01,
        "vertical":  {"anchor": 1, "width": 5},
        "condor":    {"short_offset": 5, "width": 5},
        "iron_fly":  {"width": 5},
        "butterfly": {"body_offset": 0, "wing": 5},
    },
    "QQQ": {   # ~738, 1pt grid
        "tick": 0.01,
        "vertical":  {"anchor": 1, "width": 5},
        "condor":    {"short_offset": 5, "width": 5},
        "iron_fly":  {"width": 5},
        "butterfly": {"body_offset": 0, "wing": 5},
    },
}

DEFAULT_CLOSE_TARGET_PCT = 30.0   # +30% profit target for the auto-close order


def _deep_merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _load_overrides_file() -> dict[str, Any]:
    """Optional JSON override file. Shape:
       {"tickers":[...], "default_strategy":"...", "overrides":{"NDX":{...}}}"""
    path = os.environ.get("TORQUE_TICKERS_CONFIG")
    candidates = [Path(path)] if path else []
    # repo root = .../EdgeLane (this file is market/backend/app/torque_config.py)
    candidates.append(Path(__file__).resolve().parents[3] / "torque_tickers.json")
    for p in candidates:
        try:
            if p and p.is_file():
                return json.loads(p.read_text())
        except Exception:
            continue
    return {}


def ticker_rule(ticker: str, strategy: str) -> dict[str, Any]:
    """Resolved offset rule for (ticker, strategy): base ⊕ ticker-override ⊕
    file-override, narrowed to the strategy's kind. Returns e.g.
    {"kind":"vertical","tick":0.05,"anchor":20,"width":100,"type":"debit","side":"call","dir":"bull"}.
    """
    sdef = STRATEGY_DEFS.get(strategy)
    if not sdef:
        raise KeyError(f"unknown strategy {strategy!r}")
    kind = sdef["kind"]
    tk = ticker.upper()

    merged = _deep_merge({}, _BASE_OFFSETS)
    merged = _deep_merge(merged, _TICKER_OVERRIDES.get(tk, {}))
    file_over = _load_overrides_file().get("overrides", {}).get(tk, {})
    merged = _deep_merge(merged, file_over)

    rule = dict(merged.get(kind) or {})
    rule["kind"] = kind
    rule["tick"] = float(merged.get("tick", 0.05))
    rule["type"] = sdef["type"]
    rule["side"] = sdef["side"]
    if "dir" in sdef:
        rule["dir"] = sdef["dir"]
    return rule


def tickers() -> list[str]:
    f = _load_overrides_file().get("tickers")
    return list(f) if isinstance(f, list) and f else list(TICKERS)


def default_strategy() -> str:
    return _load_overrides_file().get("default_strategy") or DEFAULT_STRATEGY


def strategy_list() -> list[dict[str, Any]]:
    """Public strategy registry for the frontend (key + display fields)."""
    return [{"key": k, **{kk: vv for kk, vv in v.items()}} for k, v in STRATEGY_DEFS.items()]

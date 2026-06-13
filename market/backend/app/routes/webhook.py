"""Webhook routes for external GEX data + extension hot-reload policy.

The private GEX provider extension POSTs the wall data here. The poller then
prefers this over its own Tradier-OI-based wall calculation when the payload
is fresh.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..config import get_settings
from ..external_gex import state as ext_state


router = APIRouter()


class EdgelaneProviderGexPayload(BaseModel):
    symbol: str = Field(default="SPX")
    spot: float | None = None
    put_wall_strike: float | None = None
    put_wall_strength: str | None = None         # 'low' | 'medium' | 'high' if extension can infer
    put_wall_net_gex: float | None = None
    call_wall_strike: float | None = None
    call_wall_strength: str | None = None
    call_wall_net_gex: float | None = None
    gex_wall_strike: float | None = None
    # Raw per-strike arrays — strikes + per-strike net_gex
    strikes: list[float] | None = None
    net_gex_by_strike: list[float] | None = None
    # Separate direction signal (NOT the GEX magnet bias)
    swing_sentiment: float | None = None      # >0 bullish, <0 bearish (e.g. 0.2746)
    longterm_sentiment: str | None = None     # e.g. "-2"
    displayed_target: float | None = None     # provider's displayed target, if available
    displayed_sentiment: str | None = None    # provider's displayed sentiment word, if available
    # Flow-dollars direction inputs (private GEX provider profile path):
    # buy/sell-signed cumulative call$ / put$ in DOLLARS (skip premium_spent>5M, skip |side|==1)
    call_premium: float | None = None
    put_premium: float | None = None
    # Extension metadata
    extension_version: str | None = None
    extracted_at_ms: int | None = None
    raw: dict | None = None                      # whatever else the extension wants to attach


@router.post("/webhook/edgelane_provider_gex")
async def receive_gex(payload: EdgelaneProviderGexPayload):
    if not payload.symbol:
        raise HTTPException(400, "symbol required")
    ext_state.set(payload.symbol, payload.model_dump(exclude_none=True))
    return {"ok": True, "symbol": payload.symbol.upper()}


_latest_audit: dict = {}


@router.post("/webhook/page_audit")
async def receive_page_audit(payload: dict):
    """Receive a full-page audit from the extension: provider verdict/target
    + full bodies of non-flow endpoints + value→endpoint correlation. Stored in
    memory for the operator to inspect which API call drives the provider's display."""
    global _latest_audit
    _latest_audit = payload or {}
    return {
        "ok": True,
        "endpoints": [e.get("url") for e in (payload or {}).get("endpoints", [])],
        "matches": (payload or {}).get("matches"),
    }


@router.get("/webhook/page_audit/latest")
async def page_audit_latest():
    """Latest audit. `?full=0` (default) omits endpoint bodies for a quick read;
    add `?full=1` to include them."""
    return _latest_audit


@router.get("/webhook/debug")
async def webhook_debug():
    """Latest payloads per symbol + last 20 received. For the operator to see
    what the extension is sending."""
    s = get_settings()
    return {
        "use_external_gex": s.use_external_gex,
        "external_gex_timeout_sec": s.external_gex_timeout_sec,
        "latest_per_symbol": ext_state.latest_per_symbol(),
        "recent_history": ext_state.recent(20),
    }


@router.get("/extension/version")
async def extension_version():
    """Extension polls this every 30s; if version changes, fetches /extension/policy.json."""
    s = get_settings()
    return {"version": s.extension_policy_version}


@router.get("/extension/policy.json")
async def extension_policy():
    """Hot-reloadable selectors + extraction hints. The extension reads these
    on startup and on /extension/version bumps so we can iterate the extraction
    strategy without the user reinstalling."""
    return {
        "version": get_settings().extension_policy_version,
        "target_url": get_settings().provider_target_url,
        "mode": "api_intercept",                       # intercept fetch/XHR
        "canvas_selector": "#0dte_chart_gex",          # kept for backward compat / fallback
        "spot_selectors": [                            # try these in order for spot price
            "[data-testid='spot-price']",
            ".spot-price",
            "#spot-price",
        ],
        "report_interval_ms": 5000,                    # re-emit interval for latest captured data
        "min_change_threshold": 0.01,                  # only POST if walls delta exceeds this
    }

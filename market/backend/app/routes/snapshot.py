"""Latest engine output per symbol. Read-only — populated by the poller.

Gated by `require_teaser_session`: anonymous visitors with a valid teaser
session see only the teaser chips (spot / bias / walls); logged-in users (and
admin/dev) get the full payload including strategies + engine_pick.

MATRIX_DECISION_SOURCE=levels (docs/level_engine.md §3.4, revised 2026-11-23)
— for SPX and the full identity only (teasers never see level data), the
payload is `level_view.level_snapshot(...)`: everything Matrix shows as a
DECISION comes from the level engine, the legacy engine is a data service only.
  * level engine has a pick  → engine_pick = the level pick,
                               strategies  = the level grid (decision.strategies)
  * reachable, no pick       → engine_pick = None, strategies = {} (the tile
    (wait / done / no tick)     shows the level state)
  * unreachable              → engine_pick = None, strategies = {} — nothing
                               legacy is shown (no degrade to the legacy pick)
  every case adds engine_pick_legacy / strategies_legacy (audit only) and
  level_decision = {call, confidence, reason, target_level, invalidation_level,
  status, ...} ({status: "unavailable"} when unreachable).
Spot / bias / walls render as today. The poller's cached snapshot is never
mutated (the payload is a copy) — the internal feed keeps sending the legacy
output to the level engine.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from .. import level_view
from ..config import get_settings
from ..poller import state as poller_state
from ..session_auth import require_teaser_session

router = APIRouter()

# Fields that are the paid/gated product — stripped for anonymous teaser viewers.
# The teaser keeps spot + bias (which carries the walls) + light context.
_GATED_FIELDS = ("strategies", "engine_pick")


_LEVELS_SYMBOL = level_view.LEVELS_SYMBOL


def _levels_on(symbol: str, ident: dict) -> bool:
    return (bool(ident.get("full")) and symbol == _LEVELS_SYMBOL
            and level_view.levels_mode(get_settings()))


async def _swap_level_pick(payload: dict) -> dict:
    """Return the levels-view COPY of `payload` (see module doc)."""
    return await level_view.level_snapshot(get_settings(), payload)


def _redact_for_teaser(payload: dict) -> dict:
    out = {k: v for k, v in payload.items() if k not in _GATED_FIELDS}
    out["teaser"] = True
    out["locked"] = list(_GATED_FIELDS)
    return out


@router.get("/snapshot/{symbol}")
async def get_snapshot(symbol: str, ident: dict = Depends(require_teaser_session)):
    sym = symbol.upper()
    snap = poller_state.latest_by_symbol.get(sym)
    if snap is None:
        # Surface an informative 404 — UI can show "waiting on first poll"
        raise HTTPException(
            status_code=404,
            detail={
                "error": f"no snapshot yet for {sym}",
                "market_open": poller_state.market_open,
                "market_reason": poller_state.market_reason,
                "is_polling": poller_state.is_polling,
                "last_error": poller_state.last_error.get(sym),
            },
        )
    payload = {
        "symbol": sym,
        "polled_at": poller_state.last_poll_at.get(sym),
        "market_open": poller_state.market_open,
        "market_reason": poller_state.market_reason,
        "evaluation_active": poller_state.evaluation_active,
        **snap,
    }
    if _levels_on(sym, ident):
        payload = await _swap_level_pick(payload)
    return payload if ident.get("full") else _redact_for_teaser(payload)


@router.get("/snapshot")
async def list_snapshots(ident: dict = Depends(require_teaser_session)):
    full = ident.get("full")
    snapshots = poller_state.latest_by_symbol
    if not full:
        snapshots = {s: _redact_for_teaser(dict(v)) for s, v in snapshots.items()}
    elif _LEVELS_SYMBOL in snapshots and _levels_on(_LEVELS_SYMBOL, ident):
        snapshots = dict(snapshots)     # never mutate the poller's cache
        snapshots[_LEVELS_SYMBOL] = await _swap_level_pick(snapshots[_LEVELS_SYMBOL])
    return {
        "market_open": poller_state.market_open,
        "market_reason": poller_state.market_reason,
        "is_polling": poller_state.is_polling,
        "last_loop_at": poller_state.last_loop_at,
        "symbols": list(poller_state.latest_by_symbol.keys()),
        "snapshots": snapshots,
        "polled_at": poller_state.last_poll_at,
        "errors": poller_state.last_error,
    }

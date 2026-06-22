"""Latest engine output per symbol. Read-only — populated by the poller.

Gated by `require_teaser_session`: anonymous visitors with a valid teaser
session see only the teaser chips (spot / bias / walls); logged-in users (and
admin/dev) get the full payload including strategies + engine_pick.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from ..poller import state as poller_state
from ..session_auth import require_teaser_session

router = APIRouter()

# Fields that are the paid/gated product — stripped for anonymous teaser viewers.
# The teaser keeps spot + bias (which carries the walls) + light context.
_GATED_FIELDS = ("strategies", "engine_pick")


def _redact_for_teaser(payload: dict) -> dict:
    out = {k: v for k, v in payload.items() if k not in _GATED_FIELDS}
    out["teaser"] = True
    out["locked"] = list(_GATED_FIELDS)
    return out


@router.get("/snapshot/{symbol}")
def get_snapshot(symbol: str, ident: dict = Depends(require_teaser_session)):
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
    return payload if ident.get("full") else _redact_for_teaser(payload)


@router.get("/snapshot")
def list_snapshots(ident: dict = Depends(require_teaser_session)):
    full = ident.get("full")
    snapshots = poller_state.latest_by_symbol
    if not full:
        snapshots = {s: _redact_for_teaser(dict(v)) for s, v in snapshots.items()}
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

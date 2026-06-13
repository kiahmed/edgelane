"""Latest engine output per symbol. Read-only — populated by the poller."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from ..poller import state as poller_state

router = APIRouter()


@router.get("/snapshot/{symbol}")
def get_snapshot(symbol: str):
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
    return {
        "symbol": sym,
        "polled_at": poller_state.last_poll_at.get(sym),
        "market_open": poller_state.market_open,
        "market_reason": poller_state.market_reason,
        "evaluation_active": poller_state.evaluation_active,
        **snap,
    }


@router.get("/snapshot")
def list_snapshots():
    return {
        "market_open": poller_state.market_open,
        "market_reason": poller_state.market_reason,
        "is_polling": poller_state.is_polling,
        "last_loop_at": poller_state.last_loop_at,
        "symbols": list(poller_state.latest_by_symbol.keys()),
        "snapshots": poller_state.latest_by_symbol,
        "polled_at": poller_state.last_poll_at,
        "errors": poller_state.last_error,
    }

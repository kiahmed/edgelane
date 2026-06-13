"""Service health + market-hours status."""
from __future__ import annotations
from datetime import datetime
from zoneinfo import ZoneInfo
from fastapi import APIRouter, Request

from ..poller import state as poller_state
from ..tradier_client import get_rate_limit_state

router = APIRouter()


def _is_market_open(tz_name: str, open_hhmm: str, close_hhmm: str) -> tuple[bool, str]:
    tz = ZoneInfo(tz_name)
    now = datetime.now(tz)
    if now.weekday() >= 5:
        return False, f"weekend ({now.strftime('%A')})"
    open_h, open_m = (int(x) for x in open_hhmm.split(":"))
    close_h, close_m = (int(x) for x in close_hhmm.split(":"))
    open_dt = now.replace(hour=open_h, minute=open_m, second=0, microsecond=0)
    close_dt = now.replace(hour=close_h, minute=close_m, second=0, microsecond=0)
    if now < open_dt:
        mins = int((open_dt - now).total_seconds() / 60)
        return False, f"market opens in {mins}m"
    if now >= close_dt:
        return False, "market closed for the day"
    return True, "market open"


def _latest_poll_ts() -> str | None:
    if not poller_state.last_poll_at:
        return None
    return max(poller_state.last_poll_at.values())


def _latest_poll_symbol() -> str | None:
    if not poller_state.last_poll_at:
        return None
    return max(poller_state.last_poll_at.items(), key=lambda kv: kv[1])[0]


@router.get("/status")
async def status(request: Request):
    s = request.app.state.settings
    is_open, reason = _is_market_open(s.market_hours_tz, s.market_open, s.market_close)
    using_mock = bool(getattr(request.app.state, "using_mock", False))
    rl = get_rate_limit_state() or None
    resolved_account_id = (
        getattr(request.app.state, "tradier_account_id", "")
        or s.active_account_id
        or ""
    )
    return {
        "alive": True,
        "version": "0.1.0",
        "market_open": is_open,
        "market_reason": reason,
        "symbols": s.symbols,
        "poll_interval_sec": s.poll_interval_sec,
        "tradier_env": s.tradier_env,
        "tradier_account_id": resolved_account_id,
        "using_mock": using_mock,
        "tradier_mode": s.tradier_mode,
        "tradier_rate_limit": rl,
        "tradier_last_auth_at": poller_state.last_tradier_success_at,
        "tradier_last_error": poller_state.last_tradier_error,
        "tradier_last_error_at": poller_state.last_tradier_error_at,
        "polling": poller_state.is_polling,
        "poller_market_open": poller_state.market_open,
        "poller_market_reason": poller_state.market_reason,
        "last_poll_at": poller_state.last_poll_at,
        "last_loop_at": poller_state.last_loop_at,
        "last_error": poller_state.last_error,
        "last_poll_ts": _latest_poll_ts(),
        "last_poll_symbol": _latest_poll_symbol(),
    }

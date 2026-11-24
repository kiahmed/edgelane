"""Matrix ↔ level-engine wiring tests (docs/level_engine.md §3.3, §3.4).

Hermetic: no network (the level engine is faked at `level_pick._http_get` or
`level_pick.fetch_decision`), safe default settings, poller cache and the
level-pick cache saved/restored around every test."""
from __future__ import annotations

import copy

import pytest

from app import config, level_pick
from app.config import Settings
from app.poller import state as poller_state

EXPIRATION = "2026-11-23"
LEVEL_PICK_ID = "a1b2c3d4e5f60718"
CAND_PICK_ID = "ffeeddccbbaa0099"


def legacy_pick() -> dict:
    return {
        "strategy": "bull_call", "name": "Bull Call Debit", "short": "BCS",
        "spread_type": "debit", "label": "Balanced", "width_preset": "balanced",
        "composite_score": 62.4, "structure_text": "Buy 7270C / Sell 7280C",
        "net_premium": 4.1, "decision_id": 77,
        "legs": [{"side": "call", "strike": 7270.0, "long_short": 1,
                  "symbol": "SPXW261123C07270000"},
                 {"side": "call", "strike": 7280.0, "long_short": -1,
                  "symbol": "SPXW261123C07280000"}],
    }


def level_pick_dict() -> dict:
    return {
        "strategy": "call_butterfly", "name": "Call Butterfly", "short": "CFly",
        "spread_type": "debit", "label": "Near", "width_preset": "levels",
        "composite_score": 71.0, "composite_verdict": {"label": "good", "color": "emerald"},
        "structure_text": "7260/7280/7300 call fly", "net_premium": 3.2,
        "max_profit": 16.8, "max_loss": 3.2, "pop_pct": 31.0, "ev": 0.4,
        "legs": [{"side": "call", "strike": 7260.0, "long_short": 1,
                  "symbol": "SPXW261123C07260000"},
                 {"side": "call", "strike": 7280.0, "long_short": -2,
                  "symbol": "SPXW261123C07280000"},
                 {"side": "call", "strike": 7300.0, "long_short": 1,
                  "symbol": "SPXW261123C07300000"}],
        "source": "levels", "pick_id": LEVEL_PICK_ID, "call": "up",
        "target_level": {"price": 7280.0, "name": "R1"},
        "invalidation_level": {"price": 7240.0, "name": "S1"},
        "confidence": 0.64, "reason": "held 7250 support",
        "expiration": EXPIRATION, "limit_premiums": {"mid": 3.2},
        "rr": {"near": {"cost": 320, "pnl": 900, "target": 7280}, "extreme": None},
        "rr_line": "Near: risk $320 → +$900 at 7,280",
        "candidates": [{"pick_id": CAND_PICK_ID, "strategy": "bull_call", "label": "Near"}],
    }


def decision_with_pick() -> dict:
    p = level_pick_dict()
    return {"ts": "2026-11-23T15:00:00+00:00", "symbol": "SPX", "call": "up",
            "confidence": 0.64, "reason": "held 7250 support",
            "target_level": p["target_level"], "invalidation_level": p["invalidation_level"],
            "status": "ok", "pick": p, "engine_status": {"feed": "ok"}}


def decision_wait(call: str = "wait") -> dict:
    return {"ts": "2026-11-23T15:00:00+00:00", "symbol": "SPX", "call": call,
            "confidence": 0.3, "reason": "inside the zone", "target_level": None,
            "invalidation_level": None, "status": "ok", "pick": None,
            "engine_status": {"feed": "ok"}}


def spx_snapshot() -> dict:
    return {"symbol": "SPX", "spot": 7255.0, "expiration": EXPIRATION, "dte": 0,
            "engine_pick": legacy_pick(),
            "strategies": {"bull_call": {"best": legacy_pick(), "all_widths": []}}}


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    monkeypatch.setattr(config, "_cached", Settings())
    saved = dict(poller_state.latest_by_symbol)
    saved_poll = dict(poller_state.last_poll_at)
    level_pick.clear_cache()
    poller_state.latest_by_symbol.clear()
    poller_state.latest_by_symbol["SPX"] = spx_snapshot()
    poller_state.latest_by_symbol["NDX"] = {"symbol": "NDX", "spot": 20000.0,
                                            "engine_pick": {"strategy": "bear_call"},
                                            "strategies": {}}
    yield
    level_pick.clear_cache()
    poller_state.latest_by_symbol.clear()
    poller_state.latest_by_symbol.update(saved)
    poller_state.last_poll_at.clear()
    poller_state.last_poll_at.update(saved_poll)


def use_settings(monkeypatch, **kw) -> Settings:
    s = Settings(**kw)
    monkeypatch.setattr(config, "_cached", s)
    return s


def fake_decision(monkeypatch, decision):
    """Replace fetch_decision; returns the list of calls (symbol, max_age)."""
    calls: list[tuple] = []

    async def _fake(settings, symbol, *, max_age=level_pick.CACHE_TTL_SEC):
        calls.append((symbol, max_age))
        return copy.deepcopy(decision)

    monkeypatch.setattr(level_pick, "fetch_decision", _fake)
    return calls

"""Levels switch — the levels view of Matrix (docs/level_engine.md §3.4, revised
2026-11-23; contract C1–C3). Hermetic: the level engine is faked at
`level_pick.fetch_decision` / `level_pick._http_get`, safe default settings,
poller cache + level-pick cache saved/restored around every test.

Reuses the tests/level_matrix fixtures' data builders."""
from __future__ import annotations

import pytest

from app import config, level_pick
from app.config import Settings
from app.poller import state as poller_state

from ..level_matrix.conftest import (CAND_PICK_ID, EXPIRATION, LEVEL_PICK_ID,  # noqa: F401
                                     decision_wait, decision_with_pick, fake_decision,
                                     legacy_pick, level_pick_dict, spx_snapshot,
                                     use_settings)

GRID_PICK_ID = "0123456789abcdef"      # bull_call grid card (near)
EXTREME_PICK_ID = "fedcba9876543210"   # call_butterfly extreme sibling


def _bull_call_card() -> dict:
    return {
        "strategy": "bull_call", "name": "Bull Call Debit", "short": "BCS",
        "spread_type": "debit", "type": "debit", "label": "Near", "width_preset": "levels",
        "composite_score": 58.0, "composite_verdict": {"label": "ok", "color": "amber"},
        "structure_text": "Buy 7265C / Sell 7280C", "net_premium": 5.0,
        "max_profit": 10.0, "max_loss": 5.0, "pop_pct": 40.0, "ev": 0.2,
        "health": "healthy", "health_legacy": "ok", "rr_profit": 1.0,
        "legs": [{"side": "call", "strike": 7265.0, "long_short": 1,
                  "symbol": "SPXW261123C07265000"},
                 {"side": "call", "strike": 7280.0, "long_short": -1,
                  "symbol": "SPXW261123C07280000"}],
        "pick_id": GRID_PICK_ID, "reach": "near", "expiration": EXPIRATION,
        "rr": {"near": {"cost": 500, "pnl": 500, "target": 7280}}, "rr_line": "Near: …",
    }


def _fly_extreme() -> dict:
    c = level_pick_dict()
    c.pop("candidates", None)
    c.update(pick_id=EXTREME_PICK_ID, label="Extreme", reach="extreme",
             structure_text="7280/7300/7320 call fly",
             legs=[{"side": "call", "strike": 7280.0, "long_short": 1,
                    "symbol": "SPXW261123C07280000"},
                   {"side": "call", "strike": 7300.0, "long_short": -2,
                    "symbol": "SPXW261123C07300000"},
                   {"side": "call", "strike": 7320.0, "long_short": 1,
                    "symbol": "SPXW261123C07320000"}])
    return c


def level_grid() -> dict:
    fly = level_pick_dict()
    fly.pop("candidates", None)
    fly["reach"] = "near"
    return {
        "call_butterfly": {"strategy": "call_butterfly", "name": "Call Butterfly",
                           "short": "CFly", "type": "debit", "side": "bull",
                           "is_recommended": True, "best": fly,
                           "all_widths": [fly, _fly_extreme()]},
        "bull_call": {"strategy": "bull_call", "name": "Bull Call Debit", "short": "BCS",
                      "type": "debit", "side": "bull", "is_recommended": False,
                      "best": _bull_call_card(), "all_widths": [_bull_call_card()]},
    }


def decision_with_grid() -> dict:
    d = decision_with_pick()
    d["strategies"] = level_grid()
    return d


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    monkeypatch.setattr(config, "_cached", Settings())
    saved = dict(poller_state.latest_by_symbol)
    saved_poll = dict(poller_state.last_poll_at)
    saved_err = dict(poller_state.last_error)
    saved_mkt = (poller_state.market_open, poller_state.market_reason)
    level_pick.clear_cache()
    poller_state.latest_by_symbol.clear()
    poller_state.latest_by_symbol["SPX"] = spx_snapshot()
    yield
    level_pick.clear_cache()
    for d, v in ((poller_state.latest_by_symbol, saved), (poller_state.last_poll_at, saved_poll),
                 (poller_state.last_error, saved_err)):
        d.clear()
        d.update(v)
    poller_state.market_open, poller_state.market_reason = saved_mkt
    poller_state._pending_feed.clear()


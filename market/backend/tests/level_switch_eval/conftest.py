"""Levels-mode self-eval tests (docs/level_engine.md §3.4, §12; contract C3).

Hermetic: the level engine's read API is faked at `level_pick.get_upstream`
(no network), settings start from safe defaults (events off, no keys), the
lowest real Supabase / Pub/Sub / email layers FAIL the test if reached, and the
poller cache, evaluator / signal state and level caches are restored around
every test."""
from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone

import pytest

from app import (config, emailer, level_accuracy, level_pick, matrix_events,
                 matrix_signals, supabase_admin)
from app.config import Settings
from app.evaluator import state as evaluator_state
from app.poller import state as poller_state

PICK_ID = "a1b2c3d4e5f60718"
OTHER_PICK_ID = "ffeeddccbbaa0099"
EXPIRATION = "2026-11-23"


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.isoformat()


def leg(i: int, result: str | None, *, opened: datetime, minutes: float = 20.0,
        pick_id: str | None = None, strategy: str = "call_butterfly", call: str = "up",
        level: str = "R1", trade_date: str | None = None, pnl: float | None = 1.25,
        close_reason: str | None = None) -> dict:
    closed = opened + timedelta(minutes=minutes) if result is not None else None
    return {
        "id": f"leg-{i}", "trade_date": trade_date or level_accuracy.today_et(),
        "opened_at": iso(opened), "closed_at": iso(closed) if closed else None,
        "call": call, "result": result,
        "close_reason": close_reason or ({"win": "target", "loss": "invalidation"}.get(result)),
        "target_level": {"price": 7280.0, "name": level},
        "invalidation_level": {"price": 7240.0, "name": "S1"},
        "entry_spot": 7255.0, "exit_spot": 7279.0 if result == "win" else 7238.0,
        "progress_pct": 100.0 if result == "win" else 20.0, "mae_pts": 3.0,
        "time_to_target_min": minutes if result == "win" else None,
        "strategy_pnl": pnl, "pick_id": pick_id or f"pid{i:013d}",
        "strategy": strategy, "label": "Near",
        "structure_text": "7260/7280/7300 call fly", "spread_type": "debit",
    }


def stats_body(legs_oldest_first: list[dict], open_leg: dict | None = None) -> dict:
    """A C3 body; legs served newest-closed first."""
    legs = sorted(legs_oldest_first, key=lambda l: l["closed_at"] or "", reverse=True)
    closed = [l for l in legs if l["result"]]
    wins = sum(1 for l in closed if l["result"] == "win")
    return {"symbol": "SPX", "as_of": iso(now_utc()), "legs": legs, "open_leg": open_leg,
            "counts": {"n": len(closed), "wins": wins, "losses": len(closed) - wins,
                       "win_rate": (100.0 * wins / len(closed)) if closed else None,
                       "by_call": {}, "by_level": {}}}


def results_stats(results: list[str], *, start: datetime | None = None, **kw) -> dict:
    """Legs for `results` (oldest first), 25 minutes apart, ending ~now."""
    start = start or (now_utc() - timedelta(minutes=25 * len(results) + 30))
    return stats_body([leg(i, r, opened=start + timedelta(minutes=25 * i), **kw)
                       for i, r in enumerate(results)])


def level_pick_dict(pick_id: str = PICK_ID) -> dict:
    return {
        "strategy": "call_butterfly", "name": "Call Butterfly", "short": "CFly",
        "spread_type": "debit", "label": "Near", "width_preset": "levels",
        "composite_score": 71.0, "composite_verdict": {"label": "good", "color": "emerald"},
        "structure_text": "7260/7280/7300 call fly", "net_premium": 3.2,
        "max_profit": 16.8, "max_loss": 3.2, "pop_pct": 31.0, "ev": 0.4,
        "health": "healthy", "health_legacy": "thin", "rr_profit": 1.9,
        "legs": [{"side": "call", "strike": 7260.0, "long_short": 1,
                  "symbol": "SPXW261123C07260000"},
                 {"side": "call", "strike": 7280.0, "long_short": -2,
                  "symbol": "SPXW261123C07280000"},
                 {"side": "call", "strike": 7300.0, "long_short": 1,
                  "symbol": "SPXW261123C07300000"}],
        "source": "levels", "pick_id": pick_id, "call": "up",
        "target_level": {"price": 7280.0, "name": "R1"},
        "invalidation_level": {"price": 7240.0, "name": "S1"},
        "rr_line": "Near: risk $320 → +$900 at 7,280",
    }


def decision_with_pick(pick_id: str = PICK_ID) -> dict:
    p = level_pick_dict(pick_id)
    return {"ts": iso(now_utc()), "symbol": "SPX", "call": "up", "confidence": 0.64,
            "reason": "held support", "target_level": p["target_level"],
            "invalidation_level": p["invalidation_level"], "status": "ok", "pick": p,
            "strategies": {"call_butterfly": {"strategy": "call_butterfly", "best": p,
                                              "all_widths": [p], "is_recommended": True}},
            "engine_status": {"feed": "ok"}}


def legacy_pick() -> dict:
    return {"strategy": "bear_call", "name": "Bear Call Spread", "label": "Aggressive",
            "composite_score": 86.1, "health": "healthy",
            "structure_text": "Short 7680C / Long 7720C",
            "composite_verdict": {"label": "tradeable on limit"},
            "legs": [{"symbol": "SPXW261123C07680000", "long_short": -1},
                     {"symbol": "SPXW261123C07720000", "long_short": 1}]}


def spx_snapshot() -> dict:
    return {"symbol": "SPX", "spot": 7255.0, "expiration": EXPIRATION,
            "bias": {"confidence": "high", "call_wall_strike": 7300.0,
                     "put_wall_strike": 7200.0},
            "engine_pick": legacy_pick(),
            "strategies": {"bear_call": {"best": legacy_pick(), "all_widths": []}}}


class Upstream:
    """Fake read API: path → (status, body) | callable | exception."""

    def __init__(self) -> None:
        self.routes: dict[str, object] = {}
        self.calls: list[tuple[str, dict | None]] = []

    async def __call__(self, settings, path, params=None):
        self.calls.append((path, params))
        r = self.routes.get(path)
        if r is None:
            raise level_pick.LevelEngineUnavailable("no fake route")
        if isinstance(r, Exception):
            raise r
        status, body = r
        return status, copy.deepcopy(body)

    def stats(self, body: dict | None) -> None:
        if body is None:
            self.routes.pop("/levels/stats/SPX", None)
        else:
            self.routes["/levels/stats/SPX"] = (200, body)

    def day(self, d: str, body: dict | None) -> None:
        if body is None:
            self.routes.pop(f"/levels/day/{d}", None)
        else:
            self.routes[f"/levels/day/{d}"] = (200, body)

    def decision(self, dec: dict | None) -> None:
        self.routes["/levels/decision/SPX"] = (200, {"symbol": "SPX", "decision": dec,
                                                     "status": {"feed": "ok"}})


@pytest.fixture
def upstream(monkeypatch) -> Upstream:
    u = Upstream()
    monkeypatch.setattr(level_pick, "get_upstream", u)
    return u


def use_settings(monkeypatch, **kw) -> Settings:
    s = Settings(**kw)
    monkeypatch.setattr(config, "_cached", s)
    return s


@pytest.fixture
def levels(monkeypatch) -> Settings:
    return use_settings(monkeypatch, matrix_decision_source="levels")


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    monkeypatch.setattr(config, "_cached", Settings())

    async def _no_supabase(*a, **k):
        raise AssertionError("test reached a REAL Supabase write — stub it")

    def _no_pubsub():
        raise AssertionError("test reached the REAL Pub/Sub client — stub it")

    async def _no_email(*a, **k):
        raise AssertionError("test reached the REAL email send — stub it")

    monkeypatch.setattr(supabase_admin, "insert_row_ignore_duplicates", _no_supabase)
    monkeypatch.setattr(supabase_admin, "insert_row", _no_supabase)
    monkeypatch.setattr(matrix_events, "_get_publisher", _no_pubsub)
    monkeypatch.setattr(emailer, "send_email", _no_email)
    monkeypatch.setattr(matrix_signals, "_within_open_window", lambda settings: False)

    saved_snaps = dict(poller_state.latest_by_symbol)
    saved_eval = {k: copy.deepcopy(v) for k, v in vars(evaluator_state).items()}
    level_pick.clear_cache()
    level_accuracy.clear_cache()
    matrix_signals.state.reset()
    poller_state.latest_by_symbol.clear()
    poller_state.latest_by_symbol["SPX"] = spx_snapshot()
    yield
    matrix_signals.state.reset()
    level_pick.clear_cache()
    level_accuracy.clear_cache()
    poller_state.latest_by_symbol.clear()
    poller_state.latest_by_symbol.update(saved_snaps)
    for k, v in saved_eval.items():
        setattr(evaluator_state, k, v)


@pytest.fixture
def sent(monkeypatch):
    """Every publish handed to the transport (nothing real is reached)."""
    calls: list[dict] = []

    async def _fake(symbol, state, expiry=None, *, day=None, discriminator=None,
                    extra_attributes=None):
        calls.append({"symbol": symbol, "state": state, "disc": discriminator,
                      "attrs": extra_attributes or {}})
        return True

    monkeypatch.setattr(matrix_signals.matrix_events, "publish_transition", _fake)
    return calls

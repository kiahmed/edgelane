"""Poller in levels mode (§3.4): SPX only, no gradable legacy pick, signals get
the level view — and the poller cache is never mutated. Engine mode unchanged."""
from __future__ import annotations

import asyncio
import copy
import json
from types import SimpleNamespace

import pytest

from app import matrix_signals, poller
from app.mock_tradier import MockTradierClient
from app.poller import state as poller_state

from .conftest import LEVEL_PICK_ID, decision_with_grid, fake_decision, level_grid


class FakeDB:
    def __init__(self):
        self.bias_rows: list[dict] = []
        self.gex_rows: list[dict] = []

    def get_strike_profile(self, symbol):
        return None

    def insert_gex_snapshot(self, row):
        self.gex_rows.append(row)

    def insert_bias_decision(self, row):
        self.bias_rows.append(row)
        return 4242

    def insert_gex_profile_snapshot(self, row):
        pass

    def insert_edgelane_provider_display(self, row):
        pass


def _settings(mode: str | None, **kw):
    ns = SimpleNamespace(level_feed_enabled=True, **kw)
    if mode is not None:
        ns.matrix_decision_source = mode
    return ns


@pytest.fixture
def signals(monkeypatch):
    got: list[dict] = []

    async def _on(snap, settings=None):
        got.append(snap)
        return []

    monkeypatch.setattr(matrix_signals, "on_snapshot", _on)
    return got


_PICK_COLS = ("pick_legs", "pick_entry_mid", "pick_spread_type", "pick_strategy")


async def test_engine_mode_captures_the_legacy_pick_and_signals_get_the_output(
        monkeypatch, signals):
    calls = fake_decision(monkeypatch, decision_with_grid())
    db = FakeDB()
    out = await poller.poll_symbol("SPX", MockTradierClient(), db, _settings("engine"))
    assert out["engine_pick"], "mock chain should yield a legacy pick"
    row = db.bias_rows[-1]
    assert json.loads(row["pick_legs"]) == out["engine_pick"]["legs"]
    assert row["pick_strategy"] == out["engine_pick"]["strategy"]
    assert out["engine_pick"]["decision_id"] == 4242
    assert signals == [out] and signals[0] is out                # the output itself
    assert calls == []                                           # level engine never asked


async def test_levels_mode_writes_bias_but_no_gradable_pick(monkeypatch, signals):
    fake_decision(monkeypatch, decision_with_grid())
    db = FakeDB()
    out = await poller.poll_symbol("SPX", MockTradierClient(), db, _settings("levels"))
    assert out["engine_pick"]                                    # legacy computed (data service)
    row = db.bias_rows[-1]
    for k in _PICK_COLS:
        assert row[k] is None, k
    assert row["label"] == (out["bias"] or {}).get("bias_label")  # bias still written
    assert row["put_wall_strike"] == out["bias"].get("put_wall_strike")
    assert "decision_id" not in out["engine_pick"]               # nothing written back


async def test_levels_mode_signals_get_the_level_view_and_the_output_is_untouched(
        monkeypatch, signals):
    fake_decision(monkeypatch, decision_with_grid())
    out = await poller.poll_symbol("SPX", MockTradierClient(), FakeDB(),
                                   _settings("levels"))
    assert len(signals) == 1
    view = signals[0]
    assert view is not out
    assert view["engine_pick"]["pick_id"] == LEVEL_PICK_ID
    assert view["strategies"] == level_grid()
    assert view["engine_pick_legacy"] == out["engine_pick"]
    assert out["engine_pick"].get("pick_id") is None             # output still legacy
    assert out["strategies"] != level_grid()
    assert "level_decision" not in out and "engine_pick_legacy" not in out


async def test_levels_mode_unreachable_signals_see_no_pick(monkeypatch, signals):
    fake_decision(monkeypatch, None)
    await poller.poll_symbol("SPX", MockTradierClient(), FakeDB(), _settings("levels"))
    assert signals[0]["engine_pick"] is None and signals[0]["strategies"] == {}


async def test_levels_mode_display_only_poll_sends_no_signals(monkeypatch, signals):
    calls = fake_decision(monkeypatch, decision_with_grid())
    await poller.poll_symbol("SPX", MockTradierClient(), FakeDB(), _settings("levels"),
                             persist=False)
    assert signals == [] and calls == []


async def test_the_poller_cache_is_never_mutated(monkeypatch, signals):
    """The internal feed sends the CACHED legacy output to the level engine; the
    level view handed to signals (or anyone) must never leak into it."""
    fake_decision(monkeypatch, decision_with_grid())
    poller_state.latest_by_symbol.clear()
    await poller._poll_all(["SPX"], MockTradierClient(), FakeDB(), _settings("levels"))
    cached = poller_state.latest_by_symbol["SPX"]
    before = copy.deepcopy(cached)
    pick_obj = cached["engine_pick"]
    # consumers mutate their view freely
    view = signals[0]
    view["engine_pick"]["legs"].clear()
    view["engine_pick_legacy"]["strategy"] = "x"
    view["strategies_legacy"].clear()
    view["strategies"].clear()
    assert cached == before
    assert cached["engine_pick"] is pick_obj
    assert not {"level_decision", "engine_pick_legacy", "strategies_legacy"} & set(cached)
    assert cached["engine_pick"].get("pick_id") is None
    # and a fresh level view doesn't touch it either
    from app import level_view
    await level_view.level_snapshot(_settings("levels"), cached)
    assert cached == before


# ── poll_loop symbol set ────────────────────────────────────────────────────

async def _loop_symbols(monkeypatch, settings) -> list[str]:
    seen: list[list[str]] = []

    async def _poll_all(symbols, *a, **kw):
        seen.append(list(symbols))
        raise asyncio.CancelledError

    monkeypatch.setattr(poller, "_poll_all", _poll_all)
    monkeypatch.setattr(poller, "_is_market_open", lambda *a, **k: (True, "open"))
    with pytest.raises(asyncio.CancelledError):
        await poller.poll_loop(MockTradierClient(), FakeDB(), settings)
    return seen[0]


@pytest.mark.parametrize("mode,expected", [("engine", ["SPX", "NDX"]),
                                           (None, ["SPX", "NDX"]),
                                           ("levels", ["SPX"])])
async def test_poll_loop_polls_only_spx_in_levels_mode(monkeypatch, mode, expected):
    s = _settings(mode, symbols=["SPX", "NDX"], poll_interval_sec=20)
    assert await _loop_symbols(monkeypatch, s) == expected


async def test_poll_loop_levels_mode_from_process_settings(monkeypatch):
    """The real poller gets main._settings_view (no matrix_decision_source)."""
    from .conftest import use_settings
    use_settings(monkeypatch, matrix_decision_source="levels")
    s = _settings(None, symbols=["SPX", "NDX"], poll_interval_sec=20)
    assert await _loop_symbols(monkeypatch, s) == ["SPX"]




async def test_levels_mode_view_failure_still_shows_no_legacy_pick(monkeypatch, signals):
    from app import level_view

    async def _boom(*a, **k):
        raise RuntimeError("x")

    monkeypatch.setattr(level_view, "level_snapshot", _boom)
    out = await poller.poll_symbol("SPX", MockTradierClient(), FakeDB(), _settings("levels"))
    assert signals[0]["engine_pick"] is None and signals[0]["strategies"] == {}
    assert signals[0]["level_decision"] == {"status": "unavailable"}
    assert out["engine_pick"]                                     # output untouched


async def test_real_on_snapshot_accepts_the_level_view_as_is(monkeypatch):
    """matrix_signals re-derives a view only when `level_decision` is absent —
    the poller's view must pass through without a second (legacy-corrupting) pass."""
    from app import level_view
    fake_decision(monkeypatch, decision_with_grid())
    view = await level_view.level_snapshot(_settings("levels"), {"symbol": "SPX",
                                                                 "engine_pick": None})
    assert "level_decision" in view

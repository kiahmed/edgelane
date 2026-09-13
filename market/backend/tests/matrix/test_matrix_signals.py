"""Matrix transition detection — each of the six states fires once, on a real
change, and never on a repeat poll (docs/matrix_events_update.md §2/§3)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app import matrix_signals as ms


# ── fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _clean_state():
    ms.state.reset()
    yield
    ms.state.reset()


@pytest.fixture
def sent(monkeypatch):
    """Capture publishes instead of hitting Pub/Sub."""
    calls: list[dict] = []

    async def _fake(symbol, state, expiry=None, *, day=None, extra_attributes=None):
        calls.append({"symbol": symbol, "state": state, "expiry": expiry,
                      "attrs": extra_attributes or {}})
        return True

    monkeypatch.setattr(ms.matrix_events, "publish_transition", _fake)
    return calls


def _snap(**over) -> dict:
    base = {
        "symbol": "SPX",
        "expiration": "2026-09-18",
        "spot": 7790.0,
        "bias": {"bias_label": "bearish", "directional_score": -80.0,
                 "confidence": "high", "call_wall_strike": 7800.0,
                 "put_wall_strike": 7700.0},
        "engine_pick": {"strategy": "bear_call", "label": "Aggressive",
                        "strikes": [7680.0, 7720.0], "composite_score": 86.1,
                        "structure_text": "Short 7680C / Long 7720C",
                        "composite_verdict": {"label": "tradeable on limit"}},
        "strategies": {f"s{i}": {"best": {"label": "Balanced", "health": "healthy",
                                          "composite_verdict": {"label": "ok"}}}
                       for i in range(8)},
    }
    base.update(over)
    return base


def _states(calls) -> list[str]:
    return [c["state"] for c in calls]


# ── pick_selected ───────────────────────────────────────────────────────────

async def test_pick_selected_fires_once_then_stays_quiet(sent):
    await ms.on_snapshot(_snap(), None)
    assert "pick_selected" in _states(sent)

    sent.clear()
    await ms.on_snapshot(_snap(), None)          # identical pick, next poll
    assert "pick_selected" not in _states(sent)


async def test_a_drifting_score_is_not_a_new_pick(sent):
    """Composite score moves every poll; only the structure defines the call."""
    await ms.on_snapshot(_snap(), None)
    sent.clear()
    snap = _snap()
    snap["engine_pick"]["composite_score"] = 71.4      # same legs, new score
    await ms.on_snapshot(snap, None)
    assert "pick_selected" not in _states(sent)


async def test_changed_strikes_are_a_new_pick(sent):
    await ms.on_snapshot(_snap(), None)
    sent.clear()
    snap = _snap()
    snap["engine_pick"]["strikes"] = [7690.0, 7730.0]
    await ms.on_snapshot(snap, None)
    assert "pick_selected" in _states(sent)


async def test_pick_attributes_carry_the_copy_fields(sent):
    await ms.on_snapshot(_snap(), None)
    attrs = next(c for c in sent if c["state"] == "pick_selected")["attrs"]
    assert attrs["strategy"] == "bear_call"
    assert attrs["verdict"] == "tradeable on limit"
    assert sent[0]["expiry"] == "2026-09-18"


# ── session_open ────────────────────────────────────────────────────────────

async def test_session_open_fires_once_per_day(sent):
    await ms.on_snapshot(_snap(), None)
    assert "session_open" in _states(sent)
    sent.clear()
    await ms.on_snapshot(_snap(), None)
    assert "session_open" not in _states(sent)


async def test_session_open_is_skipped_with_no_walls(sent):
    """§2: skip silently rather than post an empty frame."""
    snap = _snap(bias={"bias_label": "neutral", "directional_score": 0})
    await ms.on_snapshot(snap, None)
    assert "session_open" not in _states(sent)


# ── grid_digest ─────────────────────────────────────────────────────────────

async def test_grid_digest_respects_its_cooldown(sent):
    await ms.on_snapshot(_snap(), None)           # first ever → fires
    assert "grid_digest" in _states(sent)

    sent.clear()
    snap = _snap()                                 # a wholly different grid...
    for i in range(8):
        snap["strategies"][f"s{i}"]["best"]["health"] = "broken"
    await ms.on_snapshot(snap, None)               # ...but minutes later
    assert "grid_digest" not in _states(sent), "cooldown must hold"


async def test_grid_digest_needs_a_real_change_after_the_cooldown(sent):
    await ms.on_snapshot(_snap(), None)
    sent.clear()
    # Pretend the last digest was days ago.
    ms.state.last_digest_at["SPX"] = (
        datetime.now(timezone.utc) - timedelta(hours=ms._DIGEST_MIN_HOURS + 1)).isoformat()

    await ms.on_snapshot(_snap(), None)            # same grid → still quiet
    assert "grid_digest" not in _states(sent)

    snap = _snap()
    for i in range(ms._DIGEST_MIN_CHANGED):
        snap["strategies"][f"s{i}"]["best"]["health"] = "broken"
    await ms.on_snapshot(snap, None)               # enough cards moved → fires
    assert "grid_digest" in _states(sent)


# ── never raises ────────────────────────────────────────────────────────────

async def test_on_snapshot_swallows_a_publisher_failure(monkeypatch):
    async def _boom(*a, **k):
        raise RuntimeError("pubsub down")

    monkeypatch.setattr(ms.matrix_events, "publish_transition", _boom)
    assert await ms.on_snapshot(_snap(), None) == []      # no exception escapes


async def test_a_snapshot_without_a_symbol_is_ignored(sent):
    assert await ms.on_snapshot({}, None) == []


# ── evaluator-side: bias + win_rate + recap ────────────────────────────────

class _FakeDB:
    def __init__(self, n=20, wins=14, losses=0, neutrals=6, pct=70.0):
        self._s = {"n": n, "wins": wins, "losses": losses,
                   "neutrals": neutrals, "accuracy_pct": pct}

    def fetch_accuracy(self, sym, window):
        return dict(self._s)


class _FakePoller:
    def __init__(self, snap):
        self.latest_by_symbol = {"SPX": snap}


class _Settings:
    eval_min_graded = 10
    pill_green_pct = 60.0
    pill_red_pct = 40.0
    eval_rolling_window = 20
    regime_clear_consec_wins = 2
    regime_alert_consec_losses = 3
    auth_enabled = False


@pytest.fixture
def evaluator_state(monkeypatch):
    from app.evaluator import state as est
    est.regime_alert_active_by_symbol.clear()
    est.consec_wins_by_symbol.clear()
    est.consec_losses_by_symbol.clear()
    yield est
    est.regime_alert_active_by_symbol.clear()


async def test_bias_transition_fires_only_on_a_flip(sent, evaluator_state):
    db, poller, cfg = _FakeDB(), _FakePoller(_snap()), _Settings()

    await ms.on_evaluation(db, poller, cfg)        # first pass seeds the baseline
    sent.clear()
    await ms.on_evaluation(db, poller, cfg)        # unchanged → silent
    assert not [s for s in _states(sent) if s.startswith("bias_")]


async def test_win_rate_notable_on_recovery(sent, evaluator_state):
    """Rule 1 (§3): the regime alert clearing IS 'recovered from losses'."""
    db, poller, cfg = _FakeDB(), _FakePoller(_snap()), _Settings()
    evaluator_state.regime_alert_active_by_symbol["SPX"] = True
    await ms.on_evaluation(db, poller, cfg)        # seed: alert active
    sent.clear()

    evaluator_state.regime_alert_active_by_symbol["SPX"] = False   # recovered
    await ms.on_evaluation(db, poller, cfg)
    hit = [c for c in sent if c["state"] == "win_rate_notable"]
    assert hit and hit[0]["attrs"]["reason"] == "recovery"


async def test_win_rate_notable_on_crossing_into_green(sent, evaluator_state):
    """Rule 2 (§3): crossing pill_green_pct from below, with enough graded."""
    poller, cfg = _FakePoller(_snap()), _Settings()
    await ms.on_evaluation(_FakeDB(pct=45.0), poller, cfg)   # seed below green
    sent.clear()

    await ms.on_evaluation(_FakeDB(pct=72.0), poller, cfg)   # crosses up
    hit = [c for c in sent if c["state"] == "win_rate_notable"]
    assert hit and hit[0]["attrs"]["reason"] == "win_streak"
    assert hit[0]["attrs"]["win_rate"] == "72"


async def test_a_small_sample_cannot_trigger_a_win_streak(sent, evaluator_state):
    """Below eval_min_graded it is noise, not an achievement."""
    poller, cfg = _FakePoller(_snap()), _Settings()
    await ms.on_evaluation(_FakeDB(n=4, pct=25.0), poller, cfg)
    sent.clear()
    await ms.on_evaluation(_FakeDB(n=4, pct=100.0), poller, cfg)
    assert "win_rate_notable" not in _states(sent)


async def test_daily_recap_fires_once_for_the_finished_day(sent, evaluator_state):
    db, poller, cfg = _FakeDB(), _FakePoller(_snap()), _Settings()
    # A day's worth of picks, recorded by the poll side.
    await ms.on_snapshot(_snap(), None)
    low = _snap()
    low["engine_pick"] = dict(low["engine_pick"], strikes=[1.0, 2.0], composite_score=12.0)
    await ms.on_snapshot(low, None)
    ms.state.day_date["SPX"] = "2026-09-12"        # that day is now over
    sent.clear()

    await ms.on_evaluation(db, poller, cfg)
    hit = [c for c in sent if c["state"] == "daily_recap"]
    assert hit, "a finished day must recap"
    attrs = hit[0]["attrs"]
    assert attrs["session_date"] == "2026-09-12"
    assert attrs["best_composite_score"] == "86.1"
    assert attrs["worst_composite_score"] == "12.0"

    sent.clear()
    await ms.on_evaluation(db, poller, cfg)        # same day must not recap twice
    assert "daily_recap" not in _states(sent)


async def test_on_evaluation_swallows_a_broken_db(sent, evaluator_state):
    class _Boom:
        def fetch_accuracy(self, *a):
            raise RuntimeError("db gone")

    assert await ms.on_evaluation(_Boom(), _FakePoller(_snap()), _Settings()) == []

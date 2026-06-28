"""Bias-trust state machine: calibrating → in_sync → paused → resume.

Drives the per-symbol regime counters via the evaluator's _update_regime off
synthetic outcome streaks, then asserts the /accuracy trust state + display text.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.evaluator import _update_regime, state as evaluator_state
from app.poller import state as poller_state
from app.routes.accuracy import _trust_state

SYM = "TESTSYM"

SETTINGS = SimpleNamespace(
    regime_alert_consec_losses=3,
    regime_clear_consec_wins=2,
    eval_min_graded=10,
)


@pytest.fixture(autouse=True)
def _reset():
    for d in (evaluator_state.consec_losses_by_symbol,
              evaluator_state.consec_wins_by_symbol,
              evaluator_state.regime_alert_active_by_symbol,
              evaluator_state.regime_alert_triggered_at):
        d.pop(SYM, None)
    poller_state.latest_by_symbol.pop(SYM, None)
    yield
    poller_state.latest_by_symbol.pop(SYM, None)


def _set_bias_conf(conf: str | None):
    poller_state.latest_by_symbol[SYM] = {"bias": {"confidence": conf}}


def test_calibrating_below_min_graded():
    st = _trust_state(SYM, graded=3, pct=66.0, settings=SETTINGS)
    assert st["state"] == "calibrating"
    assert st["win_rate"] is None
    assert st["display_text"] == "Calibrating — 3/10 graded"
    assert st["show_hint"] is True


def test_in_sync_publishes_win_rate():
    st = _trust_state(SYM, graded=14, pct=64.0, settings=SETTINGS)
    assert st["state"] == "in_sync"
    assert st["win_rate"] == 64.0
    assert st["show_hint"] is False
    assert "64% win rate" in st["display_text"]


def test_low_confidence_overrides_in_sync_but_shows_rate():
    _set_bias_conf("low")
    st = _trust_state(SYM, graded=20, pct=58.0, settings=SETTINGS)
    assert st["state"] == "low_conf"
    assert st["win_rate"] == 58.0          # win-rate still shown (muted)
    assert st["show_hint"] is True
    assert st["bias_confidence"] == "low"


def test_paused_after_three_losses_then_resume_on_two_wins():
    # 3 consecutive losses → regime alert ON → paused, win-rate suppressed.
    for _ in range(3):
        _update_regime(SYM, "loss", SETTINGS)
    st = _trust_state(SYM, graded=20, pct=70.0, settings=SETTINGS)
    assert st["state"] == "paused"
    assert st["win_rate"] is None
    assert st["wins_to_resume"] == 2
    assert "2 confirming wins to resume" in st["display_text"]

    # 1 win — still paused, 1 to go (singular).
    _update_regime(SYM, "win", SETTINGS)
    st = _trust_state(SYM, graded=20, pct=70.0, settings=SETTINGS)
    assert st["state"] == "paused"
    assert st["wins_to_resume"] == 1
    assert "1 confirming win to resume" in st["display_text"]

    # 2nd win — alert clears → win-rate returns.
    _update_regime(SYM, "win", SETTINGS)
    st = _trust_state(SYM, graded=20, pct=70.0, settings=SETTINGS)
    assert st["state"] == "in_sync"
    assert st["win_rate"] == 70.0
    assert st["wins_to_resume"] == 0


def test_neutral_does_not_move_regime_counters():
    _update_regime(SYM, "loss", SETTINGS)
    _update_regime(SYM, "neutral", SETTINGS)   # noise — must not reset losses
    _update_regime(SYM, "loss", SETTINGS)
    _update_regime(SYM, "loss", SETTINGS)
    st = _trust_state(SYM, graded=20, pct=70.0, settings=SETTINGS)
    assert st["state"] == "paused"             # 3 losses reached despite neutral

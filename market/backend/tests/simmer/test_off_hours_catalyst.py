"""Off-hours catalyst exception (docs/simmer_off_hours_catalyst.md): a closed-market
ready/watch name WITH a live catalyst publishes an event flagged
`off_hours_catalyst=true`; everything else stays silent off-hours."""
from __future__ import annotations

import pytest

from app import simmer_watcher as sw
from app import simmer_config

from .conftest import EXP, readiness_env


def _env(score=85.0, *, in_window=True, go=True, held_back=False,
         vetoed=False, symbol="NVDA"):
    e = readiness_env(score=score, vetoed=vetoed)
    e["symbol"] = symbol
    e["expiration"] = EXP
    e["score"] = None if vetoed else score
    e["earnings"] = ({"in_window": True, "go": go, "held_back": held_back}
                     if in_window else None)
    return e


def test_off_hours_catalyst_flag():
    # Affirmative go read in an earnings window → yes.
    assert sw._off_hours_catalyst(_env(in_window=True, go=True)) is True
    # No earnings window → no.
    assert sw._off_hours_catalyst(_env(in_window=False)) is False
    # In window but HELD BACK (no-go) → no: the analyzer rejected it, don't post.
    assert sw._off_hours_catalyst(_env(go=False, held_back=True)) is False
    # In window but cold cache (no go read yet) → no.
    assert sw._off_hours_catalyst(_env(go=False, held_back=False)) is False
    assert sw._off_hours_catalyst({"earnings": None}) is False
    assert sw._off_hours_catalyst({}) is False


@pytest.fixture()
def captured(monkeypatch):
    calls: list[dict] = []

    async def _fake_publish(symbol, state, expiry=None, *, db=None,
                            extra_attributes=None, takeaways=None, **kw):
        calls.append({"symbol": symbol, "state": state,
                      "extra": extra_attributes or {}})
        return True

    monkeypatch.setattr(sw.simmer_events, "publish_transition", _fake_publish)
    return calls


async def test_ready_with_catalyst_publishes_flagged(captured):
    await sw.process_off_hours_catalyst_events({"NVDA|X": _env(score=85.0)}, None)
    assert len(captured) == 1
    c = captured[0]
    assert c["state"] == "ready" and c["extra"].get("off_hours_catalyst") == "true"


async def test_watch_band_emits_watch_entered(captured):
    watch = float(simmer_config.decision_bands().get("watch", 50.0))
    await sw.process_off_hours_catalyst_events({"NVDA|X": _env(score=watch + 1)}, None)
    assert captured and captured[0]["state"] == "watch_entered"


async def test_no_catalyst_is_silent(captured):
    await sw.process_off_hours_catalyst_events({"NVDA|X": _env(score=85.0, in_window=False)}, None)
    assert captured == []


async def test_held_back_no_go_is_silent(captured):
    # held_back (no-go) sits at 69.99 → clears the watch band, but the analyzer
    # rejected it: must NOT be posted as an off-hours catalyst.
    await sw.process_off_hours_catalyst_events(
        {"NVDA|X": _env(score=69.99, go=False, held_back=True)}, None)
    assert captured == []


async def test_vetoed_is_silent(captured):
    await sw.process_off_hours_catalyst_events({"NVDA|X": _env(vetoed=True)}, None)
    assert captured == []


async def test_below_watch_is_silent(captured):
    await sw.process_off_hours_catalyst_events({"NVDA|X": _env(score=10.0)}, None)
    assert captured == []


async def test_does_not_mutate_event_bands(captured):
    before = dict(sw.state.event_bands)
    await sw.process_off_hours_catalyst_events({"NVDA|X": _env(score=85.0)}, None)
    assert dict(sw.state.event_bands) == before      # off-hours must not touch bands

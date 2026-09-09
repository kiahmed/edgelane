"""Simmer → Facades Pub/Sub event publisher + the watcher's transition detector.

No network / no GCP: the pubsub client is stubbed. Covers the deterministic
event_id, the best-effort no-op when unconfigured, a real publish when enabled,
the band edge-detector, and the process_alerts wiring firing both transitions.
"""
from __future__ import annotations

from datetime import date

import pytest

from app import config
from app.config import Settings
from app import simmer_events as ev
from app import simmer_watcher as sw

from .conftest import readiness_env


# ── deterministic event_id ──────────────────────────────────────────────────
def test_event_id_is_deterministic_and_encodes_the_tuple():
    a = ev.event_id("NVDA", "ready", "2026-09-18", day=date(2026, 9, 9))
    b = ev.event_id("nvda", "ready", "2026-09-18", day=date(2026, 9, 9))
    assert a == "SMR-NVDA-260909-260918-ready"
    assert a == b                                   # case-insensitive symbol, same id


def test_event_id_varies_by_state_expiry_and_day():
    base = dict(symbol="NVDA", expiry="2026-09-18", day=date(2026, 9, 9))
    rid = ev.event_id(state="ready", **base)
    assert ev.event_id(state="watch_entered", **base) != rid          # state
    assert ev.event_id("NVDA", "ready", "2026-10-16", day=base["day"]) != rid  # expiry
    assert ev.event_id("NVDA", "ready", "2026-09-18", day=date(2026, 9, 10)) != rid  # day


def test_event_id_tolerates_missing_expiry():
    assert ev.event_id("NVDA", "watch_entered", None, day=date(2026, 9, 9)) \
        == "SMR-NVDA-260909--watch_entered"


# ── publisher: best-effort no-op vs real publish ────────────────────────────
class _FakeFuture:
    def result(self, timeout=None):
        return "message-id"


class _FakePublisher:
    def __init__(self):
        self.published: list[dict] = []

    def topic_path(self, project, topic):
        return f"projects/{project}/topics/{topic}"

    def publish(self, topic_path, data, **attrs):
        self.published.append({"topic": topic_path, "data": data, "attrs": attrs})
        return _FakeFuture()


async def test_publish_noop_when_disabled(monkeypatch):
    monkeypatch.setattr(config, "_cached", Settings())          # events disabled (default)

    def _boom():
        raise AssertionError("publisher must not be built when disabled")

    monkeypatch.setattr(ev, "_get_publisher", _boom)
    assert await ev.publish_transition("NVDA", "ready", "2026-09-18") is False


async def test_publish_noop_when_enabled_but_no_project(monkeypatch):
    monkeypatch.setattr(config, "_cached",
                        Settings(simmer_events_enabled=True, gcp_project=""))
    monkeypatch.setattr(ev, "_get_publisher",
                        lambda: (_ for _ in ()).throw(AssertionError("no client")))
    assert await ev.publish_transition("NVDA", "ready", "2026-09-18") is False


async def test_publish_fires_when_configured(monkeypatch):
    monkeypatch.setattr(config, "_cached", Settings(
        simmer_events_enabled=True, gcp_project="proj",
        facades_events_topic="facades.ticker-events"))
    fake = _FakePublisher()
    monkeypatch.setattr(ev, "_get_publisher", lambda: fake)

    ok = await ev.publish_transition("NVDA", "watch_entered", "2026-09-18",
                                     day=date(2026, 9, 9))
    assert ok is True
    assert len(fake.published) == 1
    msg = fake.published[0]
    assert msg["topic"] == "projects/proj/topics/facades.ticker-events"
    assert msg["attrs"] == {
        "product": "simmer", "symbol": "NVDA", "state": "watch_entered",
        "expiry": "2026-09-18", "event_id": "SMR-NVDA-260909-260918-watch_entered"}


async def test_publish_swallows_client_errors(monkeypatch):
    monkeypatch.setattr(config, "_cached", Settings(
        simmer_events_enabled=True, gcp_project="proj"))

    class _Broken(_FakePublisher):
        def publish(self, *a, **k):
            raise RuntimeError("pubsub down")

    monkeypatch.setattr(ev, "_get_publisher", lambda: _Broken())
    assert await ev.publish_transition("NVDA", "ready", "2026-09-18") is False


# ── band edge-detector ──────────────────────────────────────────────────────
WATCH, READY = 50.0, 70.0


def _env(score, vetoed=False):
    return readiness_env(score=score, vetoed=vetoed)


def test_transition_cold_to_watch_then_ready():
    assert sw.event_transitions("K", _env(55), WATCH, READY) == ["watch_entered"]
    assert sw.event_transitions("K", _env(75), WATCH, READY) == ["ready"]
    assert sw.event_transitions("K", _env(80), WATCH, READY) == []      # already ready


def test_transition_cold_to_ready_emits_both_boundaries():
    assert sw.event_transitions("J", _env(85), WATCH, READY) == \
        ["watch_entered", "ready"]


def test_transition_downward_and_veto_are_silent():
    sw.event_transitions("D", _env(85), WATCH, READY)                   # → ready
    assert sw.event_transitions("D", _env(40), WATCH, READY) == []      # drop, silent
    assert sw.state.event_bands["D"] == "cold"
    assert sw.event_transitions("D", _env(90, vetoed=True), WATCH, READY) == []
    assert sw.state.event_bands["D"] == "cold"


# ── process_alerts wiring: both transitions published ───────────────────────
async def test_process_alerts_publishes_both_transitions(monkeypatch):
    published: list[tuple] = []

    async def _rec(symbol, state, expiry=None, **k):
        published.append((str(symbol).upper(), state, expiry))
        return True

    async def _no_fanout(env, regime):     # isolate the event path from Supabase
        return 0

    monkeypatch.setattr(sw.simmer_events, "publish_transition", _rec)
    monkeypatch.setattr(sw, "fanout_alert", _no_fanout)

    env = readiness_env(symbol="NVDA", score=85)     # cold → ready in one sweep
    await sw.process_alerts({"NVDA|" + env["expiration"]: env}, {"state": "contango"})

    assert ("NVDA", "watch_entered", env["expiration"]) in published
    assert ("NVDA", "ready", env["expiration"]) in published

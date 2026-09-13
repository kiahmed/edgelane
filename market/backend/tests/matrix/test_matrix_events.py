"""Matrix Pub/Sub publisher — deterministic id, best-effort no-op, real publish
path (stubbed client). Mirrors the Simmer events test."""
from __future__ import annotations

from datetime import date

import pytest

from app import matrix_events as me
from app import config
from app.config import Settings


@pytest.fixture(autouse=True)
def _reset_cfg():
    yield
    config._cached = None
    me._publisher = None


def _cfg(**kw):
    base = dict(matrix_events_enabled=True, matrix_events_topic="facades.matrix-events",
                gcp_project="proj")
    base.update(kw)
    config._cached = Settings(**base)


def test_event_id_deterministic():
    d = date(2026, 9, 13)
    a = me.event_id("nvda", "pick_selected", d)
    assert a == "MTX-NVDA-260913-pick_selected"
    assert a == me.event_id("NVDA", "pick_selected", d)          # same inputs → same id
    assert a != me.event_id("NVDA", "bias_diverged", d)          # state differs


async def test_disabled_is_noop():
    _cfg(matrix_events_enabled=False)
    assert await me.publish_transition("NVDA", "pick_selected", "2026-10-17") is False


async def test_unconfigured_is_noop():
    _cfg(matrix_events_enabled=True, gcp_project="")
    assert await me.publish_transition("NVDA", "pick_selected") is False


async def test_publish_calls_client_with_attributes(monkeypatch):
    _cfg()
    sent = {}

    class _Future:
        def result(self, timeout=None):
            return "mid-1"

    class _Pub:
        def topic_path(self, project, topic):
            return f"projects/{project}/topics/{topic}"

        def publish(self, path, body, **attrs):
            sent["path"] = path
            sent["attrs"] = attrs
            return _Future()

    monkeypatch.setattr(me, "_get_publisher", lambda: _Pub())
    ok = await me.publish_transition("nvda", "session_open", "2026-10-17")
    assert ok is True
    assert sent["path"] == "projects/proj/topics/facades.matrix-events"
    assert sent["attrs"]["product"] == "matrix"
    assert sent["attrs"]["symbol"] == "NVDA"
    assert sent["attrs"]["state"] == "session_open"
    assert sent["attrs"]["expiry"] == "2026-10-17"
    assert sent["attrs"]["event_id"].startswith("MTX-NVDA-")


async def test_publish_swallows_client_error(monkeypatch):
    _cfg()

    def _boom():
        raise RuntimeError("pubsub down")

    monkeypatch.setattr(me, "_get_publisher", _boom)
    assert await me.publish_transition("NVDA", "pick_selected") is False  # never raises

"""Centralized topic dedupe: the simmer_published_events ledger + the
exactly-once behavior of simmer_events.publish_transition. Pub/Sub client is
stubbed — no network."""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from app import config
from app.config import Settings
from app import simmer_events as ev


DAY = date(2026, 9, 9)


class _FakeFuture:
    def result(self, timeout=None):
        return "mid"


class _FakePublisher:
    def __init__(self):
        self.published: list[dict] = []

    def topic_path(self, project, topic):
        return f"projects/{project}/topics/{topic}"

    def publish(self, topic_path, data, **attrs):
        self.published.append(attrs)
        return _FakeFuture()


@pytest.fixture
def events_on(monkeypatch):
    monkeypatch.setattr(config, "_cached",
                        Settings(simmer_events_enabled=True, gcp_project="proj"))
    fake = _FakePublisher()
    monkeypatch.setattr(ev, "_get_publisher", lambda: fake)
    return fake


# ── table ───────────────────────────────────────────────────────────────────
def test_table_insert_and_exists(fresh_db):
    eid = "SMR-NVDA-260909-260918-ready"
    assert fresh_db.simmer_published_event_exists(eid) is False
    fresh_db.insert_simmer_published_event({
        "event_id": eid, "symbol": "NVDA", "expiration": "2026-09-18",
        "state": "ready", "fired_at": datetime.now(timezone.utc).replace(tzinfo=None),
        "takeaways": {"symbol": "NVDA", "score": 78},
    })
    assert fresh_db.simmer_published_event_exists(eid) is True


def test_insert_is_idempotent_on_event_id(fresh_db):
    eid = "SMR-NVDA-260909-260918-ready"
    for score in (70, 80):
        fresh_db.insert_simmer_published_event({
            "event_id": eid, "symbol": "NVDA", "expiration": "2026-09-18",
            "state": "ready", "fired_at": None, "takeaways": {"score": score}})
    conn = fresh_db.connect()
    n = conn.execute("SELECT count(*) FROM simmer_published_events WHERE event_id=?",
                     [eid]).fetchone()[0]
    assert n == 1                                   # replace, not duplicate


# ── publish_transition dedupe ────────────────────────────────────────────────
async def test_publish_records_then_dedupes(events_on, fresh_db):
    fake = events_on
    eid = ev.event_id("NVDA", "ready", "2026-09-18", DAY)

    first = await ev.publish_transition("NVDA", "ready", "2026-09-18",
                                        day=DAY, db=fresh_db)
    assert first is True
    assert len(fake.published) == 1
    assert fresh_db.simmer_published_event_exists(eid) is True

    second = await ev.publish_transition("NVDA", "ready", "2026-09-18",
                                         day=DAY, db=fresh_db)
    assert second is False                          # deduped
    assert len(fake.published) == 1                 # topic NOT hit again


async def test_force_bypasses_dedupe(events_on, fresh_db):
    fake = events_on
    await ev.publish_transition("NVDA", "ready", "2026-09-18", day=DAY, db=fresh_db)
    forced = await ev.publish_transition("NVDA", "ready", "2026-09-18",
                                         day=DAY, db=fresh_db, force=True,
                                         extra_attributes={"test": "true"})
    assert forced is True
    assert len(fake.published) == 2                 # forced re-publish reached the topic
    assert fake.published[-1]["test"] == "true"     # extra attribute carried


async def test_no_db_means_no_dedupe(events_on):
    fake = events_on
    await ev.publish_transition("NVDA", "ready", "2026-09-18", day=DAY, db=None)
    await ev.publish_transition("NVDA", "ready", "2026-09-18", day=DAY, db=None)
    assert len(fake.published) == 2                 # without a ledger, both publish


async def test_email_fanout_not_gated_by_topic_dedupe(events_on, fresh_db):
    """The ledger gates only the TOPIC — distinct states of the same symbol/day
    each publish (watch_entered then ready), never collapsed to one."""
    fake = events_on
    await ev.publish_transition("NVDA", "watch_entered", "2026-09-18", day=DAY, db=fresh_db)
    await ev.publish_transition("NVDA", "ready", "2026-09-18", day=DAY, db=fresh_db)
    states = [p["state"] for p in fake.published]
    assert states == ["watch_entered", "ready"]

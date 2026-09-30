"""The Simmer engine → poster hand-off: every published event has a ledger row,
frozen at event time and written BEFORE the Pub/Sub message — and, unlike Matrix,
the message goes out ONLY when the insert created a NEW row (exactly-once §3)."""
from __future__ import annotations

import pytest

from app import simmer_ledger

from .conftest import EXP, readiness_env


def _env(score=85.0, symbol="NVDA"):
    e = readiness_env(symbol=symbol, score=score)
    e["expiration"] = EXP
    return e


def test_build_row_freezes_the_blocks_and_the_card():
    env = _env(score=88.0)
    row = simmer_ledger.build_row(
        event_id="SMR-NVDA-260101-260130-ready", symbol="nvda", state="ready",
        expiry=EXP, attrs={"product": "simmer", "off_hours_catalyst": "true"}, env=env)
    assert row["symbol"] == "NVDA" and row["product"] == "simmer"
    assert row["status"] == "pending" and row["state"] == "ready"
    # every state block the poster composes from is present…
    assert set(row["data"]) == {"card", "score", "gates", "sentiment", "evolution"}
    assert row["data"]["card"]["symbol"] == "NVDA"
    # …the card HTML is frozen standalone…
    assert row["snap_html"] and "data-snap" in row["snap_html"]
    # …and the attributes (incl. off_hours_catalyst) are carried verbatim.
    assert row["attributes"]["off_hours_catalyst"] == "true"


@pytest.fixture
def wired(monkeypatch):
    """Enable events and stub the two network layers the background task hits:
    record() (the ledger insert) and publish_transition() (the Pub/Sub send)."""
    rows: list[dict] = []
    pubs: list[dict] = []
    created = {"value": True}          # what record() reports back

    async def _record(row):
        rows.append(row)
        return created["value"]

    async def _publish(symbol, state, expiry=None, *, force=False,
                       extra_attributes=None, **k):
        pubs.append({"state": state, "force": force,
                     "attrs": dict(extra_attributes or {}),
                     "rows_at_publish": len(rows)})
        return True

    monkeypatch.setattr(simmer_ledger.simmer_events, "is_enabled", lambda: True)
    monkeypatch.setattr(simmer_ledger, "record", _record)
    monkeypatch.setattr(simmer_ledger.simmer_events, "publish_transition", _publish)
    return rows, pubs, created


async def test_row_is_written_before_the_message_and_only_on_a_new_row(wired):
    rows, pubs, _ = wired
    simmer_ledger.fire("NVDA", "ready", EXP, env=_env(),
                       extra_attributes={"off_hours_catalyst": "true"})
    await simmer_ledger.drain()
    assert len(rows) == 1 and len(pubs) == 1
    assert pubs[0]["rows_at_publish"] == 1               # row existed before publish
    assert pubs[0]["force"] is True                      # ledger already deduped
    assert pubs[0]["attrs"]["ledger"] == "1" and "event_at" in pubs[0]["attrs"]
    assert pubs[0]["attrs"]["off_hours_catalyst"] == "true"


async def test_a_duplicate_row_does_not_publish(wired):
    """Exactly-once: a re-seen transition (record reports NOT created) is silent."""
    rows, pubs, created = wired
    created["value"] = False
    simmer_ledger.fire("NVDA", "ready", EXP, env=_env())
    await simmer_ledger.drain()
    assert len(rows) == 1 and pubs == []


async def test_a_write_error_does_not_publish(wired):
    """No new row (None = write failed) means no message — a later sweep re-fires."""
    rows, pubs, created = wired
    created["value"] = None
    simmer_ledger.fire("NVDA", "ready", EXP, env=_env())
    await simmer_ledger.drain()
    assert pubs == []


async def test_force_publishes_even_on_a_duplicate(wired):
    """The admin fire tool bypasses the new-row check."""
    rows, pubs, created = wired
    created["value"] = False
    simmer_ledger.fire("NVDA", "ready", EXP, env=_env(), force=True)
    await simmer_ledger.drain()
    assert len(pubs) == 1 and pubs[0]["attrs"]["ledger"] == "1"


async def test_disabled_events_write_nothing(wired, monkeypatch):
    rows, pubs, _ = wired
    monkeypatch.setattr(simmer_ledger.simmer_events, "is_enabled", lambda: False)
    simmer_ledger.fire("NVDA", "ready", EXP, env=_env())
    await simmer_ledger.drain()
    assert rows == [] and pubs == []


async def test_the_row_is_frozen_at_event_time(wired):
    """A later mutation of the envelope must not change what the row describes."""
    rows, _, _ = wired
    env = _env(score=88.0)
    simmer_ledger.fire("NVDA", "ready", EXP, env=env)
    env["score"] = 12.34
    env["decision"] = "vetoed"
    await simmer_ledger.drain()
    assert rows[0]["data"]["card"]["score"] == 88.0
    assert rows[0]["data"]["card"]["decision"] != "vetoed"


async def test_record_reports_the_new_row_distinction(monkeypatch):
    """record() forwards insert_row_report_created's created/duplicate/error."""
    seen = {}

    async def _insert(table, row, on_conflict):
        seen.update(table=table, on_conflict=on_conflict)
        return True

    monkeypatch.setattr(simmer_ledger.supabase_admin, "insert_row_report_created", _insert)
    assert await simmer_ledger.record({"event_id": "e"}) is True
    assert seen == {"table": "simmer_event_ledger", "on_conflict": "event_id"}

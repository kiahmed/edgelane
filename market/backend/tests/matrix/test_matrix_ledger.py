"""The engine → poster hand-off: every published event has a ledger row, frozen
at event time, written BEFORE the Pub/Sub message goes out."""
from __future__ import annotations

import pytest

from app import matrix_ledger, matrix_signals as ms


def _snap():
    return {
        "symbol": "SPX", "expiration": "2026-09-30", "spot": 7790.0, "expected_move": 40.0,
        "bias": {"bias_label": "bearish", "directional_score": -45.0, "confidence": "medium",
                 "call_wall_strike": 7800.0, "put_wall_strike": 7700.0},
        "engine_pick": {"strategy": "bear_call", "label": "Aggressive",
                        "legs": [{"strike": 7780.0}], "composite_score": 82.0,
                        "structure_text": "Short 7780C / Long 7800C", "health": "healthy",
                        "net_premium": 3.1, "composite_verdict": {"label": "tradeable on limit"}},
        "strategies": {},
        "_quote_by_symbol": {"huge": "not needed"},
    }


@pytest.fixture
def wired(monkeypatch):
    rows, pubs = [], []

    async def _record(row):
        rows.append(row)
        return True

    async def _publish(symbol, state, expiry=None, *, day=None, discriminator=None,
                       extra_attributes=None):
        pubs.append({"state": state, "attrs": dict(extra_attributes or {}),
                     "rows_at_publish": len(rows)})
        return True

    monkeypatch.setattr(ms.matrix_events, "is_enabled", lambda: True)
    monkeypatch.setattr(ms.matrix_ledger, "record", _record)
    monkeypatch.setattr(ms.matrix_events, "publish_transition", _publish)
    # no DB in tests: the record read comes back empty
    monkeypatch.setattr("app.routes.matrix._accuracy_view", lambda sym: ({}, {}))
    return rows, pubs


def test_build_row_freezes_data_and_the_card():
    row = matrix_ledger.build_row(event_id="MTX-SPX-260930-pick_selected-x", symbol="spx",
                                  state="pick_selected", expiry="2026-09-30",
                                  attrs={"strategy": "bear_call"}, snap=_snap())
    assert row["symbol"] == "SPX" and row["snap_view"] == "engine_pick"
    assert 'data-snap="engine_pick"' in row["snap_html"] and "82.0" in row["snap_html"]
    assert row["data"]["pick"]["pick"]["strategy"] == "bear_call"
    assert row["data"]["walls"]["key_levels"]["call_wall"] == 7800.0


def test_a_past_pick_is_carded_as_itself_not_as_todays_pick():
    old = dict(_snap()["engine_pick"], strategy="bear_put", structure_text="PAST PICK",
               composite_score=71.0)
    row = matrix_ledger.build_row(event_id="e", symbol="SPX", state="pick_result",
                                  expiry="", attrs={}, snap=_snap(), card_pick=old)
    assert "PAST PICK" in row["snap_html"] and "Short 7780C" not in row["snap_html"]
    assert row["data"]["card_pick"]["strategy"] == "bear_put"


async def test_the_row_is_written_before_the_message(wired):
    rows, pubs = wired
    ms._fire("SPX", "session_open", "2026-09-30", {"x": "1"}, snap=_snap())
    await ms.drain()
    assert len(rows) == 1 and pubs[0]["rows_at_publish"] == 1
    assert pubs[0]["attrs"]["ledger"] == "1" and "event_at" in pubs[0]["attrs"]
    assert rows[0]["snap_view"] == "walls_chip"


async def test_the_row_is_frozen_at_event_time(wired):
    """A later poll must not change what the row describes."""
    rows, _ = wired
    snap = _snap()
    ms._fire("SPX", "pick_selected", "2026-09-30", {}, snap=snap)
    snap["engine_pick"]["structure_text"] = "CHANGED BY A LATER POLL"
    await ms.drain()
    assert "CHANGED BY A LATER POLL" not in rows[0]["snap_html"]


async def test_no_ledger_row_means_no_message(wired, monkeypatch):
    rows, pubs = wired

    async def _fail(row):
        return False
    monkeypatch.setattr(ms.matrix_ledger, "record", _fail)
    ms._fire("SPX", "grid_digest", "", {}, snap=_snap())
    await ms.drain()
    assert pubs == [], "a poster woken for a row that isn't there only logs an error"


async def test_the_frozen_snapshot_drops_what_a_post_cannot_use():
    assert "_quote_by_symbol" not in matrix_ledger.freeze_snapshot(_snap())

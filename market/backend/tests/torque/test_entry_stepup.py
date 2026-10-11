"""News-signal entry step-up + bracket-aware entry state.

The entry goes in at mid; every NEWS_SIGNAL_ENTRY_STEP_SEC an unfilled entry
is cancelled (confirmed) and re-placed one price step higher, never above the
ask, up to NEWS_SIGNAL_ENTRY_STEPS times, then cancelled. Tradier reports a
bracket's entry fill on its first leg — the parent can read "open" after the
entry filled — so every decision reads the entry leg.
"""
from __future__ import annotations

import pytest

from app import torque_config as tcfg
from app.routes import torque as troute

from .conftest import FakeTradier


def _bracket(entry_status="pending", entry_exec=0.0, exits_exec=(0.0, 0.0), parent="pending"):
    return {"class": "otoco", "status": parent,
            "leg": [{"status": entry_status, "exec_quantity": entry_exec},
                    {"status": "pending", "exec_quantity": exits_exec[0]},
                    {"status": "pending", "exec_quantity": exits_exec[1]}]}


class BracketBook(FakeTradier):
    """Brackets keyed by order id; `fill_on_read[oid]` fills that order's entry
    on its Nth read; `fill_during_cancel` fills it as the cancel lands."""
    def __init__(self):
        super().__init__()
        self.book, self.reads, self.fill_on_read, self.fill_during_cancel = {}, {}, {}, set()
        self.cancels = []

    def add(self, oid):
        self.book[str(oid)] = _bracket()

    async def get_order(self, account_id, order_id):
        k = str(order_id)
        self.reads[k] = self.reads.get(k, 0) + 1
        if self.fill_on_read.get(k) == self.reads[k]:
            self.book[k] = _bracket("filled", 1.0, parent="open")
        return dict(self.book[k], id=order_id)

    async def cancel_order(self, account_id, order_id):
        k = str(order_id)
        self.cancels.append(k)
        if k in self.fill_during_cancel:
            self.book[k] = _bracket("filled", 1.0, parent="canceled")    # exits cancelled with it
            raise RuntimeError("entry already filled")
        if self.book[k]["leg"][0]["status"] == "filled":
            raise RuntimeError("already filled")
        self.book[k] = _bracket("canceled", 0.0, parent="canceled")
        return {"order": {"id": order_id, "status": "ok"}}


@pytest.fixture
def world(monkeypatch):
    s = troute.get_settings().model_copy(update={"news_signal_entry_steps": 5,
                                                 "news_signal_entry_step_sec": 0.0})
    monkeypatch.setattr(troute, "get_settings", lambda: s)
    book = BracketBook()
    book.add(100)
    w = type("W", (), {})()
    w.book, w.placed, w.status, w.cancelled_db = book, [], {}, False

    async def _resolve(uid):
        return "tradier", book, "ACC"
    monkeypatch.setattr(troute, "resolve_client_for_uid", _resolve)

    async def _place(uid, *, limit_price, **kw):
        oid = str(101 + len(w.placed))
        w.placed.append(limit_price)
        book.add(oid)
        return {"uid": uid, "accepted": True, "entry_order_id": oid}
    monkeypatch.setattr(troute, "_place_news_signal_for_uid", _place)

    async def _status(oid, status, reason=None):
        w.status[str(oid)] = (status, reason)
        return True
    monkeypatch.setattr(troute.supabase_admin, "update_torque_order_status", _status)

    async def _select(table, **kw):
        return [{"source_event_id": "x"}] if w.cancelled_db else []
    monkeypatch.setattr(troute.supabase_admin, "select_many", _select)
    return w


async def _run(w, *, limit=0.48, ask=0.76, ticker="XLE"):
    await troute._chase_entry_fill(
        "u1", "100", limit_price=limit, ask=ask, tick=tcfg.structure_tick(ticker, single=True),
        legs=[{"symbol": "X"}], expiration="2030-01-04", attempt=1,
        common={"source_event_id": "evt-1"})


async def test_unfilled_entry_steps_up_five_times_then_cancels(world):
    await _run(world)
    assert world.placed == [0.49, 0.50, 0.51, 0.52, 0.53]
    for oid, px in zip(["100", "101", "102", "103", "104"], world.placed):
        assert world.status[oid] == ("canceled", f"repriced: step {world.placed.index(px) + 1} → {px}")
    assert world.status["105"][0] == "canceled" and "no fill after 5 price steps" in world.status["105"][1]


async def test_stops_as_soon_as_the_entry_fills(world):
    world.book.fill_on_read["102"] = 1          # the 2nd re-placed order fills on its first check
    await _run(world)
    assert world.placed == [0.49, 0.50]
    assert "102" not in world.book.cancels and "102" not in world.status


async def test_never_steps_above_the_ask(world):
    await _run(world, limit=0.48, ask=0.50)
    assert world.placed == [0.49, 0.50]         # then waits out the remaining steps at the ask
    assert "no fill after 5 price steps" in world.status["102"][1]


async def test_penny_pilot_steps_switch_to_nickels_at_three_dollars(world):
    await _run(world, limit=2.98, ask=9.0)
    assert world.placed == [2.99, 3.00, 3.05, 3.10, 3.15]


async def test_a_cancel_signal_stops_the_step_up(world):
    world.book.fill_on_read = {}
    troute._CANCELLED_SIGNALS.add("evt-1")
    await _run(world)
    assert world.placed == [] and world.book.cancels == []    # the cancel handler owns it


async def test_a_cancel_seen_in_the_signal_log_stops_it_on_another_container(world):
    world.cancelled_db = True
    await _run(world)
    assert world.placed == []


async def test_fill_during_the_reprice_cancel_is_flagged_not_replaced(world):
    world.book.fill_during_cancel.add("100")
    await _run(world)
    assert world.placed == []                    # never re-placed on top of a fill
    assert world.status["100"][0] == "needs_attention"


def test_spawn_is_off_when_steps_is_zero(monkeypatch):
    from .conftest import REAL_SPAWN_ENTRY_CHASE
    started = []
    monkeypatch.setattr(troute.asyncio, "create_task", lambda coro, **k: (started.append(1), coro.close()))
    for steps, expect in ((0, 0), (5, 1)):
        s = troute.get_settings().model_copy(update={"news_signal_entry_steps": steps})
        monkeypatch.setattr(troute, "get_settings", lambda s=s: s)
        started.clear()
        try:
            REAL_SPAWN_ENTRY_CHASE("u1", "100", {"limit_price": 1.0, "ask": 1.1, "tick": 0.01, "legs": [],
                                                 "expiration": None, "attempt": 1}, {"source_event_id": "e"})
        except AttributeError:
            pass                                  # the stubbed create_task returns a tuple, not a Task
        assert len(started) == expect, steps


# ── bracket-aware entry state ────────────────────────────────────────────────
def test_entry_state_reads_the_entry_leg_of_a_bracket():
    assert troute._entry_state(_bracket("filled", 2.0, parent="open")) == ("filled", 2.0, 2.0)
    assert troute._entry_state(_bracket("filled", 2.0, (1.0, 0.0), parent="open")) == ("filled", 2.0, 1.0)
    assert troute._entry_state(_bracket("pending")) == ("pending", 0.0, 0.0)
    assert troute._entry_state({"class": "option", "status": "filled", "exec_quantity": 1}) == ("filled", 1.0, 1.0)


@pytest.mark.parametrize("order,expect", [
    (_bracket("filled", 2.0, parent="open"), ("close_market", 2.0)),     # was mis-read as unfilled
    (_bracket("filled", 2.0, (2.0, 0.0), parent="filled"), ("already_closed", 0.0)),
    (_bracket("filled", 2.0, (1.0, 0.0), parent="open"), ("close_market", 1.0)),
    (_bracket("pending"), ("canceled", 0.0)),
])
async def test_cancel_signal_closes_only_what_a_bracket_still_holds(order, expect):
    book = BracketBook()
    book.book["7"] = order
    out = await troute._cancel_or_close_entry(book, "ACC", "7", 2)
    assert out == expect

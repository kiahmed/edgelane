"""Torque watcher restart recovery (Supabase torque_watchers).

See docs/torque.md "Multi-user order persistence". Covers the safety-critical
behavior: never blindly resubmitting a stop-exit order that might already be
resting on the broker's book after a restart — ground truth is always
resolved first, exactly like an in-process stall.
"""
from __future__ import annotations

import asyncio

import pytest

from app import broker_resolver
from app.routes import torque as troute

from .conftest import FakeTradier


@pytest.fixture(autouse=True)
def _fast_stop(monkeypatch):
    monkeypatch.setattr(troute, "_STOP_INTERVAL", 0.01)


async def test_resolve_client_for_uid_returns_none_with_no_broker_config(monkeypatch):
    async def _no_cfg(uid):
        return None
    monkeypatch.setattr(broker_resolver.supabase_admin, "get_broker_config", _no_cfg)
    assert await broker_resolver.resolve_client_for_uid("u1") is None


async def test_resolve_client_for_uid_builds_a_tradier_client(monkeypatch):
    async def _cfg(uid):
        return {"broker": "tradier", "tradier_token": "tok", "tradier_env": "sandbox",
               "tradier_account_id": "ACC1"}
    monkeypatch.setattr(broker_resolver.supabase_admin, "get_broker_config", _cfg)
    result = await broker_resolver.resolve_client_for_uid("u1")
    assert result is not None
    broker, client, account = result
    assert broker == "tradier" and account == "ACC1"
    assert client.base_url == "https://sandbox.tradier.com"


async def test_resume_skips_and_flags_a_watcher_with_no_usable_broker(monkeypatch):
    async def _rows():
        return [{"entry_order_id": "o1", "uid": "u1", "symbol": "NDX", "strategy": "long_call",
                "state": "stop_placed", "done": False}]
    monkeypatch.setattr(troute.supabase_admin, "get_active_torque_watchers", _rows)

    async def _no_client(uid):
        return None
    monkeypatch.setattr(troute, "resolve_client_for_uid", _no_client)

    flagged = []
    async def _upsert(row):
        flagged.append(row)
        return True
    monkeypatch.setattr(troute.supabase_admin, "upsert_torque_watcher", _upsert)

    n = await troute.resume_active_torque_watchers()
    await asyncio.sleep(0)   # let the fire-and-forget checkpoint task run
    assert n == 0
    assert "o1" not in troute._WATCHERS
    assert flagged and flagged[0]["state"] == "stop_needs_attention" and flagged[0]["done"] is True


async def test_resume_watching_fill_state_repolls_from_scratch(monkeypatch):
    """No order beyond the entry was ever placed — safe to resume via
    _watch_and_close exactly like a fresh watcher."""
    client = FakeTradier(order_status={"status": "filled", "avg_fill_price": 1.0})
    w = {
        "entry_order_id": "o1", "uid": "u1", "account_id": "ACC1", "symbol": "NDX",
        "strategy": "long_call", "legs": [{"symbol": "X1", "action": "buy_to_open", "quantity": 1}],
        "is_single": True, "entry_type": "debit", "quantity": 1, "tick": 0.05,
        "auto_close": False, "close_target_pct": 30.0, "stop_loss_pct": None,
        "floor_pct": 1.0, "state": "watching_fill", "done": False,
    }
    troute._WATCHERS.pop("o1", None)
    await troute._resume_watcher_from_row(client, "ACC1", w)
    assert w["state"] == "entry_not_filled" or w["entry_status"] == "filled"
    assert w["done"] is True   # auto_close=False, no stop -> nothing left after fill confirmed


async def test_resume_with_resting_stop_order_resolves_ground_truth_first(monkeypatch):
    """A stop_order_id was already checkpointed as resting — resuming must
    NOT submit a brand-new ladder from scratch (that would duplicate it). It
    must resolve the existing order's real status first."""
    # The persisted stop order is confirmed FILLED on resume — nothing further
    # should ever be submitted.
    client = FakeTradier(order_status={"status": "filled"})
    w = {
        "entry_order_id": "o1", "uid": "u1", "account_id": "ACC1", "symbol": "NDX",
        "strategy": "long_call", "legs": [{"symbol": "X1", "action": "buy_to_open", "quantity": 1}],
        "is_single": True, "entry_type": "debit", "quantity": 1, "tick": 0.05,
        "state": "stop_placed", "stop_order_id": "999", "done": False,
    }
    await troute._resume_watcher_from_row(client, "ACC1", w)
    assert w["state"] == "stop_filled"
    assert client.placed == []   # confirmed already filled -> nothing new submitted


async def test_resume_watching_fill_with_an_existing_resting_close_adopts_it_not_duplicated(monkeypatch):
    """Cross-session review repro: the entry is filled and a torqueClose-
    tagged order is ALREADY resting at the broker (the real _watch_and_close
    ran and submitted it), but its checkpoint write never landed — the row
    is stuck at watching_fill. Resuming from that stale row must reconcile
    with the broker's own order list and adopt the existing close, never
    submit a second one (which, if both filled, over-closes into naked)."""
    existing_close = {"id": "close-1", "tag": troute._close_tag("torqueClose", "o1", "long_call"),
                      "option_symbol": "X1", "status": "open", "quantity": 1}
    client = FakeTradier(order_status={"status": "filled", "avg_fill_price": 1.0},
                         orders=[existing_close])
    w = {
        "entry_order_id": "o1", "uid": "u1", "account_id": "ACC1", "symbol": "NDX",
        "strategy": "long_call", "legs": [{"symbol": "X1", "action": "buy_to_open", "quantity": 1}],
        "is_single": True, "entry_type": "debit", "quantity": 1, "tick": 0.05,
        "auto_close": True, "close_target_pct": 30.0, "stop_loss_pct": None,
        "floor_pct": 1.0, "state": "watching_fill", "done": False,
    }
    troute._WATCHERS.pop("o1", None)
    await troute._resume_watcher_from_row(client, "ACC1", w)
    assert w["close_order_id"] == "close-1"
    assert w["state"] == "close_placed"
    real_orders = [p for p in client.placed if "_modify" not in p]
    assert real_orders == []   # adopted the existing resting close — placed nothing new


async def test_resume_watching_fill_with_nothing_resting_places_exactly_one_close(monkeypatch):
    """The mirror image: reconciliation finds NOTHING on the broker's order
    list, so the persisted watching_fill row genuinely reflects reality and
    _watch_and_close-from-scratch is safe — exactly one close, not zero, not
    two."""
    async def _fake_price(client, symbol, legs):
        return {"complete": True, "net_bid": 0.60, "net_ask": 0.64, "abs_mid": 0.62}
    monkeypatch.setattr(troute, "_package_price", _fake_price)

    client = FakeTradier(order_status={"status": "filled", "avg_fill_price": 1.0},
                         place_responses=[{"order": {"id": "close-new", "status": "ok"}}])
    w = {
        "entry_order_id": "o1", "uid": "u1", "account_id": "ACC1", "symbol": "NDX",
        "strategy": "long_call", "legs": [{"symbol": "X1", "action": "buy_to_open", "quantity": 1}],
        "is_single": True, "entry_type": "debit", "quantity": 1, "tick": 0.05,
        "auto_close": True, "close_target_pct": 30.0, "stop_loss_pct": None,
        "floor_pct": 1.0, "state": "watching_fill", "done": False,
    }
    await troute._resume_watcher_from_row(client, "ACC1", w)
    real_orders = [p for p in client.placed if "_modify" not in p]
    assert len(real_orders) == 1
    assert w["close_order_id"] == "close-new"


async def test_resume_close_placed_with_an_existing_resting_stop_adopts_it_not_duplicated(monkeypatch):
    """Same lost-checkpoint gap, stop side: the ladder fired and submitted a
    stop-exit, but its checkpoint never landed, so the row is stuck at
    close_placed with a stop_loss_pct still armed. Resuming must reconcile,
    find the resting stop, and ground-truth it (_resolve_stalled_stop) —
    never submit a second stop exit on top of it."""
    existing_stop = {"id": "stop-1", "tag": troute._close_tag("torqueStop", "o1", "long_call"),
                     "option_symbol": "X1", "status": "open", "quantity": 1}
    client = FakeTradier(order_status={"status": "filled"},   # confirmed filled once resume resolves it
                         orders=[existing_stop])
    w = {
        "entry_order_id": "o1", "uid": "u1", "account_id": "ACC1", "symbol": "NDX",
        "strategy": "long_call", "legs": [{"symbol": "X1", "action": "buy_to_open", "quantity": 1}],
        "is_single": True, "entry_type": "debit", "quantity": 1, "tick": 0.05,
        "stop_loss_pct": 30.0, "close_order_id": "close-9", "state": "close_placed", "done": False,
    }
    await troute._resume_watcher_from_row(client, "ACC1", w)
    assert w["state"] == "stop_filled"
    assert w["stop_order_id"] == "stop-1"
    assert client.placed == []   # confirmed already filled -> nothing new submitted


async def test_resume_watching_fill_ignores_an_old_filled_close_from_a_different_entry(monkeypatch):
    """Cross-session follow-up: matching on option symbol + tag PREFIX alone
    can adopt a stale, unrelated FILLED close from an EARLIER, already-closed
    trade in the same contract (very plausible for 0DTE strikes reused across
    signals in one session) — silently leaving the CURRENT position with no
    exit at all. The tag is scoped to THIS entry's own order id, so an old
    close belonging to a different entry must never be adopted; a fresh close
    for the current position is placed instead."""
    async def _fake_price(client, symbol, legs):
        return {"complete": True, "net_bid": 0.60, "net_ask": 0.64, "abs_mid": 0.62}
    monkeypatch.setattr(troute, "_package_price", _fake_price)

    old_close = {"id": "close-OLD", "tag": troute._close_tag("torqueClose", "entry-2-OLD", "long_call"),
                "option_symbol": "X1", "status": "filled", "quantity": 1}
    client = FakeTradier(order_status={"status": "filled", "avg_fill_price": 1.0},
                         orders=[old_close],
                         place_responses=[{"order": {"id": "close-new", "status": "ok"}}])
    w = {
        "entry_order_id": "o1", "uid": "u1", "account_id": "ACC1", "symbol": "NDX",
        "strategy": "long_call", "legs": [{"symbol": "X1", "action": "buy_to_open", "quantity": 1}],
        "is_single": True, "entry_type": "debit", "quantity": 1, "tick": 0.05,
        "auto_close": True, "close_target_pct": 30.0, "stop_loss_pct": None,
        "floor_pct": 1.0, "state": "watching_fill", "done": False,
    }
    await troute._resume_watcher_from_row(client, "ACC1", w)
    real_orders = [p for p in client.placed if "_modify" not in p]
    assert len(real_orders) == 1                 # the old, unrelated close was NOT adopted
    assert w["close_order_id"] == "close-new"     # a fresh close for THIS position was placed instead


async def test_resume_close_placed_ignores_an_old_filled_stop_from_a_different_entry(monkeypatch):
    """Same follow-up, stop side: an old FILLED torqueStop from a DIFFERENT,
    already-closed position in the same contract must never be mistaken for
    "this position already stopped out" — that would silently drop stop
    protection for a position that's actually still open. A fresh stop gets
    armed for the current position instead."""
    async def _fake_price(client, symbol, legs):
        return {"complete": True, "net_bid": 0.60, "net_ask": 0.64, "abs_mid": 0.62}
    monkeypatch.setattr(troute, "_package_price", _fake_price)
    monkeypatch.setattr(troute, "_STOP_INTERVAL", 0.0)

    old_stop = {"id": "stop-OLD", "tag": troute._close_tag("torqueStop", "entry-2-OLD", "long_call"),
               "option_symbol": "X1", "status": "filled", "quantity": 1}
    client = FakeTradier(
        order_status_by_id={"close-9": {"status": "open"}, "1000": {"status": "filled"}},
        orders=[old_stop])
    w = {
        "entry_order_id": "o1", "uid": "u1", "account_id": "ACC1", "symbol": "NDX",
        "strategy": "long_call", "legs": [{"symbol": "X1", "action": "buy_to_open", "quantity": 1}],
        "is_single": True, "entry_type": "debit", "quantity": 1, "tick": 0.05,
        "entry_fill": 1.00, "stop_loss_pct": 30.0, "close_order_id": "close-9",
        "state": "close_placed", "done": False,
    }
    await troute._resume_watcher_from_row(client, "ACC1", w)
    assert w["stop_order_id"] != "stop-OLD"       # the old, unrelated stop was NOT adopted as ground truth
    assert w["state"] == "stop_filled"            # a fresh stop was armed for THIS position instead


async def test_resume_with_dead_stop_order_requotes_fresh_not_duplicated(monkeypatch):
    """The persisted stop order is confirmed CANCELED (dead) on resume — safe
    to submit exactly one fresh replacement, never two."""
    async def _fake_price(client, symbol, legs):
        return {"complete": True, "net_bid": 0.60, "net_ask": 0.64, "abs_mid": 0.62}
    monkeypatch.setattr(troute, "_package_price", _fake_price)

    client = FakeTradier(
        order_status_by_id={"999": {"status": "canceled", "exec_quantity": 0.0}},
        order_status={"status": "filled"},   # the fresh replacement fills
    )
    w = {
        "entry_order_id": "o1", "uid": "u1", "account_id": "ACC1", "symbol": "NDX",
        "strategy": "long_call", "legs": [{"symbol": "X1", "action": "buy_to_open", "quantity": 1}],
        "is_single": True, "entry_type": "debit", "quantity": 1, "tick": 0.05,
        "state": "stop_placed", "stop_order_id": "999", "done": False,
    }
    await troute._resume_watcher_from_row(client, "ACC1", w)
    real_orders = [p for p in client.placed if "_modify" not in p]
    assert len(real_orders) == 1   # exactly one fresh order, not a duplicate

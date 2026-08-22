"""roll_expired_watchlist — pinned expiries auto-bump to the next listed date.

Supabase writes and the provider are faked. asyncio_mode=auto.
"""
from __future__ import annotations

from datetime import date, timedelta

from app import simmer_watcher as sw

YESTERDAY = (date.today() - timedelta(days=1)).isoformat()
NEXT = (date.today() + timedelta(days=2)).isoformat()
LATER = (date.today() + timedelta(days=9)).isoformat()


class _Prov:
    def __init__(self, exps):
        self._exps = exps

    async def expirations(self, symbol):
        return self._exps


def _wire(monkeypatch, rows, exps):
    calls = {"update": [], "delete": []}

    async def sel(table, select="*", filters=None, **kw):
        return rows

    async def upd(table, filters, values):
        calls["update"].append((filters, values)); return True

    async def dl(table, filters):
        calls["delete"].append(filters); return True

    monkeypatch.setattr(sw.supabase_admin, "select_many", sel)
    monkeypatch.setattr(sw.supabase_admin, "update_rows", upd)
    monkeypatch.setattr(sw.supabase_admin, "delete_rows", dl)
    return calls


async def test_expired_pin_rolls_to_next_listed(monkeypatch):
    rows = [{"id": "r1", "user_id": "u1", "symbol": "TSLA", "expiration": YESTERDAY, "active": True}]
    calls = _wire(monkeypatch, rows, [NEXT, LATER])
    n = await sw.roll_expired_watchlist(_Prov([NEXT, LATER]))
    assert n == 1
    assert calls["update"] == [({"id": "eq.r1"}, {"expiration": NEXT})]
    assert calls["delete"] == []


async def test_future_and_autopick_rows_untouched(monkeypatch):
    rows = [
        {"id": "r1", "user_id": "u1", "symbol": "MU", "expiration": NEXT, "active": True},   # future
        {"id": "r2", "user_id": "u1", "symbol": "NVDA", "expiration": None, "active": True},  # auto-pick
    ]
    calls = _wire(monkeypatch, rows, [NEXT, LATER])
    assert await sw.roll_expired_watchlist(_Prov([NEXT, LATER])) == 0
    assert calls["update"] == [] and calls["delete"] == []


async def test_dedupe_deletes_expired_when_target_exists(monkeypatch):
    # user already has the rolled-to date → the expired row is removed, not updated.
    rows = [
        {"id": "old", "user_id": "u1", "symbol": "TSLA", "expiration": YESTERDAY, "active": True},
        {"id": "new", "user_id": "u1", "symbol": "TSLA", "expiration": NEXT, "active": True},
    ]
    calls = _wire(monkeypatch, rows, [NEXT, LATER])
    assert await sw.roll_expired_watchlist(_Prov([NEXT, LATER])) == 1
    assert calls["delete"] == [{"id": "eq.old"}]
    assert calls["update"] == []


async def test_no_listed_expirations_leaves_row(monkeypatch):
    rows = [{"id": "r1", "user_id": "u1", "symbol": "TSLA", "expiration": YESTERDAY, "active": True}]
    calls = _wire(monkeypatch, rows, [])
    assert await sw.roll_expired_watchlist(_Prov([])) == 0
    assert calls["update"] == [] and calls["delete"] == []

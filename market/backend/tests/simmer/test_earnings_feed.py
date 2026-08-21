"""Next-earnings-date feed → catalyst pipeline (what makes the toggle appear).

SEC confirms only PAST earnings; SIMMER_EARNINGS_PROVIDER=yahoo surfaces the
NEXT date via the data provider's `next_earnings`. Provider I/O is faked.
asyncio_mode=auto.
"""
from __future__ import annotations

import json
from datetime import date, timedelta

from app import simmer_watcher as sw


class _Prov:
    def __init__(self, r):
        self._r = r

    async def next_earnings(self, symbol):
        return self._r


def _provider_setting(monkeypatch, value="yahoo"):
    class _S:
        simmer_earnings_provider = value
    monkeypatch.setattr("app.config.get_settings", lambda: _S())


async def test_fetch_upcoming(monkeypatch):
    _provider_setting(monkeypatch)
    d = (date.today() + timedelta(days=6)).isoformat()
    out = await sw._fetch_next_earnings("NVDA", _Prov({"date": d, "confirmed": True}))
    assert out == {"date": d, "days": 6, "confirmed": True}


async def test_fetch_disabled_when_provider_off(monkeypatch):
    _provider_setting(monkeypatch, "off")
    d = (date.today() + timedelta(days=6)).isoformat()
    assert await sw._fetch_next_earnings("NVDA", _Prov({"date": d, "confirmed": True})) is None


async def test_fetch_ignores_past(monkeypatch):
    _provider_setting(monkeypatch)
    d = (date.today() - timedelta(days=2)).isoformat()
    assert await sw._fetch_next_earnings("NVDA", _Prov({"date": d, "confirmed": True})) is None


async def test_fetch_ignores_beyond_horizon(monkeypatch):
    _provider_setting(monkeypatch)
    d = (date.today() + timedelta(days=200)).isoformat()
    assert await sw._fetch_next_earnings("NVDA", _Prov({"date": d, "confirmed": True})) is None


async def test_fetch_provider_without_method(monkeypatch):
    _provider_setting(monkeypatch)
    assert await sw._fetch_next_earnings("NVDA", object()) is None


async def test_refresh_injects_future_earnings(monkeypatch):
    _provider_setting(monkeypatch)
    monkeypatch.setattr(sw, "fetch_recent_catalysts", lambda db, sym, lb: [])  # no SEC rows
    d = (date.today() + timedelta(days=6)).isoformat()
    out = await sw._refresh_catalysts(None, "NVDA", {}, _Prov({"date": d, "confirmed": False}))
    flags = json.loads(out["catalyst_flags"])
    assert flags["earnings_date"] == d
    assert any(e["type"] == "earnings" and e["days"] == 6 for e in flags["events"])
    assert out["_catalysts"][-1]["confirmed"] is False

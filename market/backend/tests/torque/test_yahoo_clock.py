"""Torque market clock: Tradier's own market calendar, not Yahoo.

Regression for a bug found 2026-09-26 during the facades-news-reactor
integration: Yahoo's v8 chart endpoint stopped returning `marketState` in the
meta block, silently degrading `/torque/clock` to always `open: null`.
facades-news-reactor's own follow-up recommended replacing the Yahoo
dependency entirely with Tradier's `GET /v1/markets/calendar` (Torque already
has the credentials/client for it) rather than patching around Yahoo's
missing field — verified against a real closed day (Thanksgiving 2026-11-26)
and a real early close (2026-11-27, 13:00) via the live API.
"""
from __future__ import annotations

from datetime import datetime

import pytest

from app.routes import torque as troute
from app.routes.torque import torque_clock

from .conftest import FakeTradier, FakeRequest


@pytest.fixture(autouse=True)
def _reset_caches():
    troute._MKT_CACHE.update(t=0.0, state=None, open=None)
    troute._CALENDAR_CACHE.update(ym=None, days={})
    yield
    troute._MKT_CACHE.update(t=0.0, state=None, open=None)
    troute._CALENDAR_CACHE.update(ym=None, days={})


def _today_str():
    return datetime.now(troute._NY_TZ).strftime("%Y-%m-%d")


async def test_regular_session_reports_open(monkeypatch):
    now_ny = datetime.now(troute._NY_TZ)
    monkeypatch.setattr(troute, "datetime", type("D", (), {"now": staticmethod(lambda tz: now_ny.replace(hour=11, minute=0))}))
    client = FakeTradier(calendar_days={_today_str(): {
        "date": _today_str(), "status": "open", "description": "Market is open",
        "premarket": {"start": "07:00", "end": "09:24"},
        "open": {"start": "09:30", "end": "16:00"},
        "postmarket": {"start": "16:00", "end": "19:55"},
    }})
    r = await torque_clock(FakeRequest(client))
    assert r == {"market_state": "REGULAR", "open": True}


async def test_holiday_reports_closed_with_no_session_times(monkeypatch):
    now_ny = datetime.now(troute._NY_TZ)
    monkeypatch.setattr(troute, "datetime", type("D", (), {"now": staticmethod(lambda tz: now_ny.replace(hour=11, minute=0))}))
    client = FakeTradier(calendar_days={_today_str(): {
        "date": _today_str(), "status": "closed", "description": "Market is closed for Thanksgiving Day",
    }})
    r = await torque_clock(FakeRequest(client))
    assert r == {"market_state": "CLOSED", "open": False}


async def test_early_close_day_is_closed_after_the_shortened_end(monkeypatch):
    """The 2026-11-27-shaped day: a normal open day, but `open.end` is 13:00
    instead of 16:00 — after that, must report closed, not open."""
    now_ny = datetime.now(troute._NY_TZ)
    monkeypatch.setattr(troute, "datetime", type("D", (), {"now": staticmethod(lambda tz: now_ny.replace(hour=14, minute=0))}))
    client = FakeTradier(calendar_days={_today_str(): {
        "date": _today_str(), "status": "open", "description": "Market closes early at 13:00",
        "premarket": {"start": "07:00", "end": "09:24"},
        "open": {"start": "09:30", "end": "13:00"},
        "postmarket": {"start": "13:00", "end": "16:55"},
    }})
    r = await torque_clock(FakeRequest(client))
    assert r["market_state"] == "POST" and r["open"] is False   # inside the (shifted) postmarket window


async def test_before_premarket_reports_closed(monkeypatch):
    now_ny = datetime.now(troute._NY_TZ)
    monkeypatch.setattr(troute, "datetime", type("D", (), {"now": staticmethod(lambda tz: now_ny.replace(hour=3, minute=0))}))
    client = FakeTradier(calendar_days={_today_str(): {
        "date": _today_str(), "status": "open", "description": "Market is open",
        "premarket": {"start": "07:00", "end": "09:24"},
        "open": {"start": "09:30", "end": "16:00"},
        "postmarket": {"start": "16:00", "end": "19:55"},
    }})
    r = await torque_clock(FakeRequest(client))
    assert r == {"market_state": "CLOSED", "open": False}


async def test_calendar_fetch_failure_with_nothing_cached_reports_open_none(monkeypatch):
    client = FakeTradier()

    async def _boom(month, year):
        raise RuntimeError("network down")
    monkeypatch.setattr(client, "market_calendar", _boom)

    r = await torque_clock(FakeRequest(client))
    assert r == {"market_state": None, "open": None}


async def test_calendar_is_cached_for_the_whole_month(monkeypatch):
    """One Tradier call per month, not one per /torque/clock poll."""
    calls = {"n": 0}
    client = FakeTradier()
    real_calendar = client.market_calendar

    async def _counting(month, year):
        calls["n"] += 1
        return await real_calendar(month, year)
    monkeypatch.setattr(client, "market_calendar", _counting)

    await _fetch_calendar_twice(client)
    assert calls["n"] == 1


async def _fetch_calendar_twice(client):
    # Bypass the outer 30s _MKT_CACHE (irrelevant here) by calling the
    # calendar-days helper directly twice, same as two separate cache misses
    # inside the same month would.
    await troute._tradier_calendar_days(client)
    await troute._tradier_calendar_days(client)

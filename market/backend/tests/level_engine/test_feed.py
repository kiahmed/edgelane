"""feed.py — FeedClient: token header, 404/503/network → None + reason,
new-ts dedupe (no double ticks), ok / stale / down status, prior closes."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx

from level_engine.feed import FeedClient, parse_ts

T0 = datetime(2026, 11, 20, 15, 0, tzinfo=timezone.utc)


class Backend:
    def __init__(self):
        self.status = 200
        self.ts = T0
        self.seen_headers = []
        self.raise_exc = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.seen_headers.append(dict(request.headers))
        if self.raise_exc:
            raise httpx.ConnectError("refused", request=request)
        if self.status != 200:
            return httpx.Response(self.status, json={"detail": "x"})
        return httpx.Response(200, json={"ts": self.ts.isoformat().replace("+00:00", "Z"),
                                         "spot": 7000.0, "spx_prevclose": 6990.5,
                                         "spy_prevclose": 699.1, "market_open": True})


def _client(be: Backend, clock):
    return FeedClient("http://backend:8789/internal/feed/", "s3cret", "spx", tick_sec=20,
                      now=clock, transport=httpx.MockTransport(be.handler))


class Clock:
    def __init__(self):
        self.t = T0

    def __call__(self):
        return self.t


def test_fetch_sends_token_and_url():
    be, clock = Backend(), Clock()
    fc = _client(be, clock)
    doc = fc.fetch()
    assert doc["spot"] == 7000.0 and fc.last_error is None
    assert fc.url == "http://backend:8789/internal/feed/SPX"
    assert be.seen_headers[0]["x-internal-token"] == "s3cret"


def test_failures_return_none_with_reason():
    be, clock = Backend(), Clock()
    fc = _client(be, clock)
    be.status = 404
    assert fc.fetch() is None and fc.last_error == "auth_or_missing"
    be.status = 503
    assert fc.fetch() is None and fc.last_error == "not_ready"
    be.status = 500
    assert fc.fetch() is None and fc.last_error == "error"
    be.status, be.raise_exc = 200, True
    assert fc.fetch() is None and fc.last_error == "error"
    assert fc.status["state"] == "down" and fc.status["last_ok_at"] is None
    be.raise_exc = False
    assert fc.fetch() is not None and fc.last_error is None


def test_is_new_ignores_same_and_older_ts():
    be, clock = Backend(), Clock()
    fc = _client(be, clock)
    f1 = fc.fetch()
    assert fc.is_new(f1) is True
    assert fc.is_new(f1) is False                       # same ts → no double tick
    assert fc.is_new({"ts": (T0 - timedelta(seconds=20)).isoformat()}) is False
    assert fc.is_new({"ts": None}) is False and fc.is_new(None) is False
    be.ts = T0 + timedelta(seconds=20)
    assert fc.is_new(fc.fetch()) is True


def test_status_ok_stale_down():
    be, clock = Backend(), Clock()
    fc = _client(be, clock)
    assert fc.status["state"] == "down"                 # nothing fetched yet
    fc.is_new(fc.fetch())
    assert fc.status["state"] == "ok"
    for i in range(1, 4):                               # ts stuck for 60 s: still ok
        clock.t = T0 + timedelta(seconds=20 * i)
        assert fc.is_new(fc.fetch()) is False
    assert fc.status["state"] == "ok"
    clock.t = T0 + timedelta(seconds=61)                # > 3×TICK_SEC without a new ts
    fc.fetch()
    st = fc.status
    assert st["state"] == "stale" and st["last_ok_at"] == clock.t
    be.ts = T0 + timedelta(seconds=80)
    assert fc.is_new(fc.fetch()) is True and fc.status["state"] == "ok"
    be.status = 503
    fc.fetch()
    assert fc.status["state"] == "down" and fc.status["error"] == "not_ready"


def test_prior_close_from_last_feed():
    be, clock = Backend(), Clock()
    fc = _client(be, clock)
    assert fc.prior_close("SPX") is None
    fc.fetch()
    assert fc.prior_close("SPX") == 6990.5 and fc.prior_close("spy") == 699.1
    assert fc.prior_close("NDX") is None
    be.status = 503
    fc.fetch()                                          # a failed fetch keeps the last good
    assert fc.prior_close("SPX") == 6990.5


def test_parse_ts():
    assert parse_ts("2026-11-20T15:00:00Z") == T0
    assert parse_ts("2026-11-20T10:00:00-05:00") == T0
    assert parse_ts("2026-11-20T15:00:00") == T0        # naive → UTC
    assert parse_ts("garbage") is None and parse_ts(None) is None

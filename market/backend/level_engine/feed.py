"""Feed client — reads the backend's `GET /internal/feed/{symbol}` (docs §3.2).

The level engine never calls Tradier; this is its only data input.

* `fetch()` → the feed dict, or None. On None, `last_error` says why:
    'auth_or_missing' (404 — token wrong/blank, tunnel refusal, or no route),
    'not_ready'       (503 — backend has no snapshot / LEVEL_FEED_ENABLED off),
    'error'           (network error, timeout, other status, bad JSON).
* `is_new(feed)` → True only when the feed `ts` advanced past the last
  accepted one; it then ACCEPTS it (so the same ts never ticks twice, §3.2
  "no double ticks"). Same or older ts → False.
* `status` → {state: ok|stale|down, last_ok_at, last_new_at, last_ts, error}.
    down  = the last fetch failed;
    stale = fetches work but ts hasn't advanced for > 3×TICK_SEC (§9 "Feed
            stale": no tick, no decision);
    ok    = otherwise.
* `prior_close(symbol)` → spx_prevclose / spy_prevclose from the last good
  feed (the admin page's soft ±TYPO_CHECK_PCT typo check, §17).

The token is sent as `X-Internal-Token` and never logged.
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from typing import Callable

import httpx

from .clock import now_utc

log = logging.getLogger("level_engine.feed")

STALE_TICKS = 3


def parse_ts(v) -> datetime | None:
    """Feed ts (ISO string, 'Z' ok, or datetime) → tz-aware UTC datetime."""
    if v is None:
        return None
    if isinstance(v, datetime):
        dt = v
    else:
        try:
            s = str(v).strip()
            if s.endswith("Z"):
                s = s[:-1] + "+00:00"
            dt = datetime.fromisoformat(s)
        except ValueError:
            return None
    if dt.tzinfo is None or dt.utcoffset() is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


class FeedClient:
    def __init__(self, url_base: str, token: str, symbol: str, timeout: float = 5,
                 tick_sec: float = 20, now: Callable[[], datetime] = now_utc,
                 transport: httpx.BaseTransport | None = None) -> None:
        self.url = f"{url_base.rstrip('/')}/{symbol.upper()}"
        self.symbol = symbol.upper()
        self._token = token or ""
        self._now = now
        self.tick_sec = float(tick_sec)
        self._client = httpx.Client(timeout=timeout, transport=transport)
        self._lock = threading.Lock()
        self.last_feed: dict | None = None
        self.last_error: str | None = None
        self.last_ok_at: datetime | None = None     # last successful fetch
        self.last_new_at: datetime | None = None    # last time ts advanced
        self.last_ts: datetime | None = None        # last accepted feed ts

    def close(self) -> None:
        self._client.close()

    # ---- fetch ----
    def fetch(self) -> dict | None:
        try:
            r = self._client.get(self.url, headers={"X-Internal-Token": self._token,
                                                    "Accept": "application/json"})
        except httpx.HTTPError as e:
            return self._fail("error", f"{type(e).__name__}")
        if r.status_code == 404:
            return self._fail("auth_or_missing", "404")
        if r.status_code == 503:
            return self._fail("not_ready", "503")
        if r.status_code != 200:
            return self._fail("error", f"HTTP {r.status_code}")
        try:
            doc = r.json()
        except ValueError:
            return self._fail("error", "bad JSON")
        if not isinstance(doc, dict):
            return self._fail("error", "payload not an object")
        with self._lock:
            self.last_feed = doc
            self.last_error = None
            self.last_ok_at = self._now()
        return doc

    def _fail(self, reason: str, detail: str) -> None:
        if self.last_error != reason:
            log.warning("feed %s: %s (%s)", self.url, reason, detail)
        self.last_error = reason
        return None

    # ---- dedupe ----
    def is_new(self, feed: dict | None) -> bool:
        ts = parse_ts((feed or {}).get("ts"))
        if ts is None:
            return False
        if self.last_ts is not None and ts <= self.last_ts:
            return False
        self.last_ts = ts
        self.last_new_at = self._now()
        return True

    # ---- status ----
    @property
    def status(self) -> dict:
        now = self._now()
        if self.last_error is not None or self.last_ok_at is None:
            state = "down"
        elif (self.last_new_at is None
              or (now - self.last_new_at).total_seconds() > STALE_TICKS * self.tick_sec):
            state = "stale"
        else:
            state = "ok"
        return {"state": state, "error": self.last_error,
                "last_ok_at": self.last_ok_at, "last_new_at": self.last_new_at,
                "last_ts": self.last_ts}

    # ---- admin guard ----
    def prior_close(self, symbol: str) -> float | None:
        with self._lock:
            f = self.last_feed or {}
        # Reference price for the admin's ±% typo guard (§17). The LATEST trade
        # first: levels are entered evenings / pre-market, and after the close
        # Tradier's `prevclose` still points at the session BEFORE today, so a
        # big day made valid levels look like typos. prevclose is the fallback.
        keys = {"SPX": ("spot", "spx_prevclose"),
                "SPY": ("spy_spot", "spy_prevclose")}.get(str(symbol).upper())
        if keys is None:
            return None
        for key in keys:
            try:
                v = float(f.get(key))
            except (TypeError, ValueError):
                continue
            if v > 0:
                return v
        return None

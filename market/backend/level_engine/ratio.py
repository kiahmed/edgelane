"""SPX/SPY ratio tracker (§6). Pure, stdlib only; time comes from the caller.

Before RATIO_LOCK_TIME (ET) the ratio is provisional = prior-close SPX / prior-
close SPY. The first update at/after RATIO_LOCK_TIME with valid live SPX and SPY
locks ratio = live SPX / live SPY for the rest of the ET trading day.

Decisions (spec ambiguities, resolved conservatively)
-----------------------------------------------------
* Provisional source priority: prior closes (both valid) > the FIRST valid live
  ratio seen that day, which is then frozen (a drifting provisional ratio would
  move converted levels every tick). Prior closes appearing later replace a
  live-fallback provisional ratio. No valid inputs → ratio None.
* At/after lock time with live SPX or SPY missing/invalid: NOT locked; the
  provisional ratio is returned and the lock happens on the first later update
  with both live quotes valid. (Restarting after lock time without a stored
  locked ratio therefore locks at the restart's first tick — callers must
  `restore()` from `store.latest_locked_ratio` first.)
* Once locked the ratio never changes that day; inputs are ignored.
* Day change: the ET date of `now` is the trading day. The first update after
  the date changes resets the tracker. `restore(ratio)` with no trade_date
  adopts the date of the next update (so a restore before the first tick is
  not wiped by the first tick's day initialisation).
* Valid price = a finite float > 0. `now` must be tz-aware (ValueError if not).
"""
from __future__ import annotations

import math
from datetime import date, datetime, time
from zoneinfo import ZoneInfo


def _ok(x) -> bool:
    try:
        return x is not None and math.isfinite(float(x)) and float(x) > 0
    except (TypeError, ValueError):
        return False


class RatioTracker:
    def __init__(self, lock_time: time, tz: str = "America/New_York") -> None:
        self.lock_time = lock_time
        self.tz = ZoneInfo(tz)
        self._date: date | None = None
        self._ratio: float | None = None
        self._locked = False
        self._prov_source: str | None = None   # 'prevclose' | 'live' | None
        self.locked_at: datetime | None = None

    # ---- properties ----
    @property
    def ratio(self) -> float | None:
        return self._ratio

    @property
    def locked(self) -> bool:
        return self._locked

    @property
    def trade_date(self) -> date | None:
        return self._date

    # ---- lifecycle ----
    def reset_for_day(self, trade_date: date | None) -> None:
        self._date = trade_date
        self._ratio = None
        self._locked = False
        self._prov_source = None
        self.locked_at = None

    def restore(self, ratio: float, trade_date: date | None = None) -> None:
        """Restore a locked ratio (restart; from store.latest_locked_ratio)."""
        if not _ok(ratio):
            raise ValueError(f"invalid ratio {ratio!r}")
        self._date = trade_date
        self._ratio = float(ratio)
        self._locked = True
        self._prov_source = None

    def update(self, now: datetime, spx: float | None, spy: float | None,
               spx_prevclose: float | None, spy_prevclose: float | None
               ) -> tuple[float | None, bool]:
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
        local = now.astimezone(self.tz)
        d = local.date()
        if self._date is None:
            self._date = d
        elif d != self._date:
            self.reset_for_day(d)
        if self._locked:
            return self._ratio, True
        if local.time() >= self.lock_time and _ok(spx) and _ok(spy):
            self._ratio = float(spx) / float(spy)
            self._locked = True
            self._prov_source = None
            self.locked_at = now
            return self._ratio, True
        if _ok(spx_prevclose) and _ok(spy_prevclose):
            if self._prov_source != "prevclose":
                self._ratio = float(spx_prevclose) / float(spy_prevclose)
                self._prov_source = "prevclose"
        elif self._prov_source is None and _ok(spx) and _ok(spy):
            self._ratio = float(spx) / float(spy)
            self._prov_source = "live"
        return self._ratio, False

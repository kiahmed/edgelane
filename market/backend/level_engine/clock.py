"""Level Engine clock helpers — the ET trading day and a UTC 'now'.

Every component asks these (not `date.today()` / `datetime.now()`) so tests and
replay can inject a fixed clock and the trading day is always the ET one.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def today_et(now: datetime | None = None) -> date:
    """The ET calendar date of `now` (default: the current instant).
    A naive `now` is taken as UTC."""
    now = now or now_utc()
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now.astimezone(ET).date()

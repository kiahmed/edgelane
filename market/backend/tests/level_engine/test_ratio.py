"""ratio.py — provisional prior-close ratio, lock at RATIO_LOCK_TIME, restore (§6)."""
from __future__ import annotations

from datetime import date, datetime, time, timezone
from zoneinfo import ZoneInfo

import pytest

from level_engine.levels import convert_levels
from level_engine.ratio import RatioTracker

ET = ZoneInfo("America/New_York")


def et(y, m, d, hh, mm, ss=0):
    return datetime(y, m, d, hh, mm, ss, tzinfo=ET)


def rt():
    return RatioTracker(time(9, 35))


def test_before_lock_uses_prior_close():
    r = rt()
    assert r.update(et(2026, 11, 20, 9, 30), 7300, 730, 7200, 720) == (10.0, False)
    assert r.ratio == 10.0 and not r.locked


def test_before_lock_live_fallback_is_frozen():
    r = rt()
    assert r.update(et(2026, 11, 20, 9, 30), 7300, 730, None, None) == (10.0, False)
    # live drifts — provisional does not
    assert r.update(et(2026, 11, 20, 9, 31), 7310, 730, None, None) == (10.0, False)
    # prior closes appear → they take over
    assert r.update(et(2026, 11, 20, 9, 32), 7310, 730, 7250, 725) == (10.0, False)
    r2 = rt()
    assert r2.update(et(2026, 11, 20, 9, 30), None, None, None, None) == (None, False)


def test_lock_at_lock_time_and_stays():
    r = rt()
    r.update(et(2026, 11, 20, 9, 34, 40), 7300, 730, 7200, 719)
    ratio, locked = r.update(et(2026, 11, 20, 9, 35), 7305, 730.5, 7200, 719)
    assert locked and ratio == pytest.approx(7305 / 730.5)
    assert r.update(et(2026, 11, 20, 12, 0), 7400, 700, 7200, 719) == (ratio, True)


def test_lock_waits_for_valid_live():
    r = rt()
    assert r.update(et(2026, 11, 20, 9, 35), None, 730, 7200, 720) == (10.0, False)
    assert r.update(et(2026, 11, 20, 9, 35, 20), 7300, 0, 7200, 720) == (10.0, False)
    ratio, locked = r.update(et(2026, 11, 20, 9, 35, 40), 7303, 730, 7200, 720)
    assert locked and ratio == pytest.approx(7303 / 730)


def test_lock_time_is_et_across_dst():
    # summer: 09:35 ET = 13:35 UTC; winter: 14:35 UTC
    r = rt()
    assert r.update(datetime(2026, 7, 1, 13, 34, tzinfo=timezone.utc), 1, 1, 2, 1)[1] is False
    assert r.update(datetime(2026, 7, 1, 13, 35, tzinfo=timezone.utc), 1, 1, 2, 1)[1] is True
    r = rt()
    assert r.update(datetime(2026, 11, 20, 14, 34, tzinfo=timezone.utc), 1, 1, 2, 1)[1] is False
    assert r.update(datetime(2026, 11, 20, 14, 35, tzinfo=timezone.utc), 1, 1, 2, 1)[1] is True


def test_auto_reset_on_new_day():
    r = rt()
    r.update(et(2026, 11, 20, 10, 0), 7300, 730, 7200, 720)
    assert r.locked
    ratio, locked = r.update(et(2026, 11, 23, 9, 30), 7400, 740.5, 7300, 731)
    assert not locked and ratio == pytest.approx(7300 / 731)
    assert r.trade_date == date(2026, 11, 23)


def test_restore_survives_first_update_then_resets_next_day():
    r = rt()
    r.restore(10.01)
    assert r.locked and r.ratio == 10.01
    assert r.update(et(2026, 11, 20, 11, 0), 7300, 700, 7200, 720) == (10.01, True)
    assert r.update(et(2026, 11, 23, 9, 30), 7300, 730, 7200, 720) == (10.0, False)


def test_restore_with_date_and_validation():
    r = rt()
    r.restore(10.02, trade_date=date(2026, 11, 19))
    assert r.update(et(2026, 11, 20, 9, 31), 7300, 730, 7200, 720) == (10.0, False)
    with pytest.raises(ValueError):
        r.restore(0)


def test_reset_for_day_and_naive_rejected():
    r = rt()
    r.update(et(2026, 11, 20, 10, 0), 7300, 730, None, None)
    r.reset_for_day(date(2026, 11, 20))
    assert r.ratio is None and not r.locked
    with pytest.raises(ValueError):
        r.update(datetime(2026, 11, 20, 10, 0), 1, 1, 1, 1)


def test_spy_conversion_before_and_after_lock():
    rows = [{"id": "y", "symbol": "SPY", "price": 600.0, "name": "PM high", "source": "entered"}]
    r = rt()
    prov, _ = r.update(et(2026, 11, 20, 9, 31), 6015, 601, 6000, 600)      # 10.0
    before, _ = convert_levels(rows, prov, 2.5)
    locked, ok = r.update(et(2026, 11, 20, 9, 35), 6012, 600, 6000, 600)  # 10.02
    after, persist = convert_levels(rows, locked, 2.5)
    assert ok and before[0].price == 6000.0 and after[0].price == 6012.0
    assert persist[0]["ratio"] == pytest.approx(10.02)

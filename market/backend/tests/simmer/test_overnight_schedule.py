"""Overnight scheduler timing predicates (simmer_watcher). Pure functions of the
ET clock + per-day flags — this is the "when does X fire" logic the live loop
relies on, tested here with a faked clock so a timing bug can't hide. No I/O."""
from __future__ import annotations

from datetime import datetime

from app.simmer_watcher import (_captures_due, _combiner_lock, _overnight_sleep,
                                 _should_catalyst, _should_import, _should_reset)

MON, SAT = 12, 17          # 2026-10-12 is a Monday; 2026-10-17 a Saturday


def dt(h, m, day=MON):
    return datetime(2026, 10, day, h, m)


# ── import window ────────────────────────────────────────────────────────────
def test_should_import_window():
    assert not _should_import(dt(9, 24), False)        # before 09:25
    assert _should_import(dt(9, 25), False)            # opens at 09:25
    assert _should_import(dt(15, 59), False)           # late start still imports
    assert not _should_import(dt(16, 0), False)        # not from 16:00
    assert not _should_import(dt(9, 30), True)         # already imported today
    assert not _should_import(dt(9, 30, SAT), False)   # weekend


# ── catalyst (evaluated with the live imported flag → same-tick) ─────────────
def test_should_catalyst():
    assert _should_catalyst(dt(9, 25), True, False)    # imported this tick, not done
    assert not _should_catalyst(dt(9, 25), False, False)   # no baseline yet
    assert not _should_catalyst(dt(9, 25), True, True)     # already done today
    assert not _should_catalyst(dt(16, 1), True, False)    # not after 16:00


# ── combiner freeze cutoff ───────────────────────────────────────────────────
def test_combiner_lock_at_1000():
    assert not _combiner_lock(dt(9, 59))
    assert _combiner_lock(dt(10, 0))
    assert _combiner_lock(dt(13, 30))


def test_should_reset_eod():
    assert not _should_reset(dt(15, 59), False)        # before the close
    assert _should_reset(dt(16, 0), False)             # from 16:00
    assert _should_reset(dt(18, 0), False)             # later still fine
    assert not _should_reset(dt(16, 30), True)         # already done today
    assert not _should_reset(dt(16, 30, SAT), False)   # weekend


# ── which readings are due ───────────────────────────────────────────────────
def test_captures_due():
    assert _captures_due(dt(10, 0), open_=True, readings_done=set()) == ["10:00"]
    assert _captures_due(dt(10, 0), open_=True, readings_done={"10:00"}) == []   # idempotent
    assert _captures_due(dt(10, 0), open_=False, readings_done=set()) == []      # 10:00 needs open
    # close window 15:58–16:30 fires regardless of open_, outcome stamp included
    assert _captures_due(dt(15, 58), open_=True, readings_done={"10:00"}) == ["16:00"]
    assert _captures_due(dt(16, 20), open_=False, readings_done={"10:00"}) == ["16:00"]
    assert _captures_due(dt(16, 31), open_=False, readings_done={"10:00"}) == []  # past window
    assert _captures_due(dt(9, 59), open_=True, readings_done=set()) == []        # 10:00 not yet
    assert _captures_due(dt(16, 0, SAT), open_=False, readings_done=set()) == []  # weekend


# ── loop cadence ─────────────────────────────────────────────────────────────
def test_overnight_sleep_buckets():
    assert _overnight_sleep(dt(9, 35), True) == 60.0        # first 30 min
    assert _overnight_sleep(dt(11, 0), True) == 180.0       # mid-session
    assert _overnight_sleep(dt(15, 50), True) == 60.0       # tighten into the close
    assert _overnight_sleep(dt(16, 10), False) == 120.0     # catch the close reading
    assert _overnight_sleep(dt(20, 0), False) == 600.0      # overnight idle
    pre = _overnight_sleep(dt(9, 20), False)                # wake near 09:25
    assert 0 < pre <= 600.0
    assert _overnight_sleep(dt(6, 0), False) == 600.0       # far pre-open → capped
    # import not yet landed in-window → brisk 60s retry (no 600s pre-open strand)
    assert _overnight_sleep(dt(9, 26), False, import_pending=True) == 60.0
    assert _overnight_sleep(dt(11, 0), True, import_pending=True) == 60.0

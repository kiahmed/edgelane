"""Overnight scheduler timing predicates (simmer_watcher). Pure functions of the
ET clock + per-day flags — this is the "when does X fire" logic the live loop
relies on, tested here with a faked clock so a timing bug can't hide. No I/O."""
from __future__ import annotations

from datetime import datetime

from app.simmer_watcher import (_captures_due, _combiner_lock, _import_window_closed,
                                 _new_day_reset_action, _overnight_sleep, _should_catalyst,
                                 _should_import)

MON, SAT = 12, 17          # 2026-10-12 is a Monday; 2026-10-17 a Saturday


def dt(h, m, day=MON):
    return datetime(2026, 10, day, h, m)


# ── import window (one-shot; `imported_today`=full board captured) ───────────
def test_should_import_window():
    assert not _should_import(dt(6, 59), False)        # before 07:00
    assert _should_import(dt(7, 0), False)             # opens at 07:00 (pre-open baseline)
    assert _should_import(dt(9, 25), False)            # still imports at the open
    assert _should_import(dt(15, 59), False)           # late start still imports
    assert not _should_import(dt(16, 0), False)        # not from 16:00
    assert not _should_import(dt(7, 30), True)         # full board already captured
    assert not _should_import(dt(7, 30, SAT), False)   # weekend


def test_should_catalyst():
    # Floor at 09:25 (an LLM read before the open is pointless), NOT capped at 10:30 —
    # a late start must still analyze directly rather than skip the day.
    assert not _should_catalyst(dt(7, 0), True, False)     # before 09:25 → no LLM yet
    assert not _should_catalyst(dt(9, 24), True, False)    # one minute before → still no
    assert _should_catalyst(dt(9, 25), True, False)        # opens at 09:25
    assert _should_catalyst(dt(10, 0), True, False)        # re-runs (retries failures)
    assert _should_catalyst(dt(13, 0), True, False)        # a late start past 10:30 still runs
    assert _should_catalyst(dt(15, 59), True, False)       # right up to the close
    assert not _should_catalyst(dt(16, 0), True, False)    # not after the close
    assert not _should_catalyst(dt(9, 25), False, False)   # nothing captured yet
    assert not _should_catalyst(dt(9, 25), True, True)     # already complete (done)


def test_import_window_closed_caps_retries():
    assert not _import_window_closed(dt(9, 25))            # retry while capturing stragglers
    assert not _import_window_closed(dt(10, 29))
    assert _import_window_closed(dt(10, 30))               # accept partial, stop retrying
    assert _import_window_closed(dt(11, 0))


# ── combiner freeze cutoff ───────────────────────────────────────────────────
def test_combiner_lock_at_1000():
    assert not _combiner_lock(dt(9, 59))
    assert _combiner_lock(dt(10, 0))
    assert _combiner_lock(dt(13, 30))


def test_new_day_reset_never_wipes_on_read_error():
    # None = Supabase read error → MUST NOT reset (would delete a live board), and must
    # not mark done so it re-checks next tick.
    assert _new_day_reset_action(None) == (False, False)
    # [] = confirmed empty → genuine first import → reset + mark done
    assert _new_day_reset_action([]) == (True, True)
    # rows present (e.g. mid-day restart) → skip reset, mark done
    assert _new_day_reset_action([{"symbol": "MU"}]) == (False, True)


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
    # full board not yet captured in-window → brisk 60s retry for the stragglers
    assert _overnight_sleep(dt(9, 40), True, import_pending=True) == 60.0

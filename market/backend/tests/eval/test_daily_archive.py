"""End-of-day rollup + data-quality gate (evaluator.archive_completed_days).

Days are bucketed by ET date; `complete` requires covering ≥75% of the 390-min RTH
session with no polling gap > 15 min. Partial days are kept but flagged incomplete.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from app.evaluator import archive_completed_days

ET = ZoneInfo("America/New_York")
DAY = "2026-06-15"  # a past Monday


def _naive_utc(et_dt: datetime) -> datetime:
    """ET wall-clock → naive-UTC, mimicking how DuckDB hands timestamps back."""
    return et_dt.astimezone(timezone.utc).replace(tzinfo=None)


def _session(sym, start_hm, end_hm, step_min, result="loss"):
    """Generate (sym, ts, result) rows every `step_min` from start→end ET."""
    sh, sm = start_hm
    eh, em = end_hm
    t = datetime(2026, 6, 15, sh, sm, tzinfo=ET)
    end = datetime(2026, 6, 15, eh, em, tzinfo=ET)
    rows = []
    while t <= end:
        rows.append((sym, _naive_utc(t), result))
        t += timedelta(minutes=step_min)
    return rows


class _FakeDB:
    def __init__(self, rows, already=None):
        self._rows = rows
        self._already = set(already or [])
        self.upserts = []

    def fetch_graded_for_archive(self, before):
        return list(self._rows)

    def summarized_pairs(self):
        return set(self._already)

    def upsert_daily_summary(self, row):
        self.upserts.append(row)


def test_full_session_is_complete():
    rows = _session("NDX", (9, 30), (15, 0), 10)   # 330-min span, 10-min steps
    db = _FakeDB(rows)
    n = archive_completed_days(db)
    assert n == 1
    r = db.upserts[0]
    assert r["session_date"] == DAY and r["symbol"] == "NDX"
    assert r["complete"] is True
    assert r["losses"] == len(rows) and r["wins"] == 0
    assert r["max_gap_min"] <= 15 and r["coverage_pct"] >= 0.75


def test_big_gap_marks_incomplete():
    # Two clusters with a ~4h hole in the middle (backend was down).
    rows = _session("NDX", (9, 30), (10, 0), 10) + _session("NDX", (14, 0), (15, 30), 10)
    db = _FakeDB(rows)
    archive_completed_days(db)
    r = db.upserts[0]
    assert r["max_gap_min"] > 15
    assert r["complete"] is False


def test_short_partial_day_marks_incomplete():
    rows = _session("NDX", (13, 0), (14, 0), 10)   # only 60-min span
    db = _FakeDB(rows)
    archive_completed_days(db)
    r = db.upserts[0]
    assert r["coverage_pct"] < 0.75
    assert r["complete"] is False


def test_already_summarized_days_are_skipped():
    rows = _session("NDX", (9, 30), (15, 0), 10)
    db = _FakeDB(rows, already=[(DAY, "NDX")])
    assert archive_completed_days(db) == 0
    assert db.upserts == []


def test_buckets_per_symbol_and_counts_results():
    rows = (_session("NDX", (9, 30), (15, 0), 10, "loss")
            + _session("SPX", (9, 30), (15, 0), 10, "win"))
    db = _FakeDB(rows)
    assert archive_completed_days(db) == 2
    by = {r["symbol"]: r for r in db.upserts}
    assert by["NDX"]["losses"] == by["NDX"]["n"] and by["NDX"]["accuracy_pct"] == 0.0
    assert by["SPX"]["wins"] == by["SPX"]["n"] and by["SPX"]["accuracy_pct"] == 100.0


def test_utc_iso_stamps_naive_datetimes():
    from app.db import _utc_iso
    assert _utc_iso(datetime(2026, 7, 1, 19, 59, 58)) == "2026-07-01T19:59:58Z"
    assert _utc_iso(None) is None
    assert _utc_iso("2026-07-01T19:59:58Z") == "2026-07-01T19:59:58Z"

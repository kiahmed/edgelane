"""Regression tests for tp's +N DTE syntax (parse + resolve_dte_expiration).

+N means "N trading days out" — resolved to the Nth REAL expiration in the
live chain, so weekends/holidays are skipped and the date is guaranteed
tradeable. Run:  python3 tools/test_tp_dte.py   (or via pytest)
"""
import datetime as _dt
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
sys.argv = ["tp"]
import tradier_positions as tp  # noqa: E402
import tradier_execute_ticket as tet  # noqa: E402


def test_parse_dte_attached_to_strike():
    a = tp.parse_args(["-B", "spy", "738c+1", "-x"])
    assert a["new_dte"] == 1 and a["new_strike_side"] == "738C"
    assert a["new_expiration"] is None  # resolved later, not at parse time


def test_parse_dte_separate_token():
    a = tp.parse_args(["-B", "spy", "738c", "+2", "-L", "1.20"])
    assert a["new_dte"] == 2 and a["new_strike_side"] == "738C"


def test_parse_explicit_date_still_works():
    a = tp.parse_args(["-B", "spy", "738c", "20260624", "-x"])
    assert a["new_dte"] is None and a["new_expiration"] == "2026-06-24"


def test_parse_no_date_defaults_today():
    a = tp.parse_args(["-B", "spy", "738c", "-x"])
    assert a["new_dte"] is None and a["new_expiration"] is not None


def test_parse_decimal_strike_with_dte():
    a = tp.parse_args(["-B", "spy", "738.5c+3"])
    assert a["new_dte"] == 3 and a["new_strike_side"] == "738.5C"


def _patch_chain(monkey_dates):
    """Freeze 'today' to Tue 2026-06-23 and stub the expirations endpoint."""
    tet.tradier_get = lambda p, par, t, b, timeout=20: (
        {"expirations": {"date": monkey_dates}} if par["symbol"] == "SPY" else {})

    class _FD(_dt.datetime):
        @classmethod
        def now(cls, *a, **k):
            return cls(2026, 6, 23, 10, 0, 0)

    tp.datetime = _FD


def test_resolve_skips_weekend_and_zero_dte():
    # Fri 26 missing (holiday); Sat/Sun never listed; 0DTE (06-23) excluded.
    _patch_chain(["2026-06-22", "2026-06-23", "2026-06-24", "2026-06-25",
                  "2026-06-29", "2026-06-30"])
    assert tp.resolve_dte_expiration("t", "b", "SPY", 1) == "2026-06-24"
    assert tp.resolve_dte_expiration("t", "b", "SPY", 2) == "2026-06-25"
    assert tp.resolve_dte_expiration("t", "b", "SPY", 3) == "2026-06-29"  # skips Sat/Sun


def test_resolve_over_limit_exits():
    _patch_chain(["2026-06-24", "2026-06-25"])
    try:
        tp.resolve_dte_expiration("t", "b", "SPY", 5)
        assert False, "expected SystemExit"
    except SystemExit:
        pass


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print("PASS", fn.__name__)
    print(f"\n{len(fns)} passed")

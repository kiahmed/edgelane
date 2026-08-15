"""Tests for the Simmer catalyst calendar (macro table + earnings cross-check).

No network. The Nasdaq/Benzinga/FRED clients are exercised through their pure
parse/diff helpers and through fake clients, never over the wire — a test suite
that reaches the internet is a test suite that fails on a plane and, worse, one
that would hammer an undocumented endpoint.
"""
from __future__ import annotations

import json
from datetime import date, timedelta

import pytest

from app import simmer_calendar as sc


# ── Helpers ────────────────────────────────────────────────────────────────

def make_table(valid_through: str, events=None, **extra) -> dict:
    doc = {
        "valid_through": valid_through,
        "events": events if events is not None else [
            {"date": "2026-09-11", "time_et": "08:30", "event": "CPI",
             "severity": "high", "confirmed": True},
            {"date": "2026-09-16", "time_et": "14:00", "event": "FOMC",
             "severity": "high", "confirmed": True, "sep": True,
             "start_date": "2026-09-15"},
        ],
    }
    doc.update(extra)
    return doc


def write_table(tmp_path, doc, name="macro_calendar.json"):
    p = tmp_path / name
    p.write_text(json.dumps(doc), encoding="utf-8")
    return p


def ed(symbol, d, *, source, confirmed=False, session=sc.SESSION_UNKNOWN):
    return sc.EarningsDate(symbol=symbol, date=date.fromisoformat(d),
                           session=session, confirmed=confirmed, source=source)


@pytest.fixture(autouse=True)
def _clear_calendar_cache(monkeypatch):
    """The module caches the loaded table; never let it leak between tests."""
    monkeypatch.delenv("SIMMER_MACRO_CALENDAR", raising=False)
    monkeypatch.delenv("SIMMER_MACRO_MIN_HORIZON_DAYS", raising=False)
    monkeypatch.delenv("SIMMER_EARNINGS_NASDAQ", raising=False)
    monkeypatch.delenv("SIMMER_BENZINGA_API_KEY", raising=False)
    monkeypatch.delenv("FRED_API_KEY", raising=False)
    sc.clear_cache()
    yield
    sc.clear_cache()


# ══════════════════════════════════════════════════════════════════════════
#  The shipped table
# ══════════════════════════════════════════════════════════════════════════

def test_shipped_macro_calendar_loads_and_parses():
    cal = sc.load_macro_calendar()
    assert cal.path is not None and cal.path.name == "macro_calendar.json"
    assert len(cal.events) > 50
    assert {e.event for e in cal.events} >= {"FOMC", "CPI", "PPI", "NFP", "PCE"}
    assert all(e.severity in sc.SEVERITY_RANK for e in cal.events)
    # events come back date-sorted
    assert [e.date for e in cal.events] == sorted(e.date for e in cal.events)


def test_shipped_table_fomc_dates_and_sep_flags():
    cal = sc.load_macro_calendar()
    fomc = {e.date.isoformat(): e for e in cal.events if e.event == "FOMC"}

    # 2026 remaining + all of 2027, as published by the Fed.
    for d in ("2026-09-16", "2026-10-28", "2026-12-09",
              "2027-01-27", "2027-03-17", "2027-04-28", "2027-06-09",
              "2027-07-28", "2027-09-15", "2027-10-27", "2027-12-08"):
        assert d in fomc, f"missing FOMC {d}"
        assert fomc[d].confirmed

    # 2026 already-held meetings are retained for backtesting.
    for d in ("2026-01-28", "2026-03-18", "2026-04-29", "2026-06-17", "2026-07-29"):
        assert d in fomc

    sep_days = {d for d, e in fomc.items() if e.sep}
    assert sep_days == {"2026-03-18", "2026-06-17", "2026-09-16", "2026-12-09",
                        "2027-03-17", "2027-06-09", "2027-09-15", "2027-12-08"}

    # Two-day meetings carry both days; the announcement is the second.
    sept = fomc["2026-09-16"]
    assert sept.start_date == date(2026, 9, 15)
    assert sept.window() == (date(2026, 9, 15), date(2026, 9, 16))
    assert sept.time_et == "14:00"


def test_shipped_table_marks_derived_rows_honestly():
    """Every unverified row must say so and must carry a blocking window."""
    cal = sc.load_macro_calendar()
    for e in cal.unconfirmed():
        assert e.derived is True, f"{e.event} {e.date} unconfirmed but not marked derived"
        assert e.derivation, f"{e.event} {e.date} has no derivation note"
        assert e.uncertainty_days > 0, f"{e.event} {e.date} derived but zero-width"
        start, end = e.window()
        assert start < e.date < end

    # ...and every confirmed row must name where it came from.
    for e in cal.events:
        if e.confirmed:
            assert e.source, f"{e.event} {e.date} confirmed with no source"

    assert cal.confirmed_through is not None
    assert cal.confirmed_through <= cal.valid_through


def test_shipped_table_bls_2026_dates_are_the_published_ones():
    cal = sc.load_macro_calendar()
    by_event = {}
    for e in cal.events:
        if e.date.year == 2026 and e.confirmed:
            by_event.setdefault(e.event, set()).add(e.date.isoformat())

    assert {"2026-08-12", "2026-09-11", "2026-10-14", "2026-11-10",
            "2026-12-10"} <= by_event["CPI"]
    assert {"2026-09-04", "2026-10-02", "2026-11-06",
            "2026-12-04"} <= by_event["NFP"]
    assert {"2026-08-13", "2026-09-10", "2026-10-15", "2026-11-13",
            "2026-12-15"} <= by_event["PPI"]
    assert {"2026-08-26", "2026-09-30", "2026-10-29", "2026-11-25",
            "2026-12-23"} <= by_event["PCE"]


# ══════════════════════════════════════════════════════════════════════════
#  Parsing and validation
# ══════════════════════════════════════════════════════════════════════════

def test_parse_rejects_missing_valid_through():
    with pytest.raises(sc.MalformedMacroCalendarError, match="valid_through"):
        sc.parse_macro_calendar({"events": [
            {"date": "2026-09-11", "event": "CPI", "severity": "high"}]})


def test_parse_rejects_empty_or_missing_events():
    with pytest.raises(sc.MalformedMacroCalendarError, match="events"):
        sc.parse_macro_calendar({"valid_through": "2027-12-31", "events": []})


def test_parse_rejects_bad_date_and_bad_severity():
    with pytest.raises(sc.MalformedMacroCalendarError, match="bad date"):
        sc.parse_macro_calendar(make_table("2027-12-31", [
            {"date": "not-a-date", "event": "CPI", "severity": "high"}]))
    with pytest.raises(sc.MalformedMacroCalendarError, match="severity"):
        sc.parse_macro_calendar(make_table("2027-12-31", [
            {"date": "2026-09-11", "event": "CPI", "severity": "catastrophic"}]))


def test_load_from_explicit_path_does_not_poison_the_cache(tmp_path):
    p = write_table(tmp_path, make_table("2099-01-01"))
    cal = sc.load_macro_calendar(p)
    assert cal.valid_through == date(2099, 1, 1)
    # The shipped table is still what the cached loader returns.
    assert sc.load_macro_calendar().valid_through != date(2099, 1, 1)


def test_env_override_path_is_honoured(tmp_path, monkeypatch):
    p = write_table(tmp_path, make_table("2099-01-01"), name="macro_calendar.json")
    monkeypatch.setenv("SIMMER_MACRO_CALENDAR", str(p))
    sc.clear_cache()
    assert sc.load_macro_calendar().valid_through == date(2099, 1, 1)


# ══════════════════════════════════════════════════════════════════════════
#  The valid_through freshness assertion — the fail-loud guard
# ══════════════════════════════════════════════════════════════════════════

def test_fresh_table_passes_the_startup_assertion(tmp_path, monkeypatch):
    far = date.today() + timedelta(days=400)
    p = write_table(tmp_path, make_table(far.isoformat()))
    monkeypatch.setenv("SIMMER_MACRO_CALENDAR", str(p))
    sc.clear_cache()
    cal = sc.assert_macro_table_fresh()
    assert cal.horizon_days() >= sc.DEFAULT_MIN_HORIZON_DAYS


def test_stale_table_fires_the_assertion(tmp_path, monkeypatch):
    """89 days out is stale; the table must refuse rather than fail open."""
    near = date.today() + timedelta(days=89)
    p = write_table(tmp_path, make_table(near.isoformat()))
    monkeypatch.setenv("SIMMER_MACRO_CALENDAR", str(p))
    sc.clear_cache()
    with pytest.raises(sc.StaleMacroCalendarError) as exc:
        sc.assert_macro_table_fresh()
    msg = str(exc.value)
    assert "stale" in msg and near.isoformat() in msg and "90-day" in msg


def test_expired_table_fires_the_assertion(tmp_path, monkeypatch):
    past = date.today() - timedelta(days=5)
    p = write_table(tmp_path, make_table(past.isoformat()))
    monkeypatch.setenv("SIMMER_MACRO_CALENDAR", str(p))
    sc.clear_cache()
    with pytest.raises(sc.StaleMacroCalendarError):
        sc.assert_macro_table_fresh()


def test_freshness_boundary_is_exactly_90_days(tmp_path, monkeypatch):
    p90 = write_table(tmp_path, make_table((date.today() + timedelta(days=90)).isoformat()))
    monkeypatch.setenv("SIMMER_MACRO_CALENDAR", str(p90))
    sc.clear_cache()
    sc.assert_macro_table_fresh()          # 90 is fine

    p89 = write_table(tmp_path, make_table((date.today() + timedelta(days=89)).isoformat()),
                      name="near.json")
    monkeypatch.setenv("SIMMER_MACRO_CALENDAR", str(p89))
    sc.clear_cache()
    with pytest.raises(sc.StaleMacroCalendarError):
        sc.assert_macro_table_fresh()


def test_horizon_is_configurable_via_env(tmp_path, monkeypatch):
    p = write_table(tmp_path, make_table((date.today() + timedelta(days=100)).isoformat()))
    monkeypatch.setenv("SIMMER_MACRO_CALENDAR", str(p))
    monkeypatch.setenv("SIMMER_MACRO_MIN_HORIZON_DAYS", "365")
    sc.clear_cache()
    with pytest.raises(sc.StaleMacroCalendarError, match="365-day"):
        sc.assert_macro_table_fresh()


def test_assert_fresh_accepts_an_injected_now(tmp_path):
    cal = sc.parse_macro_calendar(make_table("2027-12-31"))
    cal.assert_fresh(now=date(2027, 1, 1))                 # 364 days out
    with pytest.raises(sc.StaleMacroCalendarError):
        cal.assert_fresh(now=date(2027, 11, 1))            # 60 days out


def test_macro_events_between_raises_on_a_stale_table_rather_than_returning_empty(
        tmp_path, monkeypatch):
    """The whole point: an empty list from a dead table is the fail-open."""
    p = write_table(tmp_path, make_table((date.today() + timedelta(days=10)).isoformat()))
    monkeypatch.setenv("SIMMER_MACRO_CALENDAR", str(p))
    sc.clear_cache()
    with pytest.raises(sc.StaleMacroCalendarError):
        sc.macro_events_between(date.today(), date.today() + timedelta(days=5))
    # Diagnostics may opt out, but must do so explicitly.
    assert sc.macro_events_between(date(2026, 9, 1), date(2026, 9, 30),
                                   strict=False) is not None


# ══════════════════════════════════════════════════════════════════════════
#  refuse_beyond_table
# ══════════════════════════════════════════════════════════════════════════

def test_refuse_beyond_table_allows_expiries_inside_the_table():
    cal = sc.parse_macro_calendar(make_table("2027-12-31"))
    assert cal.refuse_beyond_table(date(2027, 12, 31)) is None    # boundary inclusive
    assert cal.refuse_beyond_table(date(2026, 10, 16)) is None
    assert sc.refuse_beyond_table(date(2027, 6, 18), calendar=cal) is None


def test_refuse_beyond_table_refuses_past_the_last_date():
    cal = sc.parse_macro_calendar(make_table("2027-12-31"))
    reason = cal.refuse_beyond_table(date(2028, 1, 21))
    assert reason
    assert "2028-01-21" in reason and "2027-12-31" in reason
    assert "refusing" in reason.lower()
    assert sc.refuse_beyond_table(date(2028, 1, 21), calendar=cal) == reason


def test_refuse_beyond_table_uses_the_shipped_table_by_default():
    cal = sc.load_macro_calendar()
    one_day_past = cal.valid_through + timedelta(days=1)
    assert sc.refuse_beyond_table(one_day_past)
    assert sc.refuse_beyond_table(cal.valid_through) is None


# ══════════════════════════════════════════════════════════════════════════
#  macro_events_between
# ══════════════════════════════════════════════════════════════════════════

def test_macro_events_between_finds_what_falls_inside_a_tenor():
    cal = sc.load_macro_calendar()
    hits = cal.between(date(2026, 9, 1), date(2026, 9, 30))
    names = {e.event for e in hits}
    assert {"CPI", "FOMC", "NFP"} <= names
    assert all(e.overlaps(date(2026, 9, 1), date(2026, 9, 30)) for e in hits)


def test_macro_events_between_is_inclusive_at_both_ends():
    cal = sc.parse_macro_calendar(make_table("2027-12-31"))
    assert [e.event for e in cal.between(date(2026, 9, 11), date(2026, 9, 11))] == ["CPI"]
    # FOMC starts 09-15, so a tenor ending 09-15 still catches it.
    assert any(e.event == "FOMC" for e in cal.between(date(2026, 9, 12), date(2026, 9, 15)))


def test_quiet_tenor_returns_nothing():
    cal = sc.parse_macro_calendar(make_table("2027-12-31"))
    assert cal.between(date(2026, 9, 17), date(2026, 9, 30)) == []


def test_severity_and_event_filters():
    cal = sc.parse_macro_calendar(make_table("2027-12-31", [
        {"date": "2026-09-10", "event": "PPI", "severity": "medium", "confirmed": True},
        {"date": "2026-09-11", "event": "CPI", "severity": "high", "confirmed": True},
    ]))
    span = (date(2026, 9, 1), date(2026, 9, 30))
    assert [e.event for e in cal.between(*span, min_severity="high")] == ["CPI"]
    assert [e.event for e in cal.between(*span, min_severity="low")] == ["PPI", "CPI"]
    assert [e.event for e in cal.between(*span, events=["ppi"])] == ["PPI"]


def test_unconfirmed_macro_row_blocks_a_widened_window():
    """A derived date is a distribution: the window, not the day, is the gate."""
    cal = sc.parse_macro_calendar(make_table("2027-12-31", [
        {"date": "2027-09-13", "event": "CPI", "severity": "high",
         "confirmed": False, "derived": True, "uncertainty_days": 3,
         "derivation": "8th business day"},
    ]))
    (e,) = cal.events
    assert e.window() == (date(2027, 9, 10), date(2027, 9, 16))
    # A tenor that misses the nominal date but lands inside the uncertainty
    # window is still blocked.
    assert cal.between(date(2027, 9, 15), date(2027, 9, 17))
    assert cal.between(date(2027, 9, 17), date(2027, 9, 20)) == []


def test_between_normalises_reversed_ranges():
    cal = sc.parse_macro_calendar(make_table("2027-12-31"))
    assert cal.between(date(2026, 9, 30), date(2026, 9, 1))


# ══════════════════════════════════════════════════════════════════════════
#  Earnings cross-check — the core design rule
# ══════════════════════════════════════════════════════════════════════════

def test_two_sources_disagreeing_blocks_the_whole_week():
    """Nasdaq says Tuesday, Benzinga says Thursday -> block Mon..Fri."""
    out = sc.resolve_earnings_blackout("NVDA", [
        ed("NVDA", "2026-11-17", source="nasdaq"),
        ed("NVDA", "2026-11-19", source="benzinga", confirmed=True),
    ])
    assert out is not None
    assert out.precision == "week"
    assert (out.start, out.end) == (date(2026, 11, 16), date(2026, 11, 20))
    assert out.confirmed is False
    assert "disagree" in out.reason
    assert out.dates == (date(2026, 11, 17), date(2026, 11, 19))
    assert out.sources == ("benzinga", "nasdaq")


def test_disagreement_spanning_two_weeks_blocks_both_weeks():
    out = sc.resolve_earnings_blackout("AAPL", [
        ed("AAPL", "2026-10-29", source="nasdaq"),
        ed("AAPL", "2026-11-03", source="benzinga"),
    ])
    assert (out.start, out.end) == (date(2026, 10, 26), date(2026, 11, 6))
    assert out.precision == "week"


def test_unconfirmed_date_blocks_the_whole_week_even_when_sources_agree():
    """Agreement between two estimates is agreement between two guesses."""
    out = sc.resolve_earnings_blackout("MSFT", [
        ed("MSFT", "2026-10-27", source="nasdaq"),
        ed("MSFT", "2026-10-27", source="finnhub"),
    ])
    assert out.precision == "week"
    assert (out.start, out.end) == (date(2026, 10, 26), date(2026, 10, 30))
    assert out.confirmed is False
    assert "unconfirmed" in out.reason


def test_single_source_even_if_confirmed_still_blocks_the_week():
    out = sc.resolve_earnings_blackout("TSLA", [
        ed("TSLA", "2026-10-21", source="benzinga", confirmed=True,
           session=sc.SESSION_POST),
    ])
    assert out.precision == "week"
    assert "cross-check" in out.reason


def test_two_confirming_sources_narrow_to_a_single_day():
    out = sc.resolve_earnings_blackout("NVDA", [
        ed("NVDA", "2026-11-18", source="benzinga", confirmed=True,
           session=sc.SESSION_POST),
        ed("NVDA", "2026-11-18", source="wsh", confirmed=True,
           session=sc.SESSION_POST),
    ])
    assert out.precision == "day"
    assert out.confirmed is True
    # An after-hours print moves the NEXT session, so the block runs a day long.
    assert (out.start, out.end) == (date(2026, 11, 18), date(2026, 11, 19))


def test_confirmed_pre_market_report_blocks_only_that_day():
    out = sc.resolve_earnings_blackout("JPM", [
        ed("JPM", "2026-10-13", source="benzinga", confirmed=True,
           session=sc.SESSION_PRE),
        ed("JPM", "2026-10-13", source="wsh", confirmed=True, session=sc.SESSION_PRE),
    ])
    assert (out.start, out.end) == (date(2026, 10, 13), date(2026, 10, 13))
    assert out.precision == "day"


def test_no_candidates_returns_none_and_other_symbols_are_ignored():
    assert sc.resolve_earnings_blackout("NVDA", []) is None
    assert sc.resolve_earnings_blackout("NVDA", [
        ed("AMD", "2026-11-04", source="nasdaq")]) is None


def test_symbol_matching_is_case_insensitive():
    out = sc.resolve_earnings_blackout("nvda", [
        ed("NVDA", "2026-11-17", source="nasdaq"),
        ed("nvda", "2026-11-19", source="benzinga"),
    ])
    assert out.symbol == "NVDA" and out.precision == "week"


def test_blackout_covers_and_blocks_tenor():
    out = sc.resolve_earnings_blackout("NVDA", [
        ed("NVDA", "2026-11-17", source="nasdaq"),
        ed("NVDA", "2026-11-19", source="benzinga"),
    ])
    assert out.covers(date(2026, 11, 18))
    assert not out.covers(date(2026, 11, 23))
    assert sc.blocks_tenor(out, date(2026, 11, 20), date(2026, 12, 18))
    assert not sc.blocks_tenor(out, date(2026, 11, 23), date(2026, 12, 18))
    assert not sc.blocks_tenor(None, date(2026, 11, 16), date(2026, 11, 20))


def test_trading_week_is_monday_to_friday():
    assert sc.trading_week(date(2026, 11, 18)) == (date(2026, 11, 16), date(2026, 11, 20))
    assert sc.trading_week(date(2026, 11, 21)) == (date(2026, 11, 16), date(2026, 11, 20))


def test_coverage_requires_two_answering_sources():
    """A failed fetch is None (unknown), never [] (all clear)."""
    assert sc.earnings_coverage_ok({"nasdaq": [], "benzinga": []}) is True
    assert sc.earnings_coverage_ok({"nasdaq": [], "benzinga": None}) is False
    assert sc.earnings_coverage_ok({"nasdaq": None, "benzinga": None}) is False


# ══════════════════════════════════════════════════════════════════════════
#  Earnings clients — parsing and the config gate, no network
# ══════════════════════════════════════════════════════════════════════════

NASDAQ_FIXTURE = {
    "data": {
        "asOf": "Tue, Aug 18, 2026",
        "rows": [
            {"symbol": "HD", "name": "Home Depot, Inc. (The)", "time": "time-pre-market"},
            {"symbol": "BHP", "name": "BHP Group Limited", "time": "time-not-supplied"},
            {"symbol": "KEYS", "name": "Keysight Technologies Inc.",
             "time": "time-after-hours"},
        ],
    },
    "status": {"rCode": 200},
}


def test_nasdaq_parses_all_three_time_buckets():
    rows = sc.NasdaqEarningsClient.parse_day(NASDAQ_FIXTURE, date(2026, 8, 18))
    by = {r.symbol: r for r in rows}
    assert by["HD"].session == sc.SESSION_PRE
    assert by["KEYS"].session == sc.SESSION_POST
    assert by["BHP"].session == sc.SESSION_UNKNOWN
    assert all(r.date == date(2026, 8, 18) for r in rows)
    assert all(r.source == "nasdaq" for r in rows)


def test_nasdaq_never_reports_confirmed():
    """It exposes no confirmed flag, so it can only ever widen a blackout."""
    rows = sc.NasdaqEarningsClient.parse_day(NASDAQ_FIXTURE, date(2026, 8, 18))
    assert not any(r.confirmed for r in rows)


def test_nasdaq_handles_empty_and_null_rows():
    assert sc.NasdaqEarningsClient.parse_day({"data": {"rows": None}}, date(2026, 8, 18)) == []
    assert sc.NasdaqEarningsClient.parse_day({}, date(2026, 8, 18)) == []
    assert sc.NasdaqEarningsClient.parse_day(None, date(2026, 8, 18)) == []


def test_nasdaq_is_disabled_by_default_and_returns_unknown_not_empty():
    assert sc.NasdaqEarningsClient().enabled is False


def test_nasdaq_enabled_by_env_flag(monkeypatch):
    monkeypatch.setenv("SIMMER_EARNINGS_NASDAQ", "true")
    assert sc.NasdaqEarningsClient().enabled is True
    monkeypatch.setenv("SIMMER_EARNINGS_NASDAQ", "0")
    assert sc.NasdaqEarningsClient().enabled is False


async def test_disabled_nasdaq_client_makes_no_call_and_reports_unknown():
    client = sc.NasdaqEarningsClient(enabled=False)
    assert await client.fetch_day(date(2026, 8, 18)) is None
    assert await client.fetch_symbol("HD", date(2026, 8, 17), date(2026, 8, 21)) is None


def test_benzinga_parses_date_status_and_time():
    rows = sc.BenzingaEarningsClient.parse({"earnings": [
        {"ticker": "NVDA", "date": "2026-11-18", "date_status": "confirmed",
         "time": "16:20:00"},
        {"ticker": "AMD", "date": "2026-11-03", "date_status": "projected",
         "time": "07:00:00"},
    ]})
    by = {r.symbol: r for r in rows}
    assert by["NVDA"].confirmed is True and by["NVDA"].session == sc.SESSION_POST
    assert by["AMD"].confirmed is False and by["AMD"].session == sc.SESSION_PRE
    assert all(r.source == "benzinga" for r in rows)


def test_benzinga_skips_unusable_rows():
    rows = sc.BenzingaEarningsClient.parse([
        {"ticker": "", "date": "2026-11-18"},
        {"ticker": "X", "date": None},
        {"ticker": "Y", "date": "garbage"},
        "not-a-dict",
        {"symbol": "Z", "date": "2026-11-18T00:00:00", "date_status": "confirmed"},
    ])
    assert [r.symbol for r in rows] == ["Z"]
    assert rows[0].date == date(2026, 11, 18)


def test_benzinga_is_disabled_without_a_key(monkeypatch):
    assert sc.BenzingaEarningsClient(api_key="").enabled is False
    assert sc.BenzingaEarningsClient(api_key="k").enabled is True
    monkeypatch.setenv("SIMMER_BENZINGA_API_KEY", "from-env")
    assert sc.BenzingaEarningsClient().enabled is True


async def test_disabled_benzinga_client_makes_no_call():
    client = sc.BenzingaEarningsClient(api_key="")
    assert await client.fetch_symbol("NVDA", date(2026, 11, 1), date(2026, 12, 1)) is None


class FakeSource:
    """Stand-in earnings source. `rows=None` models a failed/disabled fetch."""

    def __init__(self, name, rows, boom=False):
        self.name = name
        self._rows = rows
        self._boom = boom
        self.calls = 0

    async def fetch_symbol(self, symbol, start, end):
        self.calls += 1
        if self._boom:
            raise RuntimeError("vendor exploded")
        return self._rows


async def test_earnings_blackout_for_cross_checks_two_fakes():
    blackout, coverage = await sc.earnings_blackout_for(
        "NVDA", date(2026, 11, 1), date(2026, 12, 1),
        clients=[FakeSource("nasdaq", [ed("NVDA", "2026-11-17", source="nasdaq")]),
                 FakeSource("benzinga", [ed("NVDA", "2026-11-19", source="benzinga",
                                            confirmed=True)])])
    assert coverage is True
    assert blackout.precision == "week"
    assert (blackout.start, blackout.end) == (date(2026, 11, 16), date(2026, 11, 20))


async def test_earnings_blackout_for_reports_lost_coverage_when_a_source_dies():
    good = FakeSource("benzinga", [ed("NVDA", "2026-11-18", source="benzinga",
                                      confirmed=True)])
    blackout, coverage = await sc.earnings_blackout_for(
        "NVDA", date(2026, 11, 1), date(2026, 12, 1),
        clients=[FakeSource("nasdaq", None, boom=True), good])
    assert coverage is False           # only one source answered
    assert blackout.precision == "week"
    assert good.calls == 1             # a raising vendor never kills the sweep


async def test_earnings_blackout_for_with_no_sources_enabled():
    blackout, coverage = await sc.earnings_blackout_for(
        "NVDA", date(2026, 11, 1), date(2026, 12, 1),
        clients=[FakeSource("nasdaq", None), FakeSource("benzinga", None)])
    assert blackout is None and coverage is False


# ══════════════════════════════════════════════════════════════════════════
#  FRED reconciler — advisory only, and the realtime-window gotcha
# ══════════════════════════════════════════════════════════════════════════

def test_fred_is_disabled_without_a_key(monkeypatch):
    assert sc.FredReconciler(api_key="").enabled is False
    monkeypatch.setenv("FRED_API_KEY", "abc")
    assert sc.FredReconciler().enabled is True


async def test_disabled_fred_reconciler_is_inert():
    r = sc.FredReconciler(api_key="")
    assert await r.discover_release_ids() == {}
    assert await r.release_dates(10, date(2026, 1, 1), date(2027, 1, 1)) == []
    assert await r.reconcile(sc.parse_macro_calendar(make_table("2027-12-31"))) == []


async def test_fred_release_ids_are_discovered_not_guessed(monkeypatch):
    """Hardcoding a release id silently reconciles against the wrong release."""
    r = sc.FredReconciler(api_key="k")
    seen = {}

    async def fake_get(path, params):
        seen[path] = params
        return {"releases": [
            {"id": 9, "name": "Something Else"},
            {"id": 10, "name": "Consumer Price Index"},
            {"id": 46, "name": "Producer Price Index"},
            {"id": 50, "name": "Employment Situation"},
            {"id": 54, "name": "Personal Income and Outlays"},
        ]}

    monkeypatch.setattr(r, "_get", fake_get)
    ids = await r.discover_release_ids()
    assert ids == {"CPI": 10, "PPI": 46, "NFP": 50, "PCE": 54}
    assert seen["/releases"]["limit"] == 1000
    # cached: a second call does not re-enumerate
    seen.clear()
    assert await r.discover_release_ids() == ids
    assert seen == {}


async def test_fred_release_dates_sets_the_realtime_window_for_future_dates(monkeypatch):
    """The endpoint defaults realtime_end to today, which excludes the future."""
    r = sc.FredReconciler(api_key="k")
    captured = {}

    async def fake_get(path, params):
        captured.update(params)
        return {"release_dates": [
            {"release_id": 10, "date": "2026-09-11"},
            {"release_id": 10, "date": "2026-10-14"},
            {"release_id": 10, "date": "2028-01-13"},   # outside the range
        ]}

    monkeypatch.setattr(r, "_get", fake_get)
    got = await r.release_dates(10, date(2026, 9, 1), date(2026, 12, 31))

    assert captured["realtime_end"] == "9999-12-31"
    assert captured["include_release_dates_with_no_data"] == "true"
    assert got == [date(2026, 9, 11), date(2026, 10, 14)]


async def test_fred_reconcile_logs_drift_but_never_mutates_the_table(monkeypatch):
    cal = sc.parse_macro_calendar(make_table("2027-12-31", [
        {"date": "2026-09-11", "event": "CPI", "severity": "high", "confirmed": True},
        {"date": "2026-10-14", "event": "CPI", "severity": "high", "confirmed": True},
    ]))
    before = [e.date for e in cal.events]

    r = sc.FredReconciler(api_key="k")

    async def fake_ids():
        return {"CPI": 10}

    async def fake_dates(release_id, start, end):
        return [date(2026, 9, 11), date(2026, 10, 15)]   # one shifted by a day

    monkeypatch.setattr(r, "discover_release_ids", fake_ids)
    monkeypatch.setattr(r, "release_dates", fake_dates)

    drift = await r.reconcile(cal, start=date(2026, 9, 1), end=date(2026, 12, 31))
    assert [d.to_dict() for d in drift] == [
        {"event": "CPI", "table_date": "2026-10-14", "fred_date": "2026-10-15",
         "kind": "shifted"}]
    assert [e.date for e in cal.events] == before     # advisory only


async def test_fred_reconcile_survives_a_fred_outage(monkeypatch):
    cal = sc.parse_macro_calendar(make_table("2027-12-31"))
    r = sc.FredReconciler(api_key="k")

    async def fake_ids():
        return {"CPI": 10}

    async def boom(release_id, start, end):
        raise RuntimeError("fred down")

    monkeypatch.setattr(r, "discover_release_ids", fake_ids)
    monkeypatch.setattr(r, "release_dates", boom)
    assert await r.reconcile(cal, start=date(2026, 9, 1), end=date(2026, 12, 31)) == []


def test_diff_dates_reports_both_directions():
    drift = sc._diff_dates("CPI",
                           [date(2026, 9, 11), date(2026, 10, 14)],
                           [date(2026, 9, 11), date(2027, 5, 12)],
                           tolerance_days=0)
    kinds = {(d.kind, d.event) for d in drift}
    assert ("missing_in_fred", "CPI") in kinds
    assert ("missing_in_table", "CPI") in kinds

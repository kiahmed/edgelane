"""Simmer catalyst calendar — the macro release table and the earnings gate.

Two independent layers live here because they answer the same question ("is
there an unpriced binary event inside this tenor?") from opposite ends of the
reliability spectrum:

  MACRO   A hardcoded table (`macro_calendar.json`). Deliberate, not lazy: no
          free machine-readable source exists (the Fed publishes no ICS/JSON
          feed; bls.gov 403s scripted requests under an explicit anti-bot
          policy, and api.bls.gov serves *data*, not the schedule). The dates
          essentially never move and forward visibility is enormous, so a
          hand-refreshed table beats every scrapeable alternative. See
          docs/simmer.md → "Macro calendar — a hardcoded table is the correct
          choice".

  EARNINGS Vendor calendars, none of which is trustworthy alone. ~35-45% of
          forward earnings dates in any calendar are model estimates rather
          than company-announced facts, and the cheap vendors do not tell you
          which is which. So: cross-check >=2 sources and, when they disagree
          or the date is unconfirmed, block the ENTIRE WEEK. An unconfirmed
          earnings date is a *distribution*, not a date.

THE ASYMMETRY THAT SHAPES THIS MODULE. Every failure here fails *open*: a stale
macro table, a vendor that returns nothing, a network timeout — each one
silently reports "no catalyst" and lets a spread be held through a CPI print.
Silently returning an empty list is therefore the single most dangerous thing
this module could do. So it raises instead:

  * `assert_macro_table_fresh()` refuses to start when `valid_through` is less
    than 90 days out (`StaleMacroCalendarError`).
  * `macro_events_between()` re-runs that assertion by default, so an expired
    table cannot quietly answer "nothing scheduled".
  * `refuse_beyond_table()` declines any expiry past the table's last date.

Environment (all optional; every one of them is off by default):

  SIMMER_MACRO_CALENDAR          path override for macro_calendar.json
  SIMMER_MACRO_MIN_HORIZON_DAYS  freshness horizon, default 90
  SIMMER_EARNINGS_NASDAQ         "1"/"true" to enable the free Nasdaq source
  SIMMER_BENZINGA_API_KEY        enables the Benzinga-via-Massive client
  SIMMER_BENZINGA_BASE_URL       default https://api.massive.dev
  FRED_API_KEY                   enables the optional FRED reconciler
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Iterable, Sequence

log = logging.getLogger("edgelane.simmer.calendar")

# ── Constants ──────────────────────────────────────────────────────────────

#: A macro table whose `valid_through` is nearer than this refuses to load.
#: 90 days comfortably covers the longest tenor Simmer will score, so the table
#: can never run out underneath a live position.
DEFAULT_MIN_HORIZON_DAYS = 90

SEVERITY_RANK = {"low": 0, "medium": 1, "high": 2}

_CALENDAR_FILENAME = "macro_calendar.json"


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _today() -> date:
    return date.today()


# ── Errors ─────────────────────────────────────────────────────────────────


class MacroCalendarError(RuntimeError):
    """Base class for every way the macro table can refuse to answer."""


class StaleMacroCalendarError(MacroCalendarError):
    """`valid_through` is inside the minimum horizon (or already past).

    Raised loudly and early on purpose: the alternative is a service that keeps
    running and reports "no catalyst in this tenor" because its table simply
    stops before the tenor does.
    """


class MalformedMacroCalendarError(MacroCalendarError):
    """The JSON is missing required fields or has unparseable dates."""


# ── Macro events ───────────────────────────────────────────────────────────


@dataclass(frozen=True)
class MacroEvent:
    """One scheduled macro release.

    `confirmed` is the load-bearing field. A confirmed event was read off the
    issuing agency's own schedule page; an unconfirmed one was derived from the
    historical publication rule and is therefore a distribution centred on
    `date` with half-width `uncertainty_days`. `window()` returns that
    distribution as a concrete blocking range so callers never have to
    remember the difference.
    """

    date: date
    time_et: str
    event: str
    severity: str
    sep: bool = False
    confirmed: bool = True
    derived: bool = False
    uncertainty_days: int = 0
    start_date: date | None = None
    reference_period: str | None = None
    derivation: str | None = None
    source: str | None = None
    note: str | None = None

    @property
    def severity_rank(self) -> int:
        return SEVERITY_RANK.get(self.severity, 0)

    def window(self) -> tuple[date, date]:
        """Inclusive [start, end] this event should block.

        Confirmed multi-day events (FOMC) span `start_date`..`date`. Unconfirmed
        events widen by `uncertainty_days` on each side — blocking a range we
        are sure contains the print beats naming a day we are not sure about.
        """
        start = self.start_date or self.date
        end = self.date
        pad = max(0, int(self.uncertainty_days or 0))
        if pad:
            start -= timedelta(days=pad)
            end += timedelta(days=pad)
        return start, end

    def overlaps(self, start: date, end: date) -> bool:
        w_start, w_end = self.window()
        return w_start <= end and w_end >= start

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "date": self.date.isoformat(),
            "time_et": self.time_et,
            "event": self.event,
            "severity": self.severity,
            "confirmed": self.confirmed,
        }
        if self.sep:
            out["sep"] = True
        if self.derived:
            out["derived"] = True
        if self.uncertainty_days:
            out["uncertainty_days"] = self.uncertainty_days
        if self.start_date:
            out["start_date"] = self.start_date.isoformat()
        for k in ("reference_period", "derivation", "source", "note"):
            v = getattr(self, k)
            if v:
                out[k] = v
        w_start, w_end = self.window()
        out["window"] = [w_start.isoformat(), w_end.isoformat()]
        return out


def _parse_date(raw: Any, ctx: str) -> date:
    if isinstance(raw, date):
        return raw
    try:
        return date.fromisoformat(str(raw))
    except Exception as exc:  # noqa: BLE001 - re-raised with context
        raise MalformedMacroCalendarError(f"{ctx}: bad date {raw!r}") from exc


@dataclass(frozen=True)
class MacroCalendar:
    """The parsed table. Immutable; reload by calling `load_macro_calendar`."""

    valid_through: date
    events: tuple[MacroEvent, ...]
    confirmed_through: date | None = None
    generated_at: date | None = None
    path: Path | None = None
    sources: dict[str, str] = field(default_factory=dict)

    # ---- freshness -------------------------------------------------------

    def horizon_days(self, now: date | None = None) -> int:
        return (self.valid_through - (now or _today())).days

    def assert_fresh(self, now: date | None = None,
                     min_horizon_days: int = DEFAULT_MIN_HORIZON_DAYS) -> None:
        """Raise `StaleMacroCalendarError` unless `valid_through` is far enough out.

        One assertion turns a silent wrong answer into a visible refusal.
        """
        horizon = self.horizon_days(now)
        if horizon < min_horizon_days:
            where = f" ({self.path})" if self.path else ""
            raise StaleMacroCalendarError(
                f"macro calendar{where} is stale: valid_through="
                f"{self.valid_through.isoformat()} is {horizon} day(s) out, "
                f"below the {min_horizon_days}-day minimum. Refresh "
                f"{_CALENDAR_FILENAME} from federalreserve.gov / bls.gov / bea.gov. "
                "Refusing rather than reporting 'no catalyst' for tenors the "
                "table no longer covers."
            )

    # ---- queries ---------------------------------------------------------

    def between(self, start: date, end: date, *,
                min_severity: str | None = None,
                events: Iterable[str] | None = None) -> list[MacroEvent]:
        if start > end:
            start, end = end, start
        floor = SEVERITY_RANK.get((min_severity or "low").lower(), 0)
        wanted = {e.upper() for e in events} if events else None
        out = [e for e in self.events
               if e.overlaps(start, end)
               and e.severity_rank >= floor
               and (wanted is None or e.event.upper() in wanted)]
        out.sort(key=lambda e: (e.date, e.time_et, e.event))
        return out

    def covers(self, day: date) -> bool:
        return day <= self.valid_through

    def refuse_beyond_table(self, expiry: date) -> str | None:
        """None when `expiry` is scorable; a human-readable reason when it is not.

        Truthy return == refuse. The engine must not score a candidate whose
        expiry falls past the table's last date: beyond it we cannot tell an
        empty catalyst list from an unknown one.
        """
        if self.covers(expiry):
            return None
        return (
            f"expiry {expiry.isoformat()} is beyond the macro calendar's "
            f"valid_through {self.valid_through.isoformat()} — refusing to score "
            "(an empty catalyst list past the table's end is unknown, not clear)"
        )

    # ---- introspection ---------------------------------------------------

    @property
    def last_event_date(self) -> date | None:
        return max((e.date for e in self.events), default=None)

    def unconfirmed(self) -> list[MacroEvent]:
        return [e for e in self.events if not e.confirmed]

    def summary(self) -> dict[str, Any]:
        return {
            "path": str(self.path) if self.path else None,
            "valid_through": self.valid_through.isoformat(),
            "confirmed_through": (self.confirmed_through.isoformat()
                                  if self.confirmed_through else None),
            "generated_at": (self.generated_at.isoformat()
                             if self.generated_at else None),
            "horizon_days": self.horizon_days(),
            "events": len(self.events),
            "unconfirmed": len(self.unconfirmed()),
            "last_event_date": (self.last_event_date.isoformat()
                                if self.last_event_date else None),
        }


# ── Loading ────────────────────────────────────────────────────────────────


def _candidate_paths() -> list[Path]:
    """Same discovery shape as torque_config: env override, then walk up.

    Walking the parents keeps this working regardless of how deep the package
    is mounted (the container flattens `market/backend/app` to `/srv/app`).
    """
    env = os.environ.get("SIMMER_MACRO_CALENDAR")
    out: list[Path] = [Path(env)] if env else []
    here = Path(__file__).resolve()
    out.append(here.parent / _CALENDAR_FILENAME)
    for parent in here.parents:
        out.append(parent / _CALENDAR_FILENAME)
    return out


def parse_macro_calendar(doc: dict[str, Any], *, path: Path | None = None) -> MacroCalendar:
    """Parse an already-loaded JSON document. Kept separate so tests can build
    tables in memory without touching the filesystem."""
    if not isinstance(doc, dict):
        raise MalformedMacroCalendarError("macro calendar root must be an object")
    if "valid_through" not in doc:
        raise MalformedMacroCalendarError("macro calendar is missing `valid_through`")
    valid_through = _parse_date(doc["valid_through"], "valid_through")

    raw_events = doc.get("events")
    if not isinstance(raw_events, list) or not raw_events:
        raise MalformedMacroCalendarError("macro calendar has no `events`")

    events: list[MacroEvent] = []
    for i, row in enumerate(raw_events):
        if not isinstance(row, dict):
            raise MalformedMacroCalendarError(f"events[{i}] is not an object")
        ctx = f"events[{i}]"
        for required in ("date", "event", "severity"):
            if required not in row:
                raise MalformedMacroCalendarError(f"{ctx} is missing `{required}`")
        sev = str(row["severity"]).lower()
        if sev not in SEVERITY_RANK:
            raise MalformedMacroCalendarError(
                f"{ctx}: unknown severity {row['severity']!r} "
                f"(expected one of {sorted(SEVERITY_RANK)})")
        start_raw = row.get("start_date")
        events.append(MacroEvent(
            date=_parse_date(row["date"], ctx),
            time_et=str(row.get("time_et") or "08:30"),
            event=str(row["event"]),
            severity=sev,
            sep=bool(row.get("sep", False)),
            confirmed=bool(row.get("confirmed", True)),
            derived=bool(row.get("derived", False)),
            uncertainty_days=int(row.get("uncertainty_days") or 0),
            start_date=_parse_date(start_raw, ctx) if start_raw else None,
            reference_period=row.get("reference_period"),
            derivation=row.get("derivation"),
            source=row.get("source"),
            note=row.get("note"),
        ))

    events.sort(key=lambda e: (e.date, e.time_et, e.event))

    confirmed_through = doc.get("confirmed_through")
    generated_at = doc.get("generated_at")
    return MacroCalendar(
        valid_through=valid_through,
        events=tuple(events),
        confirmed_through=(_parse_date(confirmed_through, "confirmed_through")
                           if confirmed_through else None),
        generated_at=(_parse_date(generated_at, "generated_at")
                      if generated_at else None),
        path=path,
        sources=dict(doc.get("sources") or {}),
    )


_cached: MacroCalendar | None = None


def load_macro_calendar(path: str | Path | None = None, *,
                        force: bool = False) -> MacroCalendar:
    """Load and cache the macro table. Does NOT assert freshness — callers that
    are about to make a trading decision should use `get_macro_calendar()`."""
    global _cached
    if _cached is not None and not force and path is None:
        return _cached

    candidates = [Path(path)] if path else _candidate_paths()
    last_err: Exception | None = None
    for p in candidates:
        try:
            if p and p.is_file():
                cal = parse_macro_calendar(json.loads(p.read_text(encoding="utf-8")),
                                           path=p)
                if path is None:
                    _cached = cal
                return cal
        except MacroCalendarError:
            raise
        except Exception as exc:  # noqa: BLE001 - remembered, then re-raised below
            last_err = exc
            continue
    raise MalformedMacroCalendarError(
        f"could not load {_CALENDAR_FILENAME} from any of "
        f"{[str(c) for c in candidates]}"
        + (f" (last error: {last_err})" if last_err else "")
    )


def clear_cache() -> None:
    global _cached
    _cached = None


def assert_macro_table_fresh(now: date | None = None,
                             min_horizon_days: int | None = None,
                             path: str | Path | None = None) -> MacroCalendar:
    """Startup assertion. Call this from the FastAPI lifespan, before the
    Simmer loop is created, so a stale table stops the service rather than
    quietly degrading it.

    Returns the calendar so the caller can stash it on `app.state`.
    """
    if min_horizon_days is None:
        min_horizon_days = int(os.environ.get("SIMMER_MACRO_MIN_HORIZON_DAYS")
                               or DEFAULT_MIN_HORIZON_DAYS)
    cal = load_macro_calendar(path)
    cal.assert_fresh(now=now, min_horizon_days=min_horizon_days)
    log.info("macro calendar OK: %s", cal.summary())
    if cal.unconfirmed():
        log.warning(
            "macro calendar has %d unconfirmed (derived) rows; they block a "
            "+/- uncertainty_days window rather than a single day. Refresh from "
            "bls.gov / bea.gov once the agencies publish.", len(cal.unconfirmed()))
    return cal


def get_macro_calendar(*, strict: bool = True) -> MacroCalendar:
    """The table, freshness-checked. `strict=False` is for diagnostics only."""
    cal = load_macro_calendar()
    if strict:
        cal.assert_fresh(min_horizon_days=int(
            os.environ.get("SIMMER_MACRO_MIN_HORIZON_DAYS") or DEFAULT_MIN_HORIZON_DAYS))
    return cal


# ── Module-level query helpers ─────────────────────────────────────────────


def macro_events_between(start: date, end: date, *,
                         min_severity: str | None = None,
                         events: Iterable[str] | None = None,
                         strict: bool = True,
                         calendar: MacroCalendar | None = None) -> list[MacroEvent]:
    """Macro releases falling inside [start, end] — i.e. inside a tenor.

    `strict=True` (the default) re-runs the freshness assertion, so a stale
    table raises instead of returning `[]`. Returning an empty list from a
    table that has run out is exactly the fail-open this module exists to
    prevent.
    """
    cal = calendar or (get_macro_calendar(strict=strict) if strict
                       else load_macro_calendar())
    return cal.between(start, end, min_severity=min_severity, events=events)


def refuse_beyond_table(expiry: date, *,
                        calendar: MacroCalendar | None = None) -> str | None:
    """None when the expiry is scorable; a reason string when the engine must
    decline. Truthy == refuse."""
    cal = calendar or load_macro_calendar()
    reason = cal.refuse_beyond_table(expiry)
    if reason:
        log.error("refusing to score: %s", reason)
    return reason


# ══════════════════════════════════════════════════════════════════════════
#  Earnings
# ══════════════════════════════════════════════════════════════════════════


SESSION_PRE = "pre"
SESSION_POST = "post"
SESSION_UNKNOWN = "unknown"


@dataclass(frozen=True)
class EarningsDate:
    """One vendor's opinion about when a company reports.

    `confirmed` means the *company* announced it, not that the vendor is
    confident. Vendors that cannot tell you the difference must report
    `confirmed=False`; treating a model estimate as a fact is the failure this
    whole layer is built around.
    """

    symbol: str
    date: date
    session: str = SESSION_UNKNOWN     # pre | post | unknown
    confirmed: bool = False
    source: str = ""
    raw_time: str | None = None

    def with_symbol_upper(self) -> "EarningsDate":
        return self if self.symbol.isupper() else EarningsDate(
            symbol=self.symbol.upper(), date=self.date, session=self.session,
            confirmed=self.confirmed, source=self.source, raw_time=self.raw_time)


@dataclass(frozen=True)
class EarningsBlackout:
    """A range of dates a position must not be held across.

    `precision` is "day" only when >=2 sources independently confirmed the same
    date. Everything else — disagreement, a single source, an unconfirmed flag
    — widens to "week", because an unconfirmed earnings date is a distribution
    and pretending otherwise is how you end up short gamma into a gap.
    """

    symbol: str
    start: date
    end: date
    precision: str              # "day" | "week"
    reason: str
    dates: tuple[date, ...] = ()
    sources: tuple[str, ...] = ()
    confirmed: bool = False

    def covers(self, day: date) -> bool:
        return self.start <= day <= self.end

    def overlaps(self, start: date, end: date) -> bool:
        return self.start <= end and self.end >= start

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "precision": self.precision,
            "reason": self.reason,
            "dates": [d.isoformat() for d in self.dates],
            "sources": list(self.sources),
            "confirmed": self.confirmed,
        }


def trading_week(day: date) -> tuple[date, date]:
    """Monday..Friday of the ISO week containing `day`."""
    monday = day - timedelta(days=day.weekday())
    return monday, monday + timedelta(days=4)


def resolve_earnings_blackout(symbol: str,
                              candidates: Sequence[EarningsDate]) -> EarningsBlackout | None:
    """Cross-check vendor earnings dates and produce the range to block.

    The rules, in order:

      1. No candidates -> None. (Absence of a scheduled report is not proof
         there isn't one; the *caller* decides whether it has enough coverage
         to trade at all. See `earnings_coverage_ok`.)
      2. Sources disagree on the date -> block Monday..Friday spanning every
         proposed date. Precision "week".
      3. Any surviving candidate is unconfirmed -> block the week. Precision
         "week".
      4. All sources agree AND >=2 of them confirm -> block that single day
         (extended by one day for an after-hours report, which moves the *next*
         session). Precision "day".
      5. Agreement but only one source confirms -> still a week. One vendor is
         not a cross-check.
    """
    sym = (symbol or "").upper()
    cands = [c.with_symbol_upper() for c in candidates
             if (c.symbol or "").upper() == sym]
    if not cands:
        return None

    dates = sorted({c.date for c in cands})
    sources = tuple(sorted({c.source for c in cands if c.source}))

    if len(dates) > 1:
        start, _ = trading_week(dates[0])
        _, end = trading_week(dates[-1])
        detail = ", ".join(f"{c.source or '?'}={c.date.isoformat()}" for c in cands)
        return EarningsBlackout(
            symbol=sym, start=start, end=end, precision="week",
            reason=(f"sources disagree on the earnings date ({detail}); blocking the "
                    "whole week — an uncertain date is a distribution, not a date"),
            dates=tuple(dates), sources=sources, confirmed=False)

    day = dates[0]
    confirmers = [c for c in cands if c.confirmed]
    if not confirmers:
        start, end = trading_week(day)
        return EarningsBlackout(
            symbol=sym, start=start, end=end, precision="week",
            reason=(f"earnings date {day.isoformat()} is unconfirmed in every source "
                    f"({', '.join(sources) or 'unknown'}); ~35-45% of forward dates "
                    "are model estimates, so blocking the whole week"),
            dates=(day,), sources=sources, confirmed=False)

    if len(cands) < 2 or len(confirmers) < 2:
        start, end = trading_week(day)
        return EarningsBlackout(
            symbol=sym, start=start, end=end, precision="week",
            reason=(f"only {len(confirmers)} source confirmed {day.isoformat()} "
                    "and a single vendor is not a cross-check; blocking the whole week"),
            dates=(day,), sources=sources, confirmed=False)

    # Two or more sources agree AND confirm: precise enough to block one day.
    sessions = {c.session for c in confirmers}
    end = day + timedelta(days=1) if SESSION_POST in sessions else day
    return EarningsBlackout(
        symbol=sym, start=day, end=end, precision="day",
        reason=(f"{len(confirmers)} sources confirm {day.isoformat()} "
                f"({'/'.join(sorted(sessions))})"),
        dates=(day,), sources=sources, confirmed=True)


def earnings_coverage_ok(results: dict[str, Sequence[EarningsDate] | None]) -> bool:
    """True when at least two earnings sources actually answered.

    A source that raised or timed out contributes `None`, not `[]` — the
    difference between "no earnings scheduled" and "we do not know" is the
    whole point, and callers should refuse to score rather than treat a failed
    fetch as an all-clear.
    """
    return sum(1 for v in results.values() if v is not None) >= 2


def blocks_tenor(blackout: EarningsBlackout | None, start: date, end: date) -> bool:
    return bool(blackout and blackout.overlaps(start, end))


# ── Nasdaq (free, undocumented, ToS risk — cross-check only) ───────────────

_NASDAQ_URL = "https://api.nasdaq.com/api/calendar/earnings"

# api.nasdaq.com rejects non-browser agents outright, so a browser-shaped UA is
# a functional requirement, not evasion for its own sake.
_NASDAQ_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nasdaq.com/",
}

_NASDAQ_SESSION_MAP = {
    "time-pre-market": SESSION_PRE,
    "time-after-hours": SESSION_POST,
    "time-not-supplied": SESSION_UNKNOWN,
}


class NasdaqEarningsClient:
    """Free, unauthenticated earnings calendar.

    ⚠️ THIS IS AN UNDOCUMENTED INTERNAL ENDPOINT. It powers nasdaq.com's own
    calendar widget, has no SLA, no published terms permitting programmatic
    use, no versioning, and 429s under load. It carries real ToS risk and can
    disappear without notice.

    Therefore, by construction:
      * disabled unless SIMMER_EARNINGS_NASDAQ is truthy (`enabled` below);
      * used only as a CROSS-CHECK, never as the sole input to a block/allow
        decision — it cannot report a confirmed flag at all, so everything it
        returns is `confirmed=False` and can only ever widen a blackout;
      * failures return None (unknown), never [] (all clear).

    If it goes away, nothing breaks except the second opinion — which is
    exactly the dependency posture the design calls for.
    """

    name = "nasdaq"

    def __init__(self, *, enabled: bool | None = None, timeout: float = 8.0,
                 url: str = _NASDAQ_URL):
        self.enabled = _env_flag("SIMMER_EARNINGS_NASDAQ", False) if enabled is None else enabled
        self.timeout = timeout
        self.url = url

    async def fetch_day(self, day: date) -> list[EarningsDate] | None:
        """Every company reporting on `day`, or None if the source is off/failed."""
        if not self.enabled:
            return None
        try:
            import httpx
            async with httpx.AsyncClient(timeout=self.timeout,
                                         headers=_NASDAQ_HEADERS) as client:
                resp = await client.get(self.url, params={"date": day.isoformat()})
                resp.raise_for_status()
                payload = resp.json()
        except Exception as exc:  # noqa: BLE001 - unknown, not clear
            log.warning("nasdaq earnings fetch failed for %s: %s", day, exc)
            return None
        return self.parse_day(payload, day)

    @staticmethod
    def parse_day(payload: Any, day: date) -> list[EarningsDate]:
        """Shape: {"data": {"rows": [{"symbol", "time", ...}]}, "status": ...}.

        `rows` is null on days with no reports, which is a legitimate empty
        answer (the request succeeded), unlike a transport failure.
        """
        data = (payload or {}).get("data") or {}
        rows = data.get("rows") or []
        out: list[EarningsDate] = []
        for row in rows:
            sym = (row.get("symbol") or "").strip().upper()
            if not sym:
                continue
            raw_time = row.get("time")
            out.append(EarningsDate(
                symbol=sym,
                date=day,
                session=_NASDAQ_SESSION_MAP.get(raw_time, SESSION_UNKNOWN),
                confirmed=False,        # Nasdaq exposes no confirmed flag. Ever.
                source="nasdaq",
                raw_time=raw_time,
            ))
        return out

    async def fetch_symbol(self, symbol: str, start: date,
                           end: date) -> list[EarningsDate] | None:
        """Scan a date range for one symbol. The endpoint is day-keyed only, so
        this is one request per day — keep the range short (a tenor, not a year)
        and cache the result; earnings dates move rarely."""
        if not self.enabled:
            return None
        sym = (symbol or "").upper()
        found: list[EarningsDate] = []
        any_ok = False
        day = start
        while day <= end:
            rows = await self.fetch_day(day)
            if rows is not None:
                any_ok = True
                found.extend(r for r in rows if r.symbol == sym)
            day += timedelta(days=1)
        return found if any_ok else None


# ── Benzinga via Massive (paid, optional, disabled without a key) ──────────

_BENZINGA_DEFAULT_BASE = "https://api.massive.dev"
_BENZINGA_PATH = "/benzinga/v1/earnings"

_BENZINGA_SESSION_MAP = {
    "bmo": SESSION_PRE, "pre": SESSION_PRE, "before market open": SESSION_PRE,
    "amc": SESSION_POST, "post": SESSION_POST, "after market close": SESSION_POST,
}


class BenzingaEarningsClient:
    """Benzinga earnings via the Massive partner API. The good source — it is
    the only affordable one that reports `date_status` (`confirmed` vs
    `projected`) alongside an exact `time`.

    Disabled and inert without SIMMER_BENZINGA_API_KEY; the client is coded so
    enabling it later is a config change, not a code change.
    """

    name = "benzinga"

    def __init__(self, *, api_key: str | None = None, base_url: str | None = None,
                 timeout: float = 8.0):
        self.api_key = api_key if api_key is not None else os.environ.get(
            "SIMMER_BENZINGA_API_KEY", "")
        self.base_url = (base_url or os.environ.get("SIMMER_BENZINGA_BASE_URL")
                         or _BENZINGA_DEFAULT_BASE).rstrip("/")
        self.timeout = timeout

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    async def fetch_symbol(self, symbol: str, start: date,
                           end: date) -> list[EarningsDate] | None:
        if not self.enabled:
            return None
        params = {
            "symbols": (symbol or "").upper(),
            "date_from": start.isoformat(),
            "date_to": end.isoformat(),
        }
        try:
            import httpx
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.get(
                    f"{self.base_url}{_BENZINGA_PATH}",
                    params=params,
                    headers={"Authorization": f"Bearer {self.api_key}",
                             "Accept": "application/json"})
                resp.raise_for_status()
                payload = resp.json()
        except Exception as exc:  # noqa: BLE001 - unknown, not clear
            log.warning("benzinga earnings fetch failed for %s: %s", symbol, exc)
            return None
        return self.parse(payload)

    @staticmethod
    def parse(payload: Any) -> list[EarningsDate]:
        """Accepts either a bare list or {"earnings": [...]} / {"data": [...]}."""
        if isinstance(payload, dict):
            rows = payload.get("earnings") or payload.get("data") or payload.get("results") or []
        else:
            rows = payload or []
        out: list[EarningsDate] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            sym = (row.get("ticker") or row.get("symbol") or "").strip().upper()
            raw_date = row.get("date")
            if not sym or not raw_date:
                continue
            try:
                d = date.fromisoformat(str(raw_date)[:10])
            except ValueError:
                continue
            status = str(row.get("date_status") or "").strip().lower()
            raw_time = row.get("time")
            session = _BENZINGA_SESSION_MAP.get(
                str(row.get("time_status") or "").strip().lower(), SESSION_UNKNOWN)
            if session == SESSION_UNKNOWN and raw_time:
                # An exact HH:MM:SS EST timestamp: before the 09:30 open is a
                # pre-market report, after the 16:00 close is after-hours.
                try:
                    hh, mm = int(str(raw_time)[:2]), int(str(raw_time)[3:5])
                    minutes = hh * 60 + mm
                    if minutes and minutes < 9 * 60 + 30:
                        session = SESSION_PRE
                    elif minutes >= 16 * 60:
                        session = SESSION_POST
                except (ValueError, IndexError):
                    pass
            out.append(EarningsDate(
                symbol=sym, date=d, session=session,
                confirmed=(status == "confirmed"),
                source="benzinga", raw_time=str(raw_time) if raw_time else None,
            ))
        return out


async def earnings_blackout_for(symbol: str, start: date, end: date, *,
                                clients: Sequence[Any] | None = None
                                ) -> tuple[EarningsBlackout | None, bool]:
    """Query every enabled source and cross-check them.

    Returns `(blackout, coverage_ok)`. `coverage_ok` is False when fewer than
    two sources answered — the caller should refuse to score rather than read
    "no blackout" as "safe".
    """
    srcs = list(clients) if clients is not None else [
        NasdaqEarningsClient(), BenzingaEarningsClient()]
    results: dict[str, Sequence[EarningsDate] | None] = {}
    for client in srcs:
        try:
            results[getattr(client, "name", repr(client))] = await client.fetch_symbol(
                symbol, start, end)
        except Exception as exc:  # noqa: BLE001 - one bad vendor never kills the sweep
            log.warning("earnings source %s failed: %s", getattr(client, "name", "?"), exc)
            results[getattr(client, "name", repr(client))] = None

    candidates: list[EarningsDate] = []
    for rows in results.values():
        if rows:
            candidates.extend(rows)
    return resolve_earnings_blackout(symbol, candidates), earnings_coverage_ok(results)


# ══════════════════════════════════════════════════════════════════════════
#  FRED reconciler — belt and braces, never authoritative
# ══════════════════════════════════════════════════════════════════════════

_FRED_BASE = "https://api.stlouisfed.org/fred"

#: Release *names* to match when enumerating fred/releases. Numeric release IDs
#: are deliberately NOT hardcoded — they are discovered at runtime, because a
#: guessed ID silently reconciles against the wrong release.
FRED_RELEASE_NAME_HINTS: dict[str, tuple[str, ...]] = {
    "CPI": ("consumer price index",),
    "PPI": ("producer price index",),
    "NFP": ("employment situation",),
    "PCE": ("personal income and outlays",),
    "FOMC": ("h.15",),   # no true FOMC release in FRED; left for completeness
}


@dataclass
class CalendarDrift:
    """One disagreement between our table and FRED. Advisory only."""
    event: str
    table_date: date | None
    fred_date: date | None
    kind: str          # "shifted" | "missing_in_table" | "missing_in_fred"

    def to_dict(self) -> dict[str, Any]:
        return {
            "event": self.event,
            "table_date": self.table_date.isoformat() if self.table_date else None,
            "fred_date": self.fred_date.isoformat() if self.fred_date else None,
            "kind": self.kind,
        }


class FredReconciler:
    """Optional cross-check of the hardcoded macro table against FRED.

    NOT AUTHORITATIVE, by design. FRED mirrors agency release calendars with
    its own lag and its own idea of what counts as a release; letting it
    overwrite a table we hand-verified would trade a known-good source for an
    unknown one. It logs drift and returns it. The table stays the truth.

    ⚠️ THE GOTCHA THAT MAKES THIS ENDPOINT LOOK BROKEN. `fred/releases/dates`
    defaults its realtime window to *today*, and a realtime window ending today
    EXCLUDES every future date — so the naive call returns only past releases
    and the reconciler silently confirms nothing. Two parameters fix it, and
    both are mandatory here:

        realtime_end=9999-12-31                     (open-ended window)
        include_release_dates_with_no_data=true     (future dates have no data yet)

    Release IDs are enumerated from `fred/releases`, never guessed.
    """

    name = "fred"

    def __init__(self, *, api_key: str | None = None, timeout: float = 10.0,
                 base_url: str = _FRED_BASE):
        self.api_key = api_key if api_key is not None else os.environ.get("FRED_API_KEY", "")
        self.timeout = timeout
        self.base_url = base_url.rstrip("/")
        self._release_ids: dict[str, int] | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    async def _get(self, path: str, params: dict[str, Any]) -> Any:
        import httpx
        q = dict(params)
        q.update({"api_key": self.api_key, "file_type": "json"})
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.get(f"{self.base_url}{path}", params=q)
            resp.raise_for_status()
            return resp.json()

    async def discover_release_ids(self) -> dict[str, int]:
        """Enumerate fred/releases once and map our event names to real IDs."""
        if self._release_ids is not None:
            return self._release_ids
        if not self.enabled:
            return {}
        try:
            payload = await self._get("/releases", {"limit": 1000})
        except Exception as exc:  # noqa: BLE001
            log.warning("FRED release enumeration failed: %s", exc)
            return {}
        found: dict[str, int] = {}
        for rel in (payload or {}).get("releases", []):
            name = str(rel.get("name") or "").lower()
            for event, hints in FRED_RELEASE_NAME_HINTS.items():
                if event in found:
                    continue
                if any(h in name for h in hints):
                    found[event] = int(rel.get("id"))
        self._release_ids = found
        log.info("FRED release ids discovered: %s", found)
        return found

    async def release_dates(self, release_id: int, start: date,
                            end: date) -> list[date]:
        """Forward release dates for one release. See the class docstring for
        why `realtime_end` and `include_release_dates_with_no_data` are not
        optional."""
        if not self.enabled:
            return []
        params = {
            "release_id": release_id,
            "realtime_start": date.today().isoformat(),
            "realtime_end": "9999-12-31",                  # future dates need this
            "include_release_dates_with_no_data": "true",  # ...and this
            "limit": 1000,
            "sort_order": "asc",
        }
        payload = await self._get("/release/dates", params)
        out: list[date] = []
        for row in (payload or {}).get("release_dates", []):
            try:
                d = date.fromisoformat(str(row.get("date"))[:10])
            except (ValueError, TypeError):
                continue
            if start <= d <= end:
                out.append(d)
        return sorted(set(out))

    async def reconcile(self, calendar: MacroCalendar | None = None, *,
                        start: date | None = None, end: date | None = None,
                        tolerance_days: int = 0) -> list[CalendarDrift]:
        """Compare the table against FRED and return the differences.

        Never mutates the calendar and never raises on transport failure — a
        FRED outage must not take down a service whose truth lives in a local
        JSON file.
        """
        if not self.enabled:
            log.debug("FRED reconciler disabled (no FRED_API_KEY)")
            return []
        cal = calendar or load_macro_calendar()
        start = start or date.today()
        end = end or cal.valid_through

        ids = await self.discover_release_ids()
        drift: list[CalendarDrift] = []
        for event, release_id in ids.items():
            if event == "FOMC":
                continue     # FRED has no FOMC meeting release; the Fed page is truth
            try:
                fred_dates = await self.release_dates(release_id, start, end)
            except Exception as exc:  # noqa: BLE001
                log.warning("FRED dates for %s (release %s) failed: %s",
                            event, release_id, exc)
                continue
            table_dates = [e.date for e in cal.events
                           if e.event.upper() == event and start <= e.date <= end]
            drift.extend(_diff_dates(event, table_dates, fred_dates, tolerance_days))

        for d in drift:
            log.warning("macro calendar drift vs FRED (advisory only): %s", d.to_dict())
        return drift


def _diff_dates(event: str, table_dates: Sequence[date], fred_dates: Sequence[date],
                tolerance_days: int) -> list[CalendarDrift]:
    """Pair each table date with its nearest FRED date and report the gaps."""
    out: list[CalendarDrift] = []
    remaining = list(fred_dates)
    for t in sorted(table_dates):
        if not remaining:
            out.append(CalendarDrift(event, t, None, "missing_in_fred"))
            continue
        nearest = min(remaining, key=lambda f: abs((f - t).days))
        if abs((nearest - t).days) <= tolerance_days:
            remaining.remove(nearest)
        elif abs((nearest - t).days) <= 10:
            out.append(CalendarDrift(event, t, nearest, "shifted"))
            remaining.remove(nearest)
        else:
            out.append(CalendarDrift(event, t, None, "missing_in_fred"))
    for f in remaining:
        out.append(CalendarDrift(event, None, f, "missing_in_table"))
    return out


__all__ = [
    "DEFAULT_MIN_HORIZON_DAYS",
    "MacroCalendarError", "StaleMacroCalendarError", "MalformedMacroCalendarError",
    "MacroEvent", "MacroCalendar",
    "parse_macro_calendar", "load_macro_calendar", "clear_cache",
    "assert_macro_table_fresh", "get_macro_calendar",
    "macro_events_between", "refuse_beyond_table",
    "EarningsDate", "EarningsBlackout", "trading_week",
    "resolve_earnings_blackout", "earnings_coverage_ok", "blocks_tenor",
    "NasdaqEarningsClient", "BenzingaEarningsClient", "earnings_blackout_for",
    "FredReconciler", "CalendarDrift", "FRED_RELEASE_NAME_HINTS",
    "SESSION_PRE", "SESSION_POST", "SESSION_UNKNOWN",
]

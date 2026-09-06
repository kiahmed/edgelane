"""Intraday data feed that drives the short-tier (0-1 / 2-5 DTE) logic.

Covers docs/simmer_dte_tiers.md "Prerequisite": the intraday realized-vol math,
the ATM-IV snapshot table + `iv_change`, the provider's `intraday_ohlc`, and the
watcher wiring that injects `rv_intraday` / `iv_change` into `research` during
market hours (and SKIPS the fetch off-hours, keeping the closed-freeze intact).
No network anywhere — httpx.MockTransport for Yahoo, FakeSimmerTradier + a temp
DuckDB for the sweep.
"""
from __future__ import annotations

import math
from datetime import date, datetime, timedelta, timezone

import httpx
import pytest

from app import simmer_config
from app import simmer_engine as se
from app import simmer_watcher as sw
from app.simmer_data_provider import TradierDataProvider, YahooDataProvider

from .conftest import EXP, FakeSimmerTradier, SPOT


# ═══════════════════════════════════════════════════════════════════════════
# 1. Intraday realized-vol math
# ═══════════════════════════════════════════════════════════════════════════
def _ref_intraday_rv(closes, bars_per_day, ann):
    """Independent reference: zero-mean close-to-close, annualized by
    sqrt(bars_per_day × ann)."""
    rets = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]
    var = sum(r * r for r in rets) / len(rets)
    return math.sqrt(var) * math.sqrt(bars_per_day * ann)


def test_intraday_rv_matches_independent_reference():
    closes = [100.0, 101.0, 100.0, 102.0, 101.5]
    bars = [{"open": c, "high": c, "low": c, "close": c} for c in closes]
    got = se.intraday_realized_vol(bars, bars_per_day=78, annualization=252, min_bars=4)
    assert got == pytest.approx(_ref_intraday_rv(closes, 78, 252))


def test_intraday_rv_annualization_scales_with_bars_per_day():
    closes = [100.0, 101.0, 100.5, 101.5, 100.8]
    bars = [{"open": c, "high": c, "low": c, "close": c} for c in closes]
    a = se.intraday_realized_vol(bars, bars_per_day=78, annualization=252, min_bars=4)
    b = se.intraday_realized_vol(bars, bars_per_day=39, annualization=252, min_bars=4)
    assert a == pytest.approx(b * math.sqrt(78 / 39))


def test_intraday_rv_none_below_min_bars():
    bars = [{"open": 100, "high": 100, "low": 100, "close": 100},
            {"open": 101, "high": 101, "low": 101, "close": 101}]
    assert se.intraday_realized_vol(bars, min_bars=4) is None


def test_intraday_rv_accepts_bare_closes():
    closes = [100.0, 101.0, 100.0, 102.0, 101.5]
    assert se.intraday_realized_vol(closes, min_bars=4) == pytest.approx(
        _ref_intraday_rv(closes, 78, 252))


# ═══════════════════════════════════════════════════════════════════════════
# 2. iv_change from snapshots
# ═══════════════════════════════════════════════════════════════════════════
def _snaps(*vals):
    return [{"atm_iv": v} for v in vals]


def test_iv_change_none_below_two_snapshots():
    icfg = simmer_config.intraday()
    assert sw._iv_change_from_snaps([], icfg) is None
    assert sw._iv_change_from_snaps(_snaps(0.30), icfg) is None


def test_iv_change_sign_and_normalization():
    icfg = simmer_config.intraday()   # default: session_first (normalized)
    # richening: latest > first → positive
    up = sw._iv_change_from_snaps(_snaps(0.30, 0.31, 0.33), icfg)
    assert up == pytest.approx((0.33 - 0.30) / 0.30)
    assert up > 0                                   # richening
    # bleeding out → negative
    down = sw._iv_change_from_snaps(_snaps(0.40, 0.35), icfg)
    assert down == pytest.approx((0.35 - 0.40) / 0.40)
    assert down < 0


def test_iv_change_vol_points_baseline():
    icfg = {**simmer_config.intraday(), "iv_change_baseline": "vol_points"}
    assert sw._iv_change_from_snaps(_snaps(0.30, 0.33), icfg) == pytest.approx(0.03)


# ═══════════════════════════════════════════════════════════════════════════
# 3. Snapshot table upsert + today's read
# ═══════════════════════════════════════════════════════════════════════════
def test_iv_intraday_table_insert_and_today_read(fresh_db):
    day = datetime.now(timezone.utc).date()
    base = datetime(day.year, day.month, day.day, 14, 0, tzinfo=timezone.utc).replace(tzinfo=None)
    fresh_db.insert_simmer_iv_intraday("NVDA", base, 0.30)
    fresh_db.insert_simmer_iv_intraday("NVDA", base + timedelta(minutes=5), 0.31)
    fresh_db.insert_simmer_iv_intraday("NVDA", base + timedelta(minutes=10), 0.33)
    # a yesterday row must not leak into "today"
    fresh_db.insert_simmer_iv_intraday("NVDA", base - timedelta(days=1), 0.99)

    rows = fresh_db.fetch_simmer_iv_intraday_today("NVDA", day.isoformat())
    assert [r["atm_iv"] for r in rows] == [0.30, 0.31, 0.33]     # oldest→newest
    assert 0.99 not in [r["atm_iv"] for r in rows]


def test_iv_intraday_upsert_is_idempotent_on_same_instant(fresh_db):
    day = datetime.now(timezone.utc).date()
    ts = datetime(day.year, day.month, day.day, 15, 0).replace(tzinfo=None)
    fresh_db.insert_simmer_iv_intraday("NVDA", ts, 0.30)
    fresh_db.insert_simmer_iv_intraday("NVDA", ts, 0.32)          # replace, not append
    rows = fresh_db.fetch_simmer_iv_intraday_today("NVDA", day.isoformat())
    assert len(rows) == 1 and rows[0]["atm_iv"] == 0.32


# ═══════════════════════════════════════════════════════════════════════════
# 4. Provider intraday_ohlc
# ═══════════════════════════════════════════════════════════════════════════
def _chart_5m(ts0=1_789_000_000):
    """Minimal v8 chart payload with a null-padded forming last bar."""
    return {"chart": {"result": [{
        "timestamp": [ts0, ts0 + 300, ts0 + 600, ts0 + 900],
        "indicators": {"quote": [{
            "open":  [100.0, 100.5, 101.0, None],
            "high":  [100.8, 101.2, 101.5, None],
            "low":   [99.7, 100.2, 100.8, None],
            "close": [100.5, 101.0, 101.2, None],   # last bucket still forming
            "volume": [1000, 1200, 900, None],
        }]},
    }], "error": None}}


class _ChartTransport:
    def __init__(self, chart_payload):
        self.chart_payload = chart_payload

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "fc.yahoo.com" in url:
            return httpx.Response(200, text="", headers={"set-cookie": "A3=x; Path=/"})
        if "getcrumb" in url:
            return httpx.Response(200, text="fixture-crumb")
        if "/v8/finance/chart/" in url:
            return httpx.Response(200, json=self.chart_payload)
        return httpx.Response(404, text=f"unrouted: {url}")

    def provider(self):
        return YahooDataProvider(transport=httpx.MockTransport(self.handler))


async def test_yahoo_intraday_ohlc_parses_and_skips_null_bars():
    p = _ChartTransport(_chart_5m()).provider()
    bars = await p.intraday_ohlc("NVDA")
    assert len(bars) == 3                       # forming null bar dropped
    assert bars[0]["open"] == 100.0 and bars[0]["close"] == 100.5
    assert bars[0]["volume"] == 1000
    assert bars[0]["t"] is not None
    # feeds the RV helper straight through
    assert se.intraday_realized_vol(bars, min_bars=3) is not None


async def test_yahoo_intraday_ohlc_empty_on_failure():
    tr = _ChartTransport(_chart_5m())
    tr.handler = lambda req: httpx.Response(429, text="throttled")  # never raises out
    p = YahooDataProvider(transport=httpx.MockTransport(tr.handler))
    assert await p.intraday_ohlc("NVDA") == []


async def test_tradier_intraday_ohlc_empty_with_note():
    p = TradierDataProvider(FakeSimmerTradier())
    assert await p.intraday_ohlc("NVDA") == []
    assert "intraday_ohlc:unsupported_by_tradier_client" in p.take_data_quality_notes()


# ═══════════════════════════════════════════════════════════════════════════
# 5/6. Sweep wiring — inject when OPEN, skip when CLOSED
# ═══════════════════════════════════════════════════════════════════════════
class _IntradayProvider(TradierDataProvider):
    """Tradier data path (so chain/quote/expirations reuse the real normalizer)
    plus a controllable intraday_ohlc and a call counter."""
    provider_name = "faketest"

    def __init__(self, base, intraday_bars):
        super().__init__(base)
        self._intraday = list(intraday_bars)
        self.intraday_calls = 0

    async def intraday_ohlc(self, symbol):
        self.intraday_calls += 1
        return list(self._intraday)


def _intraday_bars(closes):
    return [{"t": None, "open": c, "high": c, "low": c, "close": c, "volume": 1}
            for c in closes]


def _spy_readiness(monkeypatch):
    captured = {}
    real = se.evaluate_readiness

    def spy(inputs, cfg=None):
        captured["research"] = inputs.get("research")
        return real(inputs, cfg)

    monkeypatch.setattr(sw.simmer_engine, "evaluate_readiness", spy)
    return captured


async def test_sweep_injects_intraday_signals_when_open(fresh_db, monkeypatch):
    sw.state.market_open = True
    day = datetime.now(timezone.utc).date().isoformat()
    # a prior snapshot earlier today so this sweep's snapshot makes ≥ 2 → iv_change
    fresh_db.insert_simmer_iv_intraday(
        "NVDA", sw._now_naive_utc() - timedelta(minutes=5), 0.30)

    closes = [100.0, 100.6, 101.1, 100.9, 101.4]
    provider = _IntradayProvider(FakeSimmerTradier(), _intraday_bars(closes))
    captured = _spy_readiness(monkeypatch)

    await sw.analyze_symbol(provider, fresh_db, "NVDA", EXP, persist=True)

    res = captured["research"]
    assert provider.intraday_calls == 1
    assert res["rv_intraday"] == pytest.approx(
        se.intraday_realized_vol(_intraday_bars(closes),
                                 bars_per_day=78, annualization=252, min_bars=4))
    assert res["iv_change"] is not None            # 2 snapshots exist now
    # this sweep appended its own snapshot → 2 rows today
    assert len(fresh_db.fetch_simmer_iv_intraday_today("NVDA", day)) == 2


async def test_sweep_skips_intraday_fetch_when_closed(fresh_db, monkeypatch):
    sw.state.market_open = False                    # (also the autouse default)
    day = datetime.now(timezone.utc).date().isoformat()
    fresh_db.insert_simmer_iv_intraday(
        "NVDA", sw._now_naive_utc() - timedelta(minutes=5), 0.30)

    provider = _IntradayProvider(FakeSimmerTradier(), _intraday_bars([100, 101, 102, 103]))
    captured = _spy_readiness(monkeypatch)

    await sw.analyze_symbol(provider, fresh_db, "NVDA", EXP, persist=True)

    res = captured["research"]
    assert provider.intraday_calls == 0             # no fetch off-hours
    assert res["rv_intraday"] is None
    assert res["iv_change"] is None
    # no snapshot appended while closed → the single prior row is untouched
    assert len(fresh_db.fetch_simmer_iv_intraday_today("NVDA", day)) == 1


async def test_intraday_feed_disabled_switch_skips_even_when_open(fresh_db, monkeypatch):
    sw.state.market_open = True
    monkeypatch.setattr(simmer_config, "intraday",
                        lambda: {**simmer_config.INTRADAY, "intraday_feed_enabled": False})
    provider = _IntradayProvider(FakeSimmerTradier(), _intraday_bars([100, 101, 102, 103]))
    captured = _spy_readiness(monkeypatch)

    await sw.analyze_symbol(provider, fresh_db, "NVDA", EXP, persist=True)

    assert provider.intraday_calls == 0
    assert captured["research"]["rv_intraday"] is None


# ═══════════════════════════════════════════════════════════════════════════
# 7. End-to-end: a short-tier name activates the intraday branch live
# ═══════════════════════════════════════════════════════════════════════════
async def test_short_tier_activates_intraday_branch_end_to_end(fresh_db, monkeypatch):
    """Market open, a ~1-DTE expiry, real fed intraday inputs → the engine's
    metrics reflect the intraday vol reference and the IV-change gate, live."""
    sw.state.market_open = True
    # allow a 1-DTE expiry through the DTE window (calibration override)
    monkeypatch.setattr(simmer_config, "_load_overrides_file",
                        lambda: {"gates": {"dte_min": 0}})

    near = (date.today() + timedelta(days=1)).isoformat()
    base = FakeSimmerTradier(exp=near)
    # a prior, LOWER ATM-IV snapshot so this sweep (ATM IV ≈ 0.33) shows richening
    fresh_db.insert_simmer_iv_intraday(
        "NVDA", sw._now_naive_utc() - timedelta(minutes=5), 0.30)

    closes = [100.0, 100.6, 101.1, 100.9, 101.4]
    provider = _IntradayProvider(base, _intraday_bars(closes))

    env = await sw.analyze_symbol(provider, fresh_db, "NVDA", near, persist=True)
    m = env["metrics"]

    assert m["dte_tier"] == "0-1"
    assert m["vol_reference"] == "intraday"
    rv = se.intraday_realized_vol(_intraday_bars(closes),
                                  bars_per_day=78, annualization=252, min_bars=4)
    assert m["vrp"] == pytest.approx(m["atm_iv"] / rv)
    assert m["iv_gate_mode"] == "iv_change"
    assert m["iv_change"] is not None and m["iv_change"] > 0

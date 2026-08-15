"""Simmer data-provider abstraction (app/simmer_data_provider.py).

Covers: analytic greeks vs finite differences of the engine's own pricing,
the Yahoo IV clamp / dead-quote rejection guards, normalized-contract shape
parity between TradierDataProvider and YahooDataProvider on the same synthetic
book, the cookie+crumb bootstrap and the no-retry 429 discipline (both via
httpx.MockTransport — NO network anywhere in this file), factory/config
selection, and the watcher running end-to-end on a provider-shaped fake
(daily bars → real OHLC in iv-history, `touched` filled in outcomes).
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

from app import simmer_watcher as sw
from app.config import Settings, _coerce
from app.simmer_data_provider import (
    IV_MAX,
    IV_MIN,
    ProviderError,
    ProviderRateLimitError,
    TradierDataProvider,
    YahooDataProvider,
    analytic_greeks,
    as_provider,
    get_simmer_provider,
    normalize_yahoo_contract,
)
from app.simmer_engine import bs_call, bs_put

from .conftest import CHAIN_ROWS, EXP, FakeSimmerTradier

FIXTURES = Path(__file__).parent / "fixtures"
SPOT, T30 = 100.0, 30.0 / 365.0


def _price(side: str, s: float, k: float, sigma: float, t: float) -> float:
    return bs_call(s, k, sigma, t) if side == "call" else bs_put(s, k, sigma, t)


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


# ═══════════════════════════════════════════════════════════════════════════
# Analytic greeks — cross-checked against finite differences of the ENGINE's
# own bs_put/bs_call, so any drift between the two layers fails loudly.
# ═══════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("side,strike", [
    ("put", 94.0), ("put", 100.0), ("put", 106.0),
    ("call", 94.0), ("call", 100.0), ("call", 106.0),
])
def test_analytic_greeks_match_finite_difference(side, strike):
    sigma = 0.33
    g = analytic_greeks(SPOT, strike, sigma, T30, side)

    h = 0.05
    fd_delta = (_price(side, SPOT + h, strike, sigma, T30)
                - _price(side, SPOT - h, strike, sigma, T30)) / (2 * h)
    assert abs(g["delta"] - fd_delta) < 1e-4

    fd_gamma = (_price(side, SPOT + h, strike, sigma, T30)
                - 2 * _price(side, SPOT, strike, sigma, T30)
                + _price(side, SPOT - h, strike, sigma, T30)) / (h * h)
    assert abs(g["gamma"] - fd_gamma) < 1e-4

    dv = 1e-4          # vega convention: per 1 VOL POINT (1% IV)
    fd_vega = (_price(side, SPOT, strike, sigma + dv, T30)
               - _price(side, SPOT, strike, sigma - dv, T30)) / (2 * dv) / 100.0
    assert abs(g["vega"] - fd_vega) < 1e-4

    dt = 1e-6          # theta convention: PER CALENDAR DAY, negative decay
    fd_theta = -(_price(side, SPOT, strike, sigma, T30 + dt)
                 - _price(side, SPOT, strike, sigma, T30 - dt)) / (2 * dt) / 365.0
    assert abs(g["theta"] - fd_theta) < 1e-4


def test_greeks_conventions_and_signs():
    gp = analytic_greeks(SPOT, 94.0, 0.33, T30, "put")
    gc = analytic_greeks(SPOT, 106.0, 0.33, T30, "call")
    assert gp["delta"] < 0 < gc["delta"]
    assert gp["gamma"] > 0 and gc["gamma"] > 0
    assert gp["theta"] < 0 and gc["theta"] < 0
    assert gp["vega"] > 0 and gc["vega"] > 0


def test_greeks_degenerate_inputs_return_zeros_not_garbage():
    for bad in (dict(sigma=0.0), dict(t=0.0), dict(spot=0.0), dict(sigma=-1.0)):
        kw = dict(spot=SPOT, strike=100.0, sigma=0.33, t=T30, side="put")
        kw.update(bad)
        g = analytic_greeks(kw["spot"], kw["strike"], kw["sigma"], kw["t"], kw["side"])
        assert g == {"delta": 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0}


# ═══════════════════════════════════════════════════════════════════════════
# Yahoo contract normalization guards
# ═══════════════════════════════════════════════════════════════════════════
def test_iv_outside_bounds_is_treated_as_missing_not_fabricated():
    for bad_iv in (1e-5, IV_MIN / 2, IV_MAX * 2, 9.0, None):
        c = normalize_yahoo_contract(
            {"contractSymbol": "X", "strike": 94.0, "bid": 1.6, "ask": 1.62,
             "lastPrice": 1.61, "openInterest": 2100, "volume": 400,
             "impliedVolatility": bad_iv},
            "put", EXP, SPOT, T30)
        assert c is not None                       # quote is fine — keep it
        assert c["iv"] == 0.0                      # IV: missing, not invented
        assert c["delta"] == c["gamma"] == c["theta"] == c["vega"] == 0.0


def test_zero_quoted_contract_is_rejected():
    c = normalize_yahoo_contract(
        {"contractSymbol": "X", "strike": 94.0, "bid": 0, "ask": 0,
         "impliedVolatility": 0.35}, "put", EXP, SPOT, T30)
    assert c is None


def test_sane_contract_gets_analytic_greeks_from_yahoo_iv():
    c = normalize_yahoo_contract(
        {"contractSymbol": "X", "strike": 94.0, "bid": 1.60, "ask": 1.62,
         "lastPrice": 1.61, "openInterest": 2100, "volume": 400,
         "impliedVolatility": 0.3510}, "put", EXP, SPOT, T30)
    expected = analytic_greeks(SPOT, 94.0, 0.3510, T30, "put")
    assert c["iv"] == pytest.approx(0.3510)
    assert c["delta"] == pytest.approx(expected["delta"])
    assert c["open_interest"] == 2100 and isinstance(c["open_interest"], int)
    # Unknown book sizes must be None, NOT 0 — the engine's size-at-touch
    # gate reads 0 as an empty book (veto) but skips None (unknown).
    assert c["bid_size"] is None and c["ask_size"] is None


# ═══════════════════════════════════════════════════════════════════════════
# Transport plumbing (httpx.MockTransport — no network)
# ═══════════════════════════════════════════════════════════════════════════
def _v7_from_chain_rows(spot: float = SPOT, exp: str = EXP,
                        mutate=None) -> dict:
    """The conftest CHAIN_ROWS book re-emitted in Yahoo v7 shape, so parity
    tests feed BOTH providers the same synthetic book."""
    calls, puts = [], []
    for side, k, bid, ask, iv, _delta in CHAIN_ROWS:
        o = {"contractSymbol": f"NVDA-Y-{side}-{k}", "strike": float(k),
             "bid": bid, "ask": ask, "lastPrice": round((bid + ask) / 2, 4),
             "volume": 500, "openInterest": 2500, "impliedVolatility": iv}
        (calls if side == "call" else puts).append(o)
    payload = {"optionChain": {"result": [{
        "underlyingSymbol": "NVDA",
        "expirationDates": [1789689600],
        "quote": {"symbol": "NVDA", "regularMarketPrice": spot},
        "options": [{"expirationDate": 1789689600, "calls": calls, "puts": puts}],
    }], "error": None}}
    if mutate:
        mutate(payload)
    return payload


class _Transport:
    """Routing MockTransport: fc.yahoo.com → Set-Cookie, getcrumb → crumb,
    /v7/finance/options → options_payload, /v8/finance/chart → chart_payload.
    `options_status` can force an initial status (e.g. 401 until a crumb is
    presented, or a permanent 429)."""

    def __init__(self, options_payload=None, chart_payload=None,
                 options_status=None, require_crumb=False):
        self.options_payload = options_payload
        self.chart_payload = chart_payload
        self.options_status = options_status
        self.require_crumb = require_crumb
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = str(request.url)
        if "fc.yahoo.com" in url:
            return httpx.Response(200, text="",
                                  headers={"set-cookie": "A3=fixture; Path=/"})
        if "getcrumb" in url:
            return httpx.Response(200, text="fixture-crumb")
        if "/v7/finance/options/" in url:
            if self.options_status is not None:
                return httpx.Response(self.options_status, text="blocked")
            if self.require_crumb and b"crumb=fixture-crumb" not in request.url.query:
                return httpx.Response(401, text="unauthorized")
            return httpx.Response(200, json=self.options_payload)
        if "/v8/finance/chart/" in url:
            return httpx.Response(200, json=self.chart_payload)
        return httpx.Response(404, text=f"unrouted: {url}")

    def provider(self, **kw) -> YahooDataProvider:
        return YahooDataProvider(transport=httpx.MockTransport(self.handler), **kw)

    def urls(self, part: str) -> list[str]:
        return [str(r.url) for r in self.requests if part in str(r.url)]


# ═══════════════════════════════════════════════════════════════════════════
# Yahoo provider surface (checked-in v7/v8 fixtures)
# ═══════════════════════════════════════════════════════════════════════════
async def test_expirations_parsed_from_v7_fixture():
    tr = _Transport(options_payload=_load("yahoo_options_v7.json"))
    exps = await tr.provider().expirations("NVDA")
    assert exps == ["2026-09-18", "2026-10-16"]


async def test_quote_maps_v8_chart_meta():
    tr = _Transport(chart_payload=_load("yahoo_chart_v8.json"))
    q = await tr.provider().quote("NVDA")
    assert q["symbol"] == "NVDA"
    assert q["last"] == pytest.approx(101.60)
    assert q["prevclose"] == pytest.approx(102.60)
    assert q["bid"] is None and q["ask"] is None
    # the watcher's spot reader must accept it
    assert sw._first_price(q) == pytest.approx(101.60)


async def test_quote_uses_torque_config_symbol_map_for_indices():
    tr = _Transport(chart_payload=_load("yahoo_chart_v8.json"))
    p = tr.provider()
    await p.quote("SPX")
    await p.quote("VIX3M")
    charted = " ".join(tr.urls("/v8/finance/chart/"))
    assert "%5EGSPC" in charted or "^GSPC" in charted      # torque_config map
    assert "%5EVIX3M" in charted or "^VIX3M" in charted    # regime extension


async def test_chain_parses_v7_fixture_realistically():
    tr = _Transport(options_payload=_load("yahoo_options_v7.json"))
    contracts = await tr.provider().chain("NVDA", "2026-09-18")
    assert len(contracts) == 4
    by_key = {(c["strike"], c["side"]): c for c in contracts}
    put94 = by_key[(94.0, "put")]
    assert put94["expiration"] == "2026-09-18"
    assert put94["open_interest"] == 2100
    assert put94["iv"] == pytest.approx(0.3510)
    assert put94["delta"] < 0
    # exact expiration requested → ?date=<unix midnight UTC>
    assert any("date=1789689600" in u for u in tr.urls("/v7/finance/options/"))


async def test_daily_bars_skips_null_rows_and_trims():
    tr = _Transport(chart_payload=_load("yahoo_chart_v8.json"))
    p = tr.provider()
    bars = await p.daily_bars("NVDA", 5)
    assert len(bars) == 5
    assert all(None not in (b["open"], b["high"], b["low"], b["close"])
               for b in bars)
    all_bars = await p.daily_bars("NVDA", 30)
    assert len(all_bars) == 11              # 12 stamps, 1 null-padded holiday
    assert all_bars[0]["date"] < all_bars[-1]["date"]


async def test_chain_quality_note_when_over_20pct_rejected():
    def spoil(payload):
        opts = payload["optionChain"]["result"][0]["options"][0]
        for o in opts["puts"][:5]:
            o["impliedVolatility"] = 1e-5          # stale/absurd IV
        for o in opts["calls"][:3]:
            o["bid"] = 0
            o["ask"] = 0                           # dead quotes → dropped
    tr = _Transport(options_payload=_v7_from_chain_rows(mutate=spoil))
    p = tr.provider()
    contracts = await p.chain("NVDA", EXP)
    assert len(contracts) == len(CHAIN_ROWS) - 3   # dead quotes dropped
    notes = p.take_data_quality_notes()
    assert any(n.startswith("yahoo:chain_quality:NVDA") for n in notes)
    assert p.take_data_quality_notes() == []       # channel drains


# ═══════════════════════════════════════════════════════════════════════════
# Auth churn: cookie+crumb bootstrap and the 429 discipline
# ═══════════════════════════════════════════════════════════════════════════
async def test_crumb_bootstrap_on_401_then_retry_succeeds():
    tr = _Transport(options_payload=_v7_from_chain_rows(), require_crumb=True)
    p = tr.provider()
    contracts = await p.chain("NVDA", EXP)
    assert len(contracts) == len(CHAIN_ROWS)
    assert len(tr.urls("fc.yahoo.com")) == 1       # cookie fetched once
    assert len(tr.urls("getcrumb")) == 1           # crumb fetched once
    option_calls = tr.urls("/v7/finance/options/")
    assert len(option_calls) == 2                  # 401 then authorized retry
    assert "crumb=fixture-crumb" in option_calls[-1]
    # crumb is remembered: the next call carries it first time, no re-handshake
    await p.expirations("NVDA")
    assert len(tr.urls("getcrumb")) == 1


async def test_persistent_401_raises_structured_error_after_one_handshake():
    tr = _Transport(options_status=401)
    p = tr.provider()
    with pytest.raises(ProviderError, match="cookie/crumb"):
        await p.chain("NVDA", EXP)
    assert len(tr.urls("/v7/finance/options/")) == 2   # exactly one retry
    assert "yahoo:auth_failed" in p.take_data_quality_notes()


async def test_429_is_never_retried():
    tr = _Transport(options_status=429)
    p = tr.provider()
    with pytest.raises(ProviderRateLimitError):
        await p.chain("NVDA", EXP)
    # ONE request total: no retry, no bootstrap storm (EDGAR discipline)
    assert len(tr.requests) == 1
    assert "yahoo:rate_limited" in p.take_data_quality_notes()


# ═══════════════════════════════════════════════════════════════════════════
# Shape parity: both providers emit the SAME normalized contract shape
# ═══════════════════════════════════════════════════════════════════════════
async def test_normalized_contract_shape_identical_across_providers():
    t_contracts = await TradierDataProvider(FakeSimmerTradier()).chain("NVDA", EXP)
    tr = _Transport(options_payload=_v7_from_chain_rows())
    y_contracts = await tr.provider().chain("NVDA", EXP)
    assert t_contracts and y_contracts
    assert len(t_contracts) == len(y_contracts) == len(CHAIN_ROWS)

    t_keys = {frozenset(c.keys()) for c in t_contracts}
    y_keys = {frozenset(c.keys()) for c in y_contracts}
    assert t_keys == y_keys and len(t_keys) == 1

    t_book = {(c["strike"], c["side"]) for c in t_contracts}
    y_book = {(c["strike"], c["side"]) for c in y_contracts}
    assert t_book == y_book

    ty = {(c["strike"], c["side"]): c for c in y_contracts}
    for tc in t_contracts:
        yc = ty[(tc["strike"], tc["side"])]
        # same conventions: expiration string, float quotes, int oi/volume,
        # IV agrees (Yahoo carried the same surface the Tradier greeks did)
        assert yc["expiration"] == tc["expiration"] == EXP
        assert isinstance(yc["strike"], float) and isinstance(tc["strike"], float)
        assert isinstance(yc["open_interest"], int)
        assert isinstance(yc["volume"], int)
        assert yc["side"] in ("call", "put")
        assert yc["iv"] == pytest.approx(tc["iv"], abs=1e-9)
        assert yc["bid"] == pytest.approx(tc["bid"])
        assert yc["ask"] == pytest.approx(tc["ask"])
        # same sign convention as the Tradier greeks feed…
        assert (yc["delta"] < 0) == (tc["delta"] < 0)
        # …and exactly the analytic value from Yahoo's IV at the provider's t
        t = max((date.fromisoformat(EXP) - date.today()).days, 0) / 365.0
        expected = analytic_greeks(SPOT, yc["strike"], yc["iv"], t, yc["side"])
        assert yc["delta"] == pytest.approx(expected["delta"], abs=1e-9)
        assert yc["gamma"] == pytest.approx(expected["gamma"], abs=1e-9)


async def test_tradier_provider_daily_bars_empty_with_note():
    p = TradierDataProvider(FakeSimmerTradier())
    assert await p.daily_bars("NVDA", 30) == []
    assert p.take_data_quality_notes() == ["daily_bars:unsupported_by_tradier_client"]
    assert p.take_data_quality_notes() == []


# ═══════════════════════════════════════════════════════════════════════════
# Coercion + factory + config
# ═══════════════════════════════════════════════════════════════════════════
def test_as_provider_wraps_raw_client_and_passes_providers_through():
    fake = FakeSimmerTradier()
    wrapped = as_provider(fake)
    assert isinstance(wrapped, TradierDataProvider)
    assert as_provider(wrapped) is wrapped
    y = YahooDataProvider(transport=httpx.MockTransport(
        lambda r: httpx.Response(404)))
    assert as_provider(y) is y


def test_factory_defaults_to_tradier():
    client = object()
    p = get_simmer_provider(Settings(), client)
    assert isinstance(p, TradierDataProvider)
    assert p.provider_name == "tradier"
    assert p._tradier is client


def test_factory_selects_yahoo_from_settings():
    p = get_simmer_provider(Settings(simmer_data_provider="yahoo"), object())
    assert isinstance(p, YahooDataProvider)
    assert p.provider_name == "yahoo"


def test_factory_unknown_value_falls_back_to_tradier():
    p = get_simmer_provider(Settings(simmer_data_provider="webull"), object())
    assert isinstance(p, TradierDataProvider)


def test_config_coerce_parses_simmer_data_provider():
    assert _coerce({"SIMMER_DATA_PROVIDER": "yahoo"})["simmer_data_provider"] == "yahoo"
    assert _coerce({"SIMMER_DATA_PROVIDER": " Yahoo "})["simmer_data_provider"] == "yahoo"
    assert _coerce({"SIMMER_DATA_PROVIDER": "tradier"})["simmer_data_provider"] == "tradier"
    assert _coerce({"SIMMER_DATA_PROVIDER": "webull"})["simmer_data_provider"] == "tradier"
    assert "simmer_data_provider" not in _coerce({})
    assert Settings().simmer_data_provider == "tradier"


# ═══════════════════════════════════════════════════════════════════════════
# Watcher end-to-end on a provider-shaped fake
# ═══════════════════════════════════════════════════════════════════════════
def _fake_bars(days: int = 25, end: date | None = None,
               low_break: float | None = None) -> list[dict]:
    """Deterministic daily OHLC ending at `end` (default today), oldest→newest.
    `low_break` forces one bar's low below that level mid-series."""
    end = end or date.today()
    bars = []
    px = 100.0
    for i in range(days):
        d = end - timedelta(days=days - 1 - i)
        o, c = px, px + (0.3 if i % 2 == 0 else -0.2)
        hi, lo = max(o, c) + 0.8, min(o, c) - 0.9
        if low_break is not None and i == days - 8:
            lo = low_break - 0.5       # breach ~7 days before the series end
        bars.append({"date": d.isoformat(), "open": o, "high": hi,
                     "low": lo, "close": c})
        px = c
    return bars


class FakeYahooProvider:
    """Protocol-shaped provider fake (the way tests/torque fakes Tradier):
    normalized contracts built from the conftest book with Yahoo conventions
    (analytic greeks from IV, no book sizes), plus injectable daily bars."""

    provider_name = "yahoo"

    def __init__(self, bars: list[dict] | None = None,
                 quotes: dict | None = None, notes: list[str] | None = None):
        self._bars = bars if bars is not None else _fake_bars()
        self._quotes = {"VIX": 15.0, "VIX3M": 17.0, "SPY": 500.0, "QQQ": 480.0,
                        **(quotes or {})}
        self._notes = list(notes or [])
        self.bars_calls: list[tuple[str, int]] = []

    async def quote(self, symbol: str) -> dict:
        px = self._quotes.get(symbol.upper(), SPOT)
        return {"symbol": symbol.upper(), "last": px, "bid": None, "ask": None,
                "close": px, "prevclose": px}

    async def expirations(self, symbol: str) -> list[str]:
        return [EXP]

    async def chain(self, symbol: str, expiration: str) -> list[dict]:
        t = max((date.fromisoformat(expiration) - date.today()).days, 0) / 365.0
        out = []
        for side, k, bid, ask, iv, _d in CHAIN_ROWS:
            g = analytic_greeks(SPOT, float(k), iv, t, side)
            out.append({"symbol": f"FAKE-{side}-{k}", "strike": float(k),
                        "side": side, "expiration": expiration,
                        "delta": g["delta"], "gamma": g["gamma"],
                        "theta": g["theta"], "vega": g["vega"], "iv": iv,
                        "open_interest": 2500, "volume": 500,
                        "bid": bid, "ask": ask, "last": (bid + ask) / 2,
                        "last_volume": 0, "trade_date": None,
                        "bid_size": None, "ask_size": None})
        return out

    async def daily_bars(self, symbol: str, days: int) -> list[dict]:
        self.bars_calls.append((symbol.upper(), days))
        return self._bars[-days:]

    def take_data_quality_notes(self) -> list[str]:
        notes, self._notes = self._notes, []
        return notes


async def _union(pairs):
    return list(pairs)


async def test_sweep_runs_end_to_end_on_fake_yahoo_provider(fresh_db, monkeypatch):
    provider = FakeYahooProvider()
    monkeypatch.setattr(sw, "watchlist_union",
                        lambda: _union([("NVDA", None)]))
    results = await sw.sweep(provider, fresh_db)
    assert f"NVDA|{EXP}" in results
    env = results[f"NVDA|{EXP}"]
    assert env["symbol"] == "NVDA" and env["spot"] == pytest.approx(SPOT)
    assert sw.state.regime is not None            # regime quotes went through
    row = fresh_db.latest_simmer_readiness("NVDA", EXP)
    assert row is not None                        # engine verdict persisted


async def test_iv_history_gets_real_ohlc_and_yang_zhang_from_provider_bars(
        fresh_db, monkeypatch):
    provider = FakeYahooProvider()
    monkeypatch.setattr(sw, "watchlist_union",
                        lambda: _union([("NVDA", None)]))
    await sw.sweep(provider, fresh_db)
    hist = fresh_db.fetch_simmer_iv_history("NVDA", 5)
    assert len(hist) == 1
    row = hist[0]
    # the long-noted history gap is CLOSED under a bars-capable provider:
    assert row["open_px"] is not None and row["high_px"] is not None
    assert row["low_px"] is not None and row["close_px"] is not None
    assert row["rv20_yz"] is not None and row["rv20_yz"] > 0
    assert row["rv20_cc"] is not None
    assert any(sym == "NVDA" for sym, _ in provider.bars_calls)


async def test_iv_history_keeps_close_to_close_fallback_without_bars(
        fresh_db, monkeypatch):
    """Tradier provider (daily_bars == []) → the pre-provider fallback row,
    byte-for-byte: NULL OHL, sweep-spot close, NULL Yang-Zhang."""
    monkeypatch.setattr(sw, "watchlist_union",
                        lambda: _union([("NVDA", None)]))
    await sw.sweep(FakeSimmerTradier(), fresh_db)
    hist = fresh_db.fetch_simmer_iv_history("NVDA", 5)
    assert len(hist) == 1
    row = hist[0]
    assert row["open_px"] is None and row["high_px"] is None
    assert row["rv20_yz"] is None
    assert row["close_px"] == pytest.approx(SPOT)


def _expired_readiness(symbol="NVDA", expiration=None, structure="bull_put",
                       short=96.0, long_=92.0, days_before_exp=10) -> dict:
    expiration = expiration or (date.today() - timedelta(days=3)).isoformat()
    ts = (datetime.now(timezone.utc).replace(tzinfo=None)
          - timedelta(days=days_before_exp + 3))
    return {
        "ts": ts, "symbol": symbol, "expiration": expiration, "spot": 100.0,
        "score": 74.5, "vetoed": False, "veto_reasons": "[]",
        "components": json.dumps({"strikes": {"short": short, "long": long_}}),
        "regime": "contango", "structure": structure,
        "short_strike": short, "long_strike": long_, "width": abs(short - long_),
        "credit_mid": 0.8, "credit_fill": 0.75, "max_loss": 3.2,
        "pop_breakeven": 0.78, "expected_value": 0.05, "alpha": 0.016,
        "engine_version": "simmer-engine-test",
    }


def _outcome_row(db, rid):
    conn = db.connect()
    with db._lock:
        r = conn.execute(
            "SELECT held, touched FROM simmer_outcomes WHERE readiness_id = ?",
            [rid]).fetchone()
    return r


async def test_outcome_touched_filled_when_bars_cover_tenor(fresh_db):
    exp = (date.today() - timedelta(days=3)).isoformat()
    rid = fresh_db.insert_simmer_readiness(_expired_readiness(expiration=exp))
    # bars through today, with an intraday low poked below the 96 short
    provider = FakeYahooProvider(bars=_fake_bars(days=30, low_break=96.0),
                                 quotes={"NVDA": 100.0})
    n = await sw.evaluate_paper_outcomes(provider, fresh_db)
    assert n == 1
    held, touched = _outcome_row(fresh_db, rid)
    assert held is True          # settled back above the short
    assert touched is True       # ...but the strike WAS breached intraday


async def test_outcome_touched_false_when_bars_never_breach(fresh_db):
    exp = (date.today() - timedelta(days=3)).isoformat()
    rid = fresh_db.insert_simmer_readiness(_expired_readiness(expiration=exp))
    provider = FakeYahooProvider(bars=_fake_bars(days=30),   # lows stay ≥ ~97
                                 quotes={"NVDA": 100.0})
    await sw.evaluate_paper_outcomes(provider, fresh_db)
    held, touched = _outcome_row(fresh_db, rid)
    assert held is True and touched is False


async def test_outcome_touched_stays_null_when_bars_do_not_cover_tenor(fresh_db):
    exp = (date.today() - timedelta(days=3)).isoformat()
    rid = fresh_db.insert_simmer_readiness(_expired_readiness(expiration=exp))
    # bars END before the expiration → coverage incomplete → NULL, not a guess
    stale = _fake_bars(days=20, end=date.today() - timedelta(days=6))
    provider = FakeYahooProvider(bars=stale, quotes={"NVDA": 100.0})
    await sw.evaluate_paper_outcomes(provider, fresh_db)
    held, touched = _outcome_row(fresh_db, rid)
    assert held is True and touched is None


async def test_provider_notes_land_in_envelope_data_quality(fresh_db):
    provider = FakeYahooProvider(notes=["yahoo:chain_quality:NVDA@x:5/20_rejected"])
    env = await sw.analyze_symbol(provider, fresh_db, "NVDA", EXP, persist=False)
    prov_dq = env["data_quality"]["provider"]
    assert prov_dq["name"] == "yahoo"
    assert prov_dq["notes"] == ["yahoo:chain_quality:NVDA@x:5/20_rejected"]


async def test_tradier_default_path_adds_no_provider_block(fresh_db):
    env = await sw.analyze_symbol(FakeSimmerTradier(), fresh_db, "NVDA", EXP,
                                  persist=False)
    assert "provider" not in (env.get("data_quality") or {})


async def test_sweep_survives_a_fully_rate_limited_yahoo(fresh_db, monkeypatch):
    """A 429-ing Yahoo must degrade to a skipped symbol + calm regime — never
    a crashed loop."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, text="throttled")

    provider = YahooDataProvider(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(sw, "watchlist_union",
                        lambda: _union([("NVDA", None)]))
    results = await sw.sweep(provider, fresh_db)
    assert results == {}
    assert "NVDA" in sw.state.last_error_by_symbol
    assert sw.state.regime is not None
    assert sw.state.regime["state"] == "calm"      # degraded, noted
    assert sw.state.sweep_count == 1

"""Checked-in fixtures for the Simmer engine tests.

Simmer has **no JSX counterpart**, so there are no parity tests — the Python
engine is ground truth. That makes these fixtures the only guard against silent
math regressions, so they are explicit input→expected tables rather than
generated ones.

`CHAIN_ROWS` is a Black-Scholes chain on a $100 underlying at 30 DTE with a
realistic equity surface: put IV rises 0.35 vol-points per point of downside
(put skew) and call IV falls 0.12 per point of upside. Quotes are ±0.6% of mid,
floored at $0.02 wide. It is written out in full so a regression in any pricing
path shows up as a diff against literal numbers, not against a helper.

`OHLC_BARS` is an 11-bar series with genuine overnight gaps; its expected
Yang-Zhang / close-to-close / Parkinson values were computed by a **second,
independently written** implementation of each formula and are checked in below.
"""
from __future__ import annotations

import copy
import math
import random

import pytest

from app import simmer_config

# ── Option chain: spot 100, 30 DTE, r = q = 0 ──────────────────────────────
#   (side, strike, bid, ask, iv, delta)
CHAIN_ROWS: list[tuple[str, float, float, float, float, float]] = [
    ("put",  82, 0.15, 0.17, 0.3930, -0.0346),
    ("put",  84, 0.24, 0.26, 0.3860, -0.0515),
    ("put",  86, 0.37, 0.39, 0.3790, -0.0746),
    ("put",  88, 0.55, 0.57, 0.3720, -0.1053),
    ("put",  90, 0.81, 0.83, 0.3650, -0.1448),
    ("put",  92, 1.15, 1.17, 0.3580, -0.1939),
    ("put",  94, 1.60, 1.62, 0.3510, -0.2530),
    ("put",  96, 2.16, 2.19, 0.3440, -0.3216),
    ("put",  98, 2.88, 2.91, 0.3370, -0.3984),
    ("put", 100, 3.75, 3.80, 0.3300, -0.4811),
    ("call", 100, 3.75, 3.80, 0.3300, 0.5189),
    ("call", 102, 2.85, 2.88, 0.3276, 0.4349),
    ("call", 104, 2.11, 2.14, 0.3252, 0.3542),
    ("call", 106, 1.52, 1.54, 0.3228, 0.2798),
    ("call", 108, 1.06, 1.08, 0.3204, 0.2142),
    ("call", 110, 0.72, 0.74, 0.3180, 0.1587),
    ("call", 112, 0.47, 0.49, 0.3156, 0.1137),
    ("call", 114, 0.30, 0.32, 0.3132, 0.0786),
    ("call", 116, 0.18, 0.20, 0.3108, 0.0525),
    ("call", 118, 0.10, 0.12, 0.3084, 0.0338),
]

SPOT = 100.0
DTE = 30.0
T = DTE / 365.0


def chain(open_interest: int = 2500, size: int = 25) -> list[dict]:
    return [{"side": s, "strike": float(k), "bid": b, "ask": a, "iv": iv, "delta": d,
             "open_interest": open_interest, "bid_size": size, "ask_size": size}
            for (s, k, b, a, iv, d) in CHAIN_ROWS]


def clean_inputs() -> dict:
    """A candidate that clears every hard gate — the baseline every gate test
    mutates exactly one field of."""
    return {
        "symbol": "NVDA",
        "spot": SPOT,
        "dte": DTE,
        "expiration": "2026-09-18",
        "chain": chain(),
        "research": {
            "iv30": 0.33,
            "iv_rank": 55.0,
            "iv_percentile": 62.0,          # gate is on percentile, ≥ 40
            "iv_history_days": 252,
            "rv_yang_zhang": 0.244,         # IV/RV = 1.352, clears the 1.15 floor
            "rv_close_to_close": 0.239,     # agrees with YZ → not a gap artifact
            "term_slope": 1.05,
            "macro_valid_through_dte": 90,
            "catalysts": [],
            "walls": {"put_wall": 96.0, "call_wall": 106.0,
                      "zero_gamma": 97.0, "strength": "high"},
            "sentiment": {"score": 0.10, "velocity_p": 0.40},
            "days_to_cover": 2.0,
            "index": {"vix_ts": 0.90, "spot_below_gamma_flip": False},
        },
    }


def mutate(**research) -> dict:
    """clean_inputs() with `research` keys overridden."""
    inp = clean_inputs()
    inp["research"].update(research)
    return inp


def reason_kinds(out: dict) -> set[str]:
    """Veto reasons with any `structure:` prefix stripped, so a gate that trips
    on all three structures reads as one kind."""
    kinds = set()
    for r in out.get("veto_reasons") or []:
        for prefix in ("bull_put:", "bear_call:", "iron_condor:"):
            if r.startswith(prefix):
                r = r[len(prefix):]
                break
        kinds.add(r)
    return kinds


# ── Realized-vol fixtures ──────────────────────────────────────────────────
#   (open, high, low, close) — 11 bars with real overnight gaps
OHLC_BARS: list[tuple[float, float, float, float]] = [
    (100.00, 101.20,  99.40, 100.80),
    (101.30, 102.10, 100.60, 101.00),
    (100.40, 100.90,  98.80,  99.20),
    ( 99.60, 101.50,  99.30, 101.10),
    (101.80, 102.40, 100.90, 101.30),
    (100.70, 101.00,  99.10,  99.50),
    ( 99.00,  99.80,  97.60,  98.10),
    ( 98.60, 100.30,  98.40, 100.00),
    (100.50, 101.70, 100.10, 101.40),
    (101.90, 103.00, 101.50, 102.60),
    (102.20, 102.80, 101.00, 101.60),
]

# Verified against a second, independently written implementation of each
# estimator (see the module docstring). Annualized at 252 trading days.
EXPECTED_YANG_ZHANG = 0.19330835
EXPECTED_CLOSE_TO_CLOSE = 0.22500381     # zero-mean
EXPECTED_PARKINSON = 0.17445752
EXPECTED_YZ_K = 0.13269731               # k = 0.34 / (1.34 + (n+1)/(n−1)), n = 10


def ohlc_rows() -> list[dict]:
    return [{"open": o, "high": h, "low": lo, "close": c} for (o, h, lo, c) in OHLC_BARS]


def synthetic_ohlc(seed: int = 20260814, days: int = 600, vol: float = 0.30,
                   overnight_share: float = 0.35, steps: int = 390) -> list[dict]:
    """A GBM series with a **known** true vol and realistic overnight gaps —
    35% of daily variance arrives overnight, the rest over 390 intraday steps
    (so the high/low are sampled finely enough not to introduce their own
    discretization bias). Seeded, hence fully deterministic.

    This is the only generated fixture in the suite, and deliberately so: the
    property under test — that Yang-Zhang is unbiased where Parkinson is ~23%
    low — is a statistical statement that no small hand-written table can
    express."""
    rnd = random.Random(seed)
    daily_var = vol * vol / 252.0
    s_over = math.sqrt(daily_var * overnight_share)
    s_intra = math.sqrt(daily_var * (1.0 - overnight_share) / steps)
    price, bars = 100.0, []
    for _ in range(days):
        o = price * math.exp(rnd.gauss(0.0, s_over))
        x = hi = lo = o
        for _ in range(steps):
            x *= math.exp(rnd.gauss(0.0, s_intra))
            hi, lo = max(hi, x), min(lo, x)
        bars.append({"open": o, "high": hi, "low": lo, "close": x})
        price = x
    return bars


@pytest.fixture()
def cfg() -> dict:
    """Fully resolved engine config for NVDA. Deep-copied so a test that tweaks
    a threshold cannot leak into the next one."""
    return copy.deepcopy(simmer_config.resolved("NVDA"))


# ═══════════════════════════════════════════════════════════════════════════
# Phase 2 fixtures — watcher / routes / outcomes (no network, no Supabase)
# ═══════════════════════════════════════════════════════════════════════════

EXP = "2026-09-18"


def raw_tradier_chain(spot: float = SPOT, exp: str = EXP,
                      open_interest: int = 2500, size: int = 25) -> list[dict]:
    """CHAIN_ROWS re-emitted in RAW Tradier shape, so the watcher's use of
    poller._normalize_tradier_contract is exercised for real."""
    rows = []
    for (side, k, bid, ask, iv, delta) in CHAIN_ROWS:
        cp = "C" if side == "call" else "P"
        yymmdd = exp[2:].replace("-", "")
        rows.append({
            "symbol": f"XXX{yymmdd}{cp}{int(round(k * 1000)):08d}",
            "strike": float(k),
            "option_type": side,
            "expiration_date": exp,
            "bid": bid, "ask": ask, "last": (bid + ask) / 2,
            "open_interest": open_interest,
            "volume": 500,
            "bidsize": size, "asksize": size,
            "greeks": {"mid_iv": iv, "delta": delta,
                       "gamma": 0.02, "theta": -0.05, "vega": 0.10},
        })
    return rows


class FakeSimmerTradier:
    """Async stand-in for TradierClient covering everything the watcher calls:
    stock_quote (equities + VIX/VIX3M/SPY/QQQ), option_expirations,
    options_chain. `fail_symbols` raise on quote — the resilience path."""

    def __init__(self, spot: float = SPOT, exp: str = EXP, raw=None,
                 quotes: dict | None = None, fail_symbols=None,
                 vix: float | None = 15.0, vix3m: float | None = 17.0):
        self._spot = spot
        self._exp = exp
        self._raw = raw if raw is not None else raw_tradier_chain(spot, exp)
        self._quotes = dict(quotes or {})
        self._fail = set(fail_symbols or [])
        index = {"VIX": vix, "VIX3M": vix3m, "SPY": 500.0, "QQQ": 480.0}
        for k, v in index.items():
            self._quotes.setdefault(k, v)
        self.quote_calls: list[str] = []

    async def stock_quote(self, symbol: str) -> dict:
        sym = symbol.upper()
        self.quote_calls.append(sym)
        if sym in self._fail:
            raise RuntimeError(f"synthetic quote failure for {sym}")
        px = self._quotes.get(sym, self._spot)
        if px is None:
            return {}
        return {"symbol": sym, "last": px, "close": px, "prevclose": px}

    async def option_expirations(self, symbol: str) -> list[str]:
        if symbol.upper() in self._fail:
            raise RuntimeError(f"synthetic expiration failure for {symbol}")
        return [self._exp]

    async def options_chain(self, symbol: str, expiration: str, greeks: bool = True):
        return self._raw


@pytest.fixture()
def fresh_db(tmp_path):
    """A real (empty) DuckDB Database in a temp file — full schema, no state."""
    from app.db import Database
    db = Database(tmp_path / "simmer_test.duckdb")
    db.connect()
    yield db
    db.close()


@pytest.fixture(autouse=True)
def _reset_watcher_state():
    """The watcher state is a module-level singleton (routes hold a reference,
    so it is reset IN PLACE, never rebound)."""
    from app import simmer_watcher as sw

    def _reset():
        sw.state.latest_by_key.clear()
        sw.state.last_sweep_at = None
        sw.state.last_error = None
        sw.state.last_error_by_symbol.clear()
        sw.state.is_running = False
        sw.state.watched_count = 0
        sw.state.regime = None
        sw.state.sweep_count = 0
        sw.state.watchlist_source = "unknown"
        sw.state.dispersion = None
        sw.state.alert_states.clear()
        sw.state.alerts_fired = 0
        sw.state.last_outcome_run_at = None
        sw.state.last_outcome_run_ts = None
        sw.state.outcomes_recorded = 0
        sw.state.last_iv_history_date = None

    _reset()
    yield
    _reset()


def readiness_env(symbol: str = "NVDA", expiration: str = EXP,
                  score: float = 75.0, structure: str = "bull_put",
                  vetoed: bool = False, ivp_eff: float | None = 50.0) -> dict:
    """Synthetic engine envelope with just the fields the alert machine,
    dispersion check, and persistence path read."""
    return {
        "symbol": symbol.upper(),
        "expiration": expiration,
        "spot": SPOT,
        "score": 0.0 if vetoed else score,
        "decision": "vetoed" if vetoed else ("ready" if score >= 70 else "watch"),
        "structure": None if vetoed else structure,
        "strikes": None if vetoed else {"short": 94.0, "long": 90.0, "width": 4.0},
        "credit_mid": None if vetoed else 0.80,
        "credit_fill": None if vetoed else 0.76,
        "max_loss": None if vetoed else 3.20,
        "pop_breakeven": None if vetoed else 0.78,
        "expected_value": None if vetoed else 0.05,
        "alpha": None if vetoed else 0.016,
        "veto_reasons": ["iv_percentile_floor"] if vetoed else [],
        "avoid_if": [],
        "components": {"structural_safety": {"score": 0.8}},
        "data_quality": {},
        "metrics": {"iv_percentile_effective": ivp_eff, "atm_iv": 0.33,
                    "vrp": 1.3, "em_1sd": 5.5},
        "regime": {"state": "contango"},
        "engine_version": "simmer-engine-test",
        "computed_at": "2026-08-15T14:00:00Z",
    }

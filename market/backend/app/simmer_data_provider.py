"""Simmer market-data provider abstraction — Tradier (default) or Yahoo.

WHY THIS EXISTS. Matrix (the poller) and Torque already lean on Tradier's
120 req/min market-data budget. Simmer's 5-minute union-of-watchlists sweep
adds a quote + expirations + chain call per watched symbol, so at a realistic
watchlist size the sweep starts competing with the order path for quota. This
module makes the sweep's data source a config switch (`SIMMER_DATA_PROVIDER`)
so the watcher can be pointed at an alternative provider while Tradier stays
fully supported — and remains the default, so existing deployments change
nothing.

DESIGN — a deliberate hybrid, not a full abstraction:
  * `SimmerDataProvider` is a small Protocol covering exactly the four calls
    the watcher makes (`chain`, `quote`, `expirations`, `daily_bars`) plus a
    `provider_name` and a data_quality note channel. It is NOT a general
    brokerage abstraction — orders, streaming, and the poller stay on their
    existing clients.
  * `TradierDataProvider` is a thin adapter over the existing `TradierClient`
    plus `poller._normalize_tradier_contract` — no new Tradier surface, no
    behavior change. `daily_bars` returns [] with a data_quality note because
    TradierClient deliberately has no history endpoint.
  * `YahooDataProvider` speaks Yahoo Finance's public query endpoints
    (v7 options, v8 chart). Yahoo's chain carries per-contract IV and open
    interest but no greeks, so greeks are computed ANALYTICALLY from Yahoo's
    IV with the Black-Scholes helpers below (which reuse `simmer_engine`'s
    parity-locked pricing internals — no duplicated math). Its v8 chart gives
    real daily OHLC, which closes two long-noted gaps: Yang-Zhang RV gets real
    high/low instead of the close-to-close fallback, and the paper-outcome
    evaluator can fill `touched`.

⚠️ YAHOO ToS NOTE (same posture as the Nasdaq earnings cross-check in
`simmer_calendar.py`): query{1,2}.finance.yahoo.com is an UNDOCUMENTED
internal endpoint — it powers Yahoo's own pages, has no SLA, no published
terms permitting programmatic use, sometimes demands a cookie+crumb
handshake, and 429s under load. Fine for a personal deployment; MUST be
revisited before Simmer is sold commercially. This is one reason the default
provider stays `tradier`. By construction every Yahoo failure degrades soft:
a structured `ProviderError` that the watcher's per-symbol error swallowing
turns into a skipped symbol and a data_quality note — never a crashed loop,
and (following the EDGAR discipline in `simmer_catalysts`) never a retry
storm on 429.

Data-quality guards on the Yahoo chain (mandatory — Yahoo per-contract IV is
occasionally stale/absurd on illiquid strikes): IV outside [0.01, 5.0] is
rejected (contract keeps its quotes, iv/greeks read 0.0 — the same "missing
greeks" convention as an empty Tradier greeks block), contracts quoted
bid=ask=0 are dropped, and a data_quality note is stamped when >20% of a
chain is rejected. The engine's chain-sanity gate handles the rest.
"""
from __future__ import annotations

import asyncio
import logging
import math
from datetime import date, datetime, timezone
from typing import Any, Protocol, runtime_checkable

import httpx

from . import torque_config as tcfg
from .poller import _normalize_tradier_contract
from .simmer_engine import bs_call, bs_delta, bs_put, bs_vega, d1_d2, norm_cdf, norm_pdf

log = logging.getLogger("edgelane.simmer.provider")

# IV sanity bounds for Yahoo's per-contract impliedVolatility (annualized).
IV_MIN, IV_MAX = 0.01, 5.0
# Stamp a data_quality note when more than this share of a chain is rejected.
CHAIN_REJECT_NOTE_SHARE = 0.20
# Cap on the note channel so a long-running provider can't grow it unbounded.
_MAX_NOTES = 20


class ProviderError(RuntimeError):
    """Any provider failure, surfaced as a clean message. The watcher's
    per-symbol error swallowing turns this into a skipped symbol."""


class ProviderRateLimitError(ProviderError):
    """429 — throttled. Never retried here (retrying a throttle is what turns
    it into a block; see the EDGAR client's discipline)."""


# ─────────────────────────────────────────────────────────────────────────────
# Analytic Black-Scholes greeks (from a known IV)
# ─────────────────────────────────────────────────────────────────────────────
# Conventions match what the Tradier/ORATS chain feeds the engine, so a
# provider-computed contract is indistinguishable downstream:
#   delta  signed (puts negative)
#   gamma  ∂delta/∂S (always ≥ 0)
#   theta  PER CALENDAR DAY (annual/365, negative for long options)
#   vega   per 1 VOL POINT (1% IV move), i.e. simmer_engine.bs_vega / 100
# Pricing internals (d1_d2/norm_cdf/norm_pdf, bs_put/bs_call for the tests'
# finite-difference cross-check) are reused from simmer_engine — never
# duplicated.

def bs_gamma(spot: float, strike: float, sigma: float, t: float,
             r: float = 0.0, q: float = 0.0) -> float:
    if spot <= 0 or strike <= 0 or sigma <= 0 or t <= 0:
        return 0.0
    d1, _ = d1_d2(spot, strike, sigma, t, r, q)
    return math.exp(-q * t) * norm_pdf(d1) / (spot * sigma * math.sqrt(t))


def bs_theta_annual(spot: float, strike: float, sigma: float, t: float,
                    side: str, r: float = 0.0, q: float = 0.0) -> float:
    """∂price/∂(calendar time) per YEAR (negative for long options)."""
    if spot <= 0 or strike <= 0 or sigma <= 0 or t <= 0:
        return 0.0
    d1, d2 = d1_d2(spot, strike, sigma, t, r, q)
    decay = -spot * math.exp(-q * t) * norm_pdf(d1) * sigma / (2.0 * math.sqrt(t))
    if side == "call":
        return decay - r * strike * math.exp(-r * t) * norm_cdf(d2) \
            + q * spot * math.exp(-q * t) * norm_cdf(d1)
    return decay + r * strike * math.exp(-r * t) * norm_cdf(-d2) \
        - q * spot * math.exp(-q * t) * norm_cdf(-d1)


def analytic_greeks(spot: float, strike: float, sigma: float, t: float,
                    side: str, r: float = 0.0, q: float = 0.0) -> dict[str, float]:
    """delta/gamma/theta/vega in chain conventions (see block comment above).
    Degenerate inputs (t≤0, σ≤0) return zeros rather than garbage."""
    if spot <= 0 or strike <= 0 or sigma <= 0 or t <= 0:
        return {"delta": 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0}
    return {
        "delta": bs_delta(spot, strike, sigma, t, side, r, q),
        "gamma": bs_gamma(spot, strike, sigma, t, r, q),
        "theta": bs_theta_annual(spot, strike, sigma, t, side, r, q) / 365.0,
        "vega": bs_vega(spot, strike, sigma, t, r, q) / 100.0,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Protocol
# ─────────────────────────────────────────────────────────────────────────────
@runtime_checkable
class SimmerDataProvider(Protocol):
    """The exact market-data surface the Simmer watcher consumes.

    `chain` returns NORMALIZED contract dicts — the same shape
    `poller._normalize_tradier_contract` produces (symbol/strike/side/
    expiration/delta/gamma/theta/vega/iv/open_interest/volume/bid/ask/last/
    last_volume/trade_date/bid_size/ask_size) — so the engine never knows
    which provider fed it. `quote` returns a dict readable by the watcher's
    `_first_price` (keys last/close/prevclose, plus bid/ask where available).
    `daily_bars` returns [{date, open, high, low, close}] oldest→newest, or []
    where the provider has no history endpoint (with a data_quality note)."""

    provider_name: str

    async def chain(self, symbol: str, expiration: str) -> list[dict]: ...
    async def quote(self, symbol: str) -> dict: ...
    async def expirations(self, symbol: str) -> list[str]: ...
    async def daily_bars(self, symbol: str, days: int) -> list[dict]: ...
    def take_data_quality_notes(self) -> list[str]: ...


class _NotesMixin:
    """Shared data_quality note channel: providers append, consumers drain."""

    def __init__(self) -> None:
        self._notes: list[str] = []

    def _note(self, note: str) -> None:
        if note not in self._notes and len(self._notes) < _MAX_NOTES:
            self._notes.append(note)

    def take_data_quality_notes(self) -> list[str]:
        notes, self._notes = self._notes, []
        return notes


# ─────────────────────────────────────────────────────────────────────────────
# Tradier
# ─────────────────────────────────────────────────────────────────────────────
class TradierDataProvider(_NotesMixin):
    """Thin adapter over the existing TradierClient — identical data path to
    what the watcher did before the abstraction existed."""

    provider_name = "tradier"

    def __init__(self, tradier_client: Any) -> None:
        super().__init__()
        self._tradier = tradier_client

    async def chain(self, symbol: str, expiration: str) -> list[dict]:
        raw = await self._tradier.options_chain(symbol, expiration)
        return [_normalize_tradier_contract(o) for o in raw or []
                if o.get("strike") is not None]

    async def quote(self, symbol: str) -> dict:
        return await self._tradier.stock_quote(symbol)

    async def expirations(self, symbol: str) -> list[str]:
        return await self._tradier.option_expirations(symbol)

    async def daily_bars(self, symbol: str, days: int) -> list[dict]:
        # TradierClient deliberately exposes no history endpoint (and per the
        # abstraction's ground rules we do not add one). Callers keep their
        # pre-provider fallback behavior: close-to-close RV, touched=NULL.
        self._note("daily_bars:unsupported_by_tradier_client")
        return []


# ─────────────────────────────────────────────────────────────────────────────
# Yahoo
# ─────────────────────────────────────────────────────────────────────────────
_YAHOO_BASE = "https://query1.finance.yahoo.com"
_YAHOO_OPTIONS_URL = _YAHOO_BASE + "/v7/finance/options/{sym}"
_YAHOO_CHART_URL = _YAHOO_BASE + "/v8/finance/chart/{sym}"
_YAHOO_CRUMB_URL = _YAHOO_BASE + "/v1/test/getcrumb"
_YAHOO_COOKIE_URL = "https://fc.yahoo.com/"

# Same browser-shaped UA convention as the existing Yahoo spot fetch in
# routes/torque.py — Yahoo rejects non-browser agents intermittently.
_YAHOO_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
    "Accept": "application/json, text/plain, */*",
}

# Index symbols the sweep quotes that torque_config.YAHOO_SYMBOLS doesn't map
# (torque never trades them; the regime computation quotes them every sweep).
_YAHOO_EXTRA_SYMBOLS = {"VIX": "^VIX", "VIX3M": "^VIX3M"}

# range strings for the v8 chart endpoint, smallest window that covers `days`
# TRADING days (calendar ranges hold ~21 trading days/month).
_RANGE_LADDER = ((20, "3mo"), (100, "6mo"), (230, "1y"), (480, "2y"))


def _yahoo_symbol(symbol: str) -> str:
    """Reuse torque_config's Yahoo symbol map (indices need ^-prefixes), extend
    it with the regime tickers, and fall back to the bare symbol (US equities
    map 1:1)."""
    sym = (symbol or "").upper()
    return tcfg.yahoo_symbol(sym) or _YAHOO_EXTRA_SYMBOLS.get(sym) or sym


def _f(v: Any, default: float = 0.0) -> float:
    if v is None or v == "":
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _i(v: Any, default: int = 0) -> int:
    if v is None or v == "":
        return default
    try:
        return int(v)
    except (TypeError, ValueError):
        try:
            return int(float(v))
        except (TypeError, ValueError):
            return default


def _exp_unix(expiration: str) -> int:
    """Yahoo's ?date= values are midnight UTC of the expiration date."""
    d = date.fromisoformat(str(expiration)[:10])
    return int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp())


def _unix_to_iso(ts: Any) -> str | None:
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).date().isoformat()
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def normalize_yahoo_contract(o: dict, side: str, expiration: str,
                             spot: float | None, t: float) -> dict | None:
    """One Yahoo v7 contract → the normalized shape, or None when REJECTED
    (unusable quote: bid=ask=0, or no strike). IV outside [IV_MIN, IV_MAX] is
    treated as missing (iv/greeks 0.0 — the empty-Tradier-greeks convention),
    not fabricated. Yahoo has no last_volume/trade_date/size data; those carry
    the same defaults the Tradier normalizer emits for absent fields."""
    strike = o.get("strike")
    if strike is None:
        return None
    bid, ask = _f(o.get("bid")), _f(o.get("ask"))
    if bid <= 0.0 and ask <= 0.0:
        return None                       # unquoted/dead strike — reject
    iv = _f(o.get("impliedVolatility"))
    iv_ok = IV_MIN <= iv <= IV_MAX
    greeks = (analytic_greeks(spot, float(strike), iv, t, side)
              if (iv_ok and spot and spot > 0)
              else {"delta": 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0})
    return {
        "symbol": o.get("contractSymbol"),
        "strike": _f(strike),
        "side": "call" if side == "call" else "put",
        "expiration": expiration,
        "delta": greeks["delta"],
        "gamma": greeks["gamma"],
        "theta": greeks["theta"],
        "vega": greeks["vega"],
        "iv": iv if iv_ok else 0.0,
        "open_interest": _i(o.get("openInterest")),
        "volume": _i(o.get("volume")),
        "bid": bid,
        "ask": ask,
        "last": _f(o.get("lastPrice")),
        "last_volume": 0,                 # not exposed by Yahoo
        "trade_date": None,               # not exposed by Yahoo
        # Book sizes are None, NOT 0: the engine's size-at-touch liquidity
        # gate reads 0 as "no size quoted at the touch" (veto) but skips None
        # ("unknown"). Yahoo simply doesn't expose top-of-book size, so 0 here
        # would systematically veto every Yahoo-fed candidate.
        "bid_size": None,
        "ask_size": None,
    }


class YahooDataProvider(_NotesMixin):
    """Yahoo Finance v7 options + v8 chart. See the module docstring for the
    ToS posture and degradation rules.

    Session handling: Yahoo intermittently requires a cookie+crumb handshake
    (cookie from fc.yahoo.com, crumb from /v1/test/getcrumb) and answers
    401/403 without it. The handshake is attempted lazily on the first auth
    failure, then the failing request is retried ONCE. 429 is never retried.
    `transport` is injectable for tests (httpx.MockTransport — no network)."""

    provider_name = "yahoo"

    def __init__(self, timeout: float = 15.0,
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        super().__init__()
        self.timeout = timeout
        self._transport = transport
        self._client: httpx.AsyncClient | None = None
        self._crumb: str | None = None

    # -- plumbing --

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=self.timeout, headers=dict(_YAHOO_HEADERS),
                follow_redirects=True, transport=self._transport)
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _bootstrap_session(self) -> None:
        """Cookie from fc.yahoo.com (the response body is irrelevant — the
        point is Set-Cookie on the shared client), then the crumb. Both are
        best-effort: many deployments never need them, and a failed handshake
        only means the next request keeps going without a crumb."""
        client = self._get_client()
        try:
            await client.get(_YAHOO_COOKIE_URL)
        except httpx.HTTPError:
            pass                          # cookie fetch is best-effort
        try:
            resp = await client.get(_YAHOO_CRUMB_URL)
            crumb = (resp.text or "").strip()
            if resp.status_code == 200 and crumb and "<" not in crumb:
                self._crumb = crumb
                return
        except httpx.HTTPError:
            pass
        self._note("yahoo:crumb_unavailable")

    async def _get_json(self, url: str, params: dict[str, Any] | None = None,
                        _auth_retried: bool = False,
                        _retried: bool = False) -> dict:
        client = self._get_client()
        req_params = dict(params or {})
        if self._crumb:
            req_params["crumb"] = self._crumb
        try:
            resp = await client.get(url, params=req_params)
        except httpx.TimeoutException as e:
            raise ProviderError(
                f"Yahoo GET {url}: timed out after {self.timeout}s ({e!s})") from e
        except httpx.RequestError as e:
            if not _retried:
                await asyncio.sleep(2.0)
                return await self._get_json(url, params, _auth_retried, _retried=True)
            raise ProviderError(f"Yahoo GET {url}: network — {e!s}") from e

        if resp.status_code in (401, 403):
            # Missing/expired cookie+crumb. Handshake once, retry once.
            if not _auth_retried:
                await self._bootstrap_session()
                return await self._get_json(url, params, _auth_retried=True,
                                            _retried=_retried)
            self._note("yahoo:auth_failed")
            raise ProviderError(
                f"Yahoo GET {url}: HTTP {resp.status_code} after cookie/crumb "
                f"handshake — endpoint blocked")
        if resp.status_code == 429:
            # NO retry — see simmer_catalysts.EdgarClient: retrying a throttle
            # is what turns it into a block. The watcher skips the symbol this
            # sweep and tries again next sweep (5 min later).
            self._note("yahoo:rate_limited")
            raise ProviderRateLimitError(f"Yahoo GET {url}: HTTP 429 — throttled")
        if 500 <= resp.status_code < 600:
            if not _retried:
                await asyncio.sleep(2.0)
                return await self._get_json(url, params, _auth_retried, _retried=True)
            raise ProviderError(
                f"Yahoo GET {url}: HTTP {resp.status_code} (after retry)")
        if resp.status_code >= 400:
            raise ProviderError(
                f"Yahoo GET {url}: HTTP {resp.status_code} — {resp.text[:200]}")
        try:
            return resp.json()
        except ValueError as e:
            raise ProviderError(f"Yahoo GET {url}: non-JSON response — {e!s}") from e

    @staticmethod
    def _result0(payload: dict, root: str) -> dict:
        results = ((payload or {}).get(root) or {}).get("result") or []
        if not results:
            err = ((payload or {}).get(root) or {}).get("error")
            raise ProviderError(f"Yahoo {root}: empty result ({err})")
        return results[0] or {}

    # -- provider surface --

    async def quote(self, symbol: str) -> dict:
        """Spot via the v8 chart meta (the same read routes/torque.py uses for
        its live spot). Yahoo's meta has no top-of-book, so bid/ask are None —
        the watcher's `_first_price` only reads last/close/prevclose."""
        payload = await self._get_json(
            _YAHOO_CHART_URL.format(sym=_yahoo_symbol(symbol)),
            {"range": "1d", "interval": "1d"})
        meta = self._result0(payload, "chart").get("meta") or {}
        last = meta.get("regularMarketPrice")
        prev = meta.get("previousClose") or meta.get("chartPreviousClose")
        return {"symbol": (symbol or "").upper(),
                "last": last, "bid": None, "ask": None,
                "close": prev, "prevclose": prev}

    async def expirations(self, symbol: str) -> list[str]:
        payload = await self._get_json(
            _YAHOO_OPTIONS_URL.format(sym=_yahoo_symbol(symbol)))
        stamps = self._result0(payload, "optionChain").get("expirationDates") or []
        out = [d for d in (_unix_to_iso(ts) for ts in stamps) if d]
        return sorted(set(out))

    async def chain(self, symbol: str, expiration: str) -> list[dict]:
        exp = str(expiration)[:10]
        payload = await self._get_json(
            _YAHOO_OPTIONS_URL.format(sym=_yahoo_symbol(symbol)),
            {"date": _exp_unix(exp)})
        result = self._result0(payload, "optionChain")
        # Spot for the analytic greeks: the chain response embeds the
        # underlying quote, saving a second request per sweep symbol.
        spot = _f((result.get("quote") or {}).get("regularMarketPrice"), 0.0) or None
        if not spot:
            try:
                spot = (await self.quote(symbol)).get("last")
            except ProviderError:
                spot = None
        if not spot:
            self._note(f"yahoo:no_spot_for_greeks:{(symbol or '').upper()}")
        t = max((date.fromisoformat(exp) - date.today()).days, 0) / 365.0
        blocks = result.get("options") or [{}]
        block = blocks[0] or {}
        contracts: list[dict] = []
        total = rejected = 0
        for side, key in (("call", "calls"), ("put", "puts")):
            for o in block.get(key) or []:
                total += 1
                c = normalize_yahoo_contract(o, side, exp, spot, t)
                if c is None:
                    rejected += 1
                    continue
                if c["iv"] == 0.0:
                    rejected += 1         # kept, but IV was stale/absurd
                contracts.append(c)
        if total and rejected / total > CHAIN_REJECT_NOTE_SHARE:
            self._note(f"yahoo:chain_quality:{(symbol or '').upper()}@{exp}:"
                       f"{rejected}/{total}_rejected")
        return contracts

    async def daily_bars(self, symbol: str, days: int) -> list[dict]:
        rng = next((r for lo, r in _RANGE_LADDER if days <= lo), "5y")
        payload = await self._get_json(
            _YAHOO_CHART_URL.format(sym=_yahoo_symbol(symbol)),
            {"range": rng, "interval": "1d"})
        result = self._result0(payload, "chart")
        stamps = result.get("timestamp") or []
        quote = ((result.get("indicators") or {}).get("quote") or [{}])[0] or {}
        opens = quote.get("open") or []
        highs = quote.get("high") or []
        lows = quote.get("low") or []
        closes = quote.get("close") or []
        bars: list[dict] = []
        for idx, ts in enumerate(stamps):
            row = {
                "date": _unix_to_iso(ts),
                "open": opens[idx] if idx < len(opens) else None,
                "high": highs[idx] if idx < len(highs) else None,
                "low": lows[idx] if idx < len(lows) else None,
                "close": closes[idx] if idx < len(closes) else None,
            }
            # Yahoo pads holidays/halts with nulls — skip incomplete rows.
            if row["date"] and None not in (row["open"], row["high"],
                                            row["low"], row["close"]):
                bars.append(row)
        return bars[-int(days):] if days and days > 0 else bars


# ─────────────────────────────────────────────────────────────────────────────
# Coercion + factory
# ─────────────────────────────────────────────────────────────────────────────
def as_provider(source: Any) -> Any:
    """Accept either a SimmerDataProvider or a raw Tradier-shaped client and
    return a provider. This keeps every watcher entry point callable with a
    bare TradierClient (all pre-abstraction call sites and tests) while the
    loop itself passes a configured provider."""
    if hasattr(source, "provider_name") and hasattr(source, "daily_bars"):
        return source
    return TradierDataProvider(source)


def get_simmer_provider(settings: Any, tradier_client: Any) -> Any:
    """Factory reading SIMMER_DATA_PROVIDER. Unknown/unsupported values fall
    back to Tradier LOUDLY (a silent default here would hide a config typo)."""
    name = str(getattr(settings, "simmer_data_provider", "tradier") or "tradier").lower()
    if name == "yahoo":
        log.info("simmer data provider: yahoo (Tradier quota untouched by the sweep)")
        return YahooDataProvider()
    if name != "tradier":
        log.warning("SIMMER_DATA_PROVIDER=%r not recognized — falling back to tradier",
                    name)
    return TradierDataProvider(tradier_client)

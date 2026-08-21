"""Standalone earnings-bias analyzer — Simmer's opt-in "earnings mode".

Takes `(ticker, earnings_date)`, fetches recent earnings-related headlines
(Alpaca), asks Gemini for a directional-bias verdict, and returns a compact,
cacheable result. It is DECOUPLED from the sweep: Simmer calls this only when an
expiry falls in an earnings window, and it never touches options data (strikes,
premium, IV/skew stay on Simmer's side). It never vetoes — it produces a verdict
Simmer folds into the score.

Phase 1 is news-only; a fundamentals/estimates read is a later phase (the
`source` field distinguishes them). All I/O is injectable (httpx transports) for
tests, mirroring app/simmer_news.py. Degrades to a neutral no-go on any missing
input (no keys, no news) — never raises to the caller.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Sequence

import httpx

from .simmer_news import (
    AlpacaNewsClient,
    DEFAULT_GEMINI_MODEL,
    GEMINI_BASE,
)

log = logging.getLogger("edgelane.simmer.earnings")

DIRECTIONS = ("bullish", "bearish", "neutral")
_MAX_HEADLINES = 40
_DEFAULT_LOOKBACK_DAYS = 14


class EarningsError(Exception):
    """Malformed provider response (Gemini). Callers degrade, never crash."""


def _earnings_settings(settings) -> tuple[str, str, str, str]:
    """(alpaca_key, alpaca_secret, gemini_key, gemini_model) — defensive getattr,
    same style as simmer_news._news_settings."""
    return (
        getattr(settings, "alpaca_key_id", "") or "",
        getattr(settings, "alpaca_secret_key", "") or "",
        getattr(settings, "gemini_api_key", "") or "",
        getattr(settings, "gemini_model", "") or DEFAULT_GEMINI_MODEL,
    )


def build_earnings_request(symbol: str, headlines: Sequence[str],
                           *, temperature: float = 0.0) -> dict:
    """Gemini structured-output request for ONE earnings-bias verdict.

    Mirrors simmer_news.build_gemini_request: pins responseMimeType=json + a
    typed responseSchema and temperature 0 so the output is deterministic and
    parseable. The prompt forces low confidence + go=false on mixed/thin signals
    (reliability, not move prediction)."""
    lines = "\n".join(f"- {h}" for h in list(headlines)[:_MAX_HEADLINES]) \
        or "(no recent headlines)"
    prompt = (
        f"You are an options-desk analyst deciding whether it is worth SELLING a "
        f"credit spread into {symbol}'s upcoming earnings. Using ONLY the recent "
        f"headlines below, judge the directional lean and how CONSISTENT and "
        f"reliable that signal is. You are NOT predicting the exact move — you are "
        f"judging bias reliability. Mixed, contradictory, or thin coverage MUST "
        f"yield low confidence and go=false. Only set go=true when the lean is "
        f"clear and consistent.\n\nHeadlines:\n{lines}"
    )
    schema = {
        "type": "OBJECT",
        "properties": {
            "direction": {"type": "STRING", "enum": list(DIRECTIONS)},
            "confidence": {"type": "NUMBER"},
            "go": {"type": "BOOLEAN"},
            "rationale": {"type": "STRING"},
        },
        "required": ["direction", "confidence", "go", "rationale"],
    }
    return {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": temperature,
            "responseMimeType": "application/json",
            "responseSchema": schema,
        },
    }


def parse_earnings_bias(payload: dict) -> dict:
    """Extract + clamp the verdict from a Gemini generateContent response.
    Raises EarningsError on malformed JSON so the caller can skip cleanly."""
    try:
        text = payload["candidates"][0]["content"]["parts"][0]["text"]
        obj = json.loads(text)
    except (KeyError, IndexError, TypeError, ValueError) as e:
        raise EarningsError(f"malformed Gemini earnings response: {e}") from e
    direction = str(obj.get("direction", "neutral")).lower()
    if direction not in DIRECTIONS:
        direction = "neutral"
    try:
        conf = max(0.0, min(1.0, float(obj.get("confidence"))))
    except (TypeError, ValueError):
        conf = 0.0
    return {
        "direction": direction,
        "confidence": conf,
        "go": bool(obj.get("go", False)),
        "rationale": str(obj.get("rationale", ""))[:500],
    }


def _neutral(reasons: list[str]) -> dict:
    return {"direction": "neutral", "confidence": 0.0, "go": False,
            "rationale": "", "_reasons": list(reasons)}


class EarningsBiasAnalyzer:
    """News-driven earnings bias. Injectable Alpaca client + Gemini transport.

    Build it via `build_earnings_analyzer(settings)` in production; construct it
    directly with fakes in tests."""

    def __init__(self, alpaca: AlpacaNewsClient | None, gemini_key: str,
                 gemini_model: str = DEFAULT_GEMINI_MODEL, *,
                 timeout: float = 30.0,
                 gemini_transport: httpx.AsyncBaseTransport | None = None,
                 lookback_days: int = _DEFAULT_LOOKBACK_DAYS):
        self._alpaca = alpaca
        self._key = gemini_key or ""
        self._model = gemini_model or DEFAULT_GEMINI_MODEL
        self._timeout = timeout
        self._gt = gemini_transport
        self._lookback = lookback_days

    async def _headlines(self, symbol: str, now: datetime) -> list[str]:
        if not self._alpaca:
            return []
        start = now - timedelta(days=self._lookback)
        arts = await self._alpaca.fetch_news([symbol.upper()], start=start)
        return [a["headline"] for a in arts if isinstance(a, dict) and a.get("headline")]

    async def _gemini_bias(self, symbol: str, headlines: Sequence[str]) -> dict:
        url = f"{GEMINI_BASE}/models/{self._model}:generateContent"
        body = build_earnings_request(symbol, headlines)
        async with httpx.AsyncClient(timeout=self._timeout, transport=self._gt) as c:
            r = await c.post(
                url,
                headers={"x-goog-api-key": self._key,
                         "Content-Type": "application/json"},
                json=body,
            )
            r.raise_for_status()
            return parse_earnings_bias(r.json())

    async def analyze(self, symbol: str, earnings_date: str | None = None,
                      *, now: datetime | None = None) -> dict:
        """Return a compact, cacheable earnings-bias verdict.

        Shape: {symbol, earnings_date, direction, confidence, go, rationale,
        headline_count, reasons (list), source, computed_at}. Never raises."""
        now = now or datetime.now(timezone.utc)
        symbol = symbol.upper()
        reasons: list[str] = []

        headlines: list[str] = []
        try:
            headlines = await self._headlines(symbol, now)
        except Exception as e:                       # provider hiccup — degrade
            log.warning("earnings news fetch failed for %s: %s", symbol, e)
            reasons.append("news_unavailable")

        if not self._key:
            reasons.append("gemini_key_missing")
        if not headlines and "news_unavailable" not in reasons:
            reasons.append("no_recent_headlines")

        verdict = _neutral(reasons)
        if self._key and headlines:
            try:
                v = await self._gemini_bias(symbol, headlines)
                verdict = {**v, "_reasons": reasons}
            except Exception as e:
                log.warning("earnings bias synth failed for %s: %s", symbol, e)
                reasons.append("bias_synth_failed")
                verdict = _neutral(reasons)

        return {
            "symbol": symbol,
            "earnings_date": earnings_date,
            "direction": verdict["direction"],
            "confidence": verdict["confidence"],
            "go": verdict["go"],
            "rationale": verdict["rationale"],
            "headline_count": len(headlines),
            "reasons": verdict["_reasons"],
            "source": "news",
            "computed_at": now,
        }


def build_earnings_analyzer(settings, *,
                            alpaca: AlpacaNewsClient | None = None,
                            gemini_transport: httpx.AsyncBaseTransport | None = None,
                            lookback_days: int = _DEFAULT_LOOKBACK_DAYS
                            ) -> EarningsBiasAnalyzer:
    """Production builder: reads Alpaca + Gemini creds off `settings`. Missing
    keys are fine — the analyzer degrades to a neutral no-go with reasons."""
    kid, sec, gkey, gmodel = _earnings_settings(settings)
    if alpaca is None and kid and sec:
        alpaca = AlpacaNewsClient(kid, sec)
    return EarningsBiasAnalyzer(alpaca, gkey, gmodel,
                                gemini_transport=gemini_transport,
                                lookback_days=lookback_days)

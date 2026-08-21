"""EarningsBiasAnalyzer — Simmer's standalone earnings-mode verdict.

All I/O faked with httpx.MockTransport (Alpaca + Gemini), mirroring test_news.py.
asyncio_mode=auto, so async tests run without a marker.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import httpx

from app import earnings_engine as ee
from app import simmer_news as sn

NOW = datetime(2026, 8, 10, tzinfo=timezone.utc)


def _alpaca(headlines):
    def handler(request):
        news = [
            {"id": i, "headline": h, "source": "wire", "url": f"http://x/{i}",
             "symbols": ["NVDA"], "created_at": "2026-08-01T12:00:00Z",
             "updated_at": "2026-08-01T12:00:00Z"}
            for i, h in enumerate(headlines)
        ]
        return httpx.Response(200, json={"news": news, "next_page_token": None})
    return sn.AlpacaNewsClient("k", "s", transport=httpx.MockTransport(handler))


def _gemini(verdict=None, *, raw=None):
    def handler(request):
        text = raw if raw is not None else json.dumps(verdict)
        return httpx.Response(
            200, json={"candidates": [{"content": {"parts": [{"text": text}]}}]})
    return httpx.MockTransport(handler)


async def test_full_path_returns_bias():
    a = ee.EarningsBiasAnalyzer(
        _alpaca(["NVDA crushes estimates", "Analysts raise targets"]),
        "gm", gemini_transport=_gemini(
            {"direction": "bullish", "confidence": 0.8, "go": True, "rationale": "strong beat"}))
    out = await a.analyze("nvda", "2026-08-27", now=NOW)
    assert out["direction"] == "bullish"
    assert out["go"] is True
    assert 0.79 < out["confidence"] < 0.81
    assert out["headline_count"] == 2
    assert out["source"] == "news"
    assert out["symbol"] == "NVDA" and out["earnings_date"] == "2026-08-27"
    assert out["reasons"] == []


async def test_no_gemini_key_degrades_neutral():
    a = ee.EarningsBiasAnalyzer(_alpaca(["h1"]), "", gemini_transport=_gemini(
        {"direction": "bullish", "confidence": 1, "go": True, "rationale": "x"}))
    out = await a.analyze("nvda", "2026-08-27", now=NOW)
    assert out["go"] is False
    assert out["direction"] == "neutral" and out["confidence"] == 0.0
    assert "gemini_key_missing" in out["reasons"]
    # headlines were still fetched — the analyzer just can't synthesize a bias
    assert out["headline_count"] == 1


async def test_no_headlines_degrades_neutral():
    a = ee.EarningsBiasAnalyzer(None, "gm", gemini_transport=_gemini(
        {"direction": "bullish", "confidence": 1, "go": True, "rationale": "x"}))
    out = await a.analyze("nvda", "2026-08-27", now=NOW)
    assert out["go"] is False and out["headline_count"] == 0
    assert "no_recent_headlines" in out["reasons"]


async def test_malformed_gemini_degrades_with_reason():
    a = ee.EarningsBiasAnalyzer(_alpaca(["h1"]), "gm", gemini_transport=_gemini(raw="not json{{"))
    out = await a.analyze("nvda", "2026-08-27", now=NOW)
    assert out["go"] is False and out["direction"] == "neutral"
    assert "bias_synth_failed" in out["reasons"]


def test_parse_clamps_and_coerces():
    text = json.dumps({"direction": "sideways", "confidence": 9, "go": True, "rationale": "r"})
    p = ee.parse_earnings_bias({"candidates": [{"content": {"parts": [{"text": text}]}}]})
    assert p["direction"] == "neutral"      # bad enum → neutral
    assert p["confidence"] == 1.0           # clamped to [0,1]
    assert p["go"] is True


def test_request_pins_schema_and_temp_zero():
    body = ee.build_earnings_request("NVDA", ["a", "b"])
    gc = body["generationConfig"]
    assert gc["responseMimeType"] == "application/json"
    assert gc["temperature"] == 0.0
    assert set(gc["responseSchema"]["required"]) == {"direction", "confidence", "go", "rationale"}
    assert gc["responseSchema"]["properties"]["direction"]["enum"] == list(ee.DIRECTIONS)


def test_db_roundtrip(fresh_db):
    fresh_db.upsert_simmer_earnings_bias({
        "symbol": "nvda", "earnings_date": "2026-08-27", "direction": "bullish",
        "confidence": 0.7, "go": True, "rationale": "r", "headline_count": 3,
        "reasons": json.dumps([]), "source": "news", "computed_at": NOW})
    row = fresh_db.get_simmer_earnings_bias("NVDA", "2026-08-27")
    assert row["direction"] == "bullish" and row["go"] is True
    assert row["symbol"] == "NVDA" and json.loads(row["reasons"]) == []
    # case-insensitive read; toggle-off then back on hits the same cached row
    assert fresh_db.get_simmer_earnings_bias("nvda", "2026-08-27")["confidence"] == 0.7
    assert fresh_db.get_simmer_earnings_bias("NVDA", "2099-01-01") is None

"""EarningsBiasAnalyzer — Simmer's standalone earnings-mode verdict.

All I/O faked with httpx.MockTransport (Alpaca + Gemini), mirroring test_news.py.
asyncio_mode=auto, so async tests run without a marker.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import httpx

from app import earnings_engine as ee
from app import simmer_news as sn
from app.routes.simmer import _bias_stale


def _utc_naive(mins_ago: int) -> datetime:
    """A naive-UTC timestamp `mins_ago` in the past — how DuckDB hands back
    computed_at (aware-UTC stored → read back naive, still UTC wall-clock)."""
    return (datetime.now(timezone.utc) - timedelta(minutes=mins_ago)).replace(tzinfo=None)


def test_bias_stale_uses_computed_at_not_updated_at():
    ttl = 21600  # 6h
    assert _bias_stale(None, ttl) is True
    assert _bias_stale({}, ttl) is True
    # 3h-old computed_at is fresh — even if updated_at (DB-local now()) looks 8h
    # old. The old bug preferred updated_at and mislabeled it UTC, shrinking the
    # TTL by the host's offset.
    assert _bias_stale({"computed_at": _utc_naive(180), "updated_at": _utc_naive(500)}, ttl) is False
    assert _bias_stale({"computed_at": _utc_naive(420)}, ttl) is True  # 7h → stale

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


async def test_computed_at_is_naive_utc_so_a_non_utc_host_keeps_its_ttl():
    """The WRITER must stamp computed_at naive-UTC — not merely aware-UTC.

    `_bias_stale` reads a naive stamp as UTC. Binding a tz-AWARE datetime into a
    DuckDB TIMESTAMP column converts it to the session's LOCAL zone and drops the
    offset, so the row reads back `utcoffset` seconds old the moment it is
    written: on US/Eastern the 6h bias TTL collapsed to ~2h, and on US/Pacific
    the row was stale on arrival, re-billing Gemini on every single request.

    test_bias_stale_uses_computed_at_not_updated_at above fabricates naive
    stamps, so it exercises only the READER and cannot catch a regression here.
    """
    import duckdb

    a = ee.EarningsBiasAnalyzer(_alpaca(["h1"]), "gm", gemini_transport=_gemini(
        {"direction": "bullish", "confidence": 0.5, "go": True, "rationale": "r"}))
    out = await a.analyze("nvda", "2026-08-27", now=NOW)

    stamp = out["computed_at"]
    assert stamp.tzinfo is None, "computed_at must be naive (DuckDB TIMESTAMP)"
    assert stamp == NOW.replace(tzinfo=None), "…and still the UTC wall clock"

    # Full roundtrip on a deliberately non-UTC session: a row written now must
    # not read back as already-expired. Bind an aware stamp instead and this
    # assertion fails — which is exactly the regression being pinned.
    conn = duckdb.connect(":memory:")
    conn.execute("SET TimeZone='America/Los_Angeles'")
    conn.execute("CREATE TABLE b (computed_at TIMESTAMP)")
    fresh = await ee.EarningsBiasAnalyzer(
        _alpaca(["h1"]), "gm", gemini_transport=_gemini(
            {"direction": "bullish", "confidence": 0.5, "go": True, "rationale": "r"})
    ).analyze("nvda", "2026-08-27")          # real clock, not NOW
    conn.execute("INSERT INTO b VALUES (?)", [fresh["computed_at"]])
    row = {"computed_at": conn.execute("SELECT computed_at FROM b").fetchone()[0]}
    assert _bias_stale(row, 21600.0) is False

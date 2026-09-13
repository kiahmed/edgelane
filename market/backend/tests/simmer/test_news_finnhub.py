"""Finnhub news source + the primary→fallback escalation (app/simmer_news.py).
No network — httpx.MockTransport / stub clients only."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app import config
from app import simmer_news as sn
from app.config import Settings


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _epoch(dt: datetime) -> int:
    return int(dt.replace(tzinfo=timezone.utc).timestamp())


def _art(aid, sym, hours_ago, headline="h", url="", source="finnhub"):
    return {"id": aid, "headline": headline, "source": source, "url": url,
            "symbols": [sym], "published_at": _now() - timedelta(hours=hours_ago)}


# ── parser ───────────────────────────────────────────────────────────────────
def test_parse_finnhub_news_shape_and_body_drop():
    when = _now() - timedelta(hours=1)
    payload = [
        {"id": 7712345, "datetime": _epoch(when), "headline": "NVDA beats Q2",
         "summary": "a body that must never survive", "source": "Reuters",
         "url": "https://x/1"},
        {"id": 7712346, "datetime": _epoch(when), "headline": "",  # dropped: no headline
         "source": "PR", "url": "https://x/2"},
        {"datetime": _epoch(when), "headline": "no id"},           # dropped: no id
    ]
    arts = sn.parse_finnhub_news(payload, "nvda")
    assert len(arts) == 1
    a = arts[0]
    assert a["id"] == "fh-7712345"                     # namespaced off Finnhub id
    assert a["headline"] == "NVDA beats Q2"
    assert a["source"] == "reuters"
    assert a["symbols"] == ["NVDA"]
    assert a["url"] == "https://x/1"
    assert abs((a["published_at"] - when.replace(microsecond=0)).total_seconds()) < 2
    assert "summary" not in a and "content" not in a   # body never transits


def test_parse_finnhub_news_tolerates_junk():
    assert sn.parse_finnhub_news(None, "NVDA") == []
    assert sn.parse_finnhub_news([], "NVDA") == []
    assert sn.parse_finnhub_news([{"id": 1, "headline": "x"}], "") == []  # no symbol


# ── client (MockTransport) ───────────────────────────────────────────────────
class _FinnhubFake:
    def __init__(self, status=None, articles=None):
        self.requests: list[httpx.Request] = []
        self._statuses = list(status) if isinstance(status, list) else \
            ([status] if status else [])
        self._articles = articles

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self._statuses:
            code = self._statuses.pop(0)
            if code != 200:
                return httpx.Response(code, text="err")
        when = _now() - timedelta(hours=1)
        arts = self._articles if self._articles is not None else [
            {"id": 900, "datetime": _epoch(when), "headline": "NVDA news",
             "source": "finnhub", "url": "https://f/900"}]
        return httpx.Response(200, json=arts)

    def client(self) -> sn.FinnhubNewsClient:
        return sn.FinnhubNewsClient("fh-key",
                                    transport=httpx.MockTransport(self.handler))


async def test_finnhub_client_fetches_with_date_window():
    fake = _FinnhubFake()
    client = fake.client()
    now = _now()
    arts = await client.fetch_news(["NVDA"], start=now - timedelta(hours=24), end=now)
    assert len(arts) == 1 and arts[0]["id"] == "fh-900"
    p = fake.requests[0].url.params
    assert p["symbol"] == "NVDA"
    assert p["from"] == (now - timedelta(hours=24)).date().isoformat()
    assert p["to"] == now.date().isoformat()
    # Key travels in the HEADER, never the query string — a query param would be
    # in the request line and so copied into every proxy/CDN access log en route.
    assert fake.requests[0].headers["X-Finnhub-Token"] == "fh-key"
    assert "token" not in p
    assert "fh-key" not in str(fake.requests[0].url)


async def test_finnhub_client_soft_fails_never_raises():
    # 429 → no retry, returns [] (never raises)
    fake429 = _FinnhubFake(status=[429])
    assert await fake429.client().fetch_news(["NVDA"]) == []
    assert len(fake429.requests) == 1
    # 5xx → one retry then still [], never raises
    fake500 = _FinnhubFake(status=[500, 500])
    assert await fake500.client().fetch_news(["NVDA"]) == []
    assert len(fake500.requests) == 2


async def test_finnhub_one_bad_symbol_does_not_sink_the_rest():
    # first symbol 500 (x2 retry), second symbol 200
    fake = _FinnhubFake(status=[500, 500])
    arts = await fake.client().fetch_news(["AAA", "NVDA"])
    assert [a["symbols"][0] for a in arts] == ["NVDA"]     # AAA failed, NVDA survived


# ── config parses both knobs ─────────────────────────────────────────────────
def test_config_parses_both_provider_knobs():
    assert Settings().simmer_news_provider == "finnhub"          # new default
    assert Settings().simmer_news_fallback == "alpaca"           # new default
    s = Settings(**config._coerce({"SIMMER_NEWS_PROVIDER": "alpaca",
                                   "SIMMER_NEWS_FALLBACK": "finnhub"}))
    assert s.simmer_news_provider == "alpaca"
    assert s.simmer_news_fallback == "finnhub"
    # invalid values fall back to the defaults
    bad = Settings(**config._coerce({"SIMMER_NEWS_PROVIDER": "bogus",
                                     "SIMMER_NEWS_FALLBACK": "bogus"}))
    assert bad.simmer_news_provider == "finnhub"
    assert bad.simmer_news_fallback == "alpaca"


def test_build_fallback_client_selects_provider():
    fb, notes = sn.build_fallback_client(Settings(finnhub_api_key="k"))  # default alpaca
    assert "news:alpaca_credentials_missing" in notes and fb is None
    fb2, _ = sn.build_fallback_client(
        Settings(simmer_news_fallback="finnhub", finnhub_api_key="k"))
    assert isinstance(fb2, sn.FinnhubNewsClient)
    fb3, notes3 = sn.build_fallback_client(Settings(simmer_news_fallback="none"))
    assert fb3 is None and "news:fallback_none" in notes3


# ── escalation ───────────────────────────────────────────────────────────────
class _StubClient:
    """Records (symbols, window_hours) and returns queued responses."""

    def __init__(self, responses=None):
        self.calls: list[tuple] = []
        self._responses = list(responses or [])

    async def fetch_news(self, symbols, start=None, end=None, **k):
        hours = round((end - start).total_seconds() / 3600) if start and end else None
        self.calls.append((tuple(symbols), hours))
        return self._responses.pop(0) if self._responses else []

    async def close(self):
        pass


async def test_escalation_primary_hit_no_widen():
    primary = _StubClient(responses=[["should-not-be-used"]])
    articles = [_art("fh-1", "NVDA", hours_ago=2, headline="fresh NVDA")]
    notes: list[str] = []
    pool = await sn._escalate_thin_symbols(primary, Settings(), ["NVDA"],
                                           articles, _now(), notes)
    assert pool == articles                 # unchanged
    assert primary.calls == []              # NOT re-fetched — 24h pull was heavy
    assert notes == []


async def test_escalation_widen_satisfies_no_fallback():
    # primary 24h was thin; widen 36h returns a fresh-within-36h article
    primary = _StubClient(responses=[[_art("fh-2", "NVDA", hours_ago=30,
                                           headline="widened NVDA")]])
    fallback = _StubClient(responses=[["fallback-should-not-run"]])
    notes: list[str] = []
    pool = await sn._escalate_thin_symbols(primary, Settings(), ["NVDA"], [],
                                           _now(), notes, fallback_client=fallback)
    assert primary.calls == [(("NVDA",), 36)]       # widened to 36h
    assert fallback.calls == []                     # widen satisfied → no fallback
    assert "news:widened:NVDA" in notes
    assert [a["id"] for a in pool] == ["fh-2"]


async def test_escalation_falls_back_when_widen_thin():
    primary = _StubClient(responses=[[]])           # widen still empty
    fallback = _StubClient(responses=[[_art("fh-3", "NVDA", hours_ago=2,
                                            headline="from fallback",
                                            source="alpaca")]])
    notes: list[str] = []
    pool = await sn._escalate_thin_symbols(primary, Settings(), ["NVDA"], [],
                                           _now(), notes, fallback_client=fallback)
    assert primary.calls == [(("NVDA",), 36)]       # widened first
    assert fallback.calls == [(("NVDA",), 24)]      # then fallback over 24h
    assert any(n.startswith("news:fallback_merged:NVDA") for n in notes)
    assert [a["id"] for a in pool] == ["fh-3"]


# ── dedupe across sources ────────────────────────────────────────────────────
def test_merge_dedupe_by_id_url_and_headline():
    pool = [_art("fh-1", "NVDA", 1, headline="NVDA beats Q2 (NASDAQ: NVDA)",
                 url="https://a/1")]
    incoming = [
        _art("fh-1", "NVDA", 1, headline="dup by id"),               # same id
        _art("fh-9", "NVDA", 1, headline="dup url", url="https://a/1"),  # same url
        _art("fh-8", "NVDA", 1, headline="NVDA beats Q2", url="https://b/8"),  # same norm headline
        _art("fh-7", "NVDA", 1, headline="genuinely new story", url="https://b/7"),
    ]
    merged = sn._merge_dedupe(list(pool), incoming)
    ids = [a["id"] for a in merged]
    assert ids == ["fh-1", "fh-7"]              # only the genuinely-new one added


# ── refresh_news wiring: finnhub primary empty → fallback merged & persisted ──
async def test_refresh_escalates_to_fallback_and_persists(fresh_db):
    primary = _FinnhubFake(articles=[]).client()        # primary yields nothing
    fb = _StubClient(responses=[[_art("fh-77", "NVDA", hours_ago=2,
                                      headline="fallback story", source="alpaca")]])
    settings = Settings(finnhub_api_key="k", simmer_news_fallback="alpaca",
                        gemini_api_key="")              # unscored, still persists
    out = await sn.refresh_news(fresh_db, ["NVDA"], settings,
                                client=primary, fallback_client=fb, scorer=None)
    rows = sn.fetch_news_since(fresh_db, "NVDA", _now() - timedelta(days=2))
    assert [r["source"] for r in rows] == ["alpaca"]    # came from the fallback
    assert any(n.startswith("news:fallback_merged:NVDA")
               for n in out["NVDA"]["_news_notes"])

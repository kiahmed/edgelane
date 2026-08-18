"""Simmer Phase 3a — news ingestion + LLM sentiment scoring
(app/simmer_news.py + watcher wiring + the /simmer/news/{symbol} route).

NO network anywhere: every HTTP client takes an injectable transport
(httpx.MockTransport), exactly like the simmer_data_provider tests, and the
Gemini/Alpaca fixtures are checked-in JSON pages written to the real v1beta1 /
generateContent shapes.

Tests that assert on the aggregate research-cache fields depend on the
sibling module `app.simmer_velocity` (pure math: velocity p-value + trailing
sentiment aggregation) and guard with pytest.importorskip — ingestion,
dedup, caching and the route do NOT depend on it.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import config
from app import simmer_news as sn
from app import simmer_watcher as sw
from app.config import Settings
from app.routes import simmer as sroute

FIXTURES = Path(__file__).parent / "fixtures"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


@pytest.fixture(autouse=True)
def _reset_news_module_state():
    """The Alpaca consecutive-failure counter is module state (the RSS
    failover trigger) — reset it so tests can't order-couple."""
    sn._alpaca_consecutive_failures = 0
    yield
    sn._alpaca_consecutive_failures = 0


# ── Transports ──────────────────────────────────────────────────────────────
class AlpacaFake:
    """Serves the two checked-in v1beta1 pages; page 2 only via page_token.
    created_at is remapped relative to construction time so the fixtures never
    age out of the 24 h / trailing-session windows as the calendar advances."""

    def __init__(self, status: int | list[int] | None = None,
                 single_page: bool = False):
        self.requests: list[httpx.Request] = []
        self._statuses = list(status) if isinstance(status, list) else \
            ([status] if status else [])
        self.single_page = single_page
        self.base = datetime.now(timezone.utc)

    def _restamp(self, page: dict, offset_min: int) -> dict:
        page = json.loads(json.dumps(page))
        for i, art in enumerate(page.get("news") or []):
            ts = self.base - timedelta(minutes=offset_min + 5 * i)
            art["created_at"] = art["updated_at"] = \
                ts.replace(microsecond=0).isoformat().replace("+00:00", "Z")
        return page

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self._statuses:
            code = self._statuses.pop(0)
            if code != 200:
                return httpx.Response(code, text="synthetic error")
        if request.url.params.get("page_token"):
            return httpx.Response(
                200, json=self._restamp(_load("alpaca_news_page2.json"), 60))
        page = _load("alpaca_news_page1.json")
        if self.single_page:
            page = dict(page, next_page_token=None)
        return httpx.Response(200, json=self._restamp(page, 30))

    def client(self) -> sn.AlpacaNewsClient:
        return sn.AlpacaNewsClient("key-id", "secret",
                                   transport=httpx.MockTransport(self.handler))


class GeminiFake:
    """Echoes a fixed score for every id it finds in the request prompt —
    lets the article-id cache be observed as a REQUEST COUNT."""

    def __init__(self, score: float = 0.5, text_override: str | None = None):
        self.requests: list[httpx.Request] = []
        self.score = score
        self.text_override = text_override

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        body = json.loads(request.content.decode("utf-8"))
        prompt = body["contents"][0]["parts"][0]["text"]
        ids = re.findall(r"id=(\S+)", prompt)
        text = self.text_override if self.text_override is not None else \
            json.dumps([{"id": i, "score": self.score, "confidence": 0.9,
                         "reason": "earnings"} for i in ids])
        return httpx.Response(200, json={
            "candidates": [{"content": {"parts": [{"text": text}],
                                        "role": "model"},
                            "finishReason": "STOP", "index": 0}]})

    def scorer(self) -> sn.GeminiScorer:
        return sn.GeminiScorer("gm-key",
                               transport=httpx.MockTransport(self.handler))


def _settings(**kw) -> Settings:
    base = dict(alpaca_key_id="key-id", alpaca_secret_key="secret",
                gemini_api_key="gm-key")
    base.update(kw)
    return Settings(**base)


# ═══════════════════════════════════════════════════════════════════════════
# Alpaca client: parsing, pagination, 429 discipline
# ═══════════════════════════════════════════════════════════════════════════
def test_parse_alpaca_page_drops_bodies_and_keeps_headline_fields():
    articles, token = sn.parse_alpaca_news(_load("alpaca_news_page1.json"))
    assert len(articles) == 3 and token
    a = articles[0]
    assert a["id"] == "24803233"
    assert a["headline"].startswith("NVIDIA Announces Record Q2")
    assert a["source"] == "benzinga"
    assert a["url"] == "https://www.benzinga.com/news/24803233"
    assert a["symbols"] == ["NVDA"]
    assert a["published_at"] == datetime(2026, 8, 15, 12, 4, 5)
    # licensing rule: bodies must not survive the parse boundary
    for art in articles:
        assert "content" not in art and "summary" not in art


async def test_alpaca_pagination_follows_page_token():
    fake = AlpacaFake()
    client = fake.client()
    arts = await client.fetch_news(["NVDA", "AMD"],
                                   start=_now() - timedelta(hours=24), end=_now())
    await client.close()
    assert len(fake.requests) == 2
    first = fake.requests[0].url.params
    assert first["symbols"] == "AMD,NVDA"          # comma-batched, one call/sweep
    assert first["limit"] == "50"
    assert first["include_content"] == "false"     # bodies never even requested
    assert "page_token" not in dict(first)
    assert fake.requests[1].url.params["page_token"] \
        == "MTcyNDc4NTM0NTAwMDAwMDAwMHwyNDgwMzM4OA=="
    assert [a["id"] for a in arts] == \
        ["24803233", "24803301", "24803388", "24802710", "24801950"]


async def test_alpaca_429_is_never_retried():
    fake = AlpacaFake(status=[429])
    client = fake.client()
    with pytest.raises(sn.NewsRateLimitError):
        await client.fetch_news(["NVDA"])
    await client.close()
    assert len(fake.requests) == 1                 # house rule: 429 → no retry


async def test_alpaca_5xx_retries_exactly_once_then_succeeds():
    fake = AlpacaFake(status=[500, 200], single_page=True)
    client = fake.client()
    arts = await client.fetch_news(["NVDA"])
    await client.close()
    assert len(fake.requests) == 2
    assert len(arts) == 3


# ═══════════════════════════════════════════════════════════════════════════
# Wire-RSS fallback: ticker extraction + symbol filter
# ═══════════════════════════════════════════════════════════════════════════
def test_rss_ticker_extraction_patterns():
    assert sn.extract_tickers("Wix Reports Results (NASDAQ: WIX)") == ["WIX"]
    assert sn.extract_tickers("(NYSE: GS) and (NYSE American: XYZ)") == ["GS", "XYZ"]
    assert sn.extract_tickers("(nasdaq: aapl) lowercase feed") == ["AAPL"]
    assert sn.extract_tickers("(OTCQB: ABCD) micro cap") == ["ABCD"]
    assert sn.extract_tickers("(NASDAQ: WIX) twice (NASDAQ: WIX)") == ["WIX"]
    assert sn.extract_tickers("no ticker here, NYSE is just a word") == []


async def test_rss_fetch_filters_to_watched_symbols():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, text=(FIXTURES / "prnewswire_sample.rss").read_text())
    client = sn.RssNewsClient(transport=httpx.MockTransport(handler))
    arts = await client.fetch_news(["WIX", "NVDA"])
    await client.close()
    assert len(arts) == 1
    a = arts[0]
    assert a["symbols"] == ["WIX"]
    assert a["source"] == "prnewswire"
    assert a["headline"].startswith("Wix Reports")
    # RFC-822 pubDate with -0400 offset → naive UTC
    assert a["published_at"] == datetime(2026, 8, 15, 12, 30, 0)
    assert a["id"].startswith("rss-")


# ═══════════════════════════════════════════════════════════════════════════
# Dedup / clustering — where the signal lives
# ═══════════════════════════════════════════════════════════════════════════
def _art(aid: str, headline: str, ts: datetime) -> dict:
    return {"article_id": aid, "headline": headline, "published_at": ts}


def test_normalize_strips_tags_suffix_and_punctuation():
    assert sn.normalize_headline(
        "NVIDIA Announces Record Q2 Results; Raises Full-Year Guidance "
        "(NASDAQ: NVDA) - Reuters") == \
        "nvidia announces record q2 results raises full year guidance"


def test_same_wire_story_across_three_sources_is_one_cluster():
    t0 = datetime(2026, 8, 15, 12, 0)
    arts = [
        _art("a1", "NVIDIA Announces Record Q2 Results, Raises Full-Year "
                   "Guidance", t0),
        _art("a2", "NVIDIA announces record Q2 results, raises full-year "
                   "guidance - Reuters", t0 + timedelta(minutes=2)),
        _art("a3", "NVIDIA Announces Record Q2 Results; Raises Full Year "
                   "Guidance (NASDAQ: NVDA)", t0 + timedelta(minutes=5)),
        _art("b1", "NVIDIA Partners With Automaker on Next-Gen Autonomous "
                   "Platform", t0 + timedelta(minutes=10)),
    ]
    keys = sn.assign_clusters(arts, [])
    assert keys["a1"] == keys["a2"] == keys["a3"]      # 1 cluster, breadth 3
    assert keys["b1"] != keys["a1"]                    # distinct story separate
    assert len(set(keys.values())) == 2


def test_six_hour_window_is_respected():
    t0 = datetime(2026, 8, 15, 8, 0)
    same = "Acme Corp Receives FDA Approval For Lead Drug Candidate"
    keys = sn.assign_clusters(
        [_art("x1", same, t0), _art("x2", same, t0 + timedelta(hours=7))], [])
    assert keys["x1"] != keys["x2"]                    # 7 h apart → new cluster
    keys2 = sn.assign_clusters(
        [_art("y1", same, t0), _art("y2", same, t0 + timedelta(hours=5))], [])
    assert keys2["y1"] == keys2["y2"]                  # 5 h apart → same cluster


def test_new_articles_join_persisted_clusters_with_stored_keys():
    t0 = datetime(2026, 8, 15, 12, 0)
    existing = [{"cluster_key": "stored-key-1",
                 "headline": "NVIDIA Announces Record Q2 Results, Raises "
                             "Full-Year Guidance",
                 "published_at": t0}]
    keys = sn.assign_clusters(
        [_art("n1", "NVIDIA announces record Q2 results, raises full-year "
                    "guidance - Reuters", t0 + timedelta(minutes=30))], existing)
    assert keys["n1"] == "stored-key-1"                # keys stable across refreshes


def test_cluster_counts_by_bucket_counts_clusters_not_articles():
    t0 = datetime(2026, 8, 15, 12, 1)
    rows = [
        {"cluster_key": "c1", "published_at": t0},
        {"cluster_key": "c1", "published_at": t0 + timedelta(minutes=2)},
        {"cluster_key": "c2", "published_at": t0 + timedelta(minutes=3)},
        {"cluster_key": "c3", "published_at": t0 + timedelta(minutes=6)},
    ]
    counts = sn.cluster_counts_by_bucket(rows, bucket_minutes=5)
    assert counts == {datetime(2026, 8, 15, 12, 0): 2,     # c1 counted ONCE
                      datetime(2026, 8, 15, 12, 5): 1}


# ═══════════════════════════════════════════════════════════════════════════
# Gemini: clamping + malformed-response posture
# ═══════════════════════════════════════════════════════════════════════════
def test_gemini_scores_clamped_server_side_regardless_of_schema():
    scores = sn.parse_gemini_scores(_load("gemini_scores.json"))
    assert scores["a1"] == {"score": 0.62, "confidence": 0.85,
                            "reason": "earnings"}
    assert scores["a2"]["score"] == 1.0                # 1.7 clamped down
    assert scores["a2"]["confidence"] == 1.0           # 1.4 clamped down
    assert scores["a3"]["score"] == -1.0               # -3.0 clamped up
    assert scores["a3"]["confidence"] == 0.0
    assert scores["a3"]["reason"] == "other"           # unknown enum → other
    assert "a4" not in scores                          # unusable score → dropped


async def test_gemini_malformed_json_skips_batch_without_crashing():
    fake = GeminiFake(text_override="this is not json {")
    scorer = fake.scorer()
    out = await scorer.score_headlines(
        [{"id": "a1", "symbol": "NVDA", "headline": "h"}])
    await scorer.close()
    assert out == {}
    assert len(fake.requests) == 1


async def test_gemini_request_pins_schema_and_temperature_zero():
    fake = GeminiFake()
    scorer = fake.scorer()
    await scorer.score_headlines([{"id": "a1", "symbol": "NVDA", "headline": "h"}])
    await scorer.close()
    body = json.loads(fake.requests[0].content.decode("utf-8"))
    gc = body["generationConfig"]
    assert gc["temperature"] == 0
    assert gc["responseMimeType"] == "application/json"
    props = gc["responseSchema"]["items"]["properties"]
    assert props["score"] == {"type": "NUMBER", "minimum": -1.0, "maximum": 1.0}
    assert fake.requests[0].url.path.endswith(
        "/models/gemini-2.5-flash-lite:generateContent")
    assert fake.requests[0].headers["x-goog-api-key"] == "gm-key"


# ═══════════════════════════════════════════════════════════════════════════
# refresh_news — the entry point
# ═══════════════════════════════════════════════════════════════════════════
async def test_missing_keys_is_a_clean_noop_with_note(fresh_db):
    out = await sn.refresh_news(fresh_db, ["NVDA"], Settings())   # no keys at all
    upd = out["NVDA"]
    assert upd["sentiment_score"] is None and upd["sentiment_n"] is None
    assert upd["velocity_p"] is None and upd["velocity_tier"] is None
    assert isinstance(upd["news_at"], datetime)        # TTL machinery still cycles
    assert "news:alpaca_credentials_missing" in upd["_news_notes"]
    assert sn.fetch_news_since(fresh_db, "NVDA", _now() - timedelta(days=2)) == []


async def test_provider_off_is_a_clean_noop(fresh_db):
    out = await sn.refresh_news(fresh_db, ["NVDA"],
                                _settings(simmer_news_provider="off"))
    assert "news:provider_off" in out["NVDA"]["_news_notes"]


async def test_refresh_ingests_dedupes_and_scores_only_new(fresh_db):
    alpaca, gemini = AlpacaFake(), GeminiFake()
    settings = _settings()
    kw = dict(client=alpaca.client(), scorer=gemini.scorer())

    await sn.refresh_news(fresh_db, ["NVDA", "AMD"], settings, **kw)
    rows = sn.fetch_news_since(fresh_db, "NVDA", _now() - timedelta(days=2))
    assert len(rows) == 4          # 3-copy earnings wave + the partnership story
    by_id = {r["article_id"]: r for r in rows}
    # multi-symbol article got the :SYM suffix so the PK can't collide
    assert "24803388:NVDA" in by_id
    assert sn.fetch_existing_article_ids(fresh_db, ["24803388:AMD"]) \
        == {"24803388:AMD"}
    # dedup: the syndicated wave is ONE cluster (breadth 3); distinct story apart
    wave = {by_id[i]["cluster_key"] for i in
            ("24803233", "24803301", "24803388:NVDA")}
    assert len(wave) == 1
    assert by_id["24801950"]["cluster_key"] not in wave
    # every new row was scored, clamped range, model recorded
    for r in rows:
        assert r["sentiment"] == 0.5
        assert r["model"] == "gemini-2.5-flash-lite"
        assert r["scored_at"] is not None
    gemini_calls_after_first = len(gemini.requests)
    assert gemini_calls_after_first == 1               # 6 headlines, one batch

    # ── second refresh: identical feed → article-id cache → ZERO new scoring
    await sn.refresh_news(fresh_db, ["NVDA", "AMD"], settings, **kw)
    assert len(gemini.requests) == gemini_calls_after_first
    assert len(sn.fetch_news_since(fresh_db, "NVDA",
                                   _now() - timedelta(days=2))) == 4


async def test_no_gemini_key_persists_unscored_rows_with_note(fresh_db):
    alpaca = AlpacaFake()
    out = await sn.refresh_news(fresh_db, ["NVDA"],
                                _settings(gemini_api_key=""),
                                client=alpaca.client())
    assert "news:gemini_key_missing_unscored" in out["NVDA"]["_news_notes"]
    rows = sn.fetch_news_since(fresh_db, "NVDA", _now() - timedelta(days=2))
    assert rows and all(r["sentiment"] is None and r["scored_at"] is None
                        for r in rows)


async def test_fetch_failure_notes_and_never_raises(fresh_db):
    alpaca = AlpacaFake(status=[429])
    out = await sn.refresh_news(fresh_db, ["NVDA"], _settings(),
                                client=alpaca.client(), scorer=GeminiFake().scorer())
    upd = out["NVDA"]
    assert any(n.startswith("news:fetch_failed:") for n in upd["_news_notes"])
    assert isinstance(upd["news_at"], datetime)
    assert len(alpaca.requests) == 1                   # 429 still never retried


async def test_persistent_alpaca_failure_engages_rss_fallback(fresh_db):
    def rss_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, text=(FIXTURES / "prnewswire_sample.rss").read_text())
    rss = sn.RssNewsClient(transport=httpx.MockTransport(rss_handler))
    # no Gemini key → no scorer is ever constructed (nothing to transport-mock)
    settings = _settings(gemini_api_key="")
    for i in range(sn.ALPACA_FAILOVER_AFTER):
        failing = AlpacaFake(status=[500, 500])        # retry also fails
        out = await sn.refresh_news(fresh_db, ["WIX"], settings,
                                    client=failing.client(), rss_fallback=rss)
    upd = out["WIX"]
    assert "news:rss_fallback_engaged" in upd["_news_notes"]
    rows = sn.fetch_news_since(fresh_db, "WIX", datetime(2000, 1, 1))
    assert len(rows) == 1 and rows[0]["source"] == "prnewswire"
    await rss.close()


async def test_refresh_writes_research_cache_fields(fresh_db):
    """End-to-end against fake transports: the research-cache news fields land.
    The aggregate/velocity values come from the sibling `app.simmer_velocity`
    module (importorskip — pure math, may land after this file)."""
    pytest.importorskip("app.simmer_velocity")
    out = await sn.refresh_news(fresh_db, ["NVDA"], _settings(),
                                client=AlpacaFake().client(),
                                scorer=GeminiFake(score=0.5).scorer(),
                                persist_research=True)
    upd = out["NVDA"]
    assert not any("failed" in n or "unavailable" in n
                   for n in upd["_news_notes"]), upd["_news_notes"]
    row = fresh_db.get_simmer_research("NVDA")
    assert row is not None and row["news_at"] is not None
    assert row["sentiment_n"] is not None and row["sentiment_n"] >= 1
    assert row["sentiment_score"] is not None
    assert -1.0 <= row["sentiment_score"] <= 1.0
    assert row["velocity_tier"] in (None, "none", "warn", "alert")


# ═══════════════════════════════════════════════════════════════════════════
# Watcher wiring
# ═══════════════════════════════════════════════════════════════════════════
async def test_watcher_news_group_calls_refresh_news_on_its_ttl(fresh_db,
                                                                monkeypatch):
    calls = []

    async def fake_refresh(db, symbols, settings, **kw):
        calls.append((tuple(symbols), kw.get("persist_research")))
        return {"NVDA": {"news_at": _now(), "sentiment_score": 0.2,
                         "sentiment_n": 3, "velocity_p": 0.4,
                         "velocity_tier": "none", "_news_notes": []}}

    monkeypatch.setattr(sn, "refresh_news", fake_refresh)
    row = await sw.load_research(fresh_db, "NVDA")
    # the watcher hands persistence to load_research (single write path)
    assert calls == [(("NVDA",), False)]
    assert row["sentiment_score"] == 0.2
    persisted = fresh_db.get_simmer_research("NVDA")
    assert persisted["sentiment_n"] == 3
    assert persisted["velocity_tier"] == "none"

    # fresh TTL → no second provider call
    await sw.load_research(fresh_db, "NVDA")
    assert len(calls) == 1


async def test_velocity_alert_feeds_the_mandatory_invalidator(fresh_db,
                                                              monkeypatch):
    async def fake_refresh(db, symbols, settings, **kw):
        return {"NVDA": {"news_at": _now(), "sentiment_score": -0.6,
                         "sentiment_n": 9, "velocity_p": 0.0004,
                         "velocity_tier": "alert", "_news_notes": []}}

    monkeypatch.setattr(sn, "refresh_news", fake_refresh)
    await sw.load_research(fresh_db, "NVDA")
    assert fresh_db.get_simmer_research("NVDA")["velocity_tier"] == "alert"
    # next tier-1 read: the persisted "alert" trips the velocity_burst
    # invalidator, voiding every TTL regardless of age
    row = await sw.load_research(fresh_db, "NVDA")
    assert "velocity_burst" in row["_invalidated"]


async def test_news_failure_never_kills_the_sweep(fresh_db, monkeypatch):
    from .conftest import FakeSimmerTradier

    async def boom(*a, **kw):
        raise RuntimeError("synthetic news outage")

    monkeypatch.setattr(sn, "refresh_news", boom)

    async def _union():
        return [("NVDA", None)]
    monkeypatch.setattr(sw, "watchlist_union", _union)

    results = await sw.sweep(FakeSimmerTradier(), fresh_db)
    assert any(k.startswith("NVDA|") for k in results)     # sweep completed
    # news_at still stamped (fallback) so the no-op doesn't hot-loop
    assert fresh_db.get_simmer_research("NVDA")["news_at"] is not None


async def test_sweep_with_default_settings_runs_news_as_clean_noop(fresh_db,
                                                                   monkeypatch):
    """The real (unmonkeypatched) path with no news keys configured: the sweep
    completes and the research row carries NULL sentiment fields."""
    from .conftest import FakeSimmerTradier

    async def _union():
        return [("NVDA", None)]
    monkeypatch.setattr(sw, "watchlist_union", _union)
    results = await sw.sweep(FakeSimmerTradier(), fresh_db)
    assert any(k.startswith("NVDA|") for k in results)
    row = fresh_db.get_simmer_research("NVDA")
    assert row["news_at"] is not None
    assert row["sentiment_score"] is None and row["velocity_tier"] is None


# ═══════════════════════════════════════════════════════════════════════════
# GET /simmer/news/{symbol}
# ═══════════════════════════════════════════════════════════════════════════
def _make_client(db) -> TestClient:
    app = FastAPI()
    app.include_router(sroute.router)
    app.state.db = db
    return TestClient(app, raise_server_exceptions=False)


def _seed_news(db) -> None:
    t = _now() - timedelta(hours=2)
    sn.persist_news_rows(db, [
        {"article_id": "a1", "symbol": "NVDA", "cluster_key": "wave",
         "headline": "NVIDIA Announces Record Q2 Results",
         "source": "benzinga", "url": "https://x/a1", "published_at": t,
         "sentiment": 0.6, "confidence": 0.9,
         "model": "gemini-2.5-flash-lite", "scored_at": t},
        {"article_id": "a2", "symbol": "NVDA", "cluster_key": "wave",
         "headline": "NVIDIA announces record Q2 results - Reuters",
         "source": "reuters", "url": "https://x/a2",
         "published_at": t + timedelta(minutes=3),
         "sentiment": 0.4, "confidence": 0.8,
         "model": "gemini-2.5-flash-lite", "scored_at": t},
        {"article_id": "b1", "symbol": "NVDA", "cluster_key": "solo",
         "headline": "NVIDIA Partners With Automaker",
         "source": "benzinga", "url": "https://x/b1",
         "published_at": t + timedelta(minutes=9),
         "sentiment": None, "confidence": None, "model": None,
         "scored_at": None},
        # outside the 24 h window — must not appear
        {"article_id": "old", "symbol": "NVDA", "cluster_key": "ancient",
         "headline": "Old story", "source": "benzinga", "url": "https://x/old",
         "published_at": _now() - timedelta(hours=30),
         "sentiment": 0.1, "confidence": 0.5,
         "model": "gemini-2.5-flash-lite", "scored_at": t},
    ])
    db.upsert_simmer_research({"symbol": "NVDA", "sentiment_score": 0.31,
                               "sentiment_n": 2, "velocity_p": 0.2,
                               "velocity_tier": "none", "news_at": _now()})


def test_news_route_returns_clusters_aggregate_and_velocity(fresh_db):
    _seed_news(fresh_db)
    r = _make_client(fresh_db).get("/simmer/news/NVDA")
    assert r.status_code == 200
    body = r.json()
    assert body["symbol"] == "NVDA" and body["window_hours"] == 24
    clusters = {c["cluster_key"]: c for c in body["clusters"]}
    assert set(clusters) == {"wave", "solo"}           # 24 h window respected
    wave = clusters["wave"]
    assert wave["breadth"] == 2
    assert wave["sentiment"] == 0.5                    # mean of 0.6 / 0.4
    assert wave["headline"] == "NVIDIA Announces Record Q2 Results"  # earliest
    assert wave["url"] == "https://x/a1" and wave["source"] == "benzinga"
    assert wave["published_at"].endswith("Z")
    assert clusters["solo"]["breadth"] == 1
    assert clusters["solo"]["sentiment"] is None       # unscored, not invented
    assert body["aggregate"] == {"sentiment_score": 0.31, "sentiment_n": 2,
                                 "news_at": body["aggregate"]["news_at"]}
    assert body["aggregate"]["news_at"].endswith("Z")
    assert body["velocity"] == {"p": 0.2, "tier": "none"}


def test_news_route_empty_symbol_returns_empty_shape(fresh_db):
    r = _make_client(fresh_db).get("/simmer/news/TSLA")
    assert r.status_code == 200
    body = r.json()
    assert body["clusters"] == []
    assert body["aggregate"]["sentiment_score"] is None
    assert body["velocity"] == {"p": None, "tier": None}


def test_news_route_rejects_malformed_symbol(fresh_db):
    assert _make_client(fresh_db).get("/simmer/news/NV%24DA").status_code == 422


def test_news_route_sits_behind_the_simmer_gate(fresh_db, monkeypatch):
    monkeypatch.setattr(config, "_cached",
                        Settings(auth_enabled=True, admin_api_token="tok"))
    c = _make_client(fresh_db)
    assert c.get("/simmer/news/NVDA").status_code == 401
    assert c.get("/simmer/news/NVDA",
                 headers={"X-Admin-Token": "tok"}).status_code == 200

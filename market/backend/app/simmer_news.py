"""Simmer news ingestion + LLM sentiment scoring (Phase 3a — docs/simmer.md,
"News, sentiment, and catalysts").

Pipeline, per 15-minute news refresh (TTL `ticker_sentiment` in simmer_config):

    Alpaca REST /v1beta1/news  ──┐
    (or wire-RSS fallback)       ├─▶ dedup/cluster ─▶ persist NEW rows
                                 │   (3-shingle Jaccard ≥ 0.70, 6 h window)
                                 └─▶ Gemini scores ONLY new article ids
                                     ─▶ aggregate (simmer_velocity) ─▶
                                     simmer_research_cache fields

Four load-bearing decisions, all from docs/simmer.md:

1. **Store only derived outputs + headline + publisher URL. Never article
   bodies.** Alpaca's payload carries `content`/`summary`; both are dropped at
   parse time (`include_content=false` is also sent) so a body can never even
   transit this process. Licensing posture, not an optimization.

2. **Cache scores by article id — only NEW headlines are ever scored.** At 96
   news polls/day, re-scoring the 24 h window each poll costs ~100× more for
   identical output. `INSERT OR IGNORE` on the `article_id` PK plus a
   sentiment-IS-NULL select is the entire mechanism. `model` is recorded per
   row because scores are NOT reproducible across models (or even across calls
   — Google documents temperature 0/seed as best-effort).

3. **Dedup is where the signal lives** (Boudoukh et al., NBER w18725): one
   press release becomes a dozen wire copies within 90 seconds; undeduped, the
   velocity detector measures syndication breadth, not novelty. Headlines are
   normalized (lowercase, punctuation/ticker-tag/source-suffix stripped),
   3-shingled, and collapsed at Jaccard ≥ 0.70 within a 6-hour window. Velocity
   counts CLUSTERS; cluster size is kept as the separate `breadth` feature.

4. **Schema constraints are best-effort** — Gemini's responseSchema pins
   score ∈ [−1, 1], and the server clamps anyway ("schema-compliant but
   semantically incorrect outputs" is Google's own warning).

House client discipline (see `simmer_catalysts.EdgarClient` /
`simmer_data_provider.YahooDataProvider`): pure parsers over text/JSON so the
whole path tests without a network, a thin async-httpx shell with structured
errors, an injectable transport, one retry on 5xx/transport errors, and
**never a retry on 429**.

Failure posture: `refresh_news` NEVER raises for the watcher — missing config
is a clean no-op (None fields + a data_quality note), a provider failure notes
itself and returns stamps, and after `ALPACA_FAILOVER_AFTER` consecutive
Alpaca failures the wire-RSS firehose takes over (also noted).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Iterable, Mapping, Sequence
from xml.etree import ElementTree as ET
try:  # hardened parser for untrusted feeds (entity-expansion / XXE safe)
    from defusedxml.ElementTree import fromstring as _safe_fromstring
except Exception:  # pragma: no cover — dep missing → stdlib (still size-capped)
    _safe_fromstring = ET.fromstring

# Wire-RSS is an untrusted, unauthenticated firehose. Cap the body we parse so a
# hostile/huge feed can't exhaust memory; entity attacks are handled by the
# defused parser above.
_RSS_MAX_BYTES = 5 * 1024 * 1024

import httpx

from . import simmer_config

log = logging.getLogger("edgelane.simmer.news")


# --- Endpoints / constants ---------------------------------------------------

ALPACA_NEWS_URL = "https://data.alpaca.markets/v1beta1/news"
PRNEWSWIRE_RSS_URL = "https://www.prnewswire.com/rss/news-releases-list.rss"
GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta"
DEFAULT_GEMINI_MODEL = "gemini-2.5-flash-lite"

ALPACA_PAGE_LIMIT = 50          # the endpoint's own max per page
ALPACA_MAX_PAGES = 10           # 500 articles/backfill — far beyond 24 h needs
BACKFILL_HOURS = 24
GEMINI_BATCH_SIZE = 25          # ~20 calls/day at 500 headlines/day (measured cost table)
ALPACA_FAILOVER_AFTER = 3       # consecutive failures before the RSS fallback engages

#: How far back cluster counts are read for the velocity baseline. The
#: negative-binomial baseline wants `lookback_days` TRADING days of same
#: bucket-of-day counts; 31 calendar days covers 20 trading days + slack.
VELOCITY_LOOKBACK_CALENDAR_DAYS = 31

#: Reason codes the Gemini schema pins `reason` to (string enum). Coarse on
#: purpose — the enum exists to force a classification, not a summary.
REASON_CODES: tuple[str, ...] = (
    "earnings", "guidance", "analyst_rating", "mna", "product",
    "legal_regulatory", "offering_dilution", "management_change",
    "partnership", "macro", "other",
)

# Module-level consecutive-failure counter (per process, matches the watcher's
# singleton pattern): Alpaca "persistently failing" → RSS takes over.
_alpaca_consecutive_failures = 0


# --- Exceptions --------------------------------------------------------------

class NewsError(RuntimeError):
    """Base class for news-layer client errors."""


class NewsTimeoutError(NewsError):
    """Network timeout (read or connect)."""


class NewsRateLimitError(NewsError):
    """HTTP 429 — NEVER retried (house discipline: retrying a throttle is what
    escalates it into a block)."""

    def __init__(self, message: str, retry_after_sec: float = 0.0):
        super().__init__(message)
        self.retry_after_sec = retry_after_sec


# --- Time helpers ------------------------------------------------------------

def _utc_naive(dt: datetime | None) -> datetime | None:
    """Aware or naive → naive UTC (the tier-1 cache's clock)."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


def _now_naive_utc() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def parse_iso_utc(raw: Any) -> datetime | None:
    """`2026-08-15T13:04:05Z` / `...+00:00` → naive UTC. Bad input → None."""
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return _utc_naive(raw)
    s = str(raw).strip()
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        return _utc_naive(datetime.fromisoformat(s))
    except ValueError:
        return None


def parse_rfc822_utc(raw: Any) -> datetime | None:
    """RSS pubDate (`Fri, 15 Aug 2026 09:30:00 -0400`) → naive UTC."""
    if not raw:
        return None
    try:
        return _utc_naive(parsedate_to_datetime(str(raw).strip()))
    except (TypeError, ValueError):
        return None


# --- Pure parsers: Alpaca ----------------------------------------------------

def parse_alpaca_news(payload: Any) -> tuple[list[dict], str | None]:
    """`GET /v1beta1/news` page → (articles, next_page_token).

    Only headline-level fields survive. `content` and `summary` are dropped
    HERE, at the parse boundary, so no caller can accidentally persist an
    article body (the licensing rule in docs/simmer.md).
    """
    if not isinstance(payload, Mapping):
        return [], None
    out: list[dict] = []
    for row in payload.get("news") or []:
        if not isinstance(row, Mapping):
            continue
        rid = row.get("id")
        headline = str(row.get("headline") or "").strip()
        if rid is None or not headline:
            continue
        out.append({
            "id": str(rid),
            "headline": headline,
            "source": str(row.get("source") or "alpaca").strip().lower(),
            "url": str(row.get("url") or "").strip(),
            "symbols": [str(s).strip().upper()
                        for s in (row.get("symbols") or []) if str(s).strip()],
            "published_at": parse_iso_utc(row.get("created_at")
                                          or row.get("updated_at")),
        })
    token = payload.get("next_page_token")
    return out, (str(token) if token else None)


# --- Pure parsers: wire RSS --------------------------------------------------

#: `(NASDAQ: WIX)` / `(NYSE: GS)` / `(NYSE American: XYZ)` / `(OTCQB: ABCD)` —
#: the wire firehose has no structured symbol field; tickers ride the prose.
_TICKER_INLINE_RE = re.compile(
    r"\((?:NASDAQ|NYSE(?:\s+American)?|AMEX|OTC(?:QB|QX|MKTS)?|CBOE|TSX)"
    r"\s*:\s*([A-Za-z][A-Za-z.\-]{0,9})\)", re.IGNORECASE)


def extract_tickers(text: str | None) -> list[str]:
    """Inline exchange-tagged tickers, de-duplicated, order preserved."""
    if not text:
        return []
    seen: list[str] = []
    for m in _TICKER_INLINE_RE.finditer(str(text)):
        sym = m.group(1).upper()
        if sym not in seen:
            seen.append(sym)
    return seen


def parse_rss_items(xml_text: str) -> list[dict]:
    """RSS 2.0 → item dicts. Malformed items are skipped, never fatal (the
    fallback path must be less brittle than the primary, not more)."""
    if not xml_text or not xml_text.strip():
        return []
    if len(xml_text) > _RSS_MAX_BYTES:
        xml_text = xml_text[:_RSS_MAX_BYTES]
    try:
        root = _safe_fromstring(xml_text)
    except Exception as e:   # ParseError or a defused-XML security exception
        raise NewsError(f"wire RSS: unparseable/blocked XML — {e!s}") from e
    out: list[dict] = []
    for item in root.iter("item"):
        try:
            def _text(tag: str) -> str:
                el = item.find(tag)
                return (el.text or "").strip() if el is not None and el.text else ""
            title = _text("title")
            if not title:
                continue
            link = _text("link")
            guid = _text("guid") or link or title
            desc = _text("description")
            out.append({
                "id": "rss-" + hashlib.sha1(guid.encode("utf-8")).hexdigest()[:16],
                "headline": title,
                "source": "prnewswire",
                "url": link,
                "symbols": extract_tickers(f"{title} {desc}"),
                "published_at": parse_rfc822_utc(_text("pubDate")),
            })
        except Exception:
            continue
    return out


# --- Dedup / clustering ------------------------------------------------------

_TAG_STRIP_RE = re.compile(
    r"\((?:NASDAQ|NYSE(?:\s+American)?|AMEX|OTC(?:QB|QX|MKTS)?|CBOE|TSX)"
    r"\s*:\s*[A-Za-z.\-]{1,10}\)", re.IGNORECASE)
#: Trailing " - Reuters" / " | Benzinga" style source suffixes.
_SOURCE_SUFFIX_RE = re.compile(r"\s+[-–—|]\s+[A-Za-z][\w.&' ]{1,30}$")
_PUNCT_RE = re.compile(r"[^\w\s]")


def normalize_headline(text: str | None) -> str:
    """Lowercase; strip ticker tags, a trailing source suffix, and punctuation;
    collapse whitespace. The dedup key domain."""
    s = str(text or "")
    s = _TAG_STRIP_RE.sub(" ", s)
    s = _SOURCE_SUFFIX_RE.sub(" ", s)
    s = _PUNCT_RE.sub(" ", s.lower())
    return " ".join(s.split())


def shingles(normalized: str, n: int = 3) -> frozenset:
    """Token n-shingles. Shorter-than-n headlines fall back to the token set
    (else two 2-word headlines could never match anything)."""
    toks = normalized.split()
    if len(toks) < n:
        return frozenset(toks)
    return frozenset(tuple(toks[i:i + n]) for i in range(len(toks) - n + 1))


def jaccard(a: frozenset, b: frozenset) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    if inter == 0:
        return 0.0
    return inter / len(a | b)


def cluster_key_for(normalized: str, seed_published: datetime | None) -> str:
    """Deterministic key for a NEW cluster: hash of the seed's normalized
    headline + timestamp. The timestamp is in the key so the same wire story
    re-running outside the 6-hour window forms a DISTINCT cluster (a re-issued
    press release is a new event, and the window says so)."""
    stamp = seed_published.isoformat() if seed_published else "?"
    return hashlib.sha1(f"{normalized}|{stamp}".encode("utf-8")).hexdigest()[:16]


def assign_clusters(new_articles: Sequence[dict], existing_rows: Sequence[dict],
                    jaccard_threshold: float | None = None,
                    window_hours: float | None = None) -> dict[str, str]:
    """article_id → cluster_key for `new_articles`, clustering against both the
    already-persisted rows (their stored keys are reused, so keys are stable
    across refreshes) and each other.

    Membership: Jaccard ≥ threshold against a cluster whose MOST RECENT member
    is within `window_hours`. The sliding-recent-member window is what makes
    "6 h window respected" true: the same headline 7 h later starts fresh.
    """
    vcfg = simmer_config.velocity()
    thr = float(jaccard_threshold if jaccard_threshold is not None
                else vcfg.get("dedup_jaccard", 0.70))
    win = timedelta(hours=float(window_hours if window_hours is not None
                                else vcfg.get("dedup_window_hours", 6)))

    # Cluster reps from what's already in the table.
    reps: dict[str, dict] = {}
    for r in existing_rows or []:
        ck = r.get("cluster_key")
        if not ck:
            continue
        ts = _utc_naive(r.get("published_at")) or datetime.min
        rep = reps.get(ck)
        if rep is None:
            reps[ck] = {"shingles": shingles(normalize_headline(r.get("headline"))),
                        "latest": ts}
        elif ts > rep["latest"]:
            rep["latest"] = ts

    out: dict[str, str] = {}
    for art in sorted(new_articles, key=lambda a: a.get("published_at") or datetime.min):
        aid = str(art.get("article_id") or art.get("id") or "")
        norm = normalize_headline(art.get("headline"))
        sh = shingles(norm)
        ts = _utc_naive(art.get("published_at")) or _now_naive_utc()
        best_key, best_j = None, 0.0
        for ck, rep in reps.items():
            if abs((ts - rep["latest"]).total_seconds()) > win.total_seconds():
                continue
            j = jaccard(sh, rep["shingles"])
            if j > best_j:
                best_key, best_j = ck, j
        if best_key is not None and best_j >= thr:
            out[aid] = best_key
            if ts > reps[best_key]["latest"]:
                reps[best_key]["latest"] = ts
        else:
            ck = cluster_key_for(norm, ts)
            out[aid] = ck
            reps[ck] = {"shingles": sh, "latest": ts}
    return out


def cluster_counts_by_bucket(rows: Sequence[dict],
                             bucket_minutes: int | None = None) -> dict[datetime, int]:
    """Distinct-CLUSTER counts per time bucket — the velocity statistic's input
    (clusters, never raw articles; see the dedup rationale in the module doc).

    Keys are naive-UTC bucket-START datetimes floored to `bucket_minutes` —
    the convention `simmer_velocity.velocity_score` consumes.
    """
    vcfg = simmer_config.velocity()
    bm = int(bucket_minutes if bucket_minutes is not None
             else vcfg.get("bucket_minutes", 5))
    per_bucket: dict[datetime, set] = {}
    for r in rows or []:
        ts = _utc_naive(r.get("published_at"))
        ck = r.get("cluster_key")
        if ts is None or not ck:
            continue
        floored = ts.replace(minute=(ts.minute // bm) * bm, second=0, microsecond=0)
        per_bucket.setdefault(floored, set()).add(ck)
    return {k: len(v) for k, v in per_bucket.items()}


# --- Gemini response parsing / clamping --------------------------------------

def clamp(v: Any, lo: float, hi: float) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f != f:                     # NaN
        return None
    return max(lo, min(hi, f))


def parse_gemini_scores(payload: Any) -> dict[str, dict]:
    """generateContent response → {id: {score, confidence, reason}}.

    Clamps score to [−1, 1] and confidence to [0, 1] REGARDLESS of the response
    schema — Google's own docs warn the schema is best-effort. Malformed JSON
    raises NewsError (the caller skips the batch with a warning, never crashes).
    """
    try:
        text = payload["candidates"][0]["content"]["parts"][0]["text"]
    except (TypeError, KeyError, IndexError) as e:
        raise NewsError(f"gemini: unexpected response shape — {e!r}") from e
    try:
        rows = json.loads(text)
    except (TypeError, ValueError) as e:
        raise NewsError(f"gemini: response is not valid JSON — {e!s}") from e
    if not isinstance(rows, list):
        raise NewsError("gemini: response JSON is not an array")
    out: dict[str, dict] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        rid = str(row.get("id") or "").strip()
        score = clamp(row.get("score"), -1.0, 1.0)
        if not rid or score is None:
            continue
        reason = str(row.get("reason") or "other")
        out[rid] = {
            "score": score,
            "confidence": clamp(row.get("confidence"), 0.0, 1.0),
            "reason": reason if reason in REASON_CODES else "other",
        }
    return out


def build_gemini_request(items: Sequence[dict], temperature: float = 0.0) -> dict:
    """generateContent body for one batch: JSON-mode structured output with the
    score pinned to [−1, 1] by schema (and clamped again server-side)."""
    lines = [f'id={it["id"]} symbol={it.get("symbol", "?")} '
             f'headline={it["headline"]}' for it in items]
    prompt = (
        "Score each financial news headline for its likely price impact on the "
        "NAMED symbol only (entity-aware: a headline positive for one company "
        "may be neutral for another it merely mentions).\n"
        "Return a JSON array with exactly one object per input line, echoing "
        "the given id. score: -1.0 (strongly negative) to 1.0 (strongly "
        "positive), 0 for neutral or irrelevant. confidence: 0.0 to 1.0. "
        "reason: the single best-fitting category.\n\nHeadlines:\n"
        + "\n".join(lines)
    )
    return {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": temperature,
            "responseMimeType": "application/json",
            "responseSchema": {
                "type": "ARRAY",
                "items": {
                    "type": "OBJECT",
                    "properties": {
                        "id":         {"type": "STRING"},
                        "score":      {"type": "NUMBER", "minimum": -1.0, "maximum": 1.0},
                        "confidence": {"type": "NUMBER", "minimum": 0.0, "maximum": 1.0},
                        "reason":     {"type": "STRING", "enum": list(REASON_CODES)},
                    },
                    "required": ["id", "score", "confidence", "reason"],
                },
            },
        },
    }


# --- HTTP shell (shared plumbing) --------------------------------------------

class _HttpShell:
    """Thin async-httpx base with the house error discipline: structured
    errors, one retry on transport errors / 5xx, NO retry on 429, injectable
    transport (httpx.MockTransport in tests — no network anywhere)."""

    def __init__(self, timeout: float = 20.0,
                 headers: dict[str, str] | None = None,
                 transport: httpx.AsyncBaseTransport | None = None):
        self.timeout = timeout
        self._headers = dict(headers or {})
        self._transport = transport
        self._client: httpx.AsyncClient | None = None

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=self.timeout, headers=self._headers,
                follow_redirects=True, transport=self._transport)
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _request(self, method: str, url: str, *,
                       params: dict | None = None, json_body: dict | None = None,
                       _retried: bool = False) -> httpx.Response:
        client = self._get_client()
        try:
            resp = await client.request(method, url, params=params or None,
                                        json=json_body)
        except httpx.TimeoutException as e:
            raise NewsTimeoutError(
                f"news {method} {url}: timed out after {self.timeout}s ({e!s})"
            ) from e
        except httpx.RequestError as e:
            if not _retried:
                await asyncio.sleep(1.0)
                return await self._request(method, url, params=params,
                                           json_body=json_body, _retried=True)
            raise NewsError(f"news {method} {url}: network — {e!s}") from e

        if resp.status_code == 429:
            # Never retried — house rule (see EdgarClient / YahooDataProvider).
            retry_after = 0.0
            ra = resp.headers.get("Retry-After")
            if ra:
                try:
                    retry_after = max(0.0, float(ra))
                except (TypeError, ValueError):
                    retry_after = 0.0
            raise NewsRateLimitError(
                f"news {method} {url}: HTTP 429 — rate limited. "
                f"Body: {resp.text[:200]}", retry_after_sec=retry_after)
        if 500 <= resp.status_code < 600:
            if not _retried:
                await asyncio.sleep(1.0)
                return await self._request(method, url, params=params,
                                           json_body=json_body, _retried=True)
            raise NewsError(f"news {method} {url}: HTTP {resp.status_code} "
                            f"(after retry) — {resp.text[:300]}")
        if resp.status_code >= 400:
            raise NewsError(f"news {method} {url}: HTTP {resp.status_code} — "
                            f"{resp.text[:300]}")
        return resp


class AlpacaNewsClient(_HttpShell):
    """Alpaca Market Data news, REST (`GET /v1beta1/news`).

    Free with a paper account; 200 req/min on the free tier — one
    comma-separated-symbols call per sweep covers the whole watchlist, so the
    budget is never a factor. `include_content=false` + `parse_alpaca_news`
    dropping body fields = bodies never transit this process.
    """

    def __init__(self, key_id: str, secret_key: str, timeout: float = 20.0,
                 base_url: str = ALPACA_NEWS_URL,
                 transport: httpx.AsyncBaseTransport | None = None):
        super().__init__(timeout=timeout, transport=transport, headers={
            "APCA-API-KEY-ID": key_id,
            "APCA-API-SECRET-KEY": secret_key,
            "Accept": "application/json",
        })
        self.base_url = base_url

    async def fetch_news(self, symbols: Sequence[str],
                         start: datetime | None = None,
                         end: datetime | None = None,
                         limit: int = ALPACA_PAGE_LIMIT,
                         max_pages: int = ALPACA_MAX_PAGES) -> list[dict]:
        """Paginated fetch, newest first, ≤ `max_pages` pages."""
        syms = sorted({str(s).strip().upper() for s in symbols if str(s).strip()})
        if not syms:
            return []
        params: dict[str, Any] = {
            "symbols": ",".join(syms),
            "limit": int(max(1, min(int(limit), ALPACA_PAGE_LIMIT))),
            "sort": "desc",
            "include_content": "false",     # headlines only — licensing posture
            "exclude_contentless": "false",  # headline-only wires are wanted
        }
        if start is not None:
            params["start"] = start.replace(microsecond=0).isoformat() + "Z" \
                if start.tzinfo is None else start.isoformat()
        if end is not None:
            params["end"] = end.replace(microsecond=0).isoformat() + "Z" \
                if end.tzinfo is None else end.isoformat()

        out: list[dict] = []
        token: str | None = None
        for _ in range(max(1, int(max_pages))):
            page_params = dict(params)
            if token:
                page_params["page_token"] = token
            resp = await self._request("GET", self.base_url, params=page_params)
            try:
                payload = resp.json()
            except ValueError as e:
                raise NewsError(f"alpaca news: bad JSON — {e!s}") from e
            articles, token = parse_alpaca_news(payload)
            out.extend(articles)
            if not token or not articles:
                break
        return out


class RssNewsClient(_HttpShell):
    """Wire-RSS fallback (PRNewswire firehose). Keyless; tickers extracted by
    regex from the item text. Engaged only when Alpaca credentials are absent
    by explicit config (SIMMER_NEWS_PROVIDER=rss) or Alpaca is persistently
    failing — and always stamps a data_quality note when used."""

    def __init__(self, url: str = PRNEWSWIRE_RSS_URL, timeout: float = 20.0,
                 transport: httpx.AsyncBaseTransport | None = None):
        super().__init__(timeout=timeout, transport=transport,
                         headers={"Accept": "application/rss+xml, application/xml"})
        self.url = url

    async def fetch_news(self, symbols: Sequence[str], **_ignored) -> list[dict]:
        """Firehose filtered to the watched symbols (an item with no watched
        ticker inline is dropped — the firehose is market-wide)."""
        watched = {str(s).strip().upper() for s in symbols if str(s).strip()}
        resp = await self._request("GET", self.url)
        items = parse_rss_items(resp.text)
        out = []
        for it in items:
            hits = [s for s in it.get("symbols") or [] if s in watched]
            if hits:
                out.append({**it, "symbols": hits})
        return out


class GeminiScorer(_HttpShell):
    """Gemini headline scorer over raw REST (no SDK — httpx is the house
    client). Model comes from config; temperature 0; JSON mode with a pinned
    responseSchema; server-side clamping regardless.

    Batch-failure posture: one retry on 5xx (inherited), none on 429,
    malformed JSON → skip THAT batch with a warning; never crash the refresh.
    """

    def __init__(self, api_key: str, model: str = DEFAULT_GEMINI_MODEL,
                 timeout: float = 30.0, base_url: str = GEMINI_BASE,
                 transport: httpx.AsyncBaseTransport | None = None):
        super().__init__(timeout=timeout, transport=transport, headers={
            "x-goog-api-key": api_key,
            "Content-Type": "application/json",
        })
        self.model = (model or DEFAULT_GEMINI_MODEL).strip()
        self.base_url = base_url.rstrip("/")
        self.calls = 0          # observable in tests: the 100× cost control

    async def score_batch(self, items: Sequence[dict]) -> dict[str, dict]:
        if not items:
            return {}
        url = f"{self.base_url}/models/{self.model}:generateContent"
        self.calls += 1
        resp = await self._request("POST", url,
                                   json_body=build_gemini_request(items))
        try:
            payload = resp.json()
        except ValueError as e:
            raise NewsError(f"gemini: bad JSON envelope — {e!s}") from e
        return parse_gemini_scores(payload)

    async def score_headlines(self, items: Sequence[dict],
                              batch_size: int = GEMINI_BATCH_SIZE) -> dict[str, dict]:
        """Chunked scoring. A failed batch is skipped (warned), the rest
        proceed — one bad batch must never cost the other headlines."""
        out: dict[str, dict] = {}
        step = max(1, int(batch_size))
        for i in range(0, len(items), step):
            chunk = list(items[i:i + step])
            try:
                out.update(await self.score_batch(chunk))
            except asyncio.CancelledError:
                raise
            except NewsRateLimitError as e:
                log.warning("gemini rate limited — remaining batches skipped: %s", e)
                break               # 429: stop entirely, never retry
            except NewsError as e:
                log.warning("gemini batch of %d skipped: %s", len(chunk), e)
        return out


# --- Persistence (simmer_catalysts precedent: sync, conn-before-lock) --------

_NEWS_COLS = (
    "article_id", "symbol", "cluster_key", "headline", "source", "url",
    "published_at", "sentiment", "confidence", "model", "scored_at",
)


def persist_news_rows(db: Any, rows: Sequence[dict]) -> int:
    """INSERT OR IGNORE — new article ids only. Deliberately never REPLACE: a
    re-seen article must keep its stored sentiment (the article-id cache IS the
    Gemini cost control, and a replace would silently void it).

    connect() migrates under the non-reentrant Database lock, so the connection
    is resolved BEFORE the lock is taken (see the simmer_catalysts note)."""
    clean = [r for r in (rows or []) if r.get("article_id") and r.get("symbol")]
    if not clean:
        return 0
    conn = db.connect()
    lock = getattr(db, "_lock", None)
    cols = ", ".join(_NEWS_COLS)
    ph = ", ".join("?" for _ in _NEWS_COLS)
    params = [[r.get(c) for c in _NEWS_COLS] for r in clean]
    with (lock if lock is not None else nullcontext()):
        conn.executemany(
            f"INSERT OR IGNORE INTO simmer_news ({cols}) VALUES ({ph})", params)
    return len(clean)


def update_news_scores(db: Any, scores: Mapping[str, dict], model: str,
                       scored_at: datetime | None = None) -> int:
    """Write Gemini verdicts onto their rows. `model` is recorded per row —
    scores are not reproducible across models."""
    if not scores:
        return 0
    ts = scored_at or _now_naive_utc()
    conn = db.connect()
    lock = getattr(db, "_lock", None)
    params = [[v.get("score"), v.get("confidence"), model, ts, str(aid)]
              for aid, v in scores.items()]
    with (lock if lock is not None else nullcontext()):
        conn.executemany(
            "UPDATE simmer_news SET sentiment = ?, confidence = ?, model = ?, "
            "scored_at = ? WHERE article_id = ?", params)
    return len(params)


def fetch_news_since(db: Any, symbol: str, since: datetime) -> list[dict]:
    """Rows for one symbol with published_at ≥ since, oldest first."""
    conn = db.connect()
    lock = getattr(db, "_lock", None)
    with (lock if lock is not None else nullcontext()):
        cur = conn.execute(
            f"SELECT {', '.join(_NEWS_COLS)} FROM simmer_news "
            "WHERE symbol = ? AND published_at >= ? ORDER BY published_at ASC",
            [str(symbol).upper(), since])
        rows = cur.fetchall()
    return [dict(zip(_NEWS_COLS, r)) for r in rows]


def fetch_existing_article_ids(db: Any, ids: Sequence[str]) -> set[str]:
    """Which of `ids` are already persisted — the only-new-headlines filter."""
    ids = [str(i) for i in (ids or []) if i]
    if not ids:
        return set()
    conn = db.connect()
    lock = getattr(db, "_lock", None)
    ph = ", ".join("?" for _ in ids)
    with (lock if lock is not None else nullcontext()):
        cur = conn.execute(
            f"SELECT article_id FROM simmer_news WHERE article_id IN ({ph})", ids)
        rows = cur.fetchall()
    return {r[0] for r in rows}


# --- Aggregation (via app.simmer_velocity — the sibling pure-math module) ----

def _aggregate_symbol(db: Any, symbol: str, now: datetime,
                      notes: list[str]) -> dict[str, Any]:
    """Trailing-window sentiment aggregate + velocity state for one symbol,
    computed over the persisted (deduped) rows via `app.simmer_velocity`
    (pure math — no I/O; this side owns the DB lookups and the clock):

        aggregate_sentiment(scored_rows, now, cfg) -> {score, n, weighted, ...}
        velocity_score(counts_by_bucket, now, cfg, *, baseline_samples)
            -> {p, tier, ratio, x, mu}

    `baseline_samples` (same-clock-window totals on prior trading days, guard
    band applied) is assembled by its own `collect_baseline_samples` helper
    from the flat bucket-count history. Imported lazily so news ingestion
    still works (fields None, noted) if the math module is unavailable."""
    out: dict[str, Any] = {"sentiment_score": None, "sentiment_n": None,
                           "velocity_p": None, "velocity_tier": None}
    try:
        from . import simmer_velocity
    except ImportError:
        notes.append("news:velocity_module_unavailable")
        return out

    lookback = now - timedelta(days=VELOCITY_LOOKBACK_CALENDAR_DAYS)
    rows = fetch_news_since(db, symbol, lookback)

    # breadth per cluster rides each row into the aggregate.
    sizes: dict[str, int] = {}
    for r in rows:
        ck = r.get("cluster_key")
        if ck:
            sizes[ck] = sizes.get(ck, 0) + 1

    scored = [{
        "article_id": r.get("article_id"),
        "symbol": r.get("symbol"),
        "cluster_key": r.get("cluster_key"),
        "sentiment": r.get("sentiment"),
        "confidence": r.get("confidence"),
        "published_at": _utc_naive(r.get("published_at")),
        "breadth": sizes.get(r.get("cluster_key"), 1),
    } for r in rows if r.get("sentiment") is not None]

    try:
        agg = simmer_velocity.aggregate_sentiment(
            scored, now, simmer_config.sentiment())
        out["sentiment_score"] = agg.get("score")
        out["sentiment_n"] = agg.get("n")
    except Exception as e:
        notes.append(f"news:sentiment_aggregation_failed:{type(e).__name__}")
        log.warning("sentiment aggregation failed for %s: %s", symbol, e)

    try:
        vcfg = simmer_config.velocity()
        counts = cluster_counts_by_bucket(rows)
        baseline = simmer_velocity.collect_baseline_samples(counts, now, vcfg)
        vel = simmer_velocity.velocity_score(counts, now, vcfg,
                                             baseline_samples=baseline)
        out["velocity_p"] = vel.get("p")
        out["velocity_tier"] = vel.get("tier")
    except Exception as e:
        notes.append(f"news:velocity_failed:{type(e).__name__}")
        log.warning("velocity computation failed for %s: %s", symbol, e)
    return out


# --- Orchestration -----------------------------------------------------------

def _article_rows_for_symbol(articles: Iterable[dict], symbol: str) -> list[dict]:
    """Provider articles → per-symbol candidate rows (pre-cluster). An article
    tagged with several watched symbols yields one row per symbol; the id is
    suffixed `:SYM` when it maps to more than one so the PK never collides
    (the simmer_catalysts share-class precedent)."""
    sym = str(symbol).upper()
    out = []
    for a in articles or []:
        syms = a.get("symbols") or []
        if sym not in syms:
            continue
        aid = a["id"] if len(syms) <= 1 else f"{a['id']}:{sym}"
        out.append({
            "article_id": aid,
            "symbol": sym,
            "headline": a.get("headline"),
            "source": a.get("source"),
            "url": a.get("url"),
            "published_at": _utc_naive(a.get("published_at")) or _now_naive_utc(),
        })
    return out


def _news_settings(settings: Any) -> dict[str, str]:
    return {
        "provider": str(getattr(settings, "simmer_news_provider", "alpaca")
                        or "alpaca").lower(),
        "alpaca_key_id": str(getattr(settings, "alpaca_key_id", "") or ""),
        "alpaca_secret_key": str(getattr(settings, "alpaca_secret_key", "") or ""),
        "gemini_api_key": str(getattr(settings, "gemini_api_key", "") or ""),
        "gemini_model": str(getattr(settings, "gemini_model", "") or ""
                            ) or DEFAULT_GEMINI_MODEL,
    }


def build_news_client(settings: Any) -> tuple[Any | None, list[str]]:
    """Provider selection per config. Returns (client, notes). None = clean
    no-op (missing keys / provider off) — with the reason noted, never silent."""
    cfg = _news_settings(settings)
    provider = cfg["provider"]
    if provider == "off":
        return None, ["news:provider_off"]
    if provider == "rss":
        return RssNewsClient(), ["news:rss_provider"]
    # alpaca (default)
    if not cfg["alpaca_key_id"] or not cfg["alpaca_secret_key"]:
        return None, ["news:alpaca_credentials_missing"]
    return AlpacaNewsClient(cfg["alpaca_key_id"], cfg["alpaca_secret_key"]), []


def build_scorer(settings: Any) -> tuple[GeminiScorer | None, list[str]]:
    cfg = _news_settings(settings)
    if not cfg["gemini_api_key"]:
        # Scoring skipped entirely; rows persist with sentiment=None and can be
        # scored later once a key is configured.
        return None, ["news:gemini_key_missing_unscored"]
    return GeminiScorer(cfg["gemini_api_key"], cfg["gemini_model"]), []


async def _ingest_and_score(db: Any, symbols: Sequence[str], articles: list[dict],
                            scorer: GeminiScorer | None, now: datetime,
                            notes: list[str]) -> None:
    """Cluster + persist NEW rows per symbol, then Gemini-score only those new
    rows in one cross-symbol batched pass (the article-id cache)."""
    window_h = float(simmer_config.velocity().get("dedup_window_hours", 6))
    to_score: list[dict] = []
    for sym in symbols:
        rows = _article_rows_for_symbol(articles, sym)
        if not rows:
            continue
        existing_ids = await asyncio.to_thread(
            fetch_existing_article_ids, db, [r["article_id"] for r in rows])
        new_rows = [r for r in rows if r["article_id"] not in existing_ids]
        if not new_rows:
            continue
        # Cluster against everything recent enough to matter (window + backfill).
        since = now - timedelta(hours=BACKFILL_HOURS + window_h)
        recent = await asyncio.to_thread(fetch_news_since, db, sym, since)
        keys = assign_clusters(new_rows, recent)
        for r in new_rows:
            r["cluster_key"] = keys.get(r["article_id"])
            r["sentiment"] = r["confidence"] = r["model"] = r["scored_at"] = None
        await asyncio.to_thread(persist_news_rows, db, new_rows)
        to_score.extend({"id": r["article_id"], "symbol": sym,
                         "headline": r["headline"]} for r in new_rows)

    if not to_score or scorer is None:
        return
    try:
        scores = await scorer.score_headlines(to_score)
    except asyncio.CancelledError:
        raise
    except Exception as e:                        # belt-and-braces: never crash
        notes.append(f"news:gemini_failed:{type(e).__name__}")
        log.warning("gemini scoring failed: %s", e)
        return
    if scores:
        await asyncio.to_thread(update_news_scores, db, scores, scorer.model, now)
    missed = len(to_score) - len(scores)
    if missed > 0:
        notes.append(f"news:unscored_headlines:{missed}")


async def refresh_news(db: Any, symbols: list[str], settings: Any, *,
                       client: Any = None, scorer: Any = None,
                       rss_fallback: Any = None,
                       persist_research: bool = True,
                       now: datetime | None = None) -> dict[str, dict]:
    """THE news entry point (watcher tier-1 + operator calls).

    One provider fetch covers the whole symbol batch (comma-separated symbols
    on Alpaca), then per-symbol: cluster → persist new → score new → aggregate
    via `app.simmer_velocity` → research-cache field updates.

    Never raises: missing config is a clean no-op (None fields + note), and a
    provider failure returns stamped no-op updates for this cycle. After
    `ALPACA_FAILOVER_AFTER` consecutive Alpaca failures the wire-RSS firehose
    is used instead (noted). `persist_research=False` lets the watcher's
    `load_research` own the research-cache upsert (single write path).

    Returns {SYMBOL: updates} where updates carries the simmer_research_cache
    news fields (sentiment_score, sentiment_n, velocity_p, velocity_tier,
    news_at) plus a transient `_news_notes` list (stripped before persistence
    by the watcher's `_persistable`).
    """
    global _alpaca_consecutive_failures
    now = now or _now_naive_utc()
    syms = sorted({str(s).strip().upper() for s in (symbols or []) if str(s).strip()})
    base_notes: list[str] = []

    own_client = False
    if client is None:
        client, base_notes = build_news_client(settings)
        own_client = client is not None
    if client is None:
        # Clean no-op: nothing configured. Stamp news_at so the TTL machinery
        # is exercised (and the sweep doesn't hot-loop the no-op).
        return {s: {"news_at": now, "sentiment_score": None, "sentiment_n": None,
                    "velocity_p": None, "velocity_tier": None,
                    "_news_notes": list(base_notes)} for s in syms}

    own_scorer = False
    if scorer is None:
        scorer, scorer_notes = build_scorer(settings)
        base_notes.extend(scorer_notes)
        own_scorer = scorer is not None

    articles: list[dict] = []
    fetch_failed = False
    try:
        try:
            articles = await client.fetch_news(
                syms, start=now - timedelta(hours=BACKFILL_HOURS), end=now)
            if isinstance(client, AlpacaNewsClient):
                _alpaca_consecutive_failures = 0
        except asyncio.CancelledError:
            raise
        except Exception as e:
            fetch_failed = True
            base_notes.append(f"news:fetch_failed:{type(e).__name__}")
            log.warning("news fetch failed (%s): %s", type(client).__name__, e)
            if isinstance(client, AlpacaNewsClient):
                _alpaca_consecutive_failures += 1
                if _alpaca_consecutive_failures >= ALPACA_FAILOVER_AFTER:
                    # Persistently failing → the wire firehose takes over.
                    fb = rss_fallback or RssNewsClient()
                    try:
                        articles = await fb.fetch_news(syms)
                        fetch_failed = False
                        base_notes.append("news:rss_fallback_engaged")
                    except asyncio.CancelledError:
                        raise
                    except Exception as e2:
                        base_notes.append(
                            f"news:rss_fallback_failed:{type(e2).__name__}")
                    finally:
                        if rss_fallback is None:
                            await fb.close()

        if not fetch_failed:
            try:
                await _ingest_and_score(db, syms, articles, scorer, now, base_notes)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                base_notes.append(f"news:ingest_failed:{type(e).__name__}")
                log.exception("news ingest failed")
    finally:
        if own_client:
            try:
                await client.close()
            except Exception:
                pass
        if own_scorer and scorer is not None:
            try:
                await scorer.close()
            except Exception:
                pass

    out: dict[str, dict] = {}
    for sym in syms:
        notes = list(base_notes)
        updates: dict[str, Any] = {"news_at": now}
        if db is not None and not fetch_failed:
            updates.update(await asyncio.to_thread(
                _aggregate_symbol, db, sym, now, notes))
        else:
            updates.update({"sentiment_score": None, "sentiment_n": None,
                            "velocity_p": None, "velocity_tier": None})
        updates["_news_notes"] = notes
        out[sym] = updates
        if persist_research and db is not None:
            try:
                row = await asyncio.to_thread(db.get_simmer_research, sym) or {}
                merged = {**row, **{k: v for k, v in updates.items()
                                    if not str(k).startswith("_")},
                          "symbol": sym}
                merged.pop("updated_at", None)
                await asyncio.to_thread(db.upsert_simmer_research, merged)
            except Exception as e:
                log.warning("news research upsert failed for %s: %s", sym, e)
    return out

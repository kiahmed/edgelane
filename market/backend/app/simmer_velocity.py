"""Simmer news-velocity detector + sentiment aggregation — pure math, no I/O.

Implements the two Phase-3b algorithms specified in `docs/simmer.md`:

**News velocity** — a negative-binomial upper-tail p-value on deduped CLUSTER
counts over a rolling 60-minute window, against a seasonally-matched,
empirical-Bayes-shrunk baseline. The naive z-score-on-counts approach fails
three ways (overdispersion from syndication, the 08:30 ET seasonality spike,
and μ ≈ 0.007 per 5-min bucket making any nonzero count a >10σ "event"), so
three design choices carry the weight:

  1. *Seasonally-matched baseline, not a trailing mean* — the baseline for the
     current window is the count in the same clock-window on the previous
     LOOKBACK_DAYS (20) trading days. Deseasonalized by construction; immune
     to the 08:30 spike.
  2. *Negative binomial, not Poisson* — one event generates a correlated
     cluster of articles, so Var > Mean structurally. The Gamma-Poisson
     (empirical-Bayes) posterior gives the NB predictive distribution for
     free.
  3. *Empirical-Bayes shrinkage across tickers* — a Gamma prior fit to the
     cross-sectional rate distribution pulls sparse tickers toward the pooled
     rate and removes the cold-start problem entirely (a brand-new ticker
     starts at the pooled rate).

The statistic reported is an **exact NB tail probability, never a z-score** —
a p-value is directly comparable between a 2-article/day small-cap and a
50-article/day mega-cap; a z-score is not. `MIN_CLUSTERS` is a hard gate that
kills the one-article-is-∞σ failure, and hysteresis keeps a burst "on" until
p > 0.05 for 3 consecutive evaluations.

**Sentiment aggregation** — per Heston & Sinha, aggregation is what converts a
2-day signal into a 13-week one, so sentiment is averaged over a trailing
5-TRADING-DAY window: a recency-weighted (exponential, ~2-day half-life) and
confidence-weighted mean of per-CLUSTER sentiment. Clusters, not articles —
one press release syndicated to a dozen wires is one opinion, not twelve.

Like `simmer_engine`, this module is a pure function of (inputs, cfg): no
network, no DB, no clock reads. The caller (`simmer_news`, which owns the DB
lookup and Gemini scoring) supplies bucket counts, baseline samples, pooled
rates, and `now`. SciPy is deliberately NOT a dependency — the NB survival
function is implemented here in log-space.

Public interface consumed by `simmer_news` (keep stable):
    velocity_score(counts_by_bucket, now, cfg, *, baseline_samples,
                   pooled_prior=None) -> dict
    aggregate_sentiment(scored_rows, now, cfg) -> dict
    fit_pooled_prior(rates, prior_weight) -> (alpha0, beta0)
    velocity_rearm(prev_tier, p, consecutive_calm, cfg) -> str
    effective_sentiment(score, horizon_days, cfg, ...) -> float
plus the pure helpers `nb_sf`, `apply_guard`, `collect_baseline_samples`,
`window_count`, `bucket_start`, `trading_age_days`, and the re-exported
`sentiment_persistence`.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping

# The persistence math itself lives in the engine (Phase 1) and already covers
# everything the spec asks for — asymmetric half-lives (POS 5d / NEG 35d),
# the day-0 ×0.25 discount, the index 1-day half-life, and the earnings gate.
# Re-exported here so news-side callers have one import surface.
from .simmer_engine import sentiment_persistence  # noqa: F401  (re-export)

__all__ = [
    "velocity_score", "aggregate_sentiment", "fit_pooled_prior",
    "velocity_rearm", "effective_sentiment", "sentiment_persistence",
    "nb_sf", "apply_guard", "collect_baseline_samples", "window_count",
    "bucket_start", "trading_age_days",
]


# ─────────────────────────────────────────────────────────────────────────────
# Time helpers (pure; caller owns the clock)
# ─────────────────────────────────────────────────────────────────────────────
def _as_dt(value: Any) -> datetime | None:
    """Coerce a datetime or ISO-8601 string to a *naive UTC* datetime.

    DuckDB hands back naive timestamps; feed APIs hand back ISO strings with
    or without a zone. Everything is normalized to naive UTC so comparisons
    never raise on aware/naive mixing."""
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def bucket_start(ts: datetime, bucket_minutes: int) -> datetime:
    """Floor a timestamp to the start of its bucket."""
    ts = _as_dt(ts)
    m = int(bucket_minutes)
    floored = (ts.minute // m) * m
    return ts.replace(minute=floored, second=0, microsecond=0)


def trading_age_days(published: Any, now: Any) -> float:
    """Fractional age in TRADING days: calendar days minus the weekend days in
    between. Holidays are not subtracted (a pure module has no exchange
    calendar) — a documented approximation that only slightly shortens
    half-lives across holiday weeks."""
    t0, t1 = _as_dt(published), _as_dt(now)
    if t0 is None or t1 is None or t1 <= t0:
        return 0.0
    raw = (t1 - t0).total_seconds() / 86400.0
    d0, d1 = t0.date(), t1.date()
    weekend = sum(1 for i in range((d1 - d0).days + 1)
                  if (d0 + timedelta(days=i)).weekday() >= 5)
    return max(0.0, raw - weekend)


def _prev_trading_day(d: datetime) -> datetime:
    """Same clock time, previous weekday (holidays not modelled — see above)."""
    d = d - timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


# ─────────────────────────────────────────────────────────────────────────────
# Window / baseline plumbing
# ─────────────────────────────────────────────────────────────────────────────
def _normalize_counts(counts_by_bucket: Any, bucket_minutes: int) -> dict[datetime, int]:
    """Accept `{bucket_start: count}` or an iterable of `(ts, count)` pairs.
    Timestamps are floored to bucket starts; coarser-than-5-min input simply
    lands on its own floor (counts are summed if two keys collide)."""
    items: Iterable
    if isinstance(counts_by_bucket, Mapping):
        items = counts_by_bucket.items()
    else:
        items = counts_by_bucket or []
    out: dict[datetime, int] = {}
    for ts, c in items:
        b = bucket_start(ts, bucket_minutes)
        out[b] = out.get(b, 0) + int(c)
    return out


def window_count(counts_by_bucket: Any, window_end: Any, cfg: dict) -> int:
    """Total cluster count in the window `(window_end − WINDOW, window_end]` —
    i.e. buckets whose start lies in `[window_end − WINDOW, window_end)`."""
    bm = int(cfg.get("bucket_minutes", 5))
    wm = int(cfg.get("window_minutes", 60))
    end = _as_dt(window_end)
    start = end - timedelta(minutes=wm)
    counts = _normalize_counts(counts_by_bucket, bm)
    return sum(c for b, c in counts.items() if start <= b < end)


def _guard_cutoff(now: datetime, cfg: dict) -> datetime:
    """Latest instant baseline data may touch: `now − WINDOW − GUARD`.

    EARS-C2-style guard band. The current window plus the GUARD_BUCKETS
    (30 min) before it are off-limits to the baseline, so a burst that began
    20 minutes ago — or anywhere inside the last window+guard span — can
    never inflate its own baseline."""
    wm = int(cfg.get("window_minutes", 60))
    gm = int(cfg.get("guard_buckets", 6)) * int(cfg.get("bucket_minutes", 5))
    return _as_dt(now) - timedelta(minutes=wm + gm)


def apply_guard(samples: Iterable[tuple[Any, int]], now: Any, cfg: dict) -> list[int]:
    """Filter `(window_end, count)` baseline samples through the guard band.

    A sample is kept only if its window ends at or before the guard cutoff
    (`now − WINDOW − GUARD`). This is how "a burst in the last 30 minutes is
    excluded from its own baseline" is enforced when the caller assembles
    baseline windows from the DB."""
    cutoff = _guard_cutoff(now, cfg)
    out: list[int] = []
    for end, count in samples:
        end_dt = _as_dt(end)
        if end_dt is not None and end_dt <= cutoff:
            out.append(int(count))
    return out


def collect_baseline_samples(counts_by_bucket: Any, now: Any, cfg: dict) -> list[int]:
    """Seasonally-matched baseline from a flat bucket-count history.

    Returns the total count in the same clock-window on each of the previous
    LOOKBACK_DAYS trading days (weekends skipped; holidays contribute a zero
    sample, which understates μ slightly — conservative in the safe direction
    is NOT true here, so callers with a holiday calendar should prefer their
    own DB lookup + `apply_guard`). The guard band is applied structurally:
    any window touching `(now − WINDOW − GUARD, now]` is discarded."""
    lookback = int(cfg.get("lookback_days", 20))
    end = _as_dt(now)
    pairs: list[tuple[datetime, int]] = []
    day_end = end
    for _ in range(lookback):
        day_end = _prev_trading_day(day_end)
        pairs.append((day_end, window_count(counts_by_bucket, day_end, cfg)))
    return apply_guard(pairs, now, cfg)


# ─────────────────────────────────────────────────────────────────────────────
# Empirical-Bayes Gamma prior + negative-binomial tail
# ─────────────────────────────────────────────────────────────────────────────
def fit_pooled_prior(rates: list[float], prior_weight: float) -> tuple[float, float]:
    """Gamma(α₀, β₀) prior on the per-window cluster rate, fit by method of
    moments to the cross-sectional distribution of per-ticker rates.

    Each entry of `rates` is one ticker's mean clusters-per-window. Because a
    rate estimated from Poisson-ish counts carries sampling variance ≈ its
    mean, only the variance **in excess of the mean** reflects true
    between-ticker dispersion:

        excess = v − m;   β₀ = m / excess;   α₀ = m·β₀   (prior mean m)

    **Degenerate case (v ≤ m, or fewer than two tickers): the cross-section
    shows no dispersion beyond Poisson noise, so the MoM strength is infinite
    /undefined.** We fall back to a prior worth exactly `prior_weight`
    pseudo-windows at the pooled mean. In the regular case `prior_weight`
    caps the strength for the same reason — a razor-tight cross-sectional
    fit must not drown a ticker's own 20 baseline samples.

    β₀ is the prior's pseudo-window count: the posterior after n windows is
    Gamma(α₀ + ΣB, β₀ + n), so a sparse ticker (n=2) sits mostly at the
    pooled rate while a dense one (n=20) barely moves."""
    xs = [float(r) for r in (rates or []) if r is not None and float(r) >= 0.0]
    pw = max(float(prior_weight), 1e-9)
    n = len(xs)
    m = (sum(xs) / n) if n else 0.0
    m = max(m, 1e-6)                       # an all-zero pool still yields a usable prior
    if n < 2:
        return (m * pw, pw)
    mean = sum(xs) / n
    v = sum((x - mean) ** 2 for x in xs) / (n - 1)
    excess = v - mean
    if excess <= 0.0:                      # degenerate: v ≤ m — documented above
        return (m * pw, pw)
    beta0 = min(m / excess, pw)
    return (m * beta0, beta0)


def nb_sf(x: int, r: float, p: float) -> float:
    """Negative-binomial upper tail P(X ≥ x) for X ~ NB(r, p), pmf
    C(k+r−1, k)·pʳ·(1−p)ᵏ — the Gamma-Poisson posterior predictive with
    r = α (shape) and p = β/(β+1).

    Implemented in log-space with the term recursion
    pmf(k+1)/pmf(k) = (k+r)/(k+1)·(1−p), summing upward from x until the
    remaining geometric tail is below 1e−18 of the accumulated sum. Stable
    for x in the hundreds where a naive linear-space sum underflows.
    SciPy is intentionally not used."""
    x = int(x)
    if x <= 0:
        return 1.0
    r = float(r)
    p = min(max(float(p), 1e-12), 1.0 - 1e-12)
    log_p, log_q = math.log(p), math.log1p(-p)
    # log pmf(x)
    log_term = (math.lgamma(x + r) - math.lgamma(r) - math.lgamma(x + 1)
                + r * log_p + x * log_q)
    log_sum = log_term
    k = x
    for _ in range(1_000_000):
        log_term += math.log((k + r) / (k + 1.0)) + log_q
        # logaddexp(log_sum, log_term)
        hi, lo = (log_sum, log_term) if log_sum >= log_term else (log_term, log_sum)
        log_sum = hi + math.log1p(math.exp(lo - hi))
        k += 1
        rho = (k + r) / (k + 1.0) * (1.0 - p)
        if rho < 1.0 and (log_term - log_sum) < math.log(1e-18) - math.log1p(-rho):
            break
    return min(1.0, math.exp(log_sum))


# ─────────────────────────────────────────────────────────────────────────────
# Velocity score
# ─────────────────────────────────────────────────────────────────────────────
def _tier_for(p: float | None, x: int, cfg: dict) -> str:
    """Threshold tiers with the MIN_CLUSTERS hard gate. Below the gate the
    tier is "none" regardless of p — a 1-cluster window is never a burst, no
    matter how quiet the baseline (the one-article-is-∞σ failure)."""
    if x < int(cfg.get("min_clusters", 3)) or p is None:
        return "none"
    if p <= float(cfg.get("alert_p", 0.001)):
        return "alert"
    if p <= float(cfg.get("warn_p", 0.01)):
        return "warn"
    return "none"


def velocity_score(counts_by_bucket: Any, now: Any, cfg: dict, *,
                   baseline_samples: list[int],
                   pooled_prior: tuple[float, float] | None = None) -> dict:
    """One velocity evaluation for one symbol at `now`.

    Args:
        counts_by_bucket: deduped CLUSTER counts (not raw articles) for this
            symbol, `{bucket_start: count}` or `(ts, count)` pairs, 5-min or
            coarser buckets. Only the trailing 60-min window is read.
        now: evaluation instant (rolling — the caller re-invokes every poll).
        cfg: `simmer_config.velocity()` (or the "velocity" block of a resolved
            config dict).
        baseline_samples: same-clock-window totals on the previous
            LOOKBACK_DAYS trading days, guard-band applied — the caller owns
            the DB lookup (`collect_baseline_samples` / `apply_guard` help).
        pooled_prior: (α₀, β₀) from `fit_pooled_prior` over all tickers'
            rates. With it, a brand-new ticker (zero baseline history) is
            scored from the pooled rate alone — no cold start. Without it, a
            near-flat prior is used and empty history yields p=None.

    Returns `{"p", "tier", "ratio", "x", "mu"}` where `p` is the EXACT NB
    upper-tail probability P(X ≥ x) — never a z-score, so it is directly
    comparable across tickers with wildly different base rates. `mu` is the
    posterior-predictive mean count per window and `ratio = x / mu`."""
    x = window_count(counts_by_bucket, now, cfg)
    samples = [int(s) for s in (baseline_samples or [])]

    if pooled_prior is not None:
        a0, b0 = float(pooled_prior[0]), float(pooled_prior[1])
    elif samples:
        a0, b0 = 1e-6, 1e-6          # near-flat: posterior ≈ (ΣB, n)
    else:
        # No history and no prior — nothing to test against. Refuse to guess.
        return {"p": None, "tier": "none", "ratio": None, "x": x, "mu": None}

    a = a0 + float(sum(samples))     # posterior Gamma(α₀+ΣB, β₀+n)
    b = b0 + float(len(samples))
    mu = a / b
    p_nb = b / (b + 1.0)             # NB success prob of the posterior predictive
    p = nb_sf(x, r=a, p=p_nb)
    ratio = (x / mu) if mu > 0 else None
    return {"p": p, "tier": _tier_for(p, x, cfg), "ratio": ratio, "x": x, "mu": mu}


def velocity_rearm(prev_tier: str, p: float | None, consecutive_calm: int,
                   cfg: dict) -> str:
    """Hysteresis latch: an alert stays ON until p > HYSTERESIS_EXIT_P (0.05)
    for HYSTERESIS_POLLS (3) consecutive evaluations.

    `consecutive_calm` is the caller-maintained count of consecutive
    evaluations (including the current one) with p above the exit threshold;
    the caller resets it to 0 whenever p dips back at or below it. When
    `prev_tier` is not "alert" this returns the plain threshold tier for `p` —
    use `velocity_score`'s own tier in that case (it also applies the
    MIN_CLUSTERS gate, which this latch does not re-check)."""
    exit_p = float(cfg.get("hysteresis_exit_p", 0.05))
    polls = int(cfg.get("hysteresis_polls", 3))
    if prev_tier == "alert":
        if p is None or p <= exit_p or int(consecutive_calm) < polls:
            return "alert"
    if p is None:
        return "none"
    if p <= float(cfg.get("alert_p", 0.001)):
        return "alert"
    if p <= float(cfg.get("warn_p", 0.01)):
        return "warn"
    return "none"


# ─────────────────────────────────────────────────────────────────────────────
# Sentiment aggregation
# ─────────────────────────────────────────────────────────────────────────────
def aggregate_sentiment(scored_rows: Iterable[Mapping], now: Any, cfg: dict) -> dict:
    """Trailing 5-TRADING-DAY aggregated sentiment for one symbol.

    Per Heston & Sinha, aggregation is what converts a 2-day signal into a
    13-week one — "feed it sentiment averaged over a trailing 5 trading days"
    is the single most evidence-backed design choice available.

    Args:
        scored_rows: `simmer_news`-shaped rows (dicts with `published_at`,
            `cluster_key`, `sentiment`, `confidence`); unscored rows
            (sentiment None) are skipped.
        now: evaluation instant.
        cfg: `simmer_config.sentiment()` block.

    Mechanics — CLUSTERS, not articles: rows sharing a `cluster_key` are one
    story (a press release syndicated to five wires is one opinion, not
    five). Cluster sentiment/confidence are the means over members; the
    cluster's timestamp is its most recent member's `published_at`. The
    symbol-level score is then the weighted mean over clusters with

        weight = confidence × 0.5^(age_trading_days / RECENCY_HALFLIFE)

    (RECENCY_HALFLIFE ≈ 2 trading days — newer headlines weigh more).

    Returns `{"score", "n", "weighted", "unweighted"}`: `score` is the
    weighted mean (what the engine's `research["sentiment"]["score"]`
    consumes), `weighted` is the same value kept under its explicit name,
    `unweighted` the plain cluster mean, and `n` the cluster count — so
    downstream can distinguish "0.0 balanced" (n>0) from "0.0 no news" (n=0,
    score None)."""
    trailing = float(cfg.get("trailing_days", 5))
    recency_hl = max(float(cfg.get("recency_halflife_days", 2.0)), 1e-9)
    now_dt = _as_dt(now)

    # Group in-window scored rows into clusters.
    clusters: dict[str, list[Mapping]] = {}
    for i, row in enumerate(scored_rows or []):
        s = row.get("sentiment")
        ts = _as_dt(row.get("published_at"))
        if s is None or ts is None:
            continue
        if trading_age_days(ts, now_dt) > trailing:
            continue
        key = row.get("cluster_key") or row.get("article_id") or f"__row_{i}"
        clusters.setdefault(str(key), []).append(row)

    n = len(clusters)
    if n == 0:
        return {"score": None, "n": 0, "weighted": None, "unweighted": None}

    w_sum = ws_sum = plain_sum = 0.0
    for members in clusters.values():
        sent = sum(float(m["sentiment"]) for m in members) / len(members)
        conf = sum(float(m.get("confidence") if m.get("confidence") is not None else 1.0)
                   for m in members) / len(members)
        newest = max(_as_dt(m.get("published_at")) for m in members)
        age = trading_age_days(newest, now_dt)
        w = max(conf, 0.0) * (0.5 ** (age / recency_hl))
        w_sum += w
        ws_sum += w * sent
        plain_sum += sent

    score = (ws_sum / w_sum) if w_sum > 0 else (plain_sum / n)
    return {"score": score, "n": n, "weighted": score, "unweighted": plain_sum / n}


def effective_sentiment(score: float, horizon_days: float, cfg: dict,
                        is_index: bool = False,
                        earnings_inside_tenor: bool = True) -> float:
    """Sentiment still effective at horizon `h` (trading days):
    `score × persistence(score, h)`.

    Thin wrapper over `simmer_engine.sentiment_persistence`, which already
    implements everything the spec requires — the asymmetric half-lives
    (POS 5d, gone by week 3; NEG 35d, significant at week 13), the day-0
    ×0.25 discount (contemporaneous impact + reversal, not prediction), the
    index 1-day half-life, and the earnings gate (negative persistence is
    realized AT the next print; if the spread expires first, fall back to the
    short half-life). `cfg` is `simmer_config.sentiment()`; the persistence
    weight alone is available via the re-exported `sentiment_persistence`."""
    s = float(score)
    return s * sentiment_persistence(s, float(horizon_days), cfg,
                                     is_index=is_index,
                                     earnings_inside_tenor=earnings_inside_tenor)

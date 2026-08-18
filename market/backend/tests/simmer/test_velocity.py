"""News-velocity detector + sentiment aggregation — hand-computed fixtures, no I/O.

Every case is an explicit input→expected table (mirroring `test_engine.py`).
The NB tail cases are computed by hand from the pmf; the scenario tests
reproduce the failure modes `docs/simmer.md` designs against — the 08:30
seasonality spike, the one-article-is-∞σ failure, cold starts, and a burst
inflating its own baseline.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta

import pytest

from app import simmer_config
from app import simmer_velocity as sv

CFG_V = dict(simmer_config.VELOCITY)      # module defaults, no override file
CFG_S = dict(simmer_config.SENTIMENT)

# A Thursday afternoon — weekday stepping stays simple, and the trailing
# 5-trading-day sentiment window reaches back across one weekend.
NOW = datetime(2026, 8, 13, 14, 30)


def _prev_trading_day(d: datetime) -> datetime:
    out = d - timedelta(days=1)
    while out.weekday() >= 5:
        out -= timedelta(days=1)
    return out


# ═══════════════════════════════════════════════════════════════════════════
# Negative-binomial upper tail — hand-computed, monotone, log-space stable
# ═══════════════════════════════════════════════════════════════════════════
# pmf(k) = C(k+r−1, k) · pʳ · (1−p)ᵏ.
# r=2, p=0.5: pmf(k) = (k+1)·0.25·0.5ᵏ → P(X≥3) = 1 − (0.25 + 0.25 + 0.1875)
# r=1, p=0.5: geometric → P(X≥x) = 0.5ˣ
# r=3, p=0.4: P(X≥2) = 1 − 0.4³ − 3·0.4³·0.6
NB_TAIL_CASES = [
    # (x, r, p, expected)
    (3, 2.0, 0.5, 0.3125),
    (3, 1.0, 0.5, 0.125),
    (5, 1.0, 0.5, 0.03125),
    (2, 3.0, 0.4, 0.8208),
    (0, 2.0, 0.5, 1.0),        # empty lower tail — probability mass is everything
    (1, 2.0, 0.5, 0.75),       # 1 − pmf(0) = 1 − 0.25
]


@pytest.mark.parametrize("x,r,p,expected", NB_TAIL_CASES)
def test_nb_tail_hand_computed(x, r, p, expected):
    assert sv.nb_sf(x, r, p) == pytest.approx(expected, abs=1e-9)


def test_nb_tail_monotone_decreasing_in_x():
    vals = [sv.nb_sf(x, 4.0, 0.7) for x in range(0, 40)]
    assert vals[0] == 1.0
    assert all(a > b for a, b in zip(vals, vals[1:]))


def test_nb_tail_log_space_stable_at_x_500():
    """r=2, p=q=0.5 has the closed form P(X≥x) = 0.5ˣ·(x/2 + 1). At x=500
    that is ~7.7e-149 — far below where a linear-space pmf sum survives, but
    exactly representable via the log-space recursion."""
    got = sv.nb_sf(500, 2.0, 0.5)
    expected_log = 500 * math.log(0.5) + math.log(251.0)
    assert got > 0.0
    assert math.log(got) == pytest.approx(expected_log, abs=1e-9)


# ═══════════════════════════════════════════════════════════════════════════
# The 08:30 spike: seasonal baseline absorbs it; a flat baseline alerts
# ═══════════════════════════════════════════════════════════════════════════
# Same current count (9 clusters in the last 60 min) both times. Against a
# baseline of 20 same-clock windows that ALSO run ~8 clusters (every morning
# looks like this), the burst is ordinary. Against a flat 1-cluster baseline
# it is a >99.9% event.
MORNING_BASELINE = [8, 7, 9, 8, 8, 7, 9, 8, 8, 8, 7, 9, 8, 8, 8, 9, 7, 8, 8, 8]
FLAT_BASELINE = [1] * 20
CURRENT_COUNTS = {NOW - timedelta(minutes=10): 5, NOW - timedelta(minutes=25): 4}


def test_morning_spike_with_seasonal_baseline_does_not_alert():
    res = sv.velocity_score(CURRENT_COUNTS, NOW, CFG_V,
                            baseline_samples=MORNING_BASELINE)
    assert res["x"] == 9
    assert res["mu"] == pytest.approx(8.0, rel=1e-3)
    assert res["p"] > CFG_V["warn_p"]
    assert res["tier"] == "none"


def test_same_count_against_flat_baseline_alerts():
    res = sv.velocity_score(CURRENT_COUNTS, NOW, CFG_V,
                            baseline_samples=FLAT_BASELINE)
    assert res["x"] == 9
    assert res["mu"] == pytest.approx(1.0, rel=1e-3)
    assert res["p"] < CFG_V["alert_p"]
    assert res["tier"] == "alert"
    assert res["ratio"] == pytest.approx(9.0, rel=1e-3)


def test_p_is_a_probability_not_a_z_score():
    """Cross-ticker comparability is the point: whatever the baseline, the
    statistic must live in [0, 1]."""
    for baseline in (MORNING_BASELINE, FLAT_BASELINE, [0] * 20):
        res = sv.velocity_score(CURRENT_COUNTS, NOW, CFG_V,
                                baseline_samples=baseline)
        assert 0.0 <= res["p"] <= 1.0


# ═══════════════════════════════════════════════════════════════════════════
# MIN_CLUSTERS hard gate — the one-article-is-∞σ failure
# ═══════════════════════════════════════════════════════════════════════════
def test_min_clusters_gate_overrides_absurd_p():
    """x=2 against a dead-silent baseline: p is astronomically small, but 2
    clusters is below MIN_CLUSTERS=3 — tier stays none regardless."""
    counts = {NOW - timedelta(minutes=10): 2}
    res = sv.velocity_score(counts, NOW, CFG_V, baseline_samples=[0] * 20)
    assert res["x"] == 2
    assert res["p"] is not None and res["p"] < CFG_V["alert_p"]   # "absurd" p
    assert res["tier"] == "none"                                   # gate wins


def test_min_clusters_boundary_x_equals_3_can_alert():
    counts = {NOW - timedelta(minutes=10): 3}
    res = sv.velocity_score(counts, NOW, CFG_V, baseline_samples=[0] * 20)
    assert res["x"] == 3
    assert res["tier"] == "alert"


# ═══════════════════════════════════════════════════════════════════════════
# Empirical-Bayes prior: method of moments + shrinkage behaviour
# ═══════════════════════════════════════════════════════════════════════════
def test_fit_pooled_prior_degenerate_v_le_m():
    """Cross-sectional variance below the mean = no dispersion beyond Poisson
    noise → fall back to prior_weight pseudo-windows at the pooled mean."""
    a0, b0 = sv.fit_pooled_prior([0.8, 1.0, 1.2, 0.9, 1.1], prior_weight=5.0)
    assert b0 == pytest.approx(5.0)
    assert a0 / b0 == pytest.approx(1.0)      # prior mean = pooled mean


def test_fit_pooled_prior_moment_matched_when_overdispersed():
    """rates m=3.8, sample v=16.4267 → excess 12.6267 → β₀ = m/excess ≈ 0.301,
    prior mean preserved, strength below the prior_weight cap."""
    rates = [0.2, 5.0, 1.0, 9.0]
    a0, b0 = sv.fit_pooled_prior(rates, prior_weight=5.0)
    assert b0 == pytest.approx(3.8 / 12.626667, rel=1e-5)
    assert a0 / b0 == pytest.approx(3.8, rel=1e-9)
    assert b0 < 5.0


def test_fit_pooled_prior_empty_and_single_are_usable():
    a0, b0 = sv.fit_pooled_prior([], prior_weight=5.0)
    assert a0 > 0 and b0 == pytest.approx(5.0)
    a0, b0 = sv.fit_pooled_prior([2.0], prior_weight=5.0)
    assert a0 / b0 == pytest.approx(2.0)


def test_eb_shrinkage_sparse_pulled_dense_barely_moves():
    """Pooled prior at rate 1.0 (strength 5). A sparse ticker with two
    windows of 4 clusters sits mostly at the pooled rate; a dense ticker with
    twenty windows of 4 keeps most of its own rate."""
    prior = sv.fit_pooled_prior([0.8, 1.0, 1.2, 0.9, 1.1], prior_weight=5.0)
    counts = {NOW - timedelta(minutes=10): 4}
    sparse = sv.velocity_score(counts, NOW, CFG_V,
                               baseline_samples=[4, 4], pooled_prior=prior)
    dense = sv.velocity_score(counts, NOW, CFG_V,
                              baseline_samples=[4] * 20, pooled_prior=prior)
    assert sparse["mu"] == pytest.approx((5.0 + 8.0) / (5.0 + 2.0), rel=1e-9)   # 1.857
    assert dense["mu"] == pytest.approx((5.0 + 80.0) / (5.0 + 20.0), rel=1e-9)  # 3.4
    assert abs(sparse["mu"] - 1.0) < abs(sparse["mu"] - 4.0)   # pulled to pool
    assert abs(dense["mu"] - 4.0) < abs(dense["mu"] - 1.0)     # barely moved


def test_cold_start_new_ticker_scores_from_prior_alone():
    """Zero baseline history + pooled prior → posterior IS the prior. No
    crash, a real p, and a big burst still alerts."""
    prior = sv.fit_pooled_prior([0.8, 1.0, 1.2, 0.9, 1.1], prior_weight=5.0)
    burst = {NOW - timedelta(minutes=10): 12}
    res = sv.velocity_score(burst, NOW, CFG_V,
                            baseline_samples=[], pooled_prior=prior)
    assert res["mu"] == pytest.approx(1.0)
    assert res["p"] is not None and 0.0 < res["p"] < CFG_V["alert_p"]
    assert res["tier"] == "alert"


def test_no_history_and_no_prior_refuses_to_guess():
    res = sv.velocity_score(CURRENT_COUNTS, NOW, CFG_V, baseline_samples=[])
    assert res == {"p": None, "tier": "none", "ratio": None, "x": 9, "mu": None}


# ═══════════════════════════════════════════════════════════════════════════
# Guard band — a burst cannot inflate its own baseline
# ═══════════════════════════════════════════════════════════════════════════
def test_apply_guard_drops_windows_inside_window_plus_guard():
    """Cutoff = now − 60 min − 30 min. A baseline window ending inside the
    current window (now−15) or the 30-min guard band (now−75) is excluded; a
    window ending exactly at the cutoff, or a day earlier, survives."""
    samples = [
        (NOW - timedelta(minutes=15), 50),    # inside current window → dropped
        (NOW - timedelta(minutes=75), 40),    # inside guard band     → dropped
        (NOW - timedelta(minutes=90), 7),     # exactly at cutoff     → kept
        (NOW - timedelta(days=1), 2),         # a day earlier         → kept
    ]
    assert sv.apply_guard(samples, NOW, CFG_V) == [7, 2]


def test_collect_baseline_samples_seasonal_and_burst_free():
    """History: 2 clusters in the same clock-window on each of the previous
    20 trading days, plus a 100-cluster burst 70 minutes ago (inside the
    guard band). The baseline must be exactly [2]*20 — the burst contributes
    nothing to its own baseline — and the current window then alerts against
    it."""
    counts: dict[datetime, int] = {}
    day_end = NOW
    for _ in range(CFG_V["lookback_days"]):
        day_end = _prev_trading_day(day_end)
        counts[day_end - timedelta(minutes=30)] = 2
    counts[NOW - timedelta(minutes=70)] = 100          # guard-band burst
    counts[NOW - timedelta(minutes=10)] = 12           # current window burst

    samples = sv.collect_baseline_samples(counts, NOW, CFG_V)
    assert samples == [2] * 20

    res = sv.velocity_score(counts, NOW, CFG_V, baseline_samples=samples)
    assert res["x"] == 12                              # guard bucket not in window
    assert res["mu"] == pytest.approx(2.0, rel=1e-3)
    assert res["tier"] == "alert"


# ═══════════════════════════════════════════════════════════════════════════
# Hysteresis — an alert stays on until p > 0.05 for 3 consecutive polls
# ═══════════════════════════════════════════════════════════════════════════
def test_hysteresis_alert_latches_and_clears():
    """Poll sequence after an alert: p=0.03 keeps it on (below exit); then
    two calm polls are not enough; the third calm poll clears it. A calm
    streak broken by one dip starts over."""
    exit_p = CFG_V["hysteresis_exit_p"]
    tier, calm = "alert", 0
    seq = [
        # (p, expected tier after this poll)
        (0.030, "alert"),   # still under exit threshold — stays on
        (0.060, "alert"),   # calm 1
        (0.200, "alert"),   # calm 2
        (0.040, "alert"),   # dip — streak resets
        (0.060, "alert"),   # calm 1 again
        (0.200, "alert"),   # calm 2
        (0.500, "none"),    # calm 3 — cleared
    ]
    for p, expected in seq:
        calm = calm + 1 if p > exit_p else 0
        tier = sv.velocity_rearm(tier, p, calm, CFG_V)
        assert tier == expected, f"p={p}: expected {expected}, got {tier}"


def test_hysteresis_non_alert_uses_plain_thresholds():
    assert sv.velocity_rearm("none", 0.0005, 0, CFG_V) == "alert"
    assert sv.velocity_rearm("none", 0.005, 0, CFG_V) == "warn"
    assert sv.velocity_rearm("warn", 0.5, 0, CFG_V) == "none"
    assert sv.velocity_rearm("alert", None, 5, CFG_V) == "alert"   # no data ≠ calm


# ═══════════════════════════════════════════════════════════════════════════
# Sentiment aggregation — trailing 5 trading days, cluster-level, weighted
# ═══════════════════════════════════════════════════════════════════════════
def _row(published: datetime, sentiment: float, confidence: float = 1.0,
         cluster: str = None, i: int = 0) -> dict:
    return {"article_id": f"a{i}", "cluster_key": cluster or f"c{i}",
            "published_at": published, "sentiment": sentiment,
            "confidence": confidence}


def test_aggregate_no_news_is_none_not_zero():
    res = sv.aggregate_sentiment([], NOW, CFG_S)
    assert res["score"] is None and res["weighted"] is None and res["n"] == 0


def test_aggregate_balanced_zero_is_distinguishable_from_no_news():
    rows = [_row(NOW - timedelta(hours=1), +0.5, cluster="c1", i=1),
            _row(NOW - timedelta(hours=1), -0.5, cluster="c2", i=2)]
    res = sv.aggregate_sentiment(rows, NOW, CFG_S)
    assert res["n"] == 2
    assert res["score"] == pytest.approx(0.0, abs=1e-12)   # balanced, NOT absent


def test_aggregate_recency_weighting_newer_wins():
    """+1 published one hour ago vs −1 published Monday (3 trading days back
    from Thursday NOW): the fresh cluster's ~1 weight dominates the stale
    cluster's 0.5^(3/2) ≈ 0.354 — the aggregate leans positive."""
    monday = NOW - timedelta(days=3)                      # Mon 14:30
    fresh = NOW - timedelta(hours=1)
    rows = [_row(fresh, +1.0, cluster="fresh", i=1),
            _row(monday, -1.0, cluster="stale", i=2)]
    res = sv.aggregate_sentiment(rows, NOW, CFG_S)
    hl = CFG_S["recency_halflife_days"]
    w_new = 0.5 ** (sv.trading_age_days(fresh, NOW) / hl)
    w_old = 0.5 ** (sv.trading_age_days(monday, NOW) / hl)
    expected = (w_new - w_old) / (w_new + w_old)
    assert res["score"] == pytest.approx(expected, rel=1e-6)
    assert res["score"] > 0                                # newer headline wins


def test_aggregate_confidence_weighting():
    """Same timestamp: +1 at confidence 0.9, −1 at 0.1 → (0.9−0.1)/1.0 = 0.8."""
    ts = NOW - timedelta(hours=2)
    rows = [_row(ts, +1.0, confidence=0.9, cluster="c1", i=1),
            _row(ts, -1.0, confidence=0.1, cluster="c2", i=2)]
    res = sv.aggregate_sentiment(rows, NOW, CFG_S)
    w = 0.5 ** (sv.trading_age_days(ts, NOW) / CFG_S["recency_halflife_days"])
    assert res["score"] == pytest.approx((0.9 * w - 0.1 * w) / (1.0 * w), rel=1e-9)
    assert res["score"] == pytest.approx(0.8, rel=1e-6)


def test_aggregate_cluster_mean_not_article_mean():
    """One press release syndicated to five wires (one cluster, s=−1) against
    two distinct positive stories: clusters vote [−1, +1, +1] → +1/3. An
    article-level mean would be (−5+2)/7 < 0 — syndication breadth must not
    masquerade as opinion."""
    ts = NOW - timedelta(hours=1)
    rows = [_row(ts, -1.0, cluster="syndicated", i=i) for i in range(5)]
    rows += [_row(ts, +1.0, cluster="story_a", i=10),
             _row(ts, +1.0, cluster="story_b", i=11)]
    res = sv.aggregate_sentiment(rows, NOW, CFG_S)
    assert res["n"] == 3
    assert res["score"] == pytest.approx(1.0 / 3.0, rel=1e-9)


def test_aggregate_trailing_window_excludes_old_and_unscored_rows():
    rows = [
        _row(NOW - timedelta(hours=1), +1.0, cluster="fresh", i=1),
        _row(NOW - timedelta(days=14), -1.0, cluster="ancient", i=2),   # out of window
        {"article_id": "a3", "cluster_key": "c3",
         "published_at": NOW - timedelta(hours=2),
         "sentiment": None, "confidence": None},                        # unscored
    ]
    res = sv.aggregate_sentiment(rows, NOW, CFG_S)
    assert res["n"] == 1
    assert res["score"] == pytest.approx(1.0)


def test_aggregate_accepts_iso_strings_like_duckdb_rows():
    rows = [_row((NOW - timedelta(hours=1)).isoformat(), +0.4, cluster="c1", i=1)]
    res = sv.aggregate_sentiment(rows, NOW.isoformat(), CFG_S)
    assert res["n"] == 1 and res["score"] == pytest.approx(0.4)


# ═══════════════════════════════════════════════════════════════════════════
# Effective sentiment — delegation to the engine's persistence math
# ═══════════════════════════════════════════════════════════════════════════
def test_effective_sentiment_negative_persists_where_positive_has_decayed():
    """h=20 trading days: positive weight 0.5^(20/5)=0.0625, negative
    0.5^(20/35)≈0.6727 — bad news travels slowly, >10× the staying power."""
    pos = sv.effective_sentiment(+0.5, 20.0, CFG_S)
    neg = sv.effective_sentiment(-0.5, 20.0, CFG_S)
    assert pos == pytest.approx(0.5 * 0.5 ** (20 / 5), rel=1e-12)
    assert neg == pytest.approx(-0.5 * 0.5 ** (20 / 35), rel=1e-12)
    assert abs(neg) / abs(pos) > 10.0


def test_effective_sentiment_day0_discount():
    got = sv.effective_sentiment(0.6, 0.5, CFG_S)
    assert got == pytest.approx(0.6 * (0.5 ** (0.5 / 5)) * 0.25, rel=1e-12)


def test_effective_sentiment_earnings_gate_falls_back_to_short_halflife():
    """Negative persistence is realized AT the next earnings print. If the
    spread expires first, the 35-day half-life is unrealizable — the gate
    swaps in the 5-day one."""
    gated = sv.effective_sentiment(-0.5, 20.0, CFG_S, earnings_inside_tenor=False)
    ungated = sv.effective_sentiment(-0.5, 20.0, CFG_S, earnings_inside_tenor=True)
    assert gated == pytest.approx(-0.5 * 0.5 ** (20 / 5), rel=1e-12)
    assert abs(gated) < abs(ungated)


def test_effective_sentiment_index_uses_one_day_halflife():
    got = sv.effective_sentiment(-0.5, 5.0, CFG_S, is_index=True)
    assert got == pytest.approx(-0.5 * 0.5 ** (5 / 1), rel=1e-12)


def test_sentiment_persistence_is_the_engine_function():
    """The wrapper re-exports the Phase-1 engine math rather than duplicating
    it — one source of truth for the half-life table."""
    from app import simmer_engine as se
    assert sv.sentiment_persistence is se.sentiment_persistence

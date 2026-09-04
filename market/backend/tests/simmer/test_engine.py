"""Simmer engine math — hand-computed / published fixtures, no I/O.

Every table here is checked in as explicit input→expected values. Several of
them reproduce tables published in `docs/simmer.md`, so a regression shows up
as a diff against the document the engine was specified from.
"""
from __future__ import annotations

import pytest

from app import simmer_engine as se

from .conftest import (DTE as DTE_DAYS, EXPECTED_CLOSE_TO_CLOSE, EXPECTED_PARKINSON,
                       EXPECTED_YZ_K, EXPECTED_YANG_ZHANG, SPOT, T, chain,
                       clean_inputs, ohlc_rows, synthetic_ohlc)

# ═══════════════════════════════════════════════════════════════════════════
# THE invariant: risk-neutral EV of a credit spread priced at mid is EXACTLY 0
# ═══════════════════════════════════════════════════════════════════════════
# Verified by numeric payoff integration. It catches whole classes of pricing
# error — a sign flip, a missing discount factor, a mis-bucketed integral, a
# drift that isn't risk-neutral — because every one of them moves EV off zero.

RN_CASES = [
    # (side, k_short, k_long, sigma, dte, r)
    ("put",   94.0,  89.0, 0.35, 30, 0.00),
    ("put",   90.0,  80.0, 0.22, 45, 0.00),
    ("put",   95.0,  94.0, 0.60,  7, 0.00),
    ("call", 107.0, 112.0, 0.35, 30, 0.00),
    ("call", 105.0, 115.0, 0.28, 60, 0.00),
    ("put",   94.0,  89.0, 0.35, 30, 0.05),   # non-zero rate must not break it
    ("call", 107.0, 112.0, 0.35, 30, 0.05),
]


@pytest.mark.parametrize("side,k_short,k_long,sigma,dte,r", RN_CASES)
def test_risk_neutral_ev_is_exactly_zero(side, k_short, k_long, sigma, dte, r):
    """Price the spread at its own model mid, then evaluate EV using that same
    vol as the "forecast". The answer must be zero to numerical precision."""
    t = dte / 365.0
    price = se.bs_put if side == "put" else se.bs_call
    credit = price(SPOT, k_short, sigma, t, r) - price(SPOT, k_long, sigma, t, r)
    assert credit > 0
    ev = se.expected_value(SPOT, k_short, k_long, credit, sigma, t, side, r)
    assert abs(ev) < 1e-5, f"EV should be exactly 0 under the RN measure, got {ev}"


def test_edge_appears_only_when_forecast_vol_differs_from_implied():
    """All edge comes from the gap between implied and subsequently realized
    vol. Selling at IV 0.35 and forecasting 0.28 is positive-EV; forecasting
    0.42 is negative-EV; forecasting 0.35 is exactly zero."""
    t = 30 / 365.0
    credit = se.bs_put(SPOT, 94.0, 0.35, t) - se.bs_put(SPOT, 89.0, 0.35, t)
    lower = se.expected_value(SPOT, 94.0, 89.0, credit, 0.28, t, "put")
    same = se.expected_value(SPOT, 94.0, 89.0, credit, 0.35, t, "put")
    higher = se.expected_value(SPOT, 94.0, 89.0, credit, 0.42, t, "put")
    assert lower > 0 > higher
    assert abs(same) < 1e-5


def test_binary_ev_formula_is_structurally_biased_negative():
    """`EV = POP×credit − (1−POP)×maxloss` must NOT be used: it treats
    everything below the breakeven as a FULL max loss and ignores the partial
    loss zone, so it returns a large negative number on a spread whose true EV
    is exactly zero."""
    t = 30 / 365.0
    k_s, k_l, sigma = 94.0, 89.0, 0.35
    credit = se.bs_put(SPOT, k_s, sigma, t) - se.bs_put(SPOT, k_l, sigma, t)
    max_loss = (k_s - k_l) - credit
    pop = se.pop_at_breakeven(SPOT, k_s, credit, sigma, t, "put")
    naive = pop * credit - (1.0 - pop) * max_loss
    integrated = se.expected_value(SPOT, k_s, k_l, credit, sigma, t, "put")
    assert abs(integrated) < 1e-5
    assert naive < -0.10, f"the naive formula's bias should be large, got {naive}"


def test_iron_condor_ev_is_additive_and_preserves_the_invariant():
    """Only one side of an IC can finish ITM, so the loss expectations add
    exactly — and the zero-EV invariant survives the combination."""
    t = 30 / 365.0
    sigma = 0.35
    put_credit = se.bs_put(SPOT, 94.0, sigma, t) - se.bs_put(SPOT, 89.0, sigma, t)
    call_credit = se.bs_call(SPOT, 106.0, sigma, t) - se.bs_call(SPOT, 111.0, sigma, t)
    ev = (se.expected_value(SPOT, 94.0, 89.0, put_credit, sigma, t, "put")
          + se.expected_value(SPOT, 106.0, 111.0, call_credit, sigma, t, "call"))
    assert abs(ev) < 1e-5


# ═══════════════════════════════════════════════════════════════════════════
# Side validation — an unrecognized side must RAISE, never fall through
# ═══════════════════════════════════════════════════════════════════════════
# Without a guard, `if side == "put" else <call branch>` silently prices the
# OPPOSITE structure for any unrecognized value. `side="bull_put"` — the
# structure vocabulary this module's own envelope uses — returned a C/W bound of
# 0.593 (the exact complement of the correct 0.407) and EVs near −2.1 instead of
# ~0. Plausible-looking, completely wrong, and no numeric test would catch it.

T45 = 45 / 365.0

SIDE_ENTRY_POINTS = {
    "bs_delta":              lambda s: se.bs_delta(SPOT, 97.0, 0.30, T45, s),
    "prob_itm":              lambda s: se.prob_itm(SPOT, 97.0, 0.30, T45, s),
    "breakeven":             lambda s: se.breakeven(97.0, 1.50, s),
    "pop_at_breakeven":      lambda s: se.pop_at_breakeven(SPOT, 97.0, 1.50, 0.30, T45, s),
    "credit_to_width_bound": lambda s: se.credit_to_width_bound(SPOT, 97.0, 0.30, T45, s),
    "theoretical_credit":    lambda s: se.theoretical_credit(SPOT, 97.0, 92.0, T45, s, 0.30),
    "solve_width_for_target_cw":
        lambda s: se.solve_width_for_target_cw(SPOT, 97.0, T45, s, 0.30, 0.25, 25.0),
    "expected_value":        lambda s: se.expected_value(SPOT, 97.0, 92.0, 1.50, 0.30, T45, s),
    "skew_cost":             lambda s: se.skew_cost(chain(), SPOT, 94.0, 88.0, T, s),
    "chain_row":             lambda s: se.chain_row(chain(), s, 94.0),
    "strikes_for":           lambda s: se.strikes_for(chain(), s),
    "iv_at_strike":          lambda s: se.iv_at_strike(chain(), s, 94.0),
    "row_abs_delta":         lambda s: se.row_abs_delta(
        {"strike": 94.0, "iv": 0.35}, SPOT, T, s),
    "strike_by_abs_delta":   lambda s: se.strike_by_abs_delta(chain(), s, SPOT, T, 0.25),
}


@pytest.mark.parametrize("name", sorted(SIDE_ENTRY_POINTS))
@pytest.mark.parametrize("bad", ["bull_put", "bear_call", "iron_condor", "puts",
                                 "PUT", "", None, 0, "both"])
def test_every_side_entry_point_rejects_a_bad_side(name, bad):
    with pytest.raises(ValueError):
        SIDE_ENTRY_POINTS[name](bad)


@pytest.mark.parametrize("name", sorted(SIDE_ENTRY_POINTS))
@pytest.mark.parametrize("good", ["put", "call"])
def test_every_side_entry_point_accepts_put_and_call(name, good):
    SIDE_ENTRY_POINTS[name](good)          # must not raise


def test_bull_put_is_the_realistic_mistake_and_the_error_says_so():
    """`bull_put` is a STRUCTURE name; the math layer takes `put`. The error has
    to name the confusion, because the two vocabularies live one function call
    apart."""
    with pytest.raises(ValueError) as exc:
        se.credit_to_width_bound(SPOT, 97.0, 0.30, T45, "bull_put")
    msg = str(exc.value)
    assert "bull_put" in msg and "STRUCTURE" in msg and "STRUCTURE_SIDE" in msg

    with pytest.raises(ValueError) as exc:
        se.expected_value(SPOT, 97.0, 92.0, 1.50, 0.30, T45, "iron_condor")
    assert "no single side" in str(exc.value)


def test_the_wrong_side_used_to_return_the_complement_silently():
    """The regression this guard exists for: the fall-through answer was the
    complement of the right one — 0.593 against a correct 0.407 — which is
    exactly the shape of a number nobody double-checks."""
    correct = se.credit_to_width_bound(SPOT, 97.0, 0.30, T45, "put")
    complement = se.credit_to_width_bound(SPOT, 97.0, 0.30, T45, "call")
    assert correct == pytest.approx(0.4065, abs=5e-4)
    assert complement == pytest.approx(1.0 - correct, abs=1e-9)
    with pytest.raises(ValueError):
        se.credit_to_width_bound(SPOT, 97.0, 0.30, T45, "bull_put")


def test_structure_side_is_the_one_deliberate_bridge():
    assert se.STRUCTURE_SIDE == {"bull_put": "put", "bear_call": "call"}
    # Mapping through it is what a caller is supposed to do, and it works.
    assert se.credit_to_width_bound(
        SPOT, 97.0, 0.30, T45, se.STRUCTURE_SIDE["bull_put"]) == pytest.approx(0.4065, abs=5e-4)
    assert "iron_condor" not in se.STRUCTURE_SIDE       # no single side to map to


def test_build_candidate_rejects_a_non_vertical_structure():
    from app import simmer_config
    cfg = simmer_config.resolved("NVDA")
    inp = clean_inputs()
    ctx = {"symbol": "NVDA", "spot": SPOT, "dte": DTE_DAYS, "t": T, "r": 0.0, "q": 0.0,
           "chain": inp["chain"], "research": inp["research"],
           "rule": cfg["ticker"], "settings": {}}
    ctx["metrics"] = se.compute_metrics(ctx, cfg)
    assert se.build_candidate("bull_put", ctx, cfg) is not None
    with pytest.raises(ValueError):
        se.build_candidate("iron_condor", ctx, cfg)     # combine two verticals instead
    with pytest.raises(ValueError):
        se.build_candidate("put", ctx, cfg)             # the OTHER direction of the mix-up


def test_iron_condor_side_is_accepted_only_where_it_is_meaningful():
    """`both` is legitimate for a condor's component scoring — and nowhere in
    the pricing layer."""
    assert se._require_side("both", allow_both=True) == "both"
    with pytest.raises(ValueError):
        se._require_side("both")
    out = se.evaluate_readiness(clean_inputs())
    assert out["veto_reasons"] == []                    # the condor path still scores


# ═══════════════════════════════════════════════════════════════════════════
# Probability of touch — 2·N(−d2), never 2·delta
# ═══════════════════════════════════════════════════════════════════════════
# Expected values from the exact GBM first-passage form. `POT/N(−d2)` stays in
# 1.81–1.99 while `POT/|delta|` runs 2.05–2.90 — i.e. the folk rule understates
# touch risk by 5–45%, worst exactly where it matters.

POT_CASES = [
    # (strike, iv, dte, pot, pot/N(−d2), pot/|delta|)
    (94.0, 0.35, 30, 0.554094, 1.9405, 2.1949),
    (90.0, 0.50, 45, 0.577261, 1.8976, 2.3489),
    (97.0, 0.20, 14, 0.443454, 1.9778, 2.0852),
    (85.0, 0.60, 60, 0.545147, 1.8645, 2.5373),
]


@pytest.mark.parametrize("strike,iv,dte,pot,ratio_n2,ratio_delta", POT_CASES)
def test_probability_of_touch_fixtures(strike, iv, dte, pot, ratio_n2, ratio_delta):
    t = dte / 365.0
    got = se.prob_touch(SPOT, strike, iv, t)
    assert got == pytest.approx(pot, abs=1e-6)

    n2 = se.prob_itm(SPOT, strike, iv, t, "put")
    delta = abs(se.bs_delta(SPOT, strike, iv, t, "put"))
    assert got / n2 == pytest.approx(ratio_n2, abs=1e-3)
    assert got / delta == pytest.approx(ratio_delta, abs=1e-3)

    # The whole point: the two rules do NOT agree, and 2×delta is the low one.
    assert 2.0 * delta < got
    assert 1.81 <= got / n2 <= 1.99            # 2·N(−d2) is the defensible one
    assert got / delta > 2.05                  # 2·delta always understates


def test_pot_understatement_worst_at_high_iv_and_long_dte():
    """The 2×delta error grows with σ√T — so the folk rule fails hardest on
    exactly the high-IV, longer-dated names a premium seller is drawn to."""
    short = se.prob_touch(SPOT, 97.0, 0.20, 14 / 365.0) / \
        (2.0 * abs(se.bs_delta(SPOT, 97.0, 0.20, 14 / 365.0, "put")))
    long = se.prob_touch(SPOT, 85.0, 0.60, 60 / 365.0) / \
        (2.0 * abs(se.bs_delta(SPOT, 85.0, 0.60, 60 / 365.0, "put")))
    assert long > short > 1.0


def test_prob_itm_always_exceeds_delta():
    """N(−d2) > N(−d1) always, and the gap scales with σ√T."""
    for iv, dte in ((0.20, 14), (0.35, 30), (0.60, 60)):
        t = dte / 365.0
        n2 = se.prob_itm(SPOT, 94.0, iv, t, "put")
        delta = abs(se.bs_delta(SPOT, 94.0, iv, t, "put"))
        assert n2 > delta


# ═══════════════════════════════════════════════════════════════════════════
# Credit-to-width is a DEPENDENT variable, bounded by N(−d2)
# ═══════════════════════════════════════════════════════════════════════════
# This table is reproduced verbatim from docs/simmer.md (σ = 0.30, 45 DTE).

CW_CASES = [
    # (k_short, |delta|, N(−d2), C/W @ W=1, W=5, W=10)
    (97.0, 0.366, 0.4065, 0.3876, 0.3155, 0.2383),
    (95.0, 0.295, 0.3320, 0.3141, 0.2479, 0.1813),
    (90.0, 0.146, 0.1717, 0.1586, 0.1148, 0.0774),
]


@pytest.mark.parametrize("k_short,delta,bound,cw1,cw5,cw10", CW_CASES)
def test_credit_to_width_bounded_by_prob_itm(k_short, delta, bound, cw1, cw5, cw10):
    t, sigma = 45 / 365.0, 0.30
    assert abs(se.bs_delta(SPOT, k_short, sigma, t, "put")) == pytest.approx(delta, abs=5e-4)
    got_bound = se.credit_to_width_bound(SPOT, k_short, sigma, t, "put")
    assert got_bound == pytest.approx(bound, abs=5e-4)
    for width, expected in ((1, cw1), (5, cw5), (10, cw10)):
        cw = se.theoretical_credit(SPOT, k_short, k_short - width, t, "put", sigma) / width
        assert cw == pytest.approx(expected, abs=5e-4)
        # Strictly below the bound, at every width.
        assert cw < got_bound


def test_credit_to_width_approaches_the_bound_as_width_shrinks():
    t, sigma, k = 45 / 365.0, 0.30, 95.0
    bound = se.credit_to_width_bound(SPOT, k, sigma, t, "put")
    prev = 0.0
    for width in (5.0, 1.0, 0.1, 0.01):
        cw = se.theoretical_credit(SPOT, k, k - width, t, "put", sigma) / width
        assert cw < bound
        assert cw > prev                       # monotonically rising as W → 0
        prev = cw
    assert prev == pytest.approx(bound, abs=1e-3)


def test_one_third_width_at_sixteen_delta_is_impossible():
    """"Collect ⅓ of the width" and "sell the 16-delta strike" are
    mathematically incompatible: a 16Δ short caps out at C/W ≈ 0.19 at 30% IV,
    at ANY width — so the solver must report infeasible rather than inventing
    a width."""
    t, sigma = 45 / 365.0, 0.30
    k16 = 90.0                                  # ~0.146 delta at these inputs
    bound = se.credit_to_width_bound(SPOT, k16, sigma, t, "put")
    assert bound < 0.20
    assert se.solve_width_for_target_cw(SPOT, k16, t, "put", sigma,
                                        1.0 / 3.0, 25.0) is None
    # At a 0.30+ delta short it IS feasible, and the solved width is narrow.
    k30 = 97.0
    w = se.solve_width_for_target_cw(SPOT, k30, t, "put", sigma, 1.0 / 3.0, 25.0)
    assert w is not None and 0 < w < 5.0


def test_solve_width_hits_the_requested_credit_to_width():
    t, sigma, k = 30 / 365.0, 0.35, 94.0
    w = se.solve_width_for_target_cw(SPOT, k, t, "put", sigma, 0.20, 25.0)
    assert w is not None
    cw = se.theoretical_credit(SPOT, k, k - w, t, "put", sigma) / w
    assert cw == pytest.approx(0.20, abs=1e-3)


# ═══════════════════════════════════════════════════════════════════════════
# Realized volatility — Yang-Zhang, and why not Parkinson
# ═══════════════════════════════════════════════════════════════════════════
def test_yang_zhang_matches_checked_in_fixture():
    assert se.yang_zhang_rv(ohlc_rows()) == pytest.approx(EXPECTED_YANG_ZHANG, abs=1e-8)


def test_close_to_close_and_parkinson_match_checked_in_fixtures():
    assert se.close_to_close_rv(ohlc_rows()) == pytest.approx(EXPECTED_CLOSE_TO_CLOSE, abs=1e-8)
    assert se.parkinson_rv(ohlc_rows()) == pytest.approx(EXPECTED_PARKINSON, abs=1e-8)


def test_yang_zhang_k_constant():
    """k = 0.34 / (1.34 + (n+1)/(n−1)) with n = 10 usable periods from 11 bars."""
    n = len(ohlc_rows()) - 1
    assert 0.34 / (1.34 + (n + 1) / (n - 1)) == pytest.approx(EXPECTED_YZ_K, abs=1e-8)


def test_yang_zhang_needs_at_least_three_periods():
    assert se.yang_zhang_rv(ohlc_rows()[:3]) is None    # 3 bars = 2 periods
    assert se.yang_zhang_rv(ohlc_rows()[:4]) is not None


def test_yang_zhang_unbiased_where_parkinson_is_23pct_low():
    """Against a series with a KNOWN true vol of 0.300 and 35% of variance
    arriving overnight, Yang-Zhang and close-to-close land on the truth while
    Parkinson misses the gaps entirely and reads ~23% low."""
    bars = synthetic_ohlc()
    yz = se.yang_zhang_rv(bars)
    cc = se.close_to_close_rv(bars)
    pk = se.parkinson_rv(bars)
    assert abs(yz / 0.30 - 1.0) < 0.06, f"Yang-Zhang should be unbiased, got {yz}"
    assert abs(cc / 0.30 - 1.0) < 0.10, f"close-to-close should be unbiased, got {cc}"
    assert pk / 0.30 - 1.0 < -0.15, f"Parkinson should be biased LOW, got {pk}"


def test_parkinson_manufactures_a_false_vrp_positive():
    """The consequence that makes the estimator choice load-bearing: on a name
    whose true IV/RV is ~1.10, Parkinson reports ~1.43 and sails through a
    1.15 VRP gate. Yang-Zhang correctly refuses it."""
    bars = synthetic_ohlc()
    iv = 1.10 * 0.30
    assert se.vrp_ratio(iv, se.yang_zhang_rv(bars)) < 1.15      # correctly vetoed
    assert se.vrp_ratio(iv, se.parkinson_rv(bars)) > 1.20       # false positive


def test_rv_agreement_cross_check():
    assert se.rv_agreement(0.244, 0.239, 0.35) is True
    assert se.rv_agreement(0.244, 0.150, 0.35) is False         # overnight artifact
    assert se.rv_agreement(0.244, None, 0.35) is False


# ═══════════════════════════════════════════════════════════════════════════
# IV rank vs IV percentile
# ═══════════════════════════════════════════════════════════════════════════
def test_iv_rank_formula():
    assert se.iv_rank(0.30, 0.20, 0.50) == pytest.approx(33.333333, abs=1e-4)
    assert se.iv_rank(0.20, 0.20, 0.50) == 0.0
    assert se.iv_rank(0.50, 0.20, 0.50) == 100.0
    assert se.iv_rank(0.30, 0.30, 0.30) is None                 # degenerate window


def test_iv_percentile_is_share_of_days_below_today():
    hist = [0.10, 0.20, 0.30, 0.40, 0.50]
    assert se.iv_percentile(0.35, hist) == pytest.approx(60.0)
    assert se.iv_percentile(0.05, hist) == 0.0
    assert se.iv_percentile(0.99, hist) == 100.0


def test_a_single_spike_pins_iv_rank_but_not_percentile():
    """The documented post-COVID SPY pathology, in one fixture: 200 days at
    0.20, one spike to 0.90, 51 days at 0.30. Today's 0.35 is higher than 99.6%
    of the year — but IV RANK reads 21, because the spike owns the denominator
    for a full year. Gate on percentile; display rank."""
    hist = [0.20] * 200 + [0.90] + [0.30] * 51
    ivr = se.iv_rank(0.35, min(hist), max(hist))
    ivp = se.iv_percentile(0.35, hist)
    assert ivr == pytest.approx(21.4286, abs=1e-3)
    assert ivp == pytest.approx(99.6032, abs=1e-3)
    assert ivr < 25.0 < 40.0 < ivp             # opposite verdicts from the same day


def test_interpolate_in_total_variance_not_in_iv():
    """0.350 @ 23d with 0.280 @ 37d → 0.3087 variance-linear, not 0.3150
    IV-linear. A 0.63 vol-point bias, applied daily."""
    got = se.interp_total_variance(0.350, 23, 0.280, 37, 30)
    assert got == pytest.approx(0.308715, abs=1e-5)
    naive = 0.350 + (30 - 23) / (37 - 23) * (0.280 - 0.350)
    assert naive == pytest.approx(0.3150, abs=1e-4)
    assert got < naive


def test_cross_sectional_percentile_needs_no_history():
    peers = [0.95, 1.05, 1.10, 1.20, 1.30]
    assert se.cross_sectional_percentile(1.25, peers) == pytest.approx(80.0)
    assert se.cross_sectional_percentile(1.25, []) is None


# ═══════════════════════════════════════════════════════════════════════════
# Expected move
# ═══════════════════════════════════════════════════════════════════════════
def test_one_sd_is_1_2533_times_the_straddle():
    """`straddle/S` is the mean absolute deviation, not 1 SD. E|X| = σ√(2/π),
    so 1 SD = 1.2533 × straddle — the raw straddle sits ~20% below."""
    assert se.MAD_TO_SIGMA == pytest.approx(1.2533141, abs=1e-6)
    em = se.expected_move(100.0, 0.33, 30, straddle_mid=7.55)
    assert em["em_straddle_raw_mad"] == 7.55
    assert em["em_straddle"] == pytest.approx(9.4625, abs=1e-3)
    assert em["em_straddle"] / em["em_straddle_raw_mad"] == pytest.approx(1.2533, abs=1e-4)


def test_expected_move_takes_the_wider_of_the_two_methods():
    wide_straddle = se.expected_move(100.0, 0.20, 30, straddle_mid=9.00)
    assert wide_straddle["method"] == "straddle"
    assert wide_straddle["em_1sd"] == wide_straddle["em_straddle"]

    wide_sigma = se.expected_move(100.0, 0.60, 30, straddle_mid=4.00)
    assert wide_sigma["method"] == "sigma"
    assert wide_sigma["em_1sd"] == wide_sigma["em_sigma"]

    assert se.expected_move(100.0, 0.33, 30)["em_1sd"] == pytest.approx(9.4608, abs=1e-3)


def test_expected_move_2sd_and_missing_inputs():
    em = se.expected_move(100.0, 0.33, 30, straddle_mid=7.55)
    assert em["em_2sd"] == pytest.approx(2.0 * em["em_1sd"])
    assert se.expected_move(100.0, None, 30)["em_1sd"] is None


# ═══════════════════════════════════════════════════════════════════════════
# POP at the breakeven, not the short strike
# ═══════════════════════════════════════════════════════════════════════════
def test_pop_is_measured_at_the_breakeven():
    t, sigma = 30 / 365.0, 0.35
    k_s, credit = 94.0, 1.05
    assert se.breakeven(k_s, credit, "put") == pytest.approx(92.95)
    assert se.breakeven(k_s, credit, "call") == pytest.approx(95.05)

    pop_be = se.pop_at_breakeven(SPOT, k_s, credit, sigma, t, "put")
    pop_short = 1.0 - se.prob_itm(SPOT, k_s, sigma, t, "put")
    # The credit is real cushion: measuring at the short strike understates POP.
    assert pop_be > pop_short
    assert pop_be == pytest.approx(0.751248, abs=1e-5)


def test_pop_market_vs_forecast_gap_is_the_edge():
    """Two POP numbers: market-implied and forecast. If they're equal there is
    no trade."""
    t = 30 / 365.0
    k_s, credit = 94.0, 1.05
    pop_mkt = se.pop_at_breakeven(SPOT, k_s, credit, 0.35, t, "put")
    pop_fc = se.pop_at_breakeven(SPOT, k_s, credit, 0.26, t, "put")
    assert pop_fc > pop_mkt
    same = se.pop_at_breakeven(SPOT, k_s, credit, 0.35, t, "put")
    assert same == pop_mkt


# ═══════════════════════════════════════════════════════════════════════════
# Skew — named for its convention, and it HURTS a put credit spread
# ═══════════════════════════════════════════════════════════════════════════
def test_rr25_uses_the_equity_convention():
    """`rr25_put_minus_call` is positive on an equity surface with put bias.
    FX uses the opposite sign — hence the field name."""
    rr = se.rr25_put_minus_call(chain(), SPOT, T)
    assert rr is not None and rr > 0


def test_steep_put_skew_costs_a_put_credit_spread():
    """The bought (further-OTM) leg carries the higher IV, so repricing the same
    structure at flat ATM vol yields MORE credit — the difference is what the
    skew costs on this specific structure."""
    out = se.skew_cost(chain(), SPOT, 94.0, 88.0, T, "put")
    assert out["cost"] < 0
    assert out["cost_pct"] < 0
    assert out["credit_skewed"] < out["credit_flat_atm"]
    # The model credit should track the real quoted mid (1.61 − 0.56 = 1.05).
    assert out["credit_skewed"] == pytest.approx(1.05, abs=0.03)


def test_downward_call_skew_pays_a_call_credit_spread():
    """Mirror case: when the surface slopes DOWN in strike, the leg you buy is
    cheaper than ATM and skew works in your favour."""
    out = se.skew_cost(chain(), SPOT, 106.0, 112.0, T, "call")
    assert out["cost"] > 0
    assert out["credit_skewed"] > out["credit_flat_atm"]


# ═══════════════════════════════════════════════════════════════════════════
# Chain helpers
# ═══════════════════════════════════════════════════════════════════════════
def test_atm_helpers():
    c = chain()
    assert se.atm_strike(c, SPOT) == 100.0
    assert se.atm_iv(c, SPOT) == pytest.approx(0.33)
    assert se.atm_straddle_mid(c, SPOT) == pytest.approx(7.55, abs=1e-6)


def test_iv_at_strike_interpolates_between_listed_strikes():
    c = chain()
    assert se.iv_at_strike(c, "put", 94.0) == pytest.approx(0.3510)
    mid = se.iv_at_strike(c, "put", 93.0)
    assert 0.3510 < mid < 0.3580               # between the 94 and 92 quotes
    assert se.iv_at_strike(c, "put", 50.0) == pytest.approx(0.3930)   # flat wing


def test_strike_by_abs_delta():
    c = chain()
    assert se.strike_by_abs_delta(c, "put", SPOT, T, 0.25) == 94.0
    assert se.strike_by_abs_delta(c, "call", SPOT, T, 0.28) == 106.0


# ═══════════════════════════════════════════════════════════════════════════
# Composite
# ═══════════════════════════════════════════════════════════════════════════
def test_clean_fixture_produces_a_ready_bull_put():
    out = se.evaluate_readiness(clean_inputs())
    assert out["veto_reasons"] == []
    assert out["decision"] == "ready"
    assert out["structure"] == "bull_put"
    assert out["strikes"] == {"short": 94.0, "long": 90.0, "width": 4.0}
    assert out["credit_mid"] == pytest.approx(0.79, abs=1e-6)
    assert out["max_loss"] == pytest.approx(3.21, abs=1e-6)
    assert out["credit_fill"] <= out["credit_mid"]          # never assumes a mid fill
    assert out["pop_breakeven"] == pytest.approx(0.7397, abs=1e-3)
    assert out["expected_value"] == pytest.approx(0.2813, abs=1e-3)
    assert out["alpha"] == pytest.approx(out["expected_value"] / out["max_loss"], abs=1e-9)
    assert out["score"] == pytest.approx(74.58, abs=0.05)
    assert out["regime"]["state"] == "contango"
    assert 0.0 < out["confidence"] <= 1.0


def test_component_scores_are_bounded_and_weighted():
    out = se.evaluate_readiness(clean_inputs())
    comps = out["components"]
    assert set(comps) == {"structural_safety", "expected_move_headroom",
                          "volatility_richness", "credit_quality",
                          "liquidity_quality", "sentiment_lean"}
    for name, c in comps.items():
        assert 0.0 <= c["score"] <= 1.0, name
    from app import simmer_config
    weights = simmer_config.weights()
    raw = sum(weights[k] * comps[k]["score"] for k in comps)
    assert out["score"] == pytest.approx(100.0 * raw * out["regime"]["multiplier"], abs=0.02)


def test_envelope_shape():
    out = se.evaluate_readiness(clean_inputs())
    for key in ("decision", "score", "confidence", "regime", "structure", "strikes",
                "credit_mid", "credit_fill", "max_loss", "pop_breakeven",
                "expected_value", "alpha", "components", "veto_reasons",
                "avoid_if", "data_quality"):
        assert key in out, key
    assert out["management"] == {"profit_target_pct": 50.0, "manage_dte": 21,
                                 "stop_credit_multiple": 2.0}


def test_engine_reports_the_infeasible_credit_to_width_target_rather_than_hiding_it():
    out = se.evaluate_readiness(clean_inputs())
    assert "credit_to_width_target_infeasible_at_this_delta" in out["avoid_if"]


def test_short_strike_respects_the_delta_band_even_when_the_wall_is_further():
    """The EM/wall placement rule is honoured only WITHIN the delta band — the
    1-SD point sits at ~16Δ, which the hard gate vetoes."""
    inp = clean_inputs()
    inp["research"]["walls"]["put_wall"] = 84.0        # far below the band
    inp["settings"] = {"structures_enabled": ["bull_put"]}
    out = se.evaluate_readiness(inp)
    assert out["veto_reasons"] == []
    row = next(r for r in chain() if r["side"] == "put"
               and r["strike"] == out["strikes"]["short"])
    assert 0.20 <= abs(row["delta"]) <= 0.35


def test_sentiment_never_promotes():
    """Positive sentiment must not raise the score above the neutral case;
    adverse sentiment lowers it."""
    neutral = se.evaluate_readiness(clean_inputs())
    inp_pos = clean_inputs()
    # horizon_days is REQUIRED to express intent now: sentiment with no horizon
    # is treated as undated and gated out of a short tenor entirely. 0 = lands
    # today, so the directional logic is what is under test here.
    inp_pos["research"]["sentiment"] = {"score": 0.95, "velocity_p": 0.4,
                                        "horizon_days": 0}
    positive = se.evaluate_readiness(inp_pos)
    assert positive["score"] <= neutral["score"] + 1e-9
    assert positive["components"]["sentiment_lean"]["score"] == pytest.approx(1.0)

    inp_neg = clean_inputs()
    inp_neg["research"]["sentiment"] = {"score": -0.20, "velocity_p": 0.4,
                                        "horizon_days": 0}
    negative = se.evaluate_readiness(inp_neg)
    assert negative["components"]["sentiment_lean"]["score"] < 1.0
    assert negative["score"] < neutral["score"]


def test_velocity_burst_suppresses_regardless_of_direction():
    inp = clean_inputs()
    inp["research"]["sentiment"] = {"score": 0.30, "velocity_p": 0.0005}   # < ALERT_P
    out = se.evaluate_readiness(inp)
    assert out["components"]["sentiment_lean"]["velocity_burst"] is True
    assert out["score"] < se.evaluate_readiness(clean_inputs())["score"]


def test_sentiment_persistence_asymmetry():
    """Negative sentiment still bites at 25 trading days; positive is gone."""
    cs = {"halflife_pos_days": 5, "halflife_neg_days": 35, "index_halflife_days": 1,
          "day0_discount": 0.25, "earnings_gates_neg_halflife": True}
    pos = se.sentiment_persistence(0.5, 25, cs)
    neg = se.sentiment_persistence(-0.5, 25, cs)
    assert neg > 0.5 > pos
    assert pos < 0.05

    # Gated on earnings: if the spread expires before the next print, the long
    # half-life is unrealizable and we fall back to the short one.
    ungated = se.sentiment_persistence(-0.5, 25, cs, earnings_inside_tenor=False)
    assert ungated == pytest.approx(pos)

    # Index underlyings: a ~1-day half-life, not 7 weeks.
    assert se.sentiment_persistence(-0.5, 25, cs, is_index=True) < 1e-6

    # Day 0 is discounted hard — contemporaneous impact, not prediction.
    assert se.sentiment_persistence(-0.5, 0.0, cs) == pytest.approx(0.25)


def test_market_regime_bands():
    from app import simmer_config
    rc = simmer_config.regime()
    assert se.market_regime({"vix_ts": 0.90}, rc)["state"] == "contango"
    assert se.market_regime({"vix_ts": 0.97}, rc)["state"] == "transition"
    assert se.market_regime({"vix_ts": 1.05}, rc)["state"] == "backwardation"
    assert se.market_regime({"vix_ts": 1.20}, rc)["state"] == "stressed"
    assert se.market_regime(None, rc)["state"] == "unknown"

    # Asymmetric action: risk-off suppresses the PUT side; bear_call survives.
    back = se.market_regime({"vix_ts": 1.05}, rc)
    assert "bull_put" in back["suppress"] and "bear_call" not in back["suppress"]
    assert back["size_factor"] < 1.0

    # Gamma flip and sector dislocation are multiplicative penalties, not vetoes.
    flip = se.market_regime({"vix_ts": 0.90, "spot_below_gamma_flip": True}, rc)
    assert flip["multiplier"] < 1.0 and "index_below_gamma_flip" in flip["notes"]
    disloc = se.market_regime({"vix_ts": 0.90, "dispersion": 0.80}, rc)
    assert disloc["sector_dislocation"] is True


def test_data_quality_degrades_with_missing_history():
    full = se.evaluate_readiness(clean_inputs())
    thin = clean_inputs()
    thin["research"]["iv_history_days"] = 40      # under min_history_days
    thin["research"]["peer_vrp_ratios"] = [1.0, 1.1, 1.2, 1.25, 1.4]
    out = se.evaluate_readiness(thin)
    assert out["data_quality"]["iv_history"] < full["data_quality"]["iv_history"]
    assert out["confidence"] < full["confidence"]
    assert "iv_rank_provisional_short_history" in out["avoid_if"]


# ═══════════════════════════════════════════════════════════════════════════
# Additive signals: max pain, per-ticker term structure, earnings-VRP split
# ═══════════════════════════════════════════════════════════════════════════
def _oi_book(rows):
    """rows: (side, strike, open_interest) → chain dicts the OI helpers read."""
    return [{"side": s, "strike": float(k), "open_interest": oi} for s, k, oi in rows]


def test_max_pain_argmin_over_a_hand_built_oi_book():
    # puts heavy at/below 100, calls heavy at 100 → total holder payoff is
    # minimized at K=100 (worked out by hand: pain 6000/1000/3000/7000).
    book = _oi_book([
        ("put", 90, 100), ("put", 100, 400), ("put", 110, 100),
        ("call", 100, 300), ("call", 110, 100), ("call", 120, 100),
    ])
    assert se.max_pain(book, spot=105.0) == 100.0


def test_max_pain_none_without_open_interest():
    assert se.max_pain(_oi_book([("put", 100, 0), ("call", 100, 0)])) is None
    assert se.max_pain([]) is None


def test_term_structure_contango_vs_backwardation_sign():
    contango = se.term_structure(iv_near=0.30, iv_far=0.33)   # far richer
    assert contango["term_slope"] > 1.0                       # good for sellers
    assert contango["term_slope_pp"] < 0                      # iv_near − iv_far
    backward = se.term_structure(iv_near=0.40, iv_far=0.30)   # front richer
    assert backward["term_slope"] < 1.0
    assert backward["term_slope_pp"] > 0
    # Degenerate legs → None, never a fabricated slope.
    assert se.term_structure(None, 0.30)["term_slope"] is None
    assert se.term_structure(0.30, 0.0)["term_slope"] is None


def test_earnings_vrp_decomposition_event_in_near():
    d = se.earnings_vrp(iv_near=0.45, t_near=30 / 365, iv_far=0.30,
                        t_far=90 / 365, rv=0.32, event_in_near=True)
    # Event only in the near tenor → ex-earnings near IV collapses to the far
    # (baseline) IV by the variance-removal identity.
    assert d["iv_near_exearn"] == pytest.approx(0.30, abs=1e-9)
    assert d["vrp_earnings_pp"] == pytest.approx((0.45 - 0.30) * 100.0)
    assert d["vrp_ex_earnings_pp"] == pytest.approx((0.30 - 0.32) * 100.0)
    assert d["vrp_total_pp"] == pytest.approx((0.45 - 0.32) * 100.0)
    assert d["vrp_earnings_pp"] > 0 and d["vrp_ex_earnings_pp"] < 0


def test_earnings_vrp_no_event_is_entirely_ex_earnings():
    d = se.earnings_vrp(0.30, 30 / 365, 0.28, 90 / 365, 0.25, event_in_near=False)
    assert d["vrp_earnings_pp"] == 0.0
    assert d["vrp_ex_earnings_pp"] == pytest.approx(d["vrp_total_pp"])
    assert d["vrp_total_pp"] == pytest.approx((0.30 - 0.25) * 100.0)


def test_earnings_vrp_avoid_if_fires_when_premium_is_all_earnings():
    from .conftest import mutate
    # Front IV inflated by an event; the ex-earnings floor (≈ iv_far 0.22) sits
    # BELOW realized (0.244) → positive total VRP, negative ex-earnings VRP.
    out = se.evaluate_readiness(mutate(iv_far=0.22, t_far=0.25,
                                       earnings_vrp_event=True))
    assert out["veto_reasons"] == []                       # soft, not a hard veto
    assert "premium_entirely_earnings_driven" in out["avoid_if"]
    m = out["metrics"]
    assert m["vrp_total_pp"] > 0 and m["vrp_ex_earnings_pp"] < 0
    assert m["vrp_earnings_pp"] > 0


def test_earnings_vrp_avoid_if_absent_without_event():
    out = se.evaluate_readiness(clean_inputs())
    assert "premium_entirely_earnings_driven" not in out["avoid_if"]
    m = out["metrics"]
    assert m["vrp_earnings_pp"] == 0.0
    assert m["vrp_ex_earnings_pp"] == pytest.approx(m["vrp_total_pp"])
    assert m["max_pain"] is not None                       # rides the envelope


def test_undated_sentiment_cannot_move_a_short_dated_trade():
    """A structural worry with no date must not touch a trade expiring first.

    "Margin compression eventually", "a rival ships next year" — real, bearish,
    and irrelevant to a contract that expires in a day or two. Before the horizon
    gate these were penalised identically to a same-day event, so the engine
    reacted to news it could not possibly be exposed to.
    """
    base = clean_inputs()
    dte = float(base.get("dte") or 1)

    far = clean_inputs()
    # Severity kept ABOVE simmer_config negative_veto (-0.35): a stronger score
    # blocks the bull put outright and the engine switches to a bear call, for
    # which bearish news is not adverse — that would test structure selection,
    # not the horizon gate.
    far["research"]["sentiment"] = {"score": -0.20, "velocity_p": 0.4,
                                    "horizon_days": dte + 30}
    near = clean_inputs()
    near["research"]["sentiment"] = {"score": -0.20, "velocity_p": 0.4,
                                     "horizon_days": 0}

    far_out = se.evaluate_readiness(far)
    near_out = se.evaluate_readiness(near)
    assert far_out["components"]["sentiment_lean"]["score"] == pytest.approx(1.0)
    assert far_out["components"]["sentiment_lean"]["horizon_gated"] is True
    # Same headline, same severity, but dated inside the tenor -> it bites.
    assert near_out["components"]["sentiment_lean"]["score"] < 1.0
    assert near_out["score"] < far_out["score"]


def test_velocity_burst_survives_the_horizon_gate():
    """A chatter spike is unpriced uncertainty NOW, not a dated fundamental.

    Regression: gating the directional penalty with an early return also skipped
    velocity suppression, so an undated story silently disabled burst detection.
    """
    inp = clean_inputs()
    inp["research"]["sentiment"] = {"score": -0.60, "velocity_p": 0.001,
                                    "horizon_days": 999}
    comp = se.evaluate_readiness(inp)["components"]["sentiment_lean"]
    assert comp["horizon_gated"] is True      # directional penalty gated out
    assert comp["velocity_burst"] is True     # burst still suppresses
    assert comp["score"] < 1.0

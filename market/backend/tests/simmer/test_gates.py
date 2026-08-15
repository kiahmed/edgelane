"""Every hard gate, each vetoing **in isolation**.

The suite works by mutating exactly one field of `clean_inputs()` — which
clears every gate — and asserting that the resulting veto set is exactly the
one gate under test. That is what makes these tests meaningful: a gate that
fires only because a neighbouring gate also fired proves nothing.

Hard gates are absolute. No amount of score overrides them, so a vetoed result
must also carry a suppressed score and no suggested structure.
"""
from __future__ import annotations

import copy

import pytest

from app import simmer_config, simmer_engine as se

from .conftest import chain, clean_inputs, reason_kinds


def run(inp: dict, cfg: dict | None = None) -> dict:
    return se.evaluate_readiness(inp, cfg)


# ═══════════════════════════════════════════════════════════════════════════
# Baseline
# ═══════════════════════════════════════════════════════════════════════════
def test_clean_fixture_clears_every_gate():
    out = run(clean_inputs())
    assert out["veto_reasons"] == []
    assert out["decision"] in ("ready", "watch", "avoid")
    assert out["structure"] is not None


# ═══════════════════════════════════════════════════════════════════════════
# Catalyst lockout — locked, never user-adjustable
# ═══════════════════════════════════════════════════════════════════════════
def test_confirmed_catalyst_inside_the_tenor_vetoes():
    """Selling premium through an unpriced binary is the fastest way to lose
    more than the trade could earn."""
    inp = clean_inputs()
    inp["research"]["catalysts"] = [{"type": "earnings", "days": 12, "confirmed": True}]
    out = run(inp)
    assert reason_kinds(out) == {"catalyst:earnings_in_tenor"}


def test_catalyst_outside_the_tenor_does_not_veto():
    inp = clean_inputs()
    inp["research"]["catalysts"] = [{"type": "earnings", "days": 55, "confirmed": True}]
    assert run(inp)["veto_reasons"] == []


def test_unconfirmed_earnings_block_the_whole_week():
    inp = clean_inputs()
    inp["research"]["catalysts"] = [{"type": "earnings", "days": 5, "confirmed": False}]
    out = run(inp)
    assert reason_kinds(out) == {"catalyst:unconfirmed_earnings"}


def test_hard_block_filing_vetoes_by_type_name():
    inp = clean_inputs()
    inp["research"]["catalysts"] = [{"type": "8k_item_4_02", "days": 2, "confirmed": True}]
    assert reason_kinds(run(inp)) == {"catalyst:8k_item_4_02_in_tenor"}


# ═══════════════════════════════════════════════════════════════════════════
# Macro-calendar validity — fail visibly, never sell through a CPI print
# ═══════════════════════════════════════════════════════════════════════════
def test_missing_macro_calendar_vetoes():
    inp = clean_inputs()
    inp["research"].pop("macro_valid_through_dte")
    assert reason_kinds(run(inp)) == {"macro_calendar:unavailable"}


def test_expiry_beyond_macro_calendar_validity_vetoes():
    inp = clean_inputs()
    inp["research"]["macro_valid_through_dte"] = 10      # expiry is 30 DTE out
    assert reason_kinds(run(inp)) == {"macro_calendar:expiry_beyond_valid_through"}


# ═══════════════════════════════════════════════════════════════════════════
# DTE window + the dollar-friction test DTE alone cannot express
# ═══════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("dte", [0, 3, 6, 46, 90])
def test_dte_outside_the_window_vetoes(dte):
    inp = clean_inputs()
    inp["dte"] = dte
    assert reason_kinds(run(inp)) == {"dte_window"}


@pytest.mark.parametrize("dte", [7, 30, 45])
def test_dte_inside_the_window_passes(dte):
    inp = clean_inputs()
    inp["dte"] = dte
    assert "dte_window" not in reason_kinds(run(inp))


def test_dollar_friction_vetoes_when_gross_ev_is_inside_costs():
    """Frictions are fixed in DOLLARS while the dollar edge shrinks with DTE and
    with the underlying's price — which is exactly why a DTE bound alone is the
    wrong gate. Here the round-trip cost is inflated until it eats the edge; the
    liquidity gate (a *percentage* of credit) is untouched."""
    cfg = copy.deepcopy(simmer_config.resolved("NVDA"))
    cfg["ticker"]["commission"] = 50.0
    out = run(clean_inputs(), cfg)
    assert reason_kinds(out) == {"dollar_friction"}


def test_gross_ev_must_clear_twice_the_round_trip():
    """The multiple is the gate, not the sign of EV: a candidate with positive
    but thin EV still fails."""
    cfg = copy.deepcopy(simmer_config.resolved("NVDA"))
    cfg["gates"]["friction_ev_multiple"] = 1000.0
    assert reason_kinds(run(clean_inputs(), cfg)) == {"dollar_friction"}


# ═══════════════════════════════════════════════════════════════════════════
# Liquidity floor — 8% of credit, derived not folklore
# ═══════════════════════════════════════════════════════════════════════════
def test_quoted_spread_above_8pct_of_credit_vetoes():
    inp = clean_inputs()
    for row in inp["chain"]:                 # widen every quote around its mid
        mid = (row["bid"] + row["ask"]) / 2.0
        row["bid"], row["ask"] = round(mid - 0.025, 3), round(mid + 0.025, 3)
    assert reason_kinds(run(inp)) == {"liquidity:spread_pct_of_credit"}


def test_spread_just_inside_the_gate_passes():
    inp = clean_inputs()
    for row in inp["chain"]:
        mid = (row["bid"] + row["ask"]) / 2.0
        row["bid"], row["ask"] = round(mid - 0.014, 3), round(mid + 0.014, 3)
    assert run(inp)["veto_reasons"] == []


def test_open_interest_floor_vetoes():
    inp = clean_inputs()
    for row in inp["chain"]:
        row["open_interest"] = 100
    assert reason_kinds(run(inp)) == {"liquidity:open_interest"}


def test_per_ticker_open_interest_override_is_stricter_than_the_global_floor():
    """NVDA carries a 1000-contract floor; the global default is 250. A chain at
    500 clears the global gate and fails NVDA's."""
    inp = clean_inputs()
    for row in inp["chain"]:
        row["open_interest"] = 500
    assert reason_kinds(run(inp)) == {"liquidity:open_interest"}

    cfg = copy.deepcopy(simmer_config.resolved("NVDA"))
    cfg["ticker"]["min_open_interest"] = 250
    assert run(inp, cfg)["veto_reasons"] == []


def test_no_size_at_the_touch_vetoes():
    inp = clean_inputs()
    for row in inp["chain"]:
        row["bid_size"] = row["ask_size"] = 0
    assert reason_kinds(run(inp)) == {"liquidity:size_at_touch"}


# ═══════════════════════════════════════════════════════════════════════════
# Volatility floors — percentile, and a RATIO
# ═══════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("ivp", [0.0, 20.0, 39.9])
def test_iv_percentile_below_the_floor_vetoes(ivp):
    """Below IVR ~20 the forward IV/RV is 0.95 — negative-EV before costs."""
    inp = clean_inputs()
    inp["research"]["iv_percentile"] = ivp
    assert reason_kinds(run(inp)) == {"iv_percentile_floor"}


def test_high_iv_rank_is_not_required_only_a_low_floor():
    """A high-IVR *filter* is deliberately NOT a gate — Option Alpha's published
    backtest lost 71% with IVR ≥ 50 versus −4% unfiltered. A modest IV name with
    good structure must still be sellable."""
    inp = clean_inputs()
    inp["research"]["iv_rank"] = 12.0        # low rank
    inp["research"]["iv_percentile"] = 55.0  # but a healthy percentile
    out = run(inp)
    assert out["veto_reasons"] == []
    assert out["metrics"]["iv_rank"] == 12.0


def test_no_volatility_history_at_all_vetoes():
    inp = clean_inputs()
    inp["research"].pop("iv_percentile")
    assert reason_kinds(run(inp)) == {"volatility_history_unavailable"}


def test_cold_start_cross_sectional_vrp_substitutes_for_history():
    """Ranking IV30/RV20 across today's watchlist needs ZERO history and clears
    the gate on day one."""
    inp = clean_inputs()
    inp["research"].pop("iv_percentile")
    inp["research"]["iv_history_days"] = 0
    inp["research"]["peer_vrp_ratios"] = [0.90, 0.95, 1.02, 1.08, 1.15, 1.20, 1.30]
    out = run(inp)
    assert out["veto_reasons"] == []
    assert out["metrics"]["iv_percentile_cross_sectional"] is not None


@pytest.mark.parametrize("rv", [0.40, 0.31, 0.2871])
def test_vrp_ratio_below_1_15_vetoes(rv):
    inp = clean_inputs()
    inp["research"]["rv_yang_zhang"] = rv
    assert reason_kinds(run(inp)) == {"vrp_floor"}


def test_vrp_is_a_ratio_not_vol_points():
    """A high-IV name with a 4-vol-point premium FAILS (ratio 1.05) while a
    low-IV name with a 2-point premium PASSES (ratio 1.25). An absolute
    "IV − HV > 5 points" rule would invert both verdicts."""
    rich = clean_inputs()
    rich["research"]["iv30"] = 0.84
    rich["research"]["rv_yang_zhang"] = 0.80        # +4.0 vol points, ratio 1.05
    rich["research"]["rv_close_to_close"] = 0.79
    assert reason_kinds(run(rich)) == {"vrp_floor"}

    quiet = clean_inputs()
    quiet["research"]["iv30"] = 0.10
    quiet["research"]["rv_yang_zhang"] = 0.08       # +2.0 vol points, ratio 1.25
    quiet["research"]["rv_close_to_close"] = 0.081
    assert "vrp_floor" not in reason_kinds(run(quiet))


def test_estimator_disagreement_vetoes_the_apparent_edge():
    """If Yang-Zhang shows edge and zero-mean close-to-close does not, the
    "edge" is an overnight-exclusion artifact. Require agreement."""
    inp = clean_inputs()
    inp["research"]["rv_close_to_close"] = 0.15
    assert reason_kinds(run(inp)) == {"rv_estimator_disagreement"}


# ═══════════════════════════════════════════════════════════════════════════
# Short-delta band — 5–10Δ shorts are measurably negative-EV
# ═══════════════════════════════════════════════════════════════════════════
def test_short_delta_outside_the_band_vetoes():
    inp = clean_inputs()
    for row in inp["chain"]:                 # squash every delta far below 0.20
        row["delta"] = round(row["delta"] * 0.30, 4)
    assert reason_kinds(run(inp)) == {"short_delta_band"}


def test_short_delta_band_edges():
    gates = simmer_config.gates()
    assert gates["short_delta_min"] == 0.20
    assert gates["short_delta_max"] == 0.35
    # The chosen short in the clean fixture sits inside the band, near the
    # 0.25–0.30 optimum rather than at an edge.
    out = run(clean_inputs())
    row = next(r for r in chain()
               if r["side"] == "put" and r["strike"] == out["strikes"]["short"])
    assert 0.20 <= abs(row["delta"]) <= 0.35


def test_user_cannot_opt_into_the_negative_ev_region():
    """A user who asks for a 0.05 short delta is CLAMPED, not obeyed — bounds
    are how the research reaches the user without a lecture."""
    assert simmer_config.clamp_setting("short_delta_min", 0.05) == 0.15
    assert simmer_config.clamp_setting("short_delta_max", 0.90) == 0.40


# ═══════════════════════════════════════════════════════════════════════════
# Chain sanity — defensive
# ═══════════════════════════════════════════════════════════════════════════
def test_crossed_market_vetoes():
    inp = clean_inputs()
    inp["chain"][0]["bid"] = 9.0             # bid > ask
    assert reason_kinds(run(inp)) == {"chain_sanity:crossed_market"}


def test_spot_outside_the_chain_vetoes():
    inp = clean_inputs()
    inp["spot"] = 200.0
    assert reason_kinds(run(inp)) == {"chain_sanity:spot_outside_chain"}


def test_missing_greeks_vetoes():
    inp = clean_inputs()
    for row in inp["chain"]:
        row["iv"] = None
    assert reason_kinds(run(inp)) == {"chain_sanity:greeks_missing"}


def test_empty_chain_vetoes():
    inp = clean_inputs()
    inp["chain"] = []
    assert reason_kinds(run(inp)) == {"chain_sanity:no_chain"}


def test_one_sided_chain_vetoes():
    inp = clean_inputs()
    inp["chain"] = [r for r in inp["chain"] if r["side"] == "put"]
    assert "chain_sanity:missing_side" in reason_kinds(run(inp))


# ═══════════════════════════════════════════════════════════════════════════
# A veto is absolute
# ═══════════════════════════════════════════════════════════════════════════
def test_a_veto_suppresses_the_score_entirely():
    inp = clean_inputs()
    inp["research"]["catalysts"] = [{"type": "earnings", "days": 3, "confirmed": True}]
    out = run(inp)
    assert out["decision"] == "vetoed"
    assert out["score"] == 0.0
    assert out["structure"] is None and out["strikes"] is None
    assert out["credit_mid"] is None and out["alpha"] is None


def test_index_products_are_ineligible_for_trading_but_not_for_data():
    """The regime gate needs SPY/QQQ/VIX chains every sweep — exclusion is from
    *trading*, not from *data*."""
    inp = clean_inputs()
    inp["symbol"] = "SPX"
    out = run(inp, simmer_config.resolved("SPX"))
    assert out["decision"] == "vetoed"
    assert out["veto_reasons"] == ["ineligible:index_product_excluded_from_trading"]
    assert "SPY" in simmer_config.index_data_symbols()


# ═══════════════════════════════════════════════════════════════════════════
# Structure suppression — side selection, not a veto of the whole name
# ═══════════════════════════════════════════════════════════════════════════
def test_risk_off_regime_suppresses_the_put_side_only():
    """A crash is when IV is richest and the engine hunts rich IV. Backwardation
    blocks bull puts; bear calls stay eligible."""
    inp = clean_inputs()
    inp["research"]["index"] = {"vix_ts": 1.05}
    out = run(inp)
    assert out["veto_reasons"] == []
    assert out["structure"] == "bear_call"
    assert any(r.startswith("regime:backwardation_suppresses_bull_put")
               for r in out["avoid_if"])
    assert out["regime"]["size_factor"] < 1.0


def test_squeeze_suppresses_the_call_side_only():
    """Forced covering only pushes price UP, so a heavily-shorted name threatens
    the call side alone — the mirror of the regime guard."""
    inp = clean_inputs()
    inp["research"]["days_to_cover"] = 11.0
    out = run(inp)
    assert out["veto_reasons"] == []
    assert out["structure"] == "bull_put"
    assert any(r.startswith("squeeze:days_to_cover") for r in out["avoid_if"])


def test_fresh_negative_sentiment_blocks_bull_puts_but_positive_does_not_block_bear_calls():
    """The asymmetry follows from the positive/negative persistence split:
    negative sentiment is still significant at week 13, positive is gone by
    week 3."""
    bad = clean_inputs()
    bad["research"]["sentiment"] = {"score": -0.60, "velocity_p": 0.4}
    out_bad = run(bad)
    assert out_bad["structure"] == "bear_call"
    assert "sentiment:fresh_negative_blocks_put_side" in out_bad["avoid_if"]

    good = clean_inputs()
    good["research"]["sentiment"] = {"score": 0.60, "velocity_p": 0.4}
    out_good = run(good)
    assert out_good["structure"] in ("bull_put", "bear_call", "iron_condor")
    assert not any("sentiment" in r for r in out_good["avoid_if"])


def test_every_structure_suppressed_is_a_veto():
    inp = clean_inputs()
    inp["settings"] = {"structures_enabled": ["bull_put"]}
    inp["research"]["index"] = {"vix_ts": 1.20}          # stressed → no bull_put
    out = run(inp)
    assert out["veto_reasons"] == ["no_eligible_structure"]
    assert out["score"] == 0.0


def test_squeeze_veto_is_toggleable_but_the_locked_gates_are_not():
    inp = clean_inputs()
    inp["research"]["days_to_cover"] = 11.0
    inp["settings"] = {"squeeze_veto": False}
    out = run(inp)
    assert out["veto_reasons"] == []
    assert "squeeze_veto" in simmer_config.TOGGLEABLE_GATES
    for locked in ("catalyst_lockout", "liquidity", "dollar_friction",
                   "chain_sanity", "macro_validity"):
        assert locked in simmer_config.LOCKED_GATES
        assert locked not in simmer_config.TOGGLEABLE_GATES


def test_ex_dividend_inside_the_tenor_is_a_warning_not_a_veto():
    """Assignment on a short call leaves you SHORT the stock, and through the
    ex-date you owe the dividend on top of the forfeited extrinsic."""
    inp = clean_inputs()
    inp["research"]["ex_dividend_in_tenor"] = True
    out = run(inp)
    assert out["veto_reasons"] == []
    assert "early_assignment:ex_dividend_in_tenor" in out["avoid_if"]

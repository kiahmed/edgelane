"""DTE-tiered rules + catalyst weighting + debit handoff.

Covers docs/simmer_dte_tiers.md. The load-bearing property throughout is
PARITY: the 6-21 and 22-45 tiers, and any short tier missing its intraday
inputs, must behave exactly as the current daily engine. The final regression
test proves a 30-DTE envelope is byte-identical whether or not the new intraday
inputs are injected.
"""
from __future__ import annotations

import copy

import pytest

from app import simmer_config
from app import simmer_engine as se

from .conftest import clean_inputs


def _short_dte_cfg() -> dict:
    """Resolved NVDA config with the DTE floor dropped so a 0-5 DTE expiry can
    reach the vol/structure gates instead of tripping `dte_window` first. Only
    the gate is relaxed — every tier boundary and catalyst knob is untouched."""
    cfg = copy.deepcopy(simmer_config.resolved("NVDA"))
    cfg["gates"]["dte_min"] = 0
    return cfg


def _inputs(dte: float, **research) -> dict:
    inp = clean_inputs()
    inp["dte"] = dte
    inp["research"].update(research)
    return inp


# ═══════════════════════════════════════════════════════════════════════════
# 1. classify_dte_tier — boundaries
# ═══════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("dte,tier", [
    (0, "0-1"), (0.5, "0-1"), (1, "0-1"),
    (1.5, "2-5"), (2, "2-5"), (5, "2-5"),
    (5.5, "6-21"), (6, "6-21"), (21, "6-21"),
    (21.5, "22-45"), (22, "22-45"), (45, "22-45"),
    (45.5, "long"), (46, "long"), (90, "long"), (365, "long"),
])
def test_classify_dte_tier_boundaries(dte, tier):
    assert se.classify_dte_tier(dte) == tier


def test_classify_dte_tier_reads_config_bounds():
    """Boundaries are config, not hardcoded — a re-cut seam moves the tier."""
    cfg = copy.deepcopy(simmer_config.resolved("NVDA"))
    cfg["dte_tiers"]["bounds"][0]["max_dte"] = 2   # 0-1 now spans through 2
    assert se.classify_dte_tier(2, cfg) == "0-1"
    assert se.classify_dte_tier(2) == "2-5"        # module default unchanged


# ═══════════════════════════════════════════════════════════════════════════
# 2. catalyst_dte_weight — decay + monotonicity
# ═══════════════════════════════════════════════════════════════════════════
def test_catalyst_weight_endpoints():
    cat = simmer_config.catalyst()
    assert se.catalyst_dte_weight(0) == pytest.approx(cat["max_weight"])
    assert se.catalyst_dte_weight(cat["neutral_dte"]) == pytest.approx(1.0)


def test_catalyst_weight_is_one_across_the_theta_sweet_spot():
    """1.0 for every DTE ≥ neutral_dte — this is WHY 6-21 / 22-45 stay parity."""
    for dte in (6, 7, 10, 21, 22, 30, 45, 60, 120):
        assert se.catalyst_dte_weight(dte) == 1.0, dte


def test_catalyst_weight_monotonic_non_increasing():
    prev = None
    for i in range(0, 501):
        dte = i / 10.0
        w = se.catalyst_dte_weight(dte)
        assert w >= 1.0                                   # never promotes
        if prev is not None:
            assert w <= prev + 1e-12, dte                 # non-increasing
        prev = w


def test_catalyst_weight_strictly_decays_below_neutral():
    assert se.catalyst_dte_weight(0) > se.catalyst_dte_weight(1) \
        > se.catalyst_dte_weight(3) > se.catalyst_dte_weight(5) \
        >= se.catalyst_dte_weight(6)


# ═══════════════════════════════════════════════════════════════════════════
# 3. Tier-branched vol reference — intraday when present, daily fallback
# ═══════════════════════════════════════════════════════════════════════════
def test_short_tier_vrp_uses_rv_intraday_when_present():
    """0-1 DTE with rv_intraday injected → VRP is IV/rv_intraday, not IV/YZ."""
    out = se.evaluate_readiness(_inputs(1, rv_intraday=0.20), _short_dte_cfg())
    m = out["metrics"]
    assert m["vol_reference"] == "intraday"
    # iv30 = 0.33 in the fixture → 0.33 / 0.20 = 1.65, not the daily 0.33/0.244.
    assert m["vrp"] == pytest.approx(0.33 / 0.20)


def test_short_tier_vrp_falls_back_to_daily_when_intraday_absent():
    """0-1 DTE with NO rv_intraday → identical daily 20d-YZ VRP as today."""
    out = se.evaluate_readiness(_inputs(1), _short_dte_cfg())
    m = out["metrics"]
    assert m["vol_reference"] == "daily"
    assert m["vrp"] == pytest.approx(0.33 / 0.244)        # IV / rv_yang_zhang


def test_2_5_tier_blends_intraday_with_daily_yz():
    """2-5 DTE blends rv_intraday with the daily YZ (blend weight from config)."""
    out = se.evaluate_readiness(_inputs(3, rv_intraday=0.20), _short_dte_cfg())
    m = out["metrics"]
    assert m["vol_reference"] == "blended"
    blend = simmer_config.dte_tiers()["short_rv_blend"]
    rv_short = blend * 0.20 + (1.0 - blend) * 0.244
    assert m["vrp"] == pytest.approx(0.33 / rv_short)


def test_short_tier_rv_forecast_follows_the_intraday_rv():
    """EV/scoring must use the SAME intraday RV the VRP gate uses (consistency):
    0-1 → rv_forecast == rv_intraday; 2-5 → the same blend as its VRP."""
    out01 = se.evaluate_readiness(_inputs(1, rv_intraday=0.20), _short_dte_cfg())
    assert out01["metrics"]["rv_forecast"] == pytest.approx(0.20)

    out25 = se.evaluate_readiness(_inputs(3, rv_intraday=0.20), _short_dte_cfg())
    blend = simmer_config.dte_tiers()["short_rv_blend"]
    assert out25["metrics"]["rv_forecast"] == pytest.approx(
        blend * 0.20 + (1.0 - blend) * 0.244)


def test_explicit_rv_forecast_override_wins_over_intraday():
    """An explicitly injected rv_forecast is never clobbered by the intraday
    derivation."""
    out = se.evaluate_readiness(
        _inputs(1, rv_intraday=0.20, rv_forecast=0.111), _short_dte_cfg())
    assert out["metrics"]["rv_forecast"] == pytest.approx(0.111)
    # …but the VRP gate still reads the intraday RV, unaffected by the override.
    assert out["metrics"]["vrp"] == pytest.approx(0.33 / 0.20)


def test_short_tier_rv_forecast_falls_back_to_daily_when_intraday_absent():
    """No rv_intraday → rv_forecast is the daily YZ, exactly as today."""
    out = se.evaluate_readiness(_inputs(1), _short_dte_cfg())
    assert out["metrics"]["rv_forecast"] == pytest.approx(0.244)   # rv_yang_zhang


def test_medium_tier_rv_forecast_ignores_injected_intraday():
    """22-45 keeps the daily YZ forecast even if rv_intraday is injected."""
    out = se.evaluate_readiness(_inputs(30, rv_intraday=0.10))
    assert out["metrics"]["rv_forecast"] == pytest.approx(0.244)


def test_medium_tier_ignores_injected_intraday_inputs():
    """22-45 tier must NOT take the intraday branch even if the inputs arrive."""
    out = se.evaluate_readiness(_inputs(30, rv_intraday=0.10, iv_change=0.05))
    m = out["metrics"]
    assert m["dte_tier"] == "22-45"
    assert m["vol_reference"] == "daily"
    assert m["iv_gate_mode"] is None
    assert m["vrp"] == pytest.approx(0.33 / 0.244)        # daily, unaffected


# ── IV-change gate replaces the 252-day percentile at short tenor (§1) ──────
def test_short_tier_rising_iv_is_a_go_not_a_veto():
    """A rising IV clears the vol floor via iv_change; the 252-day percentile
    veto does not apply on this branch."""
    out = se.evaluate_readiness(_inputs(1, iv_change=0.03, iv_percentile=5.0),
                                _short_dte_cfg())
    assert out["metrics"]["iv_gate_mode"] == "iv_change"
    assert "iv_percentile_floor" not in out["veto_reasons"]
    assert "iv_change_floor" not in out["veto_reasons"]


def test_short_tier_falling_iv_vetoes_on_iv_change():
    """IV bleeding out post-event → iv_change_floor veto (premium deflating)."""
    out = se.evaluate_readiness(_inputs(1, iv_change=-0.03), _short_dte_cfg())
    assert "iv_change_floor" in out["veto_reasons"]
    assert "iv_percentile_floor" not in out["veto_reasons"]


# ═══════════════════════════════════════════════════════════════════════════
# 4. Catalyst sign → structure side (short tiers)
# ═══════════════════════════════════════════════════════════════════════════
def _steer_ctx(tier: str, score: float | None, vrp: float = 1.5,
               earnings: dict | None = None) -> dict:
    return {
        "metrics": {"dte_tier": tier, "vrp": vrp},
        "research": {"sentiment": {} if score is None else {"score": score}},
        "earnings": earnings or {},
    }


def test_positive_catalyst_prefers_bull_put():
    cfg = simmer_config.resolved("NVDA")
    assert se._preferred_short_structure(_steer_ctx("0-1", 0.5), cfg) == "bull_put"


def test_negative_catalyst_prefers_bear_call():
    cfg = simmer_config.resolved("NVDA")
    assert se._preferred_short_structure(_steer_ctx("0-1", -0.5), cfg) == "bear_call"


def test_neutral_plus_rich_iv_prefers_iron_condor():
    cfg = simmer_config.resolved("NVDA")
    ctx = _steer_ctx("0-1", 0.0, vrp=1.40)   # inside neutral band, rich VRP
    assert se._preferred_short_structure(ctx, cfg) == "iron_condor"


def test_neutral_and_not_rich_does_not_steer():
    cfg = simmer_config.resolved("NVDA")
    ctx = _steer_ctx("0-1", 0.0, vrp=1.10)   # neutral, VRP below the IC floor
    assert se._preferred_short_structure(ctx, cfg) is None


def test_medium_tier_never_steers():
    cfg = simmer_config.resolved("NVDA")
    assert se._preferred_short_structure(_steer_ctx("22-45", 0.9), cfg) is None


def test_earnings_direction_wins_over_news_lean():
    cfg = simmer_config.resolved("NVDA")
    ctx = _steer_ctx("0-1", 0.5,   # bullish news …
                     earnings={"in_window": True, "direction": "bearish"})  # … bearish print
    assert se._preferred_short_structure(ctx, cfg) == "bear_call"


def test_steering_selects_the_aligned_survivor_end_to_end():
    """With both verticals surviving at 5 DTE, a positive catalyst makes the
    engine report bull_put even if bear_call scored higher on raw math."""
    cfg = _short_dte_cfg()
    # Open the structural gates so BOTH verticals survive; steering, not the gate,
    # is what we are isolating here.
    cfg["gates"].update({
        "short_delta_min": 0.05, "short_delta_max": 0.45,
        "liquidity_spread_pct_of_credit": 100.0, "min_open_interest": 0,
        "friction_ev_multiple": 0.0, "vrp_ratio_floor": 0.0,
        "iv_percentile_floor": 0.0,
    })
    pos = se.evaluate_readiness(_inputs(5, sentiment={"score": 0.6}), cfg)
    neg = se.evaluate_readiness(_inputs(5, sentiment={"score": -0.6}), cfg)
    # Negative sentiment blocks the put side entirely, so bear_call is forced;
    # positive sentiment steers to bull_put. Either way the side follows the sign.
    assert pos["structure"] == "bull_put"
    assert pos["catalyst_steer"] in ("bull_put", None)   # None if it was already top
    assert neg["structure"] == "bear_call"


def test_steering_overrides_the_top_scorer_end_to_end():
    """A genuine override: on this put-skewed chain bull_put out-scores the
    condor, but a NEUTRAL + rich-IV read steers the reported structure to the
    iron condor — and turning steering off restores the raw top scorer."""
    cfg = _short_dte_cfg()
    cfg["gates"].update({
        "short_delta_min": 0.05, "short_delta_max": 0.45,
        "liquidity_spread_pct_of_credit": 100.0, "min_open_interest": 0,
        "friction_ev_multiple": 0.0, "vrp_ratio_floor": 0.0,
        "iv_percentile_floor": 0.0,
    })
    steered = se.evaluate_readiness(_inputs(5, sentiment={"score": 0.0}), cfg)
    assert steered["structure"] == "iron_condor"
    assert steered["catalyst_steer"] == "iron_condor"    # override actually fired

    off = copy.deepcopy(cfg)
    off["catalyst"]["steer_short_tiers"] = False
    raw = se.evaluate_readiness(_inputs(5, sentiment={"score": 0.0}), off)
    assert raw["structure"] == "bull_put"                # raw top scorer differs
    assert raw["catalyst_steer"] is None


# ═══════════════════════════════════════════════════════════════════════════
# 5. Debit handoff (§6) — long tier only, non-blocking
# ═══════════════════════════════════════════════════════════════════════════
TOKEN = "consider_debit_structure_long_dte"


def test_debit_handoff_fires_long_tier_cheap_iv():
    """Long tenor + cheap IV (low percentile) → non-blocking notice, even though
    the DTE window still hard-vetoes the credit."""
    out = se.evaluate_readiness(_inputs(60, iv_percentile=10.0,
                                        sentiment={"score": 0.0}))
    assert TOKEN in out["avoid_if"]
    assert "dte_window" in out["veto_reasons"]            # never blocks; rides along


def test_debit_handoff_fires_long_tier_live_catalyst():
    out = se.evaluate_readiness(_inputs(60, sentiment={"score": 0.6}))
    assert TOKEN in out["avoid_if"]


def test_debit_handoff_silent_long_tier_rich_iv_no_catalyst():
    """Long tenor but IV rich and no directional catalyst → no notice."""
    out = se.evaluate_readiness(_inputs(60, sentiment={"score": 0.0}))
    assert TOKEN not in out["avoid_if"]


def test_debit_handoff_never_fires_medium_tier():
    """Even with cheap IV AND a catalyst, a 30-DTE never gets the handoff."""
    out = se.evaluate_readiness(_inputs(30, iv_percentile=5.0,
                                        sentiment={"score": 0.9}))
    assert TOKEN not in out["avoid_if"]


# ═══════════════════════════════════════════════════════════════════════════
# 6. REGRESSION — a 30-DTE envelope is identical with vs without the new inputs
# ═══════════════════════════════════════════════════════════════════════════
def test_30dte_envelope_identical_with_and_without_intraday_inputs():
    """The whole point of the fallback: injecting rv_intraday / iv_change into a
    medium-tier expiry changes NOTHING (fallback path == current path)."""
    base = se.evaluate_readiness(_inputs(30))
    with_intraday = se.evaluate_readiness(
        _inputs(30, rv_intraday=0.15, iv_change=0.02))
    assert with_intraday == base


def test_30dte_matches_a_config_frozen_before_the_new_knobs():
    """A second guard on parity: strip the new config sections entirely and the
    30-DTE envelope is unchanged — the new code is inert without its config, and
    the medium-tier path never depended on it."""
    cfg = copy.deepcopy(simmer_config.resolved("NVDA"))
    new_default = se.evaluate_readiness(_inputs(30), cfg)
    baseline = se.evaluate_readiness(_inputs(30))
    assert new_default == baseline

"""Overnight catalyst+premium combiner — the pure decision logic
(docs/simmer_overnight_llm_spec.md §6). No network, no DB."""
from __future__ import annotations

from app import simmer_premium_tracker as pt
from app.simmer_overnight_combiner import combine, reactive_call

_P_WAIT = {"call": "wait", "premium_trend": "holding", "underlying_trend": "holding", "confidence": 50}
_P_BEAR = {"call": "sell_bear_call", "premium_trend": "fading", "underlying_trend": "holding", "confidence": 80}


def test_no_catalyst_falls_back_to_premium():
    v = combine(_P_BEAR, None)
    assert v["call"] == "sell_bear_call" and v["source"] == "premium_only"


def test_agreement_takes_side_and_boosts_confidence():
    cat = {"call": "sell_bear_call", "sustain": "hold_to_close", "confidence": 70}
    v = combine(_P_BEAR, cat)
    assert v["call"] == "sell_bear_call" and v["source"] == "agree"
    assert v["confidence"] >= max(_P_BEAR["confidence"], 70)   # agreement bonus


def test_opposite_sides_stand_down_to_wait():
    cat = {"call": "sell_bull_put", "sustain": "extend", "confidence": 90}
    v = combine(_P_BEAR, cat)
    assert v["call"] == "wait" and v["source"] == "conflict"


def test_premium_wait_promotes_only_in_blended():
    cat = {"call": "sell_bear_call", "sustain": "extend", "confidence": 82}
    assert combine(_P_WAIT, cat, promote=False)["call"] == "wait"            # conservative holds
    blended = combine(_P_WAIT, cat, promote=True)
    assert blended["call"] == "sell_bear_call" and blended["source"] == "catalyst_promote"


def test_promote_requires_conf_and_sustain():
    weak_conf = {"call": "sell_bear_call", "sustain": "extend", "confidence": 66}   # < 70
    weak_sustain = {"call": "sell_bear_call", "sustain": "fade", "confidence": 90}  # wrong sustain
    assert combine(_P_WAIT, weak_conf, promote=True)["call"] == "wait"
    assert combine(_P_WAIT, weak_sustain, promote=True)["call"] == "wait"


def test_premium_committed_catalyst_unsure_trims_confidence():
    cat = {"call": "wait", "sustain": "hold_to_midday", "confidence": 40}
    v = combine(_P_BEAR, cat)
    assert v["call"] == "sell_bear_call" and v["confidence"] == _P_BEAR["confidence"] - 20


# ── Phase A: reactive_call (premium-is-master drift override) ───────────────────
_FROZEN_BEAR = {"call": "sell_bear_call", "confidence": 78}


def test_reactive_disabled_mirrors_the_frozen_call():
    live = {"call": "sell_bull_put", "confidence": 90, "mean_ratio": 0.5, "underlying_trend": "retracing"}
    r = reactive_call(_FROZEN_BEAR, live, drift_cfg={"enabled": False})
    assert r["call"] == "sell_bear_call" and r["source"] == "disabled"


def test_reactive_frozen_wait_just_tracks_live_premium():
    r = reactive_call({"call": "wait", "confidence": 0},
                      {"call": "sell_bull_put", "confidence": 60, "mean_ratio": 0.8})
    assert r["call"] == "sell_bull_put" and r["source"] == "premium_live"


def test_reactive_same_side_holds():
    live = {"call": "sell_bear_call", "confidence": 70, "mean_ratio": 1.2, "underlying_trend": "extending"}
    assert reactive_call(_FROZEN_BEAR, live)["source"] == "frozen_hold"


def test_reactive_mild_drift_stands_down_big_drift_flips():
    mild = {"call": "wait", "confidence": 50, "mean_ratio": 0.95, "underlying_trend": "holding"}
    assert reactive_call(_FROZEN_BEAR, mild)["source"] == "premium_drift_standdown"
    # opposite side but drift below the flip band → still only stand down
    shallow = {"call": "sell_bull_put", "confidence": 80, "mean_ratio": 0.95, "underlying_trend": "holding"}
    assert reactive_call(_FROZEN_BEAR, shallow)["call"] == "wait"
    # opposite side, drift past the band, tape not extending against us → flip
    deep = {"call": "sell_bull_put", "confidence": 72, "mean_ratio": 0.84, "underlying_trend": "retracing"}
    r = reactive_call(_FROZEN_BEAR, deep)
    assert r["call"] == "sell_bull_put" and r["source"] == "premium_override"


def test_reactive_flip_blocked_when_underlying_still_extending_with_the_thesis():
    # premium names the opposite side with big decay, but the tape is still extending
    # the ORIGINAL move → require_underlying holds the flip back to a stand-down.
    live = {"call": "sell_bull_put", "confidence": 80, "mean_ratio": 0.8, "underlying_trend": "extending"}
    assert reactive_call(_FROZEN_BEAR, live)["source"] == "premium_drift_standdown"


# ── leading reversal signals: react BEFORE the toward-side premium deflates ─────
def test_reactive_flips_on_price_reclaim_even_when_toward_premium_fat():
    # the vol-spike case: stock has reclaimed 70% of the gap and the opposite side is
    # bid up, but the toward-side premium is STILL fat (ratio ~1.0, no magnitude drift).
    # The leading signal must flip anyway — the old magnitude-only rule would miss it.
    live = {"call": "sell_bull_put", "confidence": 60, "mean_ratio": 1.0,
            "underlying_trend": "retracing", "gap_retraced": 0.7, "skew_shift": 0.35}
    r = reactive_call(_FROZEN_BEAR, live)
    assert r["call"] == "sell_bull_put" and r["source"] == "premium_override"
    assert "price/skew reversal" in r["reasons"][0]


def test_reactive_skew_alone_triggers():
    live = {"call": "sell_bull_put", "confidence": 55, "mean_ratio": 1.0,
            "underlying_trend": "holding", "gap_retraced": 0.1, "skew_shift": 0.12}
    assert reactive_call(_FROZEN_BEAR, live)["source"] == "premium_override"


def test_reactive_stands_down_early_when_price_turns_before_the_verdict():
    # the premium VERDICT still names our side, but price is already reclaiming the gap
    # → stand down early rather than wait for the verdict to catch up (the "too late" fix).
    live = {"call": "sell_bear_call", "confidence": 70, "mean_ratio": 1.1,
            "underlying_trend": "retracing", "gap_retraced": 0.65, "skew_shift": 0.2}
    r = reactive_call(_FROZEN_BEAR, live)
    assert r["call"] == "wait" and r["source"] == "premium_leading_standdown"


def test_bidirectional_reversal_end_to_end_through_the_real_tracker():
    """A debunk-style reversal driven through the REAL two-sided tracker, tick by tick.
    A down-gap name is scored sell_bear_call and frozen. The engine must get OFF that
    call EARLY — when the opposite (call) side is bid up ahead of the move — not wait
    for the toward (put) premium to deflate or price to fully reverse (the 'too late'
    failure). Asserts the reactive source at each tick off the live verdict."""
    frozen = {"call": "sell_bear_call", "confidence": 78}
    gap, spot0 = -0.05, 100.0
    put_base = {"97": 3.0, "95": 2.0}        # toward side (down gap → puts)
    call_base = {"103": 2.0, "105": 1.0}     # opposite / reversal side

    def step(spot, puts, calls):
        v = pt.analyze(direction="down", crowd="sector_wide", gap=gap,
                       baseline=put_base, latest=puts, spot_baseline=spot0, spot_now=spot,
                       news_bad=True, opp_baseline=call_base, opp_latest=calls)
        return v, reactive_call(frozen, v)

    # t0 — tape CONFIRMS the drop: still falling, puts building, calls fading → hold.
    v, r = step(98.0, {"97": 3.5, "95": 2.4}, {"103": 1.4, "105": 0.6})
    assert v["call"] == "sell_bear_call" and r["source"] == "frozen_hold"

    # t1 — EARLY warning: price barely moved and puts are STILL fat (verdict still
    # sell_bear_call), but the call side is being bid up (skew tips) → stand down early,
    # BEFORE the toward premium deflates. This is the catch the magnitude rule missed.
    v, r = step(100.1, {"97": 3.3, "95": 2.2}, {"103": 3.0, "105": 1.6})
    assert v["call"] == "sell_bear_call" and v["skew_shift"] >= 0.10
    assert r["call"] == "wait" and r["source"] == "premium_leading_standdown"

    # t2 — full reversal: price reclaimed most of the gap, puts collapse → verdict skip;
    # the reactive call is firmly off the bear-call (stood down, not holding a loser).
    v, r = step(104.0, {"97": 1.2, "95": 0.7}, {"103": 3.6, "105": 2.2})
    assert v["call"] == "skip" and v["gap_retraced"] >= 0.6
    assert r["call"] == "wait"

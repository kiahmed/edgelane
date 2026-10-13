"""Overnight catalyst+premium combiner — the pure decision logic
(docs/simmer_overnight_llm_spec.md §6). No network, no DB."""
from __future__ import annotations

from app.simmer_overnight_combiner import combine

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

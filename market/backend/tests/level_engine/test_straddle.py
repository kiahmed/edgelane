"""straddle.py — long straddle / strangle / single-option scorers (§8.4)."""
from __future__ import annotations

import pytest

from app.strategy_engine import (composite_score, composite_verdict, compute_limit_premiums,
                                 score_vertical)
from level_engine import straddle as sd

from .test_strategy import bs_chain

SPOT, DTE, EM = 7268.0, 0.2, 30.0
CHAIN = bs_chain(SPOT)
C = {(c["strike"], c["side"]): c for c in CHAIN}
mid = lambda c: (c["bid"] + c["ask"]) / 2
VKEYS = set(score_vertical(C[(7280.0, "call")], C[(7270.0, "call")], "debit", DTE))


def _composite(c):
    c = dict(c, wall_penalty={"factor": 1.0}, ev_adjusted=c["ev"])
    c["limit_premiums"] = compute_limit_premiums(c)
    c["composite_score"] = composite_score(c)
    c["composite_verdict"] = composite_verdict(c["composite_score"], c["ev_adjusted"])
    return c


def test_straddle_fields_and_values():
    call, put = C[(7270.0, "call")], C[(7270.0, "put")]
    s = sd.score_straddle_strangle(call, put, SPOT, DTE, EM, CHAIN)
    assert set(s) == VKEYS
    net = mid(call) + mid(put)
    assert s["type"] == "debit" and s["net_premium"] == pytest.approx(net)
    assert s["max_loss"] == pytest.approx(net) and s["capital_required"] == s["max_loss"]
    assert s["breakevens"] == pytest.approx([7270 - net, 7270 + net])
    # cap: intrinsic at spot ± 1·EM (better side) − debit
    assert s["max_profit"] == pytest.approx(max(SPOT + EM - 7270, 7270 - (SPOT - EM)) - net)
    pop = sd.prob_beyond(CHAIN, "call", 7270 + net, 0) + sd.prob_beyond(CHAIN, "put", 7270 - net, 0)
    assert s["pop_pct"] == pytest.approx(pop * 100)
    p = s["pop_pct"] / 100
    assert s["ev"] == pytest.approx(p * s["max_profit"] - (1 - p) * s["max_loss"])
    assert s["strikes"]["center"] == 7270.0 and s["strikes"]["long_call"] == 7270.0
    assert [l["long_short"] for l in s["legs"]] == [1, 1]
    assert s["legs"][0]["iv"] == pytest.approx(0.15 / 100)          # legacy leg-iv convention
    assert s["health"] == "broken" and s["liquidity"] == "high"     # legacy debit rule at dte<=2
    c = _composite(s)
    assert 0 <= c["composite_score"] <= 100 and c["limit_premiums"]["side"] == "debit"


def test_strangle_and_guards():
    call, put = C[(7280.0, "call")], C[(7260.0, "put")]
    s = sd.score_straddle_strangle(call, put, SPOT, DTE, EM, CHAIN)
    assert s["strikes"]["center"] is None and "strangle" in s["structure_text"]
    assert s["breakevens"][0] < 7260 and s["breakevens"][1] > 7280
    assert sd.score_straddle_strangle(C[(7260.0, "call")], C[(7280.0, "put")], SPOT, DTE, EM) is None
    assert sd.score_straddle_strangle(put, call, SPOT, DTE, EM) is None
    assert sd.score_straddle_strangle(None, put, SPOT, DTE, EM) is None
    zero = dict(call, bid=0.0, ask=0.0)
    assert sd.score_straddle_strangle(zero, dict(put, bid=0.0, ask=0.01), SPOT, DTE, EM) is None
    assert sd.score_straddle_strangle(call, put, SPOT, 5.0, EM, CHAIN)["health"] != "broken"


def test_long_option_fields_and_values():
    c = C[(7270.0, "call")]
    s = sd.score_long_option(c, SPOT, DTE, EM, CHAIN)
    assert set(s) == VKEYS and s["structure_text"] == "Long 7270.0C"
    assert s["breakevens"] == pytest.approx([7270 + mid(c)])
    assert s["max_profit"] == pytest.approx(max(0.0, SPOT + EM - 7270 - mid(c)))
    assert s["pop_pct"] == pytest.approx(100 * sd.prob_beyond(CHAIN, "call", 7270 + mid(c), 0))
    p = C[(7260.0, "put")]
    sp = sd.score_long_option(p, SPOT, DTE, EM, CHAIN)
    assert sp["strikes"]["long_put"] == 7260.0 and sp["breakevens"] == pytest.approx([7260 - mid(p)])
    far = sd.score_long_option(C[(7400.0, "call")], SPOT, DTE, EM, CHAIN)
    assert far is None or far["max_profit"] == 0.0
    _composite(s)


def test_prob_beyond_interpolates_delta():
    d = {k: abs(C[(k, "call")]["delta"]) for k in (7270.0, 7275.0)}
    assert sd.prob_beyond(CHAIN, "call", 7272.5, 0) == pytest.approx((d[7270.0] + d[7275.0]) / 2)
    assert sd.prob_beyond(CHAIN, "call", 10.0, 0) == pytest.approx(abs(C[(7150.0, "call")]["delta"]))
    assert sd.prob_beyond([], "call", 7272.5, -0.42) == 0.42


def test_fair_atm_option_scores_ev_near_zero():
    # calibration behind EM_CAP_MULT = 1 (see module docstring)
    s = sd.score_long_option(C[(7270.0, "call")], SPOT, DTE, EM, CHAIN)
    assert abs(s["ev"]) < 0.1 * s["net_premium"]

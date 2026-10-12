"""Overnight premium-decision analyzer — the pure verdict logic under faked
inflate / hold / deflate / reversal premium scenarios (docs/simmer.md → Overnight
movers). No network, no DB."""
from __future__ import annotations

from app import simmer_premium_tracker as pt


def _chain(spot=100.0, side_strikes=None):
    """A minimal normalized chain: puts below + calls above spot, mid = (bid+ask)/2."""
    out = []
    for k in (90.0, 92.0, 94.0, 96.0, 98.0):      # puts below
        out.append({"side": "put", "strike": k, "bid": 1.0, "ask": 1.2, "volume": 100})
    for k in (102.0, 104.0, 106.0, 108.0, 110.0):  # calls above
        out.append({"side": "call", "strike": k, "bid": 0.9, "ask": 1.1, "volume": 120})
    return out


# ── strike picking + maps ───────────────────────────────────────────────────
def test_picks_otm_puts_on_a_drop_nearest_first():
    s = pt.pick_strikes(100.0, _chain(), "down", n=3)
    assert s == [98.0, 96.0, 94.0]                 # OTM puts below spot, nearest first


def test_picks_otm_calls_on_a_pop_nearest_first():
    s = pt.pick_strikes(100.0, _chain(), "up", n=3)
    assert s == [102.0, 104.0, 106.0]


def test_premium_and_volume_maps():
    pm = pt.premium_map(_chain(), [98.0, 96.0], "put")
    assert pm == {"98.0": 1.1, "96.0": 1.1}
    vm = pt.volume_map(_chain(), [98.0], "put")
    assert vm == {"98.0": 100}


# ── classification ──────────────────────────────────────────────────────────
def test_classify_premium_building_holding_fading():
    base = {"98.0": 1.0, "96.0": 1.0}
    assert pt.classify_premium(base, {"98.0": 1.2, "96.0": 1.2})[0] == "building"
    assert pt.classify_premium(base, {"98.0": 1.02, "96.0": 0.99})[0] == "holding"
    assert pt.classify_premium(base, {"98.0": 0.8, "96.0": 0.85})[0] == "fading"
    assert pt.classify_premium(base, {})[0] is None       # no live read yet


def test_classify_underlying_extend_hold_retrace():
    # gap -6%: baseline spot 100 (already gapped-down level)
    assert pt.classify_underlying(-0.06, 100.0, 98.0) == "extending"   # falling further
    assert pt.classify_underlying(-0.06, 100.0, 100.2) == "holding"    # sitting on the level
    assert pt.classify_underlying(-0.06, 100.0, 103.0) == "retracing"  # bouncing back up
    assert pt.classify_underlying(-0.06, 100.0, None) is None


# ── the verdict: the four decisions ─────────────────────────────────────────
def test_down_move_holding_rich_premium_sector_wide_sells_bear_call():
    v = pt.analyze(direction="down", crowd="sector_wide", gap=-0.06,
                   baseline={"98.0": 1.0, "96.0": 1.0},
                   latest={"98.0": 1.05, "96.0": 1.03},        # holding
                   spot_baseline=100.0, spot_now=99.8, news_bad=True)
    assert v["call"] == "sell_bear_call"
    assert v["premium_trend"] == "holding" and v["underlying_trend"] == "holding"
    assert v["confidence"] >= 60


def test_down_move_premium_crushing_stock_holding_sells_bull_put():
    v = pt.analyze(direction="down", crowd="lone", gap=-0.05,
                   baseline={"98.0": 1.0, "96.0": 1.0},
                   latest={"98.0": 0.7, "96.0": 0.75},         # fading
                   spot_baseline=100.0, spot_now=100.1)        # holding
    assert v["call"] == "sell_bull_put"


def test_reversal_with_collapsing_premium_is_skip():
    v = pt.analyze(direction="down", crowd="lone", gap=-0.06,
                   baseline={"98.0": 1.0}, latest={"98.0": 0.6},   # collapsing
                   spot_baseline=100.0, spot_now=104.0)            # retracing hard
    assert v["call"] == "skip"
    assert any("fade" in r for r in v["reasons"])


def test_up_move_holding_sells_bull_put():
    v = pt.analyze(direction="up", crowd="sector_wide", gap=0.05,
                   baseline={"102.0": 1.0, "104.0": 1.0},
                   latest={"102.0": 1.06, "104.0": 1.05},
                   spot_baseline=100.0, spot_now=100.3)
    assert v["call"] == "sell_bull_put"


def test_mixed_signal_waits():
    # down move but premium holding while the stock is retracing (not collapsing) — unclear
    v = pt.analyze(direction="down", crowd="mixed", gap=-0.04,
                   baseline={"98.0": 1.0}, latest={"98.0": 1.01},   # holding
                   spot_baseline=100.0, spot_now=102.5)             # retracing
    assert v["call"] == "wait"


def test_before_open_baseline_only_waits():
    v = pt.analyze(direction="down", crowd="sector_wide", gap=-0.06,
                   baseline={"98.0": 1.0}, latest=None,
                   spot_baseline=100.0, spot_now=None)
    assert v["call"] == "wait" and v["premium_trend"] is None
    assert "baseline" in v["reasons"][0]


def test_sector_wide_lifts_confidence_over_lone():
    kw = dict(direction="down", gap=-0.06, baseline={"98.0": 1.0},
              latest={"98.0": 1.05}, spot_baseline=100.0, spot_now=99.9, news_bad=True)
    wide = pt.analyze(crowd="sector_wide", **kw)["confidence"]
    lone = pt.analyze(crowd="lone", **kw)["confidence"]
    assert wide > lone

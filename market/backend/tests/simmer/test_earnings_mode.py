"""Earnings as a FOLDED FACTOR (not a hard veto) — the three views.

- No `earnings` block, or view "veto" → legacy hard earnings veto.
- "consider" (default): earnings folds — go+aligned LIFTS, opposed penalizes, and
  a NO-GO holds the name back (negative lift + hard cap below the ready band).
- "ignore": earnings treated as absent — no veto, no fold (pure ex-earnings).
Structures forced so the alignment sign is deterministic.
"""
from __future__ import annotations

import inspect

from app import simmer_engine as se
from app import simmer_watcher as sw

from .conftest import mutate, reason_kinds

# earnings 5 days out — inside the 30-DTE clean_inputs tenor
EARN = {"type": "earnings", "days": 5, "confirmed": True}


def test_sweep_default_is_consider():
    # The sweep displays the earnings-CONSIDERED verdict by default (folded, no
    # hard veto). A no-go holds the name back, so alerts still can't auto-fire a
    # sell-through-earnings recommendation.
    assert inspect.signature(sw.analyze_symbol).parameters["earnings_view"].default == "consider"


def _inp(view, *, direction="bullish", go=True, confidence=1.0, struct="bull_put"):
    inp = mutate(catalysts=[EARN])
    inp["settings"] = {"structures_enabled": [struct]}
    inp["earnings"] = {
        "view": view, "in_window": True, "direction": direction,
        "confidence": confidence, "go": go, "earnings_date": "2026-09-15",
    }
    return inp


def test_no_earnings_block_hard_vetoes():
    out = se.evaluate_readiness(mutate(catalysts=[EARN]))
    assert out["decision"] == "vetoed"
    assert "catalyst:earnings_in_tenor" in reason_kinds(out)


def test_view_veto_hard_vetoes():
    out = se.evaluate_readiness(_inp("veto"))
    assert out["decision"] == "vetoed"
    assert "catalyst:earnings_in_tenor" in reason_kinds(out)


def test_consider_skips_the_hard_veto():
    out = se.evaluate_readiness(_inp("consider", go=True))
    assert out["decision"] != "vetoed"
    assert "catalyst:earnings_in_tenor" not in reason_kinds(out)
    assert out["earnings"]["view"] == "consider"
    assert out["earnings"]["applied"] is True
    assert out["earnings"]["close_before_print"] is True   # go read = run-up play


def test_ignore_skips_veto_and_does_not_fold():
    out = se.evaluate_readiness(_inp("ignore"))
    assert "catalyst:earnings_in_tenor" not in reason_kinds(out)
    assert out["earnings"]["applied"] is False
    assert out["earnings"]["lift"] == 0.0
    # ignore == pure ex-earnings: same score as no earnings block AND no veto.
    base = _inp("ignore")
    base["research"]["catalysts"] = []            # truly no earnings
    assert out["score"] == se.evaluate_readiness(base)["score"]


def test_consider_aligned_lifts_opposed_penalizes():
    bull = se.evaluate_readiness(_inp("consider", direction="bullish", struct="bull_put"))
    bear = se.evaluate_readiness(_inp("consider", direction="bearish", struct="bull_put"))
    assert bull["earnings"]["lift"] == 15.0      # +1 align × conf 1.0 × 15
    assert bear["earnings"]["lift"] == -15.0     # -1 align × conf 1.0 × 15
    assert bull["score"] > bear["score"]


def test_consider_confidence_scales_the_lift():
    out = se.evaluate_readiness(_inp("consider", direction="bullish", confidence=0.5))
    assert out["earnings"]["lift"] == 7.5        # 0.5 × 15


def test_no_go_holds_the_name_back_never_ready():
    # A no-go earnings read must keep an otherwise-strong name OUT of "ready".
    out = se.evaluate_readiness(_inp("consider", go=False, direction="bullish"))
    assert out["decision"] != "vetoed"           # visible, not hard-vetoed
    assert out["decision"] != "ready"            # but held back
    assert out["earnings"]["held_back"] is True
    assert out["earnings"]["lift"] < 0           # negative pressure
    assert out["earnings"]["close_before_print"] is False


def test_missing_bias_holds_back_fail_safe():
    # Cold cache: the sweep found an earnings window but no bias is computed yet
    # (no direction/go). go defaults False → the name is HELD BACK, never "ready".
    # This is the alert fail-safe that replaces the old hard-veto default — a
    # sell-through-earnings can't auto-fire before the analyzer has spoken.
    inp = mutate(catalysts=[EARN])
    inp["settings"] = {"structures_enabled": ["bull_put"]}
    inp["earnings"] = {"view": "consider", "in_window": True,
                       "earnings_date": "2026-09-15"}   # no direction/confidence/go
    out = se.evaluate_readiness(inp)
    assert out["decision"] != "vetoed"        # not a hard veto
    assert out["decision"] != "ready"         # but held back
    assert out["earnings"]["held_back"] is True
    assert out["earnings"]["go"] is False


def test_eight_k_still_vetoes_under_consider():
    inp = _inp("consider")
    inp["research"]["catalysts"] = [EARN, {"type": "8k_material", "days": 3, "confirmed": True}]
    out = se.evaluate_readiness(inp)
    assert out["decision"] == "vetoed"
    assert "catalyst:8k_material_in_tenor" in reason_kinds(out)

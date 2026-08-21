"""Earnings mode in the engine — the opt-in fold that replaces the hard veto.

- No `earnings` block in inputs, or mode off → legacy behavior (earnings vetoes).
- mode on → earnings catalyst stops vetoing; the cached bias folds into the
  score: aligned+go lifts (scaled by confidence, ±15 cap), opposed penalizes,
  no-go applies nothing but still scores.
Structures forced to bull_put so the alignment sign is deterministic.
"""
from __future__ import annotations

import inspect

from app import simmer_engine as se
from app import simmer_watcher as sw

from .conftest import clean_inputs, mutate, reason_kinds


def test_sweep_default_keeps_earnings_veto():
    # analyze_symbol's default (what the sweep uses) MUST be False, so persisted
    # verdicts and the alerts they fan out keep the earnings veto — nothing
    # auto-recommends selling through earnings. On-demand /analyze passes it True.
    assert inspect.signature(sw.analyze_symbol).parameters["earnings_mode"].default is False

# earnings 5 days out — inside the 30-DTE clean_inputs tenor
EARN = {"type": "earnings", "days": 5, "confirmed": True}


def _inp(mode, *, direction="bullish", go=True, confidence=1.0, struct="bull_put"):
    inp = mutate(catalysts=[EARN])
    inp["settings"] = {"structures_enabled": [struct]}
    inp["earnings"] = {
        "mode": mode, "in_window": True, "direction": direction,
        "confidence": confidence, "go": go, "earnings_date": "2026-09-15",
    }
    return inp


def test_earnings_vetoes_without_a_mode_block():
    out = se.evaluate_readiness(mutate(catalysts=[EARN]))
    assert out["decision"] == "vetoed"
    assert "catalyst:earnings_in_tenor" in reason_kinds(out)


def test_mode_off_still_vetoes():
    out = se.evaluate_readiness(_inp(mode=False))
    assert out["decision"] == "vetoed"
    assert "catalyst:earnings_in_tenor" in reason_kinds(out)


def test_mode_on_skips_the_earnings_veto():
    out = se.evaluate_readiness(_inp(mode=True, go=True))
    assert out["decision"] != "vetoed"
    assert "catalyst:earnings_in_tenor" not in reason_kinds(out)
    assert out["earnings"] is not None
    assert out["earnings"]["in_window"] is True
    assert out["earnings"]["applied"] is True


def test_aligned_lifts_opposed_penalizes():
    bull = se.evaluate_readiness(_inp(mode=True, direction="bullish", struct="bull_put"))
    bear = se.evaluate_readiness(_inp(mode=True, direction="bearish", struct="bull_put"))
    assert bull["earnings"]["lift"] == 15.0     # +1 align × conf 1.0 × 15
    assert bear["earnings"]["lift"] == -15.0    # -1 align × conf 1.0 × 15
    assert bull["score"] > bear["score"]


def test_confidence_scales_the_lift():
    out = se.evaluate_readiness(_inp(mode=True, direction="bullish", confidence=0.5))
    assert out["earnings"]["lift"] == 7.5       # 0.5 × 15


def test_no_go_scores_but_applies_no_lift():
    out = se.evaluate_readiness(_inp(mode=True, go=False))
    assert out["decision"] != "vetoed"          # mode on skips the veto regardless of go
    assert out["earnings"]["applied"] is False
    assert out["earnings"]["lift"] == 0.0


def test_eight_k_still_vetoes_under_earnings_mode():
    # Earnings mode only lifts the EARNINGS veto; other confirmed binaries stay hard.
    inp = _inp(mode=True)
    inp["research"]["catalysts"] = [EARN, {"type": "8k_material", "days": 3, "confirmed": True}]
    out = se.evaluate_readiness(inp)
    assert out["decision"] == "vetoed"
    assert "catalyst:8k_material_in_tenor" in reason_kinds(out)

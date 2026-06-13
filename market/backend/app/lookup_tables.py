"""Constants ported from spread_optimizer_v4_7_html.jsx.

These tables drive strategy classification, bias-to-strategy mapping, wall
strength multipliers, and width presets. Kept in one module so the parity
tests can confirm Python and JSX share identical literal values.

NOTE: STRATEGIES dict key order MATCHES the JSX declaration order verbatim.
recommend_strategies() iterates dict.items() and JSX uses Object.entries(),
both of which preserve insertion order. Reordering keys here will change
the recommended_strategies output and break parity.
"""
from __future__ import annotations

# Strategies (key order matches JSX line 6-15 verbatim)
STRATEGIES: dict[str, dict] = {
    "bull_put": {
        "name": "Bull Put Spread",
        "short": "Bull Put",
        "type": "credit",
        "side": "put",
        "fits": ["bullish", "mild_bullish", "neutral"],
    },
    "bear_call": {
        "name": "Bear Call Spread",
        "short": "Bear Call",
        "type": "credit",
        "side": "call",
        "fits": ["bearish", "mild_bearish", "neutral"],
    },
    "iron_condor": {
        "name": "Iron Condor",
        "short": "Iron Condor",
        "type": "credit",
        "side": "both",
        "fits": ["neutral", "mild_bullish", "mild_bearish"],
    },
    "iron_butterfly": {
        "name": "Iron Butterfly",
        # JSX short name is 'Iron Fly'.
        "short": "Iron Fly",
        "type": "credit",
        "side": "both",
        "fits": ["neutral"],
    },
    "bull_call": {
        "name": "Bull Call Spread",
        "short": "Bull Call",
        "type": "debit",
        "side": "call",
        "fits": ["bullish", "mild_bullish"],
    },
    "bear_put": {
        "name": "Bear Put Spread",
        "short": "Bear Put",
        "type": "debit",
        "side": "put",
        "fits": ["bearish", "mild_bearish"],
    },
    "call_butterfly": {
        "name": "Call Butterfly",
        "short": "Call Fly",
        "type": "debit",
        "side": "call",
        "fits": ["neutral", "mild_bullish"],
    },
    "put_butterfly": {
        "name": "Put Butterfly",
        "short": "Put Fly",
        "type": "debit",
        "side": "put",
        "fits": ["neutral", "mild_bearish"],
    },
}

BIAS_TO_STRATEGY: dict[str, str] = {
    "bullish":      "bull_put",
    "mild_bullish": "bull_put",
    "neutral":      "iron_condor",
    "mild_bearish": "bear_call",
    "bearish":      "bear_call",
}

# Wall strength multiplier used in wall penalty (JSX line 1713).
# NOTE: bias engine's _computeDirectionalScore uses a DIFFERENT scale
# (high=1.5, medium=1.0, low=0.5) which is hardcoded inline in that function
# -- preserved separately. This table feeds computeWallPenalty only.
WALL_STRENGTH_MULT: dict[str, float] = {
    "high":   1.0,
    "medium": 0.5,
    "low":    0.25,
}

# Width presets (for strategy candidate generation)
WIDTH_PREFS: dict[str, dict] = {
    "conservative": {"name": "Conservative", "factor": 0.7},
    "balanced":     {"name": "Balanced",     "factor": 1.0},
    "generous":     {"name": "Generous",     "factor": 1.4},
}

# Composite score badge contributions
HEALTH_BADGES: dict[str, dict] = {
    "healthy":      {"label": "Healthy",       "weight": +15},
    "directional":  {"label": "Directional",   "weight":  -5},
    "thin":         {"label": "Thin Premium",  "weight":  -5},
    "wide":         {"label": "Wide Spread",   "weight": -10},
    "broken":       {"label": "Broken",        "weight": -40},
    "capital_trap": {"label": "Capital Trap",  "weight": -40},
}

# Dealer contract multiplier -- 100 shares per options contract
DEALER_CONTRACT_MULT: int = 100

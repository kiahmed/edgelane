"""Simmer readiness-engine configuration — global knobs + per-ticker overrides.

Follows the **Torque config precedent** exactly (`torque_config.py`): a Python
module of defaults, optionally deep-merged with a JSON override file, rather
than a second KEY=VALUE settings file. Nothing here is a secret — Gemini/news
API keys belong in `edgelane_market.config` via `config.Settings`, NOT in
`simmer_tickers.json` (that file is not covered by the repo's gitignore
patterns).

The override file is discovered via `$SIMMER_TICKERS_CONFIG`, then by walking
`Path(__file__).parents` looking for `simmer/simmer_tickers.json` (its home in
the dev tree) and then `simmer_tickers.json` beside each ancestor. Walking rather
than indexing `parents[3]` is deliberate: in the container the package sits at
`/srv/app`, `parents[3]` does not exist, and compose points
`$SIMMER_TICKERS_CONFIG` at the bind-mounted `/config/simmer_tickers.json`
anyway.

Override-file shape (every key optional, deep-merged over the defaults):

    {
      "tickers": ["NVDA", "AMD"],
      "weights": {"structural_safety": 0.35, "credit_quality": 0.10},
      "gates":   {"vrp_ratio_floor": 1.20},
      "targets": {"short_delta": 0.30},
      "overrides": {"NVDA": {"min_open_interest": 1000}},
      "commissions": {"NVDA": 0.35}
    }

Everything the engine reads goes through an accessor, so a tuning change is a
JSON edit and a process restart — never a code change. `resolved()` returns the
whole merged blob in one dict, which is what `simmer_engine.evaluate_readiness`
takes so a test can inject a fully synthetic config.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

ENGINE_VERSION = "simmer-engine-1.0.0"

# ── Composite weights ──────────────────────────────────────────────────────
# readiness = 100 × Σ(wᵢ · componentᵢ) × regime_multiplier.
# These are *reasoned starting points, not fitted parameters* — the
# `simmer_outcomes` calibration loop is what turns them into something
# defensible. They must stay tunable without a code change.
WEIGHTS: dict[str, float] = {
    "structural_safety":     0.30,   # short strike beyond wall + wall tier + gamma-flip side
    "expected_move_headroom": 0.20,  # short strike vs max(EM_straddle, EM_sigma), in σ units
    "volatility_richness":   0.20,   # IVP/IVR + IV/RV ratio + term-structure slope
    "credit_quality":        0.15,   # Alpha = EV/MaxLoss, C/W vs its bound, POP
    "liquidity_quality":     0.10,   # spread in IV points, OI depth, size at touch
    "sentiment_lean":        0.05,   # side selection + suppression ONLY, never a promoter
}

# Sub-weights inside the composite components (each block sums to 1.0).
SUB_WEIGHTS: dict[str, dict[str, float]] = {
    "volatility_richness": {"vrp": 0.60, "iv_percentile": 0.25, "term_structure": 0.15},
    "credit_quality":      {"alpha": 0.50, "cw_efficiency": 0.25, "pop": 0.25},
    "liquidity_quality":   {"spread": 0.55, "open_interest": 0.30, "size_at_touch": 0.15},
}

# ── Hard gates (veto — no score overrides these) ───────────────────────────
# Every number here is derived in docs/simmer.md, not chosen by convention:
#   * 8% of credit — friction eats the whole edge between $0.20 and $0.40 of
#     quoted width on a 5-wide (gross EV is only ~7% of max risk at 20% VRP).
#   * IVP floor, not IVR — IVR is a two-point statistic that a single vol spike
#     pins for a year. Gate on percentile, DISPLAY rank.
#   * VRP is a RATIO. "IV − HV > 5 vol points" passes every high-IV name and
#     rejects every low-IV one for no economic reason.
#   * 0.20–0.35 short delta — 5–10Δ shorts are measurably negative-EV.
GATES: dict[str, Any] = {
    # Liquidity floor
    "liquidity_spread_pct_of_credit": 8.0,   # package bid/ask ≤ 8% of credit
    "min_open_interest":              250,   # per leg
    "min_size_at_touch":              1,     # contracts quoted at bid/ask
    # DTE window + the dollar-friction test that DTE alone cannot express
    "dte_min":                        7,
    "dte_max":                        45,
    "friction_ev_multiple":           2.0,   # gross EV ($) ≥ 2 × round-trip cost
    # Volatility floors
    "iv_percentile_floor":            40.0,  # IVP < 40 → veto (gate on this)
    "iv_rank_floor":                  25.0,  # advisory alternate, displayed not gated
    "vrp_ratio_floor":                1.15,  # IV / RV_YangZhang
    # Structure
    "short_delta_min":                0.20,
    "short_delta_max":                0.35,
    # Catalysts
    "catalyst_lockout":               True,  # any confirmed binary inside the tenor
    "unconfirmed_earnings_block_days": 7,    # unconfirmed earnings blocks the week
    # Refuse to score any expiry beyond macro_calendar.json's `valid_through`.
    # Fails VISIBLY instead of silently selling through a CPI print.
    "require_macro_calendar":         True,
    # Cross-check: if Yang-Zhang shows edge but zero-mean close-to-close does
    # not, the "edge" is an overnight-exclusion artifact. Require agreement
    # within this ratio tolerance before trusting the VRP reading.
    "rv_agreement_tolerance":         0.35,
}

# Gates a user may NEVER switch off (safety, not preference). Exposed so the
# settings route can validate `gate_overrides` server-side against the
# toggleable allow-list rather than trusting the client.
LOCKED_GATES: tuple[str, ...] = (
    "catalyst_lockout", "liquidity", "dollar_friction", "chain_sanity", "macro_validity",
)
TOGGLEABLE_GATES: tuple[str, ...] = ("squeeze_veto", "sentiment_veto", "sector_dispersion")

# Tunable-within-bounds knobs: (min, max) hard clamps applied to user settings.
# A user who sets short delta to 0.05 has silently opted into the measurably
# negative-EV region — the clamp is how the research reaches them without a
# lecture.
# Keys here are USER-FACING and MUST match the `simmer_settings` column names in
# supabase/migrations/0010_simmer.sql exactly (min_dte, not the engine-internal
# gates() key dte_min) — the settings route persists these keys verbatim via
# PostgREST, and a mismatched key 400s the whole upsert. Pinned by
# tests/simmer/test_settings_schema.py.
USER_CLAMPS: dict[str, tuple[float, float]] = {
    "short_delta_min":   (0.15, 0.40),
    "short_delta_max":   (0.15, 0.40),
    "min_dte":           (1, 120),
    "max_dte":           (1, 180),
    "min_score":         (0.0, 100.0),
    "min_iv_percentile": (0.0, 100.0),
}

# ── Targets / shaping constants ────────────────────────────────────────────
TARGETS: dict[str, float] = {
    "short_delta":       0.275,  # optimum is 0.25–0.30 under stochastic vol
    "credit_to_width":   0.30,   # a TARGET, not a gate — C/W is a dependent variable
    # Max achievable C/W = N(−d2) at the short strike (~0.29 at 25Δ/30% IV), so
    # the absolute 0.30 target is INFEASIBLE at the optimal short delta. The
    # engine therefore targets this fraction of the achievable ceiling and takes
    # whichever of the two is lower. Chasing the absolute number would silently
    # push the engine to 0.35Δ+ shorts and 1-wide spreads. 0.70 is not
    # arbitrary: at 25Δ/30 DTE it lands within one strike of the width that
    # MAXIMIZES Alpha = EV/MaxLoss, while keeping the package spread at ~4% of
    # credit — comfortably inside the 8% liquidity gate, which a 0.85 fraction
    # is not (it solves to a 2-wide whose spread is 8.9% of its own credit).
    "cw_fraction_of_bound": 0.70,
    # Full marks at 1.0 EM out — the spec's own placement rule, and also the
    # practical ceiling: the 1-SD point sits at roughly 16Δ, so inside the
    # 0.20 short-delta gate a candidate can reach at most ~0.84 EM. A higher
    # bar here would be unreachable by construction and would silently cap the
    # composite ~12 points below 100.
    "em_headroom_full":  1.0,
    "alpha_full":        0.10,   # EV/MaxLoss of 10% = full marks on credit quality
    "vrp_full":          1.35,   # IV/RV at which volatility richness saturates
    "oi_full":           1000,   # per-leg OI at which liquidity depth saturates
    "size_full":         10,     # contracts at the touch for full marks
    "max_width_factor":  0.25,   # widest spread we will solve for, as a fraction of spot
    "fill_slippage":     0.50,   # credit_fill = mid − slippage × (package spread / 2)
}

# Decision bands over the 0–100 composite.
DECISION: dict[str, float] = {"ready": 70.0, "watch": 50.0}

# Earnings mode (opt-in). `max_lift` = how many points the CACHED, aligned
# earnings bias may add to (or, when opposed, subtract from) the score; the
# earnings event stops hard-vetoing while the mode is on. `bias_ttl_seconds` =
# how long the on-demand route reuses a cached verdict before recomputing.
EARNINGS: dict[str, float] = {
    "max_lift":          15.0,
    "bias_ttl_seconds":  21600.0,   # 6h — earnings news evolves daily, not intraday
}

# Position management, surfaced with every suggestion so the user knows the exit
# before entering. Simmer never places orders (that's Torque) — it records the
# targets in the payload so the hand-off carries them.
MANAGEMENT: dict[str, float] = {
    "profit_target_pct": 50.0,   # close at 50% of max profit
    "manage_dte":        21,     # manage at 21 DTE regardless of P&L
    "stop_credit_multiple": 2.0, # stop at 2× credit received
}

# ── Social / sharing (frontend chips; display-only, no secrets) ─────────────
# Served to the browser via GET /simmer/config so the UI reads it at runtime:
# change these in simmer/simmer_tickers.json (the SAME override file as the gates) and
# restart the backend to publish — no frontend rebuild.
#
# Chips default HIDDEN (enabled False, empty URLs): linking to an unregistered
# handle would send users to a page a squatter could claim. Fill the real URLs +
# set enabled True once the accounts exist. The snapshot Share button is
# independent (share_enabled) — it posts from the USER's own account, so it needs
# no product handle and is on by default.
SOCIAL: dict[str, Any] = {
    "enabled":            False,   # master switch for the X / LinkedIn profile chips
    "handle":             "",      # display/title only, e.g. "@edgelane_simmer"
    "x_url":              "",      # product page,  e.g. "https://x.com/edgelane_simmer"
    "linkedin_url":       "",      # product page,  e.g. "https://www.linkedin.com/company/edgelane"
    "share_enabled":      True,    # the snapshot Share button
    # Composer URLs the Share button opens (image-paste target); rarely changed.
    "x_share_url":        "https://x.com/compose/post",
    "linkedin_share_url": "https://www.linkedin.com/feed/?shareActive=true",
}

# ── Volatility estimation ──────────────────────────────────────────────────
VOL: dict[str, Any] = {
    "annualization_days":   252,   # trading days
    "yz_window":            20,    # Yang-Zhang lookback (bars)
    "iv_history_days":      252,   # IV percentile lookback
    "iv_rank_window_days":  252,   # 52 weeks
    "min_history_days":     120,   # below this, IVR/IVP are PROVISIONAL, not silent
    "cross_sectional_blend": 0.50, # blend weight of cross-sectional VRP pct vs IVP
    "risk_free_rate":       0.0,   # r — the engine is rate-agnostic by default
    "dividend_yield":       0.0,   # q
    "ev_integration_step":  0.01,  # $0.01 payoff integration increments
    "ev_max_steps":         200_000,
    "days_per_year":        365.0, # EM/DTE are CALENDAR days — IV30 is 30 calendar days
}

# ── Watcher cadence + two-tier cache TTLs (seconds; 0 = sweep-only) ────────
CADENCE: dict[str, int] = {
    "sweep_seconds":       300,    # 5-minute readiness sweep
    "news_seconds":        900,    # headlines don't arrive faster than this
    "catalyst_seconds":    86_400, # earnings dates move rarely
    "iv_rank_seconds":     86_400, # a rank over DAILY history; intraday recompute is meaningless
    "premarket_lead_min":  30,
}

# Tier 1 = research (expensive, `symbol`-keyed, amortised across users).
# Tier 2 = readiness (cheap pure math, `(symbol, expiration)`-keyed) and is
# NEVER cached across sweeps — a stale "ready" would tell a second user to sell
# a spread whose credit has since collapsed.
TTL: dict[str, int] = {
    "chain":             0,        # sweep only — the one thing that must never be stale
    "walls":             0,        # pure math over the chain already in hand
    "headline_sentiment": -1,      # permanent, keyed by article id (Gemini cost control)
    "ticker_sentiment":  900,
    "velocity":          900,
    "catalysts":         86_400,   # but see MANDATORY invalidation below
    "iv_rank":           86_400,
    "realized_vol":      86_400,
    "flow_vol_oi":       0,
    "flow_delta_oi":     86_400,   # OI settles overnight at OCC
    "readiness":         0,        # tier 2 — recomputed every sweep, never cached
}

# A stale "no catalyst" is precisely the dangerous value, so tier-1 research is
# invalidated immediately regardless of age when any of these fire.
CACHE_INVALIDATORS: tuple[str, ...] = (
    "hard_block_filing", "velocity_burst", "earnings_date_changed", "chain_sanity_failed",
)

# ── News velocity ──────────────────────────────────────────────────────────
# Negative-binomial upper-tail p-value on deduped cluster counts over a rolling
# 60-min window, against a seasonally-matched, empirical-Bayes-shrunk baseline.
# A z-score is NOT comparable between a 2-article/day small cap and a
# 50-article/day mega cap; an exact tail probability is.
VELOCITY: dict[str, Any] = {
    "window_minutes":   60,     # 12 × 5-min buckets, rolling every 5 min
    "bucket_minutes":   5,
    "lookback_days":    20,     # trading days of baseline depth
    "guard_buckets":    6,      # EARS-C2 guard band (30 min) — a burst can't inflate its own baseline
    "dedup_jaccard":    0.70,
    "dedup_window_hours": 6,
    "min_clusters":     3,      # hard gate — kills the one-article-is-∞σ failure
    "alert_p":          0.001,  # ≈1 false alert per ticker per 13 days
    "warn_p":           0.01,
    "hysteresis_exit_p": 0.05,  # burst stays "on" until p > this for N polls
    "hysteresis_polls": 3,
    "dispersion_min_phi": 3.0,  # expected NB dispersion range on 5-min buckets;
    "dispersion_max_phi": 10.0, # MEASURE it on our own feed before trusting a threshold
    # Empirical-Bayes prior strength: pseudo-window count of the pooled Gamma
    # prior (and the fallback strength when the cross-section shows no
    # dispersion beyond Poisson noise, v <= m). Small vs the 20 baseline
    # windows a dense ticker carries — sparse tickers get pulled to the pooled
    # rate, dense ones barely move.
    "eb_prior_weight":  5.0,
}

# ── Sentiment (Heston & Sinha asymmetry) ───────────────────────────────────
# Positive sentiment decays with a ~1-week half-life and is gone by week 3;
# negative sentiment is still significant at week 13. Sentiment SELECTS THE SIDE
# and CAN VETO — it never promotes. The half-lives are fits to published
# coefficient decay, not numbers any paper states.
SENTIMENT: dict[str, Any] = {
    "halflife_pos_days":  5,     # trading days
    "halflife_neg_days":  35,
    "index_halflife_days": 1,    # index-level sentiment is a 1-day impulse with reversal
    "day0_discount":      0.25,  # day-0 is contemporaneous impact + reversal, not predictive
    "trailing_days":      5,     # feed sentiment averaged over 5 trailing days
    "recency_halflife_days": 2.0, # exponential recency weight inside the trailing window
    "negative_veto":     -0.35,  # fresh negative below this blocks bull_put
    "negative_penalty":  -0.10,  # softer: suppress rather than block
    "velocity_suppression": 0.50, # score multiplier on the sentiment component during a burst
    # Negative-sentiment persistence is realized AT the next earnings print. If
    # the spread expires before that date, the long half-life is unrealizable —
    # fall back to the short one. Required, not optional.
    "earnings_gates_neg_halflife": True,
}

# ── Squeeze guard (call side only — forced covering only pushes price UP) ──
SQUEEZE: dict[str, float] = {"dtc_veto": 7.0, "dtc_warn": 5.0}

# ── Market regime (index-level, computed once per sweep) ───────────────────
# Backwardation carries both the highest expected return AND the largest
# drawdowns, so the bands are a SIZING question first and a veto only at the
# extreme. `suppress` names the structures blocked in that band.
REGIME: dict[str, Any] = {
    "bands": [
        {"state": "contango",      "max_vix_ts": 0.95, "multiplier": 1.00, "size": 1.00, "suppress": []},
        {"state": "transition",    "max_vix_ts": 1.00, "multiplier": 0.92, "size": 0.66, "suppress": []},
        {"state": "backwardation", "max_vix_ts": 1.10, "multiplier": 0.80, "size": 0.33,
         "suppress": ["bull_put"]},
        {"state": "stressed",      "max_vix_ts": None, "multiplier": 0.60, "size": 0.00,
         "suppress": ["bull_put", "iron_condor"]},
    ],
    "gamma_flip_penalty": 0.90,     # spot below the index volatility trigger
    "dispersion_threshold": 0.35,   # share of watched names with an IV spike → sector shock
    "strictness_multiplier": {"relaxed": 1.05, "balanced": 1.00, "strict": 0.90},
}

# ── Execution fees (per contract, PER LEG, one way) ────────────────────────
DEFAULT_COMMISSION = 0.35
COMMISSION_PER_CONTRACT: dict[str, float] = {}

# ── Tickers ────────────────────────────────────────────────────────────────
# Simmer trades SINGLE NAMES. Index products are excluded from *trading*, not
# from *data* — the regime gate needs SPY/QQQ/VIX chains every sweep.
TICKERS: list[str] = [
    "AAPL", "AMD", "AMZN", "AVGO", "GOOGL", "META", "MSFT", "MU", "NFLX", "NVDA",
    "PLTR", "SMCI", "TSLA",
]
INDEX_DATA_SYMBOLS: list[str] = ["SPY", "QQQ", "VIX"]   # data only, never traded
BLOCKED_TICKERS: list[str] = ["SPX", "NDX", "RUT", "DJX", "VIX"]

_BASE_RULE: dict[str, Any] = {
    "eligible":           True,
    "tick":               0.01,
    "min_open_interest":  GATES["min_open_interest"],
    "wall_strength_floor": "low",       # low|medium|high
    "structures":         ["bull_put", "bear_call", "iron_condor"],
    "is_index":           False,        # index underlyings use the 1-day sentiment half-life
    "squeeze_veto":       True,
}

# Per-ticker overrides. Some names have chains too thin to sell — set
# `eligible: false` rather than letting the liquidity gate discover it every
# sweep.
_TICKER_OVERRIDES: dict[str, dict[str, Any]] = {
    "NVDA": {"min_open_interest": 1000},
    "TSLA": {"min_open_interest": 1000},
    "SPY":  {"is_index": True, "eligible": False},   # data only
    "QQQ":  {"is_index": True, "eligible": False},
}


# ── Override-file plumbing (mirrors torque_config) ─────────────────────────
def _deep_merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _load_overrides_file() -> dict[str, Any]:
    """Optional JSON override file, deep-merged over the module defaults.

    Discovery order: `$SIMMER_TICKERS_CONFIG`, then, for every ancestor of this
    file, `simmer/simmer_tickers.json` (where it lives in the dev tree) followed
    by `simmer_tickers.json` beside that ancestor. Walking `parents` (rather than
    indexing `parents[3]`) is deliberate — in the container the package sits at
    `/srv/app` and `parents[3]` does not exist.

    The bare-`simmer_tickers.json` fallback is kept so an existing deployment
    that still has the file at the repo root keeps working after the move."""
    path = os.environ.get("SIMMER_TICKERS_CONFIG")
    candidates = [Path(path)] if path else []
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidates.append(parent / "simmer" / "simmer_tickers.json")
        candidates.append(parent / "simmer_tickers.json")
    for p in candidates:
        try:
            if p and p.is_file():
                return json.loads(p.read_text())
        except Exception:
            continue
    return {}


def _merged(section: str, defaults: dict) -> dict:
    over = _load_overrides_file().get(section)
    return _deep_merge(defaults, over) if isinstance(over, dict) else dict(defaults)


# ── Accessors ──────────────────────────────────────────────────────────────
def weights() -> dict[str, float]:
    """Composite weights, normalized to sum to 1.0 so an override that forgets
    to rebalance still produces a 0–100 score."""
    w = _merged("weights", WEIGHTS)
    total = sum(float(v) for v in w.values()) or 1.0
    return {k: float(v) / total for k, v in w.items()}


def sub_weights() -> dict[str, dict[str, float]]:
    return _merged("sub_weights", SUB_WEIGHTS)


def gates() -> dict[str, Any]:
    return _merged("gates", GATES)


def targets() -> dict[str, float]:
    return _merged("targets", TARGETS)


def decision_bands() -> dict[str, float]:
    return _merged("decision", DECISION)


def earnings() -> dict[str, float]:
    return _merged("earnings", EARNINGS)


def management() -> dict[str, float]:
    return _merged("management", MANAGEMENT)


def social() -> dict[str, Any]:
    """Frontend social/share config (display-only). Overridable in
    simmer_tickers.json under "social"; see SOCIAL for the shape."""
    return _merged("social", SOCIAL)


def vol() -> dict[str, Any]:
    return _merged("vol", VOL)


def cadence() -> dict[str, int]:
    return _merged("cadence", CADENCE)


def ttls() -> dict[str, int]:
    return _merged("ttl", TTL)


def velocity() -> dict[str, Any]:
    return _merged("velocity", VELOCITY)


def sentiment() -> dict[str, Any]:
    return _merged("sentiment", SENTIMENT)


def squeeze() -> dict[str, float]:
    return _merged("squeeze", SQUEEZE)


def regime() -> dict[str, Any]:
    return _merged("regime", REGIME)


def user_clamps() -> dict[str, tuple[float, float]]:
    over = _load_overrides_file().get("user_clamps")
    out = {k: tuple(v) for k, v in USER_CLAMPS.items()}
    if isinstance(over, dict):
        for k, v in over.items():
            try:
                out[k] = (float(v[0]), float(v[1]))
            except Exception:
                continue
    return out


def clamp_setting(name: str, value: float) -> float:
    """Hard-clamp a user-supplied knob into its allowed band. Server-side
    enforcement is authoritative — a client asking for a 0.05 short delta is
    clamped, not honoured."""
    lo, hi = user_clamps().get(name, (float("-inf"), float("inf")))
    return max(lo, min(hi, float(value)))


def tickers() -> list[str]:
    f = _load_overrides_file().get("tickers")
    return list(f) if isinstance(f, list) and f else list(TICKERS)


def index_data_symbols() -> list[str]:
    f = _load_overrides_file().get("index_data_symbols")
    return list(f) if isinstance(f, list) and f else list(INDEX_DATA_SYMBOLS)


def blocked_tickers() -> set[str]:
    f = _load_overrides_file().get("blocked_tickers")
    src = f if isinstance(f, list) and f else BLOCKED_TICKERS
    return {str(t).upper() for t in src}


def ticker_rule(symbol: str) -> dict[str, Any]:
    """Resolved per-ticker rule: base ⊕ ticker-override ⊕ file-override.

    Returns e.g. {"eligible": True, "tick": 0.01, "min_open_interest": 1000,
    "wall_strength_floor": "low", "structures": [...], "is_index": False,
    "squeeze_veto": True, "commission": 0.35, "symbol": "NVDA"}."""
    tk = (symbol or "").upper()
    rule = _deep_merge({}, _BASE_RULE)
    rule = _deep_merge(rule, _TICKER_OVERRIDES.get(tk, {}))
    file_over = _load_overrides_file().get("overrides", {})
    if isinstance(file_over, dict) and isinstance(file_over.get(tk), dict):
        rule = _deep_merge(rule, file_over[tk])
    if tk in blocked_tickers():
        rule["eligible"] = False
        rule["ineligible_reason"] = "index_product_excluded_from_trading"
    rule["symbol"] = tk
    rule["commission"] = commission_per_contract(tk)
    return rule


def commission_per_contract(symbol: str) -> float:
    """Per contract, PER LEG, one way. The round-trip cost of a 2-leg vertical
    is therefore 4 × this, and it is what the dollar-friction gate measures
    against."""
    tk = (symbol or "").upper()
    f = _load_overrides_file().get("commissions", {})
    if isinstance(f, dict) and tk in f:
        return float(f[tk])
    return float(COMMISSION_PER_CONTRACT.get(tk, DEFAULT_COMMISSION))


def engine_version() -> str:
    return str(_load_overrides_file().get("engine_version") or ENGINE_VERSION)


def resolved(symbol: str | None = None) -> dict[str, Any]:
    """The whole merged config in one dict — what `simmer_engine` takes so the
    engine stays a pure function of (inputs, cfg) and a test can inject a fully
    synthetic configuration."""
    cfg: dict[str, Any] = {
        "engine_version": engine_version(),
        "weights":        weights(),
        "sub_weights":    sub_weights(),
        "gates":          gates(),
        "targets":        targets(),
        "decision":       decision_bands(),
        "earnings":       earnings(),
        "management":     management(),
        "vol":            vol(),
        "cadence":        cadence(),
        "ttl":            ttls(),
        "velocity":       velocity(),
        "sentiment":      sentiment(),
        "squeeze":        squeeze(),
        "regime":         regime(),
        "locked_gates":   list(LOCKED_GATES),
        "toggleable_gates": list(TOGGLEABLE_GATES),
    }
    if symbol:
        cfg["ticker"] = ticker_rule(symbol)
    return cfg

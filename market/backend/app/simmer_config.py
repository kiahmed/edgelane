"""Simmer readiness-engine configuration — code defaults ⊕ Supabase setup.

Follows the **Torque config precedent** exactly (`torque_config.py`): the code
holds the algorithm's built-in defaults; everything that is *data* rather than
algorithm lives in two Supabase tables (migration 0022) and is read into an
in-memory snapshot, loaded in `main.py` before the sweep starts and refreshed
every `SIMMER_SETUP_REFRESH_SEC`, so an edit reaches every backend container
without a redeploy and nothing per-ticker is hardcoded or read from a JSON file:

  * `public.simmer_tickers_setup`   — one row per symbol (default universe,
    data-only symbols, blocked names, per-ticker rule, commission, spot symbol).
  * `public.simmer_engine_settings` — one row per section (`weights`, `gates`,
    …, `engine`), each **deep-merged over the code defaults** below.

A failed refresh keeps the last good copy. The config **fails closed** until the
first successful load: `setup_loaded()` is False and the watcher skips its sweep
rather than scoring with the code defaults (which differ from production —
`dte_min` 7 in code vs 0 in use — so a silent fallback would change verdicts).

Everything the engine reads goes through an accessor reading that snapshot, so
`simmer_engine` stays a pure function of (inputs, cfg) and no caller changed.
`resolved()` returns the whole merged blob in one dict, which is what
`simmer_engine.evaluate_readiness` takes so a test can inject a synthetic config.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from typing import Any

log = logging.getLogger("edgelane.simmer.config")

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
    # Short-tenor IV gate (docs/simmer_dte_tiers.md §1). At 0-5 DTE, when a live
    # IV-change signal is present, the engine gates on it INSTEAD of the 252-day
    # percentile: IV richening into a catalyst (change ≥ this) is a GO, not a
    # veto. A negative change (premium bleeding out post-event) still vetoes.
    # 0.0 = "IV must not be falling." Paper-calibration pending (per docs §7).
    "iv_change_floor":                0.0,
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
# edit the `social` section row in public.simmer_engine_settings (it refreshes
# into every container within SIMMER_SETUP_REFRESH_SEC) — no redeploy, no
# frontend rebuild.
#
# Chips default HIDDEN (enabled False, empty URLs): linking to an unregistered
# handle would send users to a page a squatter could claim. Fill the real URLs +
# set enabled True once the accounts exist. The snapshot Share button is
# independent (share_enabled) — it posts from the USER's own account, so it needs
# no product handle and is on by default.
SOCIAL: dict[str, Any] = {
    "enabled":            False,   # master switch for the X / LinkedIn profile chips
    "handle":             "",      # display/title only, e.g. "@facades_simmer"
    "x_url":              "",      # product page,  e.g. "https://x.com/facades_simmer"
    "linkedin_url":       "",      # product page,  e.g. "https://www.linkedin.com/company/facades"
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

# ── DTE tiers (docs/simmer_dte_tiers.md) ───────────────────────────────────
# A 0DTE and a 45DTE are different trades that today pass through the SAME vol
# gates. These tiers make the vol reference and the catalyst weight depend on
# tenor. Every boundary here is tunable so the seams (0-1 vs 2-5 vs 6-21) can be
# re-cut from calibration without a code change — they are judgment calls today
# (docs §"Open decisions"). Paper-calibration pending before any tier promotes
# from advisory to gating.
#
# CRITICAL invariant: the 6-21 and 22-45 tiers are NOT listed in `short_tiers`,
# so they never take the intraday branch and stay byte-identical to the current
# daily engine — the theta sweet spot the doc says explicitly not to touch (§3).
DTE_TIERS: dict[str, Any] = {
    # Inclusive upper DTE bound per tier, ascending. A DTE above the last bound
    # falls to `long_tier`. Calendar DTE, matching the engine's `dte`.
    "bounds": [
        {"tier": "0-1",   "max_dte": 1},
        {"tier": "2-5",   "max_dte": 5},
        {"tier": "6-21",  "max_dte": 21},
        {"tier": "22-45", "max_dte": 45},
    ],
    "long_tier": "long",
    # Tiers that take the short-tenor vol branch (intraday RV + IV-change),
    # and ONLY when those inputs are actually injected — otherwise they fall back
    # to the daily path, so a missing-data 0-5DTE behaves exactly as today.
    "short_tiers": ["0-1", "2-5"],
    # 2-5 blends the intraday RV estimate with the daily 20d YZ so a single
    # catalyst bar can't dominate but recent movement still counts (§2). Weight
    # on rv_intraday; the remainder on YZ. Reasoned split, not a fit.
    "short_rv_blend": 0.5,
    # §6 debit handoff: the tier at which a cheap-IV / live-catalyst setup earns
    # a non-blocking "consider debit" notice instead of a bare veto. Governed by
    # the bounds above (default: the "long" bucket, i.e. DTE > 45).
    "debit_handoff_tier": "long",
    # Cheap IV for the handoff: VRP below this (premium not compensating) OR IVP
    # below the gate floor counts as "cheap". VRP<1 = IV under realized.
    "debit_handoff_vrp": 1.0,
}

# ── Catalyst weight by DTE (docs/simmer_dte_tiers.md §5) ────────────────────
# A catalyst's relevance to a CREDIT trade is inversely related to tenor: it is
# the whole trade at 0DTE and mostly priced by 45DTE. This multiplier scales the
# news `sentiment_lean` PENALTY and the earnings fold up at short tenor.
#
# It is 1.0 (today's behavior) at and beyond `neutral_dte`, so 6-21 / 22-45 stay
# byte-identical to the current engine. The doc's illustrative ">21 tapering to
# ~0 by 45" is deliberately NOT applied to the medium-tier weight (doing so would
# retune the theta sweet spot and break parity); at long tenor that intent is
# instead expressed by the §6 debit handoff. The multiplier only ever amplifies a
# penalty — it can never lift a component above its neutral 1.0, so catalyst can
# steer/veto harder but still NEVER promotes a credit sell on its own.
# Paper-calibration pending.
CATALYST: dict[str, Any] = {
    "max_weight":   3.0,   # multiplier at 0 DTE (heaviest)
    "neutral_dte":  6.0,   # at/after this DTE the multiplier is exactly 1.0
    # Short-tier side steering (§1, §4): wire the catalyst SIGN into side
    # selection for 0-5 DTE. Positive → prefer bull_put, negative → bear_call,
    # neutral + rich IV → iron_condor. Steering only reorders the already-built,
    # already-gated candidates; it never fabricates or promotes one.
    "steer_short_tiers": True,
    "neutral_band":      0.10,   # |sentiment| ≤ this → treated as neutral
    "iron_condor_min_vrp": 1.35, # neutral + VRP ≥ this → prefer the condor
}

# ── Intraday feed (docs/simmer_dte_tiers.md "Prerequisite") ────────────────
# The live data path that populates `research.rv_intraday` and
# `research.iv_change` during market hours, so the short (0-1 / 2-5 DTE) tiers
# activate for real instead of falling back to the daily gates. Off-hours and on
# any provider failure the fields stay absent and the engine uses the daily vol
# reference — the short tiers never gate on stale intraday data.
INTRADAY: dict[str, Any] = {
    "intraday_feed_enabled": True,   # master switch; False = daily-only, as before
    "interval":             "5m",    # Yahoo v8 chart interval for the session bars
    # Bars in a full 6.5h regular session at `interval` — the intraday-RV
    # annualization is `bars_per_day × annualization_days`. 78 = 5-min bars.
    # Retune alongside `interval`.
    "bars_per_day":         78,
    "min_bars":             4,       # need ≥ this many intraday bars for an RV
    # iv_change definition (must match what the short-tier gate expects: >0 =
    # richening). "session_first" = (latest − first)/first, a NORMALIZED
    # fractional change vs the session's opening ATM-IV snapshot (unitless, so a
    # $50 and a $500 name share one floor). Alternative "vol_points" = latest −
    # first in absolute IV. Needs ≥ 2 snapshots today, else None. The gate floor
    # itself lives in GATES["iv_change_floor"]. Paper-calibration pending.
    "iv_change_baseline":   "session_first",
}

# ── Watcher cadence + two-tier cache TTLs (seconds; 0 = sweep-only) ────────
CADENCE: dict[str, int] = {
    "sweep_seconds":       300,    # 5-minute readiness sweep
    "news_seconds":        900,    # headlines don't arrive faster than this
    "catalyst_seconds":    86_400, # earnings dates move rarely
    "iv_rank_seconds":     86_400, # a rank over DAILY history; intraday recompute is meaningless
    "premarket_lead_min":  30,
    # Age bound on the market-closed FREEZE: a stored readiness older than this
    # is NOT served frozen — it's recomputed off the last chain instead. Without
    # it, a pin that rolls onto a previously-seen expiry resurrects a weeks-old
    # verdict (e.g. a month-old "watch"). 96h keeps "the last session" served
    # across a normal weekend (Fri 16:00 → Mon 09:30 ET ≈ 65.5h) and a 3-day
    # holiday weekend (Fri → Tue open ≈ 89.5h), but never a week-plus-old one.
    "freeze_max_age_hours": 96,
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
# ── News fetch escalation (docs/simmer.md "News, sentiment, and catalysts") ──
# Per-symbol, per-refresh window widen before falling back to the secondary
# provider (simmer_news.refresh_news). Reasoned starting points, calibration-
# pending — not fitted.
NEWS: dict[str, Any] = {
    "primary_hours": 24,   # primary provider's first look-back window
    "widen_hours":   36,   # widen the PRIMARY to this before pulling the fallback
}

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

# ── Per-ticker base rule ────────────────────────────────────────────────────
# The algorithm's built-in per-ticker defaults. A simmer_tickers_setup row
# overrides any of these for a symbol; a symbol with no row gets exactly this.
_BASE_RULE: dict[str, Any] = {
    "eligible":            True,
    "tick":                0.01,
    "wall_strength_floor": "low",       # low|medium|high
    "structures":          ["bull_put", "bear_call", "iron_condor"],
    "is_index":            False,        # index underlyings use the 1-day sentiment half-life
    "squeeze_veto":        True,
    "sector":              "unknown",
}

# Engine-settings sections: the code default each table row is deep-merged over.
_SECTION_DEFAULTS: dict[str, dict] = {
    "weights": WEIGHTS, "sub_weights": SUB_WEIGHTS, "gates": GATES, "targets": TARGETS,
    "decision": DECISION, "earnings": EARNINGS, "management": MANAGEMENT, "social": SOCIAL,
    "vol": VOL, "dte_tiers": DTE_TIERS, "catalyst": CATALYST, "intraday": INTRADAY,
    "cadence": CADENCE, "ttl": TTL, "velocity": VELOCITY, "news": NEWS,
    "sentiment": SENTIMENT, "squeeze": SQUEEZE, "regime": REGIME,
}


def _deep_merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


# ── In-memory setup snapshots (Supabase; same pattern as torque_config) ─────
# simmer_tickers_setup: ticker -> row, in sort order (dict keeps insertion order).
# simmer_engine_settings: section -> settings dict.
_TICKERS: dict[str, dict[str, Any]] = {}
_ENGINE: dict[str, dict[str, Any]] = {}
_STATE: dict[str, Any] = {"loaded": False, "loaded_at": 0.0, "_prev_fp": None}


def apply_tickers_setup(rows: list[dict[str, Any]]) -> None:
    norm = []
    for r in rows or []:
        t = str(r.get("ticker") or "").strip().upper()
        if not t:
            continue
        norm.append({**r, "ticker": t})
    norm.sort(key=lambda r: (int(r.get("sort_order") or 0), r["ticker"]))
    _TICKERS.clear()
    _TICKERS.update({r["ticker"]: r for r in norm})


def apply_engine_settings(rows: list[dict[str, Any]]) -> None:
    out: dict[str, dict[str, Any]] = {}
    for r in rows or []:
        sec = str(r.get("section") or "").strip()
        if not sec:
            continue
        s = r.get("settings")
        if isinstance(s, str):
            try:
                s = json.loads(s)
            except Exception:
                s = None
        if isinstance(s, dict):
            out[sec] = s
    _ENGINE.clear()
    _ENGINE.update(out)


def apply_setup(tickers_rows, engine_rows) -> None:
    """Replace both in-memory snapshots and mark the setup loaded. Used by the
    refresh loop and, with the migration seed, by the test conftest."""
    apply_tickers_setup(tickers_rows)
    apply_engine_settings(engine_rows)
    _STATE.update(loaded=True, loaded_at=time.time())


async def refresh_setup() -> bool:
    """Reload both snapshots from Supabase. False (snapshots untouched) when
    either read failed — a transient outage must never wipe a good copy or,
    worse, silently swap production config for the code defaults."""
    from . import supabase_admin
    try:
        trows = await supabase_admin.get_simmer_tickers_setup()
        erows = await supabase_admin.get_simmer_engine_settings()
    except Exception as e:
        log.warning("simmer setup read failed: %s", e)
        trows = erows = None
    if trows is None or erows is None:
        if not _STATE["loaded"]:
            log.warning("simmer setup not loaded yet - sweeps skip until it loads")
        return False
    before_fp = _STATE.get("_prev_fp")
    before_ver = engine_version()
    apply_setup(trows, erows)
    after_fp, after_ver = _fingerprint(), engine_version()
    # Calibration honesty: a verdict-affecting change must come with a version
    # bump, or the outcomes table can't tell two engines apart (docs Calibration).
    if before_fp is not None and after_fp != before_fp and after_ver == before_ver:
        log.warning("simmer engine settings changed but engine_version did not - "
                    "calibration can't distinguish the configs; bump the engine row")
    _STATE["_prev_fp"] = after_fp
    return True


async def setup_loop(interval_sec: float) -> None:
    """Background refresh so every container converges on the tables within
    `interval_sec`. Retries every 10s while never loaded (no sweeps until then)."""
    interval = max(5.0, float(interval_sec))
    while True:
        await asyncio.sleep(interval if _STATE["loaded"] else min(10.0, interval))
        await refresh_setup()


def setup_loaded() -> bool:
    return bool(_STATE["loaded"])


# ── Engine-settings accessors (code default over the table section) ─────────
def _section(name: str, default: dict) -> dict:
    over = _ENGINE.get(name)
    return _deep_merge(default, over) if isinstance(over, dict) else dict(default)


def weights() -> dict[str, float]:
    """Composite weights, normalized to sum to 1.0 so an override that forgets
    to rebalance still produces a 0-100 score."""
    w = _section("weights", WEIGHTS)
    total = sum(float(v) for v in w.values()) or 1.0
    return {k: float(v) / total for k, v in w.items()}


def sub_weights() -> dict[str, dict[str, float]]:
    return _section("sub_weights", SUB_WEIGHTS)


def gates() -> dict[str, Any]:
    return _section("gates", GATES)


def targets() -> dict[str, float]:
    return _section("targets", TARGETS)


def decision_bands() -> dict[str, float]:
    return _section("decision", DECISION)


def earnings() -> dict[str, float]:
    return _section("earnings", EARNINGS)


def management() -> dict[str, float]:
    return _section("management", MANAGEMENT)


def social() -> dict[str, Any]:
    """Frontend social/share config (display-only). Overridable in the
    `social` section of simmer_engine_settings; see SOCIAL for the shape."""
    return _section("social", SOCIAL)


def vol() -> dict[str, Any]:
    return _section("vol", VOL)


def dte_tiers() -> dict[str, Any]:
    return _section("dte_tiers", DTE_TIERS)


def catalyst() -> dict[str, Any]:
    return _section("catalyst", CATALYST)


def intraday() -> dict[str, Any]:
    return _section("intraday", INTRADAY)


def cadence() -> dict[str, int]:
    return _section("cadence", CADENCE)


def ttls() -> dict[str, int]:
    return _section("ttl", TTL)


def velocity() -> dict[str, Any]:
    return _section("velocity", VELOCITY)


def news() -> dict[str, Any]:
    return _section("news", NEWS)


def sentiment() -> dict[str, Any]:
    return _section("sentiment", SENTIMENT)


def squeeze() -> dict[str, float]:
    return _section("squeeze", SQUEEZE)


def regime() -> dict[str, Any]:
    return _section("regime", REGIME)


def user_clamps() -> dict[str, tuple[float, float]]:
    over = _ENGINE.get("user_clamps")
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
    enforcement is authoritative - a client asking for a 0.05 short delta is
    clamped, not honoured."""
    lo, hi = user_clamps().get(name, (float("-inf"), float("inf")))
    return max(lo, min(hi, float(value)))


def engine_version() -> str:
    eng = _ENGINE.get("engine") or {}
    return str(eng.get("engine_version") or ENGINE_VERSION)


# ── Ticker accessors (read the simmer_tickers_setup snapshot) ───────────────
def _enabled_rows() -> list[dict[str, Any]]:
    return [r for r in _TICKERS.values() if r.get("enabled", True)]


def tickers() -> list[str]:
    """The default universe (today's TICKERS): enabled rows flagged
    default_watchlist, in sort order. Also the dev fallback when the per-user
    watchlist can't be read."""
    return [r["ticker"] for r in _enabled_rows() if r.get("default_watchlist")]


def index_data_symbols() -> list[str]:
    """Quoted every sweep for the regime gate, never traded (SPY, QQQ, VIX)."""
    return [r["ticker"] for r in _enabled_rows() if r.get("index_data")]


def blocked_tickers() -> set[str]:
    """Can't be traded or added to a watchlist (SPX, NDX, RUT, DJX, VIX)."""
    return {r["ticker"] for r in _TICKERS.values() if r.get("blocked")}


def yahoo_symbol(symbol: str) -> str | None:
    """Spot-quote symbol for this ticker (e.g. ^VIX), or None to use the bare
    ticker. The Simmer-only spot map now lives here, not in simmer_data_provider."""
    row = _TICKERS.get((symbol or "").upper())
    return (row or {}).get("yahoo_symbol") or None


def commission_per_contract(symbol: str) -> float:
    """Per contract, PER LEG, one way. The round-trip cost of a 2-leg vertical
    is 4 x this, and it is what the dollar-friction gate measures against."""
    row = _TICKERS.get((symbol or "").upper()) or {}
    v = row.get("commission_per_contract")
    return float(v) if v is not None else DEFAULT_COMMISSION


def ticker_rule(symbol: str) -> dict[str, Any]:
    """Resolved per-ticker rule: base over the simmer_tickers_setup row. A symbol
    with no row gets the base defaults. Keys match the pre-DB shape so callers
    and verdicts are unchanged."""
    tk = (symbol or "").upper()
    row = _TICKERS.get(tk, {})
    rule = dict(_BASE_RULE)
    rule["min_open_interest"] = int(gates().get("min_open_interest", 250))
    for key in ("eligible", "tick", "wall_strength_floor", "is_index", "squeeze_veto"):
        if row.get(key) is not None:
            rule[key] = row[key]
    if row.get("min_open_interest") is not None:
        rule["min_open_interest"] = int(row["min_open_interest"])
    if isinstance(row.get("structures"), list) and row["structures"]:
        rule["structures"] = list(row["structures"])
    if row.get("sector"):
        rule["sector"] = str(row["sector"])
    if bool(row.get("blocked")) or tk in blocked_tickers():
        rule["eligible"] = False
        rule["ineligible_reason"] = "index_product_excluded_from_trading"
    rule["symbol"] = tk
    rule["commission"] = commission_per_contract(tk)
    return rule


# ── Resolved blob + settings fingerprint ────────────────────────────────────
def resolved(symbol: str | None = None) -> dict[str, Any]:
    """The whole merged config in one dict - what `simmer_engine` takes so the
    engine stays a pure function of (inputs, cfg) and a test can inject a fully
    synthetic configuration."""
    cfg: dict[str, Any] = {
        "engine_version": engine_version(),
        "settings_fingerprint": _fingerprint(),
        "weights":        weights(),
        "sub_weights":    sub_weights(),
        "gates":          gates(),
        "targets":        targets(),
        "decision":       decision_bands(),
        "earnings":       earnings(),
        "management":     management(),
        "vol":            vol(),
        "dte_tiers":      dte_tiers(),
        "catalyst":       catalyst(),
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


def _fingerprint() -> str:
    """A stable hash of the verdict-affecting engine settings (NOT per-ticker),
    recorded on every alert payload and simmer_outcomes row so a config change is
    visible to the calibration loop even when engine_version is unchanged."""
    blob = {
        "weights": weights(), "sub_weights": sub_weights(), "gates": gates(),
        "targets": targets(), "decision": decision_bands(), "earnings": earnings(),
        "vol": vol(), "dte_tiers": dte_tiers(), "catalyst": catalyst(),
        "sentiment": sentiment(), "squeeze": squeeze(), "regime": regime(),
    }
    s = json.dumps(blob, sort_keys=True, default=str)
    return hashlib.sha256(s.encode()).hexdigest()[:16]


def settings_fingerprint() -> str:
    return _fingerprint()

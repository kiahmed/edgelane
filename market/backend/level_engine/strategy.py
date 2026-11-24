"""Strategy agent (§8.4) + Risk/Reward agent (§8.3) — level-anchored picks.

Spec: docs/level_engine.md §2, §8.3, §8.4, §11. Pure (no I/O); the brain calls it
once per tick and returns the pick in BrainOutput.pick.

A THIN ADAPTER over the existing scorers: strikes come from levels, scoring is
`app.strategy_engine.score_vertical / score_butterfly / score_condor`,
`compute_limit_premiums`, `composite_score`, `composite_verdict` — called
unchanged (plus `straddle.py` for the structures they don't cover). Nothing in
`app/` is edited, so the parity suite stays byte-identical.

API
---
    agent = StrategyAgent()            # brain: one per trading day (or call reset_day())
    pick  = agent.pick(ctx, call, up_target, down_target, invalidation)   # dict | None
    build_level_picks(ctx, call, up_target, down_target, invalidation)    # module default agent
    reprice_pick(pick, contracts) -> float | None                          # current mark

`build_level_picks` is a thin wrapper over a module-level default
StrategyAgent (it auto-resets when ctx.trade_date changes). Prefer an explicit
StrategyAgent instance owned by the brain — module state is shared by every
caller in the process (e.g. live + replay in one interpreter).

Pick shape: every legacy `engine_pick` key (app/engine.py) with the same types —
strategy, name, short, spread_type, label, width_preset, composite_score,
composite_verdict, structure_text, net_premium, max_profit, max_loss, pop_pct,
ev, ev_adjusted, health, liquidity, strikes, legs, predicted_direction — plus
source="levels", pick_id, call, reach, target_level, invalidation_level,
candidates, rr, rr_line, expiration, dte, breakevens, limit_premiums, anchor,
plan, rr_profit, health_legacy, strategies (grid; popped by the brain).
(`confidence` / `reason` of §11 are the manager's; the brain adds them.)

Decisions (spec ambiguities, documented)
----------------------------------------
CANDIDATES PER CALL (no pick on wait / done / anything else):
  up    → bull_call, call_butterfly, long_call, straddle, strangle
  down  → bear_put,  put_butterfly,  long_put,  straddle, strangle
  range → iron_condor, call_butterfly, put_butterfly (pin flies at the zone mid)
  Straddle / strangle are LONG-vol: a range call is short-vol, so they never go
  on range. They are breakout plays — the level thesis is "price leaves this
  level for the next one", and both targets are levels — so they ride the
  directional calls as alternates and composite_score ranks them against the
  directional structures. No credit verticals ever (§8.4); the condor is the
  only credit structure and only on range.
  Reach siblings (label "Extreme", reach="extreme"): long_call / long_put /
  strangle struck at the day's highest / lowest cluster (§8.4 strangle extreme,
  §15 reach selector). They are listed in `candidates` and used for rr.extreme
  of their family, but are never the best pick (best = top composite among
  reach="near").
STRIKES (nearest LISTED strike present in the chain, half-up on ties; a
  candidate whose leg strike/side is missing or has no usable quote — bid <= 0
  or ask <= 0 — is skipped that tick):
  * long leg (up/down): the nearest listed strike that is NOT in the money —
    call: the lowest strike >= spot, put: the highest strike <= spot (ATM or
    OTM, toward the level). Revised 2026-11-24: the nearest-to-spot rule could
    pick an ITM put (spot 7268 → 7270P), and a frozen strike drifted deeper ITM
    as price moved, which priced the debit up and broke its R/R. `anchor`
    records "level" when that strike is also the zone level's strike.
  * short leg / fly centre: the target level's strike (T_up / T_down; when the
    scenario gives none, the next cluster beyond spot ± BUFFER_PTS).
  * fly wings: the LEGACY width convention — width_base_for_dte(dte, EM) ×
    WIDTH_PREFS['balanced'] (0.4·EM on 0DTE), resolved with the legacy
    `find_closest_strike` on each side of the centre.
  * straddle: strike of the cluster nearest spot. Strangle (near): call at the
    next cluster above spot + BUFFER_PTS, put at the next below spot − BUFFER_PTS.
    Extreme strangle: call at the highest cluster, put at the lowest.
  * condor (range): shorts at the zone's two levels (put at the lower, call at
    the upper), longs ONE listed strike outside each short.
  * range flies: centre = listed strike nearest the zone midpoint.
STRIKE PLAN (§8.4 "fixed for the day"): strikes are resolved lazily per call
  type (up / down / range) the first time that call needs them and then frozen.
  The whole plan is rebuilt only when ctx.levels_changed (or the cluster set /
  prices differ from the plan's), the trade date changes, or EVERY cluster on
  one side of the plan's anchor spot has been passed (spot beyond it by more
  than BUFFER_PTS). Quotes are re-read every tick at the frozen strikes.
  ITM RE-STRIKE: when a frozen directional long leg (bull_call / bear_put /
  long_call / long_put) is in the money at the current spot, that call's
  strikes are re-resolved (plan.reason "long_leg_itm"). Strikes only ever move
  in the call's direction, so price oscillating around a strike can't
  flip-flop them.
SCORING: no GEX wall penalty — levels replace walls as the structure input, so
  wall_penalty = neutral (factor 1.0) and ev_adjusted = ev. label "Near" /
  "Extreme"; width_preset "levels".
HEALTH = R/R (§8.4 revised): the reused scorer's health (which marks every
  debit 'broken' at dte <= 2) is kept as `health_legacy`; `health` is set from
  rr_profit = (value_at_target − cost) / cost on rr.near (rr.extreme when near
  is not computed): >= 0.60 healthy, 0.30–0.60 thin, < 0.30 capital_trap, None
  without rr. No DTE veto. It is set on EVERY candidate BEFORE composite_score,
  so the badge term ranks on it. Limit premiums are computed as before.
GRID (C1): pick["strategies"] = the legacy `strategies` dict (one card per
  strategy key; best = near candidate, all_widths = [best, extreme sibling]);
  full candidates carry every engine_pick key + type, limit_premiums, pick_id,
  rr, rr_line, rr_profit, reach. The brain moves it to decision["strategies"].
RISK / REWARD (§8.3, §11; projection.py has the port notes): for the pick and
  each candidate, near = spot at the call's target level (range: the zone
  midpoint — "stays in range"), extreme = the day's highest (up) / lowest (down)
  cluster (none on range); time = the midpoint of now → 16:00 ET snapped to the
  Matrix grid's quarter-hours (RR_TIME_FRACTION). rr.extreme uses the family's
  extreme sibling when one was built, else the pick itself. cfg.reach limits
  which are computed ('near' | 'extreme' | 'both'); the other key is None.
  HEALTH HYSTERESIS: a candidate keeps last tick's R/R band while rr_profit
  is within cfg.health_hysteresis of it (an R/R on a band edge flipped
  thin ↔ capital_trap each tick: a ±25-point composite swing).
  PICK STICKINESS: the previous best (same pick_id) stays the pick unless a
  challenger in the gated pool beats its composite by cfg.pick_switch_margin
  points — near-tied candidates (call fly vs put fly on range) otherwise swap
  every few ticks and the Matrix pick card churns.
  R/R GATE (§8.3 "informs the pick"): the best pick is the top composite among
  near candidates whose projected P&L at the call's own target (rr.near, or
  rr.extreme when REACH=extreme) is >= 0 — a structure that loses money when
  the call plays out (e.g. a straddle struck AT the target level) contradicts
  the call. None passing → top composite anyway, `rr_gate`="none_passed".
  rr_line: "Near: risk $310 → +$900 at 7,280 · Extreme: risk $85 → +$1,400 at
  7,320" — risk = cost ($, whole dollars), the signed amount = projected P&L
  ($), target = level price, thousands separators, JS-style half-up rounding.
PICK ID: sha1("trade_date|strategy|sorted leg OCC symbols")[:16] — stable
  across ticks while the strikes are fixed.
"""
from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field
from datetime import date, datetime

from app.lookup_tables import STRATEGIES, WIDTH_PREFS
from app.strategy_engine import (_get_mid, compute_limit_premiums, composite_score,
                                 composite_verdict, find_closest_strike, score_butterfly,
                                 score_condor, score_vertical, width_base_for_dte)

from .context import TickContext
from .levels import Cluster
from .projection import minutes_to_expiry, risk_reward
from .straddle import score_long_option, score_straddle_strangle

RR_TIME_FRACTION = 0.5
HEALTH_HEALTHY, HEALTH_THIN = 0.60, 0.30          # rr_profit bands (§8.4 revised)
NEUTRAL_WALL = {"factor": 1.0, "reason": None, "verdict": "neutral"}

NAMES: dict[str, tuple[str, str]] = {
    **{k: (v["name"], v["short"]) for k, v in STRATEGIES.items()},
    "long_call": ("Long Call", "Long Call"),
    "long_put": ("Long Put", "Long Put"),
    "straddle": ("Long Straddle", "Straddle"),
    "strangle": ("Long Strangle", "Strangle"),
}

CANDIDATES_BY_CALL: dict[str, tuple[str, ...]] = {
    "up": ("bull_call", "call_butterfly", "long_call", "straddle", "strangle"),
    "down": ("bear_put", "put_butterfly", "long_put", "straddle", "strangle"),
    "range": ("iron_condor", "call_butterfly", "put_butterfly"),
}
EXTREME_FAMILIES = {"up": ("long_call", "strangle"), "down": ("long_put", "strangle")}
CREDIT_VERTICALS = ("bull_put", "bear_call")      # never built here
DIRECTION = {"up": "up", "down": "down", "range": "flat"}


# ── chain helpers ───────────────────────────────────────────────────────────
class Chain:
    def __init__(self, contracts: list[dict], expiration: str | None = None):
        self.by: dict[tuple[float, str], dict] = {}
        for c in contracts or []:
            if expiration and c.get("expiration") and str(c["expiration"]) != str(expiration):
                continue
            try:
                k = float(c["strike"])
            except (KeyError, TypeError, ValueError):
                continue
            side = c.get("side")
            if side in ("call", "put"):
                self.by[(k, side)] = c
        self.contracts = list(self.by.values())
        self.strikes = sorted({k for k, _ in self.by})

    def snap(self, price: float) -> float | None:
        if not self.strikes or price is None:
            return None
        return min(self.strikes, key=lambda k: (abs(k - price), -k))

    def get(self, strike: float, side: str) -> dict | None:
        return self.by.get((float(strike), side))

    def side(self, side: str) -> list[dict]:
        return [c for c in self.contracts if c.get("side") == side]

    def next_listed(self, strike: float, side: str, direction: int) -> float | None:
        ks = [k for (k, s) in self.by if s == side and (k > strike if direction > 0 else k < strike)]
        if not ks:
            return None
        return min(ks) if direction > 0 else max(ks)


def usable(c: dict | None) -> bool:
    if not c:
        return False
    try:
        return float(c.get("bid") or 0) > 0 and float(c.get("ask") or 0) > 0
    except (TypeError, ValueError):
        return False


def _em(feed: dict, chain: Chain, spot: float) -> float:
    em = feed.get("expected_move")
    try:
        if em is not None and float(em) > 0:
            return float(em)
    except (TypeError, ValueError):
        pass
    k = chain.snap(spot)  # legacy fallback: ATM straddle, else 0.5% of spot
    if k is not None:
        c, p = chain.get(k, "call"), chain.get(k, "put")
        if c and p and _get_mid(c) + _get_mid(p) > 0:
            return _get_mid(c) + _get_mid(p)
    return max(0.01, spot * 0.005)


# ── strike plan ─────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Spec:
    strategy: str
    reach: str                                  # 'near' | 'extreme'
    legs: tuple[tuple[str, float, str], ...]    # (role, strike, side)
    anchor: str = "levels"

    def leg(self, role: str) -> tuple[float, str]:
        for r, k, s in self.legs:
            if r == role:
                return k, s
        raise KeyError(role)


@dataclass
class StrikePlan:
    trade_date: date
    anchor_spot: float
    built_at: datetime
    cluster_sig: tuple
    above: tuple[float, ...]
    below: tuple[float, ...]
    specs: dict[str, list[Spec]] = field(default_factory=dict)
    targets: dict[str, dict] = field(default_factory=dict)
    reason: str = "initial"

    def side_passed(self, spot: float, buffer_pts: float) -> str | None:
        if self.above and all(spot > p + buffer_pts for p in self.above):
            return "above_passed"
        if self.below and all(spot < p - buffer_pts for p in self.below):
            return "below_passed"
        return None

    def to_dict(self) -> dict:
        return {"anchor_spot": self.anchor_spot, "built_at": self.built_at.isoformat(),
                "reason": self.reason, "targets": self.targets,
                "calls_built": sorted(self.specs)}


def _sig(clusters: list[Cluster]) -> tuple:
    return tuple((c.id, round(float(c.price), 6)) for c in sorted(clusters, key=lambda c: (c.price, c.id)))


def _next_beyond(clusters, spot, buffer_pts, direction):
    if direction > 0:
        return next((c for c in sorted(clusters, key=lambda c: c.price) if c.price > spot + buffer_pts), None)
    return next((c for c in sorted(clusters, key=lambda c: -c.price) if c.price < spot - buffer_pts), None)


def otm_long_strike(chain: Chain, side: str, spot: float) -> float | None:
    """Nearest listed strike of `side` that is not in the money: call → lowest
    strike >= spot, put → highest strike <= spot."""
    ks = [k for (k, sd) in chain.by if sd == side and (k >= spot if side == "call" else k <= spot)]
    if not ks:
        return None
    return min(ks) if side == "call" else max(ks)


def is_itm(strike: float, side: str, spot: float) -> bool:
    return strike < spot if side == "call" else strike > spot


DIRECTIONAL_LONGS = ("bull_call", "bear_put", "long_call", "long_put")


def _long_leg_itm(specs: list[Spec], spot: float) -> bool:
    for sp in specs:
        if sp.strategy in DIRECTIONAL_LONGS and sp.reach == "near":
            k, side = sp.leg("long")
            if is_itm(k, side, spot):
                return True
    return False


def _fly_wings(chain: Chain, side: str, center: float, w: float):
    cs = chain.side(side)
    lo = find_closest_strike(cs, side, center - w, lambda c: float(c["strike"]) < center)
    hi = find_closest_strike(cs, side, center + w, lambda c: float(c["strike"]) > center)
    if not lo or not hi:
        return None
    return float(lo["strike"]), float(hi["strike"])


def build_specs(call: str, ctx: TickContext, chain: Chain, up_t: Cluster | None,
                down_t: Cluster | None) -> tuple[list[Spec], dict]:
    """Resolve the frozen strikes for one call type (see module docstring)."""
    spot, b = float(ctx.spot), float(ctx.cfg.buffer_pts)
    clusters = sorted(ctx.clusters, key=lambda c: c.price)
    dte = float(ctx.feed.get("dte") or 0.0)
    w = width_base_for_dte(dte, _em(ctx.feed, chain, spot)) * WIDTH_PREFS["balanced"]["factor"]
    up_t = up_t or _next_beyond(clusters, spot, b, +1)
    down_t = down_t or _next_beyond(clusters, spot, b, -1)
    specs: list[Spec] = []
    targets = {"up": None if up_t is None else up_t.to_dict(),
               "down": None if down_t is None else down_t.to_dict()}
    k_atm = chain.snap(spot)
    if k_atm is None or not clusters:
        return specs, targets
    zone = getattr(ctx.scenario, "zone", None)
    zl = getattr(zone, "lower", None) if call == "up" else getattr(zone, "upper", None)

    def anchor_of(k: float) -> str:
        return "level" if zl is not None and chain.snap(zl.price) == k else "spot"

    def directional(side: str, t: Cluster | None, sgn: int):
        fly = "call_butterfly" if side == "call" else "put_butterfly"
        vert = "bull_call" if side == "call" else "bear_put"
        single = "long_call" if side == "call" else "long_put"
        k_long = otm_long_strike(chain, side, spot)
        if t is not None:
            k_t = chain.snap(t.price)
            if k_long is not None and k_t is not None and (k_t - k_long) * sgn > 0:
                specs.append(Spec(vert, "near", (("long", k_long, side), ("short", k_t, side)),
                                  anchor_of(k_long)))
            if k_t is not None:
                wings = _fly_wings(chain, side, k_t, w)
                if wings:
                    specs.append(Spec(fly, "near", (("low", wings[0], side), ("mid", k_t, side),
                                                    ("high", wings[1], side))))
        if k_long is not None:
            specs.append(Spec(single, "near", (("long", k_long, side),), anchor_of(k_long)))

    def vol_plays():
        near = min(clusters, key=lambda c: (abs(c.price - spot), c.price))
        k_s = chain.snap(near.price)
        specs.append(Spec("straddle", "near", (("call", k_s, "call"), ("put", k_s, "put"))))
        cu, cd = _next_beyond(clusters, spot, b, +1), _next_beyond(clusters, spot, b, -1)
        near_legs = None
        if cu is not None and cd is not None:
            kc, kp = chain.snap(cu.price), chain.snap(cd.price)
            if kp < kc:
                near_legs = (("call", kc, "call"), ("put", kp, "put"))
                specs.append(Spec("strangle", "near", near_legs))
        kc, kp = chain.snap(clusters[-1].price), chain.snap(clusters[0].price)
        if kp < kc and (("call", kc, "call"), ("put", kp, "put")) != near_legs:
            specs.append(Spec("strangle", "extreme", (("call", kc, "call"), ("put", kp, "put"))))

    if call == "up":
        directional("call", up_t, +1)
        vol_plays()
        kx = chain.snap(clusters[-1].price)
        if kx > k_atm and not is_itm(kx, "call", spot):
            specs.append(Spec("long_call", "extreme", (("long", kx, "call"),)))
    elif call == "down":
        directional("put", down_t, -1)
        vol_plays()
        kx = chain.snap(clusters[0].price)
        if kx < k_atm and not is_itm(kx, "put", spot):
            specs.append(Spec("long_put", "extreme", (("long", kx, "put"),)))
    elif call == "range" and up_t is not None and down_t is not None:
        k_up, k_dn = chain.snap(up_t.price), chain.snap(down_t.price)
        if k_dn < k_up:
            lp, lc = chain.next_listed(k_dn, "put", -1), chain.next_listed(k_up, "call", +1)
            if lp is not None and lc is not None:
                specs.append(Spec("iron_condor", "near", (("sp", k_dn, "put"), ("lp", lp, "put"),
                                                          ("sc", k_up, "call"), ("lc", lc, "call"))))
            mid = chain.snap((up_t.price + down_t.price) / 2)
            for side, strat in (("call", "call_butterfly"), ("put", "put_butterfly")):
                wings = _fly_wings(chain, side, mid, w)
                if wings:
                    specs.append(Spec(strat, "near", (("low", wings[0], side), ("mid", mid, side),
                                                      ("high", wings[1], side))))
    order = {s: i for i, s in enumerate(CANDIDATES_BY_CALL.get(call, ()))}
    specs.sort(key=lambda s: (s.reach != "near", order.get(s.strategy, 99)))
    return specs, targets


# ── scoring ─────────────────────────────────────────────────────────────────
def pick_id_for(trade_date, strategy: str, legs: list[dict]) -> str:
    syms = ",".join(sorted(str(l.get("symbol") or f"{l.get('strike')}{l.get('side')}") for l in legs))
    return hashlib.sha1(f"{trade_date}|{strategy}|{syms}".encode()).hexdigest()[:16]


def score_spec(spec: Spec, chain: Chain, spot: float, dte: float, em: float) -> dict | None:
    cs = {}
    for role, k, side in spec.legs:
        c = chain.get(k, side)
        if not usable(c):
            return None
        cs[role] = c
    s = spec.strategy
    if s in CREDIT_VERTICALS:
        return None
    if s in ("bull_call", "bear_put"):
        return score_vertical(cs["short"], cs["long"], "debit", dte)
    if s in ("call_butterfly", "put_butterfly"):
        return score_butterfly(cs["low"], cs["mid"], cs["high"], spec.legs[0][2], dte)
    if s == "iron_condor":
        return score_condor(cs["sp"], cs["lp"], cs["sc"], cs["lc"], dte)
    if s in ("long_call", "long_put"):
        return score_long_option(cs["long"], spot, dte, em, chain.contracts)
    if s in ("straddle", "strangle"):
        return score_straddle_strangle(cs["call"], cs["put"], spot, dte, em, chain.contracts)
    return None


def enrich(scored: dict, spec: Spec, trade_date) -> dict:
    """Level fields + limit premiums + pick_id. Health / composite come later
    (finish), once the candidate's R/R is known (§8.4: health = R/R)."""
    c = dict(scored)
    c["strategy"] = spec.strategy
    c["reach"] = spec.reach
    c["label"] = "Near" if spec.reach == "near" else "Extreme"
    c["width_preset"] = "levels"
    c["anchor"] = spec.anchor
    c["wall_penalty"] = dict(NEUTRAL_WALL)
    c["ev_adjusted"] = c["ev"] * NEUTRAL_WALL["factor"]
    c["limit_premiums"] = compute_limit_premiums(c)
    c["pick_id"] = pick_id_for(trade_date, spec.strategy, c["legs"])
    return c


def rr_health(rr_profit: float | None) -> str | None:
    """§8.4 revised: the legacy debit bands applied to the level target."""
    if rr_profit is None:
        return None
    if rr_profit >= HEALTH_HEALTHY:
        return "healthy"
    if rr_profit >= HEALTH_THIN:
        return "thin"
    return "capital_trap"


def rr_health_sticky(rr_profit: float | None, prev: str | None, hyst: float) -> str | None:
    """rr_health with hysteresis: keep the previous band while rr_profit is
    within `hyst` of it. An R/R sitting on a band edge (0.29 ↔ 0.30) otherwise
    flips thin ↔ capital_trap every tick — a ±25-point composite swing that
    made the pick and the grid churn."""
    new = rr_health(rr_profit)
    if prev is None or new is None or new == prev or hyst <= 0:
        return new
    if prev in (rr_health(rr_profit - hyst), rr_health(rr_profit + hyst)):
        return prev
    return new


def rr_profit_of(rr: dict | None) -> float | None:
    """(value_at_target − cost) / cost on the near reach (extreme when near is
    not computed, e.g. REACH=extreme)."""
    r = (rr or {}).get("near") or (rr or {}).get("extreme")
    if not r or not r.get("cost"):
        return None
    return round((float(r["value"]) - float(r["cost"])) / float(r["cost"]), 4)


def finish(c: dict, prev_health: str | None = None, hyst: float = 0.0) -> dict:
    """R/R health BEFORE composite_score (so the badge ranks on it); the
    scorer's own value is kept as health_legacy. No DTE veto. `prev_health`
    (this candidate's band last tick) + `hyst` apply band hysteresis."""
    c["health_legacy"] = c.get("health")
    c["rr_profit"] = rr_profit_of(c.get("rr"))
    c["health"] = rr_health_sticky(c["rr_profit"], prev_health, hyst)
    c["rr_line"] = format_rr_line(c.get("rr"))
    c["composite_score"] = composite_score(c)
    c["composite_verdict"] = composite_verdict(
        c["composite_score"], c["ev_adjusted"] if c.get("ev_adjusted") is not None else c.get("ev"))
    return c


# ── R/R + formatting ────────────────────────────────────────────────────────
def _r0(x: float) -> int:
    return int(math.floor(x + 0.5)) if x >= 0 else -int(math.floor(-x + 0.5))


def _money(x: float) -> str:
    return f"${_r0(abs(x)):,}"


def format_rr_line(rr: dict) -> str:
    parts = []
    for key, title in (("near", "Near"), ("extreme", "Extreme")):
        r = (rr or {}).get(key)
        if not r:
            continue
        sign = "+" if _r0(r["pnl"]) >= 0 else "−"
        parts.append(f"{title}: risk {_money(r['cost'])} → {sign}{_money(r['pnl'])} "
                     f"at {_r0(r['target']):,}")
    return " · ".join(parts)


def _rr(cand: dict, target: float | None, spot: float, m_now: float, ivs: dict) -> dict | None:
    if target is None:
        return None
    out = risk_reward(cand, target, RR_TIME_FRACTION, spot=spot, minutes_to_expiry_now=m_now,
                      iv_by_leg=ivs)
    if out is not None:
        out["pick_id"] = cand["pick_id"]
        out["structure_text"] = cand["structure_text"]
    return out


def _cl(c: Cluster | None) -> dict | None:
    return None if c is None else {"id": c.id, "price": c.price, "name": c.name, "strike": c.strike}


def compact(c: dict) -> dict:
    return {k: c.get(k) for k in ("pick_id", "strategy", "label", "reach", "structure_text",
                                  "net_premium", "max_profit", "max_loss", "pop_pct", "ev",
                                  "health", "health_legacy", "rr_profit", "liquidity",
                                  "composite_score", "composite_verdict", "strikes", "rr")}


def side_of(strategy: str, call: str) -> str:
    if call == "range" or strategy in ("straddle", "strangle", "iron_condor"):
        return "neutral"
    return "bull" if call == "up" else "bear"


def legacy_fields(c: dict, call: str) -> dict:
    """Every legacy engine_pick key (app/engine.py), same types."""
    return {
        "strategy": c["strategy"],
        "name": NAMES[c["strategy"]][0],
        "short": NAMES[c["strategy"]][1],
        "spread_type": c.get("type"),
        "label": c.get("label"),
        "width_preset": c.get("width_preset"),
        "composite_score": c.get("composite_score"),
        "composite_verdict": c.get("composite_verdict"),
        "structure_text": c.get("structure_text"),
        "net_premium": c.get("net_premium"),
        "max_profit": c.get("max_profit"),
        "max_loss": c.get("max_loss"),
        "pop_pct": c.get("pop_pct"),
        "ev": c.get("ev"),
        "ev_adjusted": c.get("ev_adjusted"),
        "health": c.get("health"),
        "liquidity": c.get("liquidity"),
        "strikes": c.get("strikes"),
        "legs": c.get("legs"),
        "predicted_direction": DIRECTION[call],
    }


def full_candidate(c: dict, call: str) -> dict:
    """Grid card candidate (C1): the scorer's dict + every legacy engine_pick
    key + level fields (type, limit_premiums, pick_id, rr, rr_line, rr_profit,
    reach, health_legacy)."""
    return {**c, **legacy_fields(c, call)}


def build_grid(cands: list[dict], call: str, best: dict) -> dict:
    """Legacy `strategies` shape (app/engine.py), keyed by strategy key, in
    candidate order: best = the near candidate (the extreme one if the family
    has no near), all_widths = [best, extreme sibling]."""
    grid: dict[str, dict] = {}
    for strat in dict.fromkeys(c["strategy"] for c in cands):
        fam = [c for c in cands if c["strategy"] == strat]
        head = next((c for c in fam if c["reach"] == "near"), fam[0])
        rest = [c for c in fam if c is not head]
        b = full_candidate(head, call)
        grid[strat] = {
            "strategy": strat, "name": NAMES[strat][0], "short": NAMES[strat][1],
            "type": head.get("type"), "side": side_of(strat, call),
            "is_recommended": strat == best["strategy"],
            "best": b,
            "all_widths": [b] + [full_candidate(c, call) for c in rest],
        }
    return grid


# ── the agent ───────────────────────────────────────────────────────────────
class StrategyAgent:
    """Holds the day's StrikePlan. One instance per trading day (or reset_day())."""

    def __init__(self) -> None:
        self.plan: StrikePlan | None = None
        self.rebuilds = 0
        self.itm_restrikes = 0                # per day; surfaced in pick["plan"] for replay
        self.held: dict[str, str] = {}        # call → pick_id of the current best (stickiness)
        self.health_mem: dict[str, str] = {}  # pick_id → R/R health band last tick (hysteresis)

    def reset_day(self) -> None:
        self.plan = None
        self.rebuilds = 0
        self.itm_restrikes = 0
        self.held = {}
        self.health_mem = {}

    def _ensure_plan(self, ctx: TickContext) -> StrikePlan:
        sig = _sig(ctx.clusters)
        spot, b = float(ctx.spot), float(ctx.cfg.buffer_pts)
        reason = None
        p = self.plan
        if p is None:
            reason = "initial"
        elif p.trade_date != ctx.trade_date:
            reason, self.rebuilds, self.itm_restrikes = "new_day", 0, 0
        elif ctx.levels_changed or p.cluster_sig != sig:
            reason = "levels_changed"
        else:
            reason = p.side_passed(spot, b)
        if reason is not None:
            prices = [c.price for c in ctx.clusters]
            self.plan = StrikePlan(trade_date=ctx.trade_date, anchor_spot=spot, built_at=ctx.ts,
                                   cluster_sig=sig,
                                   above=tuple(sorted(x for x in prices if x > spot)),
                                   below=tuple(sorted(x for x in prices if x < spot)),
                                   reason=reason)
            if reason != "initial":
                self.rebuilds += 1
        return self.plan

    def pick(self, ctx: TickContext, call: str, up_target: Cluster | None,
             down_target: Cluster | None, invalidation: Cluster | None) -> dict | None:
        if call not in CANDIDATES_BY_CALL or not ctx.clusters:
            return None
        feed = ctx.feed or {}
        chain = Chain(feed.get("contracts") or [], feed.get("expiration"))
        if not chain.strikes or ctx.spot is None:
            return None
        plan = self._ensure_plan(ctx)
        if call in plan.specs and _long_leg_itm(plan.specs[call], float(ctx.spot)):
            del plan.specs[call]                  # re-strike OTM (see ITM RE-STRIKE)
            plan.reason = "long_leg_itm"
            self.rebuilds += 1
            self.itm_restrikes += 1
        if call not in plan.specs:
            specs, targets = build_specs(call, ctx, chain, up_target, down_target)
            plan.specs[call], plan.targets[call] = specs, targets
        spot = float(ctx.spot)
        dte = float(feed.get("dte") or 0.0)
        em = _em(feed, chain, spot)
        cands = []
        for spec in plan.specs[call]:
            s = score_spec(spec, chain, spot, dte, em)
            if s is not None:
                cands.append(enrich(s, spec, ctx.trade_date))
        near = [c for c in cands if c["reach"] == "near"]
        if not near:
            return None

        # R/R (§8.3)
        reach = ctx.cfg.reach
        ivs = {c.get("symbol"): c.get("iv") for c in chain.contracts if c.get("symbol")}
        m_now = minutes_to_expiry(ctx.ts, feed.get("expiration"), feed.get("dte"))
        clusters = sorted(ctx.clusters, key=lambda c: c.price)
        if call == "up":
            near_t = up_target.price if up_target is not None else None
            ext_t = clusters[-1].price
        elif call == "down":
            near_t = down_target.price if down_target is not None else None
            ext_t = clusters[0].price
        else:
            near_t = ((up_target.price + down_target.price) / 2
                      if up_target is not None and down_target is not None else None)
            ext_t = None
        want_near, want_ext = reach in ("both", "near"), reach in ("both", "extreme")
        sib = {c["strategy"]: c for c in cands if c["reach"] == "extreme"}
        hyst = float(getattr(ctx.cfg, "health_hysteresis", 0.0) or 0.0)
        for c in cands:
            x = sib.get(c["strategy"], c) if c["reach"] == "near" else c
            c["rr"] = {"near": _rr(c, near_t, spot, m_now, ivs) if want_near else None,
                       "extreme": _rr(x, ext_t, spot, m_now, ivs) if want_ext else None}
            finish(c, self.health_mem.get(c["pick_id"]), hyst)
            if c.get("health") is not None:
                self.health_mem[c["pick_id"]] = c["health"]

        # Best: top composite among near candidates that MAKE money if the call
        # plays out (R/R gate, §8.3 "informs the pick"); none → top composite.
        def gate_pnl(c):
            r = c["rr"].get("near") or c["rr"].get("extreme")
            return None if r is None else r.get("pnl")
        passing = [c for c in near if gate_pnl(c) is None or gate_pnl(c) >= 0]
        pool, rr_gate = (passing, "passed") if passing else (near, "none_passed")
        best = max(enumerate(pool), key=lambda ic: (ic[1]["composite_score"] or 0, -ic[0]))[1]
        inc = next((c for c in pool if c["pick_id"] == self.held.get(call)), None)
        margin = float(getattr(ctx.cfg, "pick_switch_margin", 0.0) or 0.0)
        if inc is not None and inc is not best and \
                (best["composite_score"] or 0) < (inc["composite_score"] or 0) + margin:
            best = inc                            # stickiness: not a clear enough win
        self.held[call] = best["pick_id"]

        legacy = legacy_fields(best, call)
        target_level = {"up": up_target, "down": down_target}.get(call)
        return {
            **legacy,
            "source": "levels",
            "pick_id": best["pick_id"],
            "call": call,
            "reach": best["reach"],
            "anchor": best.get("anchor"),
            "target_level": _cl(target_level),
            "range_levels": ({"upper": _cl(up_target), "lower": _cl(down_target)}
                             if call == "range" else None),
            "invalidation_level": _cl(invalidation),
            "expiration": feed.get("expiration"),
            "dte": feed.get("dte"),
            "breakevens": best.get("breakevens"),
            "limit_premiums": best.get("limit_premiums"),
            "rr": best["rr"],
            "rr_line": best["rr_line"],
            "rr_profit": best["rr_profit"],
            "health_legacy": best["health_legacy"],
            "rr_gate": rr_gate,
            "candidates": [compact(c) for c in cands if c is not best],
            "plan": {**plan.to_dict(), "itm_restrikes": self.itm_restrikes},
            # full grid (C1) — the brain moves it to decision["strategies"]
            # (never persisted with the pick)
            "strategies": build_grid(cands, call, best),
        }


_DEFAULT_AGENT = StrategyAgent()


def build_level_picks(ctx: TickContext, call: str, up_target: Cluster | None,
                      down_target: Cluster | None, invalidation: Cluster | None) -> dict | None:
    """Best level pick for this tick, via the module-level default StrategyAgent
    (auto-resets on a new trade_date). Brains should own a StrategyAgent instead."""
    return _DEFAULT_AGENT.pick(ctx, call, up_target, down_target, invalidation)


def reprice_pick(pick: dict, contracts: list[dict]) -> float | None:
    """Current mark of the pick's structure, per share, signed like net_premium:
    debit → Σ long_short·mid (value to sell it now; P&L = mark − net_premium);
    credit → −Σ long_short·mid (cost to buy it back; P&L = net_premium − mark).
    Legs resolve by OCC symbol, else (strike, side). None if any leg has no
    usable mid. ×100 for $ per structure."""
    legs = (pick or {}).get("legs") or []
    if not legs:
        return None
    by_sym, by_ks = {}, {}
    for c in contracts or []:
        if c.get("symbol"):
            by_sym[c["symbol"]] = c
        try:
            by_ks[(float(c["strike"]), c.get("side"))] = c
        except (KeyError, TypeError, ValueError):
            pass
    total = 0.0
    for leg in legs:
        c = by_sym.get(leg.get("symbol")) if leg.get("symbol") else None
        if c is None:
            c = by_ks.get((float(leg["strike"]), leg.get("side")))
        if c is None:
            return None
        try:
            mid = _get_mid(c)
        except (TypeError, ValueError):
            return None
        if mid is None or mid <= 0:
            return None
        total += float(leg.get("long_short") or 0) * mid
    return -total if (pick.get("spread_type") or pick.get("type")) == "credit" else total

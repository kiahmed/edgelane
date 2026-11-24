"""[7] Manager (§8.5) + the per-tick pipeline (§8) — the brain.

Pipeline per tick (all plain code, fixed order, deterministic):

  grade open leg on this tick's spot (§12)
  [1] state-machine agent          → contested / lean posts
  [2] derivatives agent            → wall_pull / dex_skew / flow / pin
  [3] up watcher, [4] down watcher → read [2]'s posts (derivatives_agreement) and
                                     each other's progress (stall)
      thin evidence (Σ|w| < THIN_FLOOR) → re-run [2]–[4] over a wider window,
      at most MAX_REPASS times
  [7] manager: decay → lean → evidence → softmax → decide
  [5]/[6] strategy.build_level_picks for the decided call (R/R lives in strategy.py)
  grading: close / open legs for the call; session close → loss / zone_held + §13 summary

Belief update (§8.5, exactly), per scenario s ∈ {up, down, range}:
  1. ℓ_s ← λ·ℓ_s,  λ = 0.5 ^ (dt / (EVIDENCE_HALF_LIFE_MIN·60))
  2. lean: ℓ_lean += LEAN_LOGODDS
  3. ℓ_s += EVIDENCE_SCALE · Σ w over the tick's applied evidence for s
  4. p = softmax(ℓ)

Decisions (spec ambiguities, resolved conservatively)
-----------------------------------------------------
* DECAY uses the ACTUAL dt between this tick's ts and the previous tick's ts
  (not the nominal TICK_SEC): a 40 s gap decays twice as much as a 20 s one, a
  feed outage decays stale beliefs instead of freezing them. First tick: dt=0.
  A tick whose ts does not advance returns None (no double tick).
* RE-PASS: pass k uses window DETAIL_WINDOW_MIN·2^k (pass 1 = 2× as in the
  spec; a pass 2 at the same 2× window would repeat pass 1). Only ONE pass is
  applied in step 3 — the last one run — together with the state-machine posts:
  a re-pass is a wider view of the same agents, so adding every pass would count
  the same signal several times. All passes' rows are output (column `pass`);
  belief.logodds.applied_pass says which counted. Still thin after MAX_REPASS →
  status 'thin' and the call is kept (also no wait → call).
* The lean row (weight 0) is not counted in step 3 or in Σ|w|; step 2 adds it.
* CALLING: wait → X when p_top − p_second ≥ DECIDE_MARGIN. Changing X → Y
  (Y ≠ X) needs p_Y − p_X ≥ FLIP_MARGIN, MIN_DWELL_MIN since the last change,
  AND p_Y − p_second ≥ DECIDE_MARGIN (a new call must itself be decisive). A
  call never decays back to 'wait' on its own (over-trading guard): it changes
  by flip, or to 'done'. A new call also needs its levels (up: up_target, down:
  down_target, range: both); otherwise the call is kept.
* DONE: zone above_all / below_all → call 'done' (no new call, no leg). Back
  inside the map the call restarts from 'wait' (no dwell — done is not a call).
  No clusters at all → 'wait', status 'no_levels'.
* Targets: up → (up_target, invalidation_up), down → (down_target,
  invalidation_down), range → target = upper bound, invalidation = lower bound.
  Range bounds: when the range comes from a CONTESTED level E, E's neighbours
  (next below E, next above E) — scenario.py gives the zone's levels, and with
  spot just off E's band that zone has E itself as a bound, so ordinary chop
  around E would "leave" it. Otherwise the scenario's zone targets. The same
  bounds go to the strategy as (up_target, down_target) on range calls, so the
  condor's shorts and the graded zone agree. wait / done → none.
* confidence = p of the active call (= p_top except while a flip is pending);
  wait / done → p_top.
* TRACE: per (scenario, agent, kind) the brain keeps the decayed cumulative
  contribution to ℓ (the same linear decay as ℓ, so Σ per scenario = ℓ_s). The
  trace lists the top contributors by |contribution|, largest first, each with
  its latest reason — "the evidence that moved ℓ most". `reason` = the top one.
* GRID: the strategy's pick["strategies"] (C1 grid) is moved to
  decision["strategies"] — {} on wait / done / no pick — and is never part of
  the persisted pick (lvl_picks / lvl_legs stay compact).
* STRATEGY: [6] runs after the decision (the pick depends on the call). It is
  a lazy import of level_engine.strategy (a StrategyAgent owned by this brain
  when available); ImportError → no pick, legs still open (shadow grading).
  Any exception inside it → pick None for that tick (logged), never a crash.
* RESTART: on the first tick of a day the brain rebuilds from ctx.day_memory,
  rows strictly before ctx.ts only: ℓ / call / call_since from the last
  lvl_beliefs row, legs from lvl_legs, board history + spot tape from
  lvl_evidence (progress rows carry `spot`). Watcher anchors / premium
  baselines and the derivatives' raw readings re-form from the next ticks.
  Replaying the same contexts into a fresh brain reproduces the day exactly.
"""
from __future__ import annotations

import json
import logging
import math
from datetime import datetime

from ..context import BrainOutput
from ..state_machine import CONTESTED
from . import derivatives, statemachine_agent
from .board import SCENARIOS, Board, Evidence
from .day_summary import build_day_summary, merge_transitions
from .grading import Grader
from .watchers import Watcher

log = logging.getLogger("level_engine.brain")

CALLS = SCENARIOS
TRACE_TOP = 5
PRUNE = 1e-4


def _ts(v) -> datetime | None:
    if v is None or isinstance(v, datetime):
        return v
    try:
        return datetime.fromisoformat(str(v))
    except ValueError:
        return None


def _lvl(c) -> dict | None:
    return None if c is None else {"id": c.id, "price": c.price, "name": c.name}


def _load_strategy():
    """(pick_fn, reprice_fn, agent) from level_engine.strategy, or Nones."""
    try:
        from .. import strategy as st
    except ImportError:
        return None, None, None
    agent = st.StrategyAgent() if hasattr(st, "StrategyAgent") else None
    pick_fn = agent.pick if agent is not None else getattr(st, "build_level_picks", None)
    return pick_fn, getattr(st, "reprice_pick", None), agent


class Beliefs:
    """Log-odds state + the decision rules of §8.5."""

    def __init__(self, cfg) -> None:
        self.cfg = cfg
        self.l = {s: 0.0 for s in SCENARIOS}
        self.contrib: dict[tuple[str, str, str], float] = {}
        self.reasons: dict[tuple[str, str, str], str] = {}
        self.call = "wait"
        self.call_since: datetime | None = None
        self.last_ts: datetime | None = None

    def decay(self, ts: datetime) -> tuple[float, float]:
        dt = 0.0 if self.last_ts is None else max((ts - self.last_ts).total_seconds(), 0.0)
        lam = 0.5 ** (dt / (self.cfg.evidence_half_life_min * 60.0))
        for s in SCENARIOS:
            self.l[s] *= lam
        for k in list(self.contrib):
            v = self.contrib[k] * lam
            if abs(v) < PRUNE:
                del self.contrib[k]
                self.reasons.pop(k, None)
            else:
                self.contrib[k] = v
        self.last_ts = ts
        return lam, dt

    def add(self, scenario: str, amount: float, agent: str, kind: str, reason: str) -> None:
        if amount == 0:
            return
        self.l[scenario] += amount
        k = (scenario, agent, kind)
        self.contrib[k] = self.contrib.get(k, 0.0) + amount
        self.reasons[k] = reason

    def probs(self) -> dict[str, float]:
        m = max(self.l.values())
        e = {s: math.exp(self.l[s] - m) for s in SCENARIOS}
        z = sum(e.values())
        return {s: e[s] / z for s in SCENARIOS}

    def drivers(self, n: int = TRACE_TOP) -> list[tuple[tuple[str, str, str], float, str]]:
        ranked = sorted(self.contrib.items(), key=lambda kv: (-abs(kv[1]), kv[0]))[:n]
        return [(k, v, self.reasons.get(k, "")) for k, v in ranked]


def range_bounds(sc, level_map):
    """(upper, lower) of a range call. Contested level E → E's neighbours (the
    chop is AROUND E, §9 "range between neighbours"); otherwise the zone's
    targets (scenario up_target / down_target)."""
    up, down = sc.up_target, sc.down_target
    e = sc.reference_level
    if sc.reference_event == CONTESTED and e is not None and level_map is not None:
        up = level_map.next_above(e) or up
        down = level_map.next_below(e) or down
    return up, down


def _targets(sc, call: str, level_map=None):
    if call == "up":
        return sc.up_target, sc.invalidation_up
    if call == "down":
        return sc.down_target, sc.invalidation_down
    if call == "range":
        return range_bounds(sc, level_map)
    return None, None


class LevelBrain:
    """`brain(ctx) -> BrainOutput | None`; `reset_day(trade_date)`."""

    def __init__(self, cfg, strategy="auto") -> None:
        self.cfg = cfg
        if strategy == "auto":
            self._pick_fn, self._reprice_fn, self._strategy_agent = _load_strategy()
        elif strategy is None:
            self._pick_fn = self._reprice_fn = self._strategy_agent = None
        else:                                    # (pick_fn, reprice_fn) — tests / replay
            self._pick_fn, self._reprice_fn = strategy
            self._strategy_agent = None
        self.day = None
        self.reset_day(None)

    # ---- day lifecycle ----
    def reset_day(self, trade_date) -> None:
        self.day = trade_date
        self.board = Board()
        self.beliefs = Beliefs(self.cfg)
        self.up, self.down = Watcher("up"), Watcher("down")
        self.grader = Grader(self.cfg, self._reprice_fn)
        self.transitions: list[dict] = []
        self.summary_done = False
        self._needs_restore = True
        if self._strategy_agent is not None and hasattr(self._strategy_agent, "reset_day"):
            self._strategy_agent.reset_day()

    def _memory(self, ctx, table: str, inclusive: bool = False) -> list[dict]:
        try:
            rows = list(ctx.day_memory(table) or [])
        except Exception:                        # unknown table / store down → no memory
            return []
        out = []
        for r in rows:
            t = _ts(r.get("ts") or r.get("opened_at"))
            if t is not None and (t < ctx.ts or (inclusive and t <= ctx.ts)):
                out.append(r)
        return out

    def _restore(self, ctx) -> None:
        beliefs = self._memory(ctx, "beliefs")
        if beliefs:
            last = beliefs[-1]
            lo = last.get("logodds") or {}
            if isinstance(lo, str):
                try:
                    lo = json.loads(lo)
                except ValueError:
                    lo = {}
            for s in SCENARIOS:
                v = float(lo.get(s) or 0.0)
                self.beliefs.l[s] = v
                if v:
                    self.beliefs.contrib[(s, "restored", "prior")] = v
                    self.beliefs.reasons[(s, "restored", "prior")] = "belief restored after restart"
            if last.get("call") in CALLS + ("wait", "done"):
                self.beliefs.call = last["call"]
            self.beliefs.call_since = _ts(lo.get("call_since"))
            self.beliefs.last_ts = _ts(last.get("ts"))
        legs = self._memory(ctx, "legs")
        if legs:
            self.grader.restore(legs, ctx.clusters, self.beliefs.call)
        for r in self._memory(ctx, "evidence"):
            t, ev = _ts(r.get("ts")), Evidence.from_row(r)
            if ev is None:
                continue
            self.board.history.append((t, ev))
            sp = (ev.detail or {}).get("spot")
            if ev.kind == "progress" and sp is not None and (not self.board.tape
                                                             or self.board.tape[-1][0] < t):
                self.board.tape.append((t, float(sp)))

    # ---- the decision ----
    def _decide(self, ctx, thin: bool, p: dict) -> tuple[str, str]:
        cfg, sc, b = ctx.cfg, ctx.scenario, self.beliefs
        if sc.zone.kind == "none":
            return "wait", "no_levels"
        if sc.lean == "done":
            return "done", "done"
        cur = "wait" if b.call == "done" else b.call
        if thin:
            return cur, "thin"
        ranked = sorted(SCENARIOS, key=lambda s: (-p[s], SCENARIOS.index(s)))
        top, second = ranked[0], ranked[1]
        lead = p[top] - p[second]
        tgt, inv = _targets(sc, top, ctx.level_map)
        has_levels = tgt is not None and inv is not None
        if cur == "wait":
            return (top if lead >= cfg.decide_margin and has_levels else "wait"), "ok"
        if top != cur and has_levels and lead >= cfg.decide_margin \
                and p[top] - p[cur] >= cfg.flip_margin:
            since = b.call_since
            if since is None or (ctx.ts - since).total_seconds() >= cfg.min_dwell_min * 60.0:
                return top, "ok"
            return cur, "dwell"
        return cur, "ok"

    def _trace(self, call, p, target, inv, status, applied_pass, window) -> tuple[str, str]:
        head = (f"call {call} · p up {p['up']:.2f} / down {p['down']:.2f} / range "
                f"{p['range']:.2f}")
        if target is not None:
            head += f" · target {target.name} {target.price:g}"
        if inv is not None:
            head += f" · invalidation {inv.name} {inv.price:g}"
        drv = self.beliefs.drivers()
        lines = [f"{i}) {k[0]} {v:+.2f} {k[1]}/{k[2]} — {r}"
                 for i, (k, v, r) in enumerate(drv, 1)]
        tail = []
        if applied_pass:
            tail.append(f"re-pass {applied_pass} applied ({window:g} min window)")
        if status == "thin":
            tail.append("evidence thin — call kept")
        elif status == "dwell":
            tail.append("flip held back by MIN_DWELL")
        text = head + ". Drivers: " + ("; ".join(lines) if lines else "none")
        if tail:
            text += ". " + "; ".join(tail)
        reason = drv[0][2] if drv else "no evidence yet"
        return text + ".", reason

    # ---- the tick ----
    def __call__(self, ctx) -> BrainOutput | None:
        if self.day != ctx.trade_date:
            self.reset_day(ctx.trade_date)
        if self._needs_restore:
            self._needs_restore = False
            self._restore(ctx)
        if self.beliefs.last_ts is not None and ctx.ts <= self.beliefs.last_ts:
            return None
        cfg, sc, board, b = ctx.cfg, ctx.scenario, self.board, self.beliefs
        board.begin_tick(ctx.ts, ctx.spot)
        self.transitions.extend(ctx.transitions or [])
        updates: list[dict] = list(self.grader.mark(ctx))

        # [1] state machine → [2] derivatives → [3] up, [4] down (+ re-passes)
        sm_posts = board.post_all(statemachine_agent.run(ctx))
        derivatives.observe(ctx, board)
        self.up.update(ctx, board)
        self.down.update(ctx, board)
        sm_sum = sum(abs(e.weight) for e in sm_posts)
        applied: list[Evidence] = []
        applied_pass, window, sigma = 0, cfg.detail_window_min, sm_sum
        for k in range(cfg.max_repass + 1):
            window = cfg.detail_window_min * (2 ** k)
            posts = board.post_all(derivatives.run(ctx, board, window, k))
            posts += board.post_all(self.up.run(ctx, board, window, k))
            posts += board.post_all(self.down.run(ctx, board, window, k))
            applied, applied_pass = posts, k
            sigma = sm_sum + sum(abs(e.weight) for e in posts)
            if sigma >= cfg.thin_floor:
                break
        thin = sigma < cfg.thin_floor

        # [7] manager — §8.5 steps 1–4
        lam, dt = b.decay(ctx.ts)
        if sc.lean in SCENARIOS:
            lean_post = next((e for e in sm_posts if e.kind == "lean"), None)
            b.add(sc.lean, cfg.lean_logodds, statemachine_agent.AGENT, "lean",
                  lean_post.reason if lean_post else f"lean {sc.lean}")
        for e in sm_posts + applied:
            if e.kind != "lean":
                b.add(e.scenario, cfg.evidence_scale * e.weight, e.agent, e.kind, e.reason)
        p = b.probs()
        call, status = self._decide(ctx, thin, p)
        if call != b.call:
            b.call, b.call_since = call, ctx.ts
        target, inv = _targets(sc, call, ctx.level_map)
        conf = p[call] if call in SCENARIOS else max(p.values())
        trace, reason = self._trace(call, p, target, inv, status, applied_pass, window)

        # [5]/[6] strategy (pass-through)
        pick, grid = None, {}
        if call in CALLS and self._pick_fn is not None:
            try:
                up_t, dn_t = (target, inv) if call == "range" else (sc.up_target, sc.down_target)
                pick = self._pick_fn(ctx, call, up_t, dn_t, inv)
            except Exception:
                log.exception("strategy failed at %s", ctx.ts.isoformat())
                pick = None
            if pick:
                pick = {**pick, "source": "levels", "call": call, "target_level": _lvl(target),
                        "invalidation_level": _lvl(inv), "confidence": round(conf, 6),
                        "reason": reason}
                grid = pick.pop("strategies", None) or {}   # decision only, never persisted
            else:
                pick = None

        # grading (§12)
        leg_open, upd = self.grader.on_call(ctx, call, target, inv, pick,
                                            allow_open=not ctx.session_close)
        updates += upd
        day_summary = None
        if ctx.session_close:
            updates += self.grader.close_session(ctx)
            if not self.summary_done:
                trs = merge_transitions(self._memory(ctx, "level_state", inclusive=True),
                                        self.transitions)
                day_summary = build_day_summary(ctx, self.grader.legs, trs)
                self.summary_done = True
            status = "session_close"

        merged: dict[str, dict] = {}
        for u in updates:
            if u:
                merged.setdefault(u["id"], {}).update(u)

        pr = {s: round(p[s], 6) for s in SCENARIOS}
        belief = {"p_up": pr["up"], "p_down": pr["down"], "p_range": pr["range"],
                  "call": call, "target_level_id": target.id if target else None,
                  "invalidation_level_id": inv.id if inv else None,
                  "confidence": round(conf, 6), "trace": trace,
                  "logodds": {**{s: round(b.l[s], 6) for s in SCENARIOS},
                              "lambda": round(lam, 9), "dt_sec": dt, "applied_pass": applied_pass,
                              "window_min": window, "thin": thin, "sigma": round(sigma, 6),
                              "call_since": b.call_since.isoformat() if b.call_since else None}}
        leg = self.grader.current
        decision = {"ts": ctx.ts.isoformat(), "symbol": cfg.symbol, "spot": float(ctx.spot),
                    "call": call, "confidence": round(conf, 6), "target_level": _lvl(target),
                    "invalidation_level": _lvl(inv), "zone": sc.zone.to_dict(), "lean": sc.lean,
                    "reason": reason, "pick": pick, "strategies": grid, "p": pr, "status": status, "trace": trace,
                    "leg": leg.summary() if leg else None}
        out = BrainOutput(evidence=[e.to_row() for e in board.posts], belief=belief, pick=pick,
                          leg_open=leg_open, leg_updates=list(merged.values()),
                          day_summary=day_summary, decision=decision)
        board.end_tick()
        return out

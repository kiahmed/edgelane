"""[3] Up watcher and [4] Down watcher (§8.2) — ONE mirrored implementation.

`Watcher("up")` and `Watcher("down")` differ only by σ = +1 / −1, the target
(scenario.up_target / down_target), the origin (= that side's invalidation
level, the level price came from) and the option side (calls / puts).

Per tick: `update(ctx, board)` once (pass 0 — anchor + frozen premium
baseline), then `run(ctx, board, window_min, pass_no)` per pass (pure given the
state). Posts: progress, hold, premium_build, retouch, derivatives_agreement,
and — from whichever watcher runs SECOND, after reading the first one's
`progress` post on the board — stall (range).

Talking to each other: derivatives_agreement reads the derivatives agent's
posts of the same pass (wall_pull / dex_skew / flow); stall reads the other
watcher's progress post. Nothing here is an LLM or a framework — plain code.

Decisions (spec ambiguities, resolved conservatively)
-----------------------------------------------------
* progress "since the zone was entered": the anchor (spot, ts) resets whenever
  this side's (target, origin) pair changes — that is when the §7.2 zone/event
  context the watcher is measuring changes (a wick into a band does not reset
  it). progress_frac = (σ(spot − anchor) shrunk by BUFFER_PTS toward 0) /
  max(σ(T − anchor), BUFFER_PTS) — a wiggle inside one band width is noise (the
  state machine's own tolerance). The pace term is 0 while |pace_ratio| <
  stall_ratio (that is a stall, not a direction) or while the regressed move
  over the observed span (|pace|·minutes) is within BUFFER_PTS (noise).
* pace = σ · least-squares slope of the spot tape over the pass window (pts/min;
  a two-point Δspot is too noisy on a 20 s tape);
  needed = remaining distance to T / window minutes (floor min_needed_ppm);
  pace_ratio = clip(pace / needed). `_reach`-style scaling = _reach(remaining /
  spot) multiplies the pace term (near targets count more). Pace needs at least
  min_pace_min minutes of tape; the window is "full" when the tape spans it
  (within 2 ticks).
* hold f: crossed_* toward T → hold_sec / (HOLD_MIN·60); held_* / broken_* → 1;
  the opposite failed_* on the origin (support / resistance held) → ½.
* premium_build: baseline = mid at T's listed strike (calls up, puts down),
  frozen on a tick with a side-cross transition (cross / knife / fail /
  contested) when there is no baseline for the current T; cleared when T
  changes without a cross. No baseline before the first cross (an OTM 0DTE mid
  only decays on a flat tape, which would bias every side to "fading"). Trend
  via simmer_premium_tracker.classify_premium (±8 % dead band); holding → no post.
* retouch: spot back inside the origin level's band while the scenario's
  reference event on that origin points this side's way → −retouch.
* derivatives_agreement (only when the net derivatives weight D for this side
  is > 0; the other side handles its own): confirm = max(clip(gated pace_ratio,
  0, 1), hold f). Post D·(confirm·(1+agree_amp) − 1): price flat → −D (the claim is
  fully discounted), confirmed → +agree_amp·D (amplified).
* stall: both watchers' pace_ratio < stall_ratio over FULL windows → one range
  post (by the second watcher), so a fresh open never stalls on a short tape.
"""
from __future__ import annotations

import math

from app.bias_engine import _reach
from app.simmer_premium_tracker import classify_premium, premium_map, strike_key

from ..state_machine import (BROKEN_DOWN, BROKEN_UP, CROSSED_DOWN, CROSSED_UP, FAILED_DOWN,
                             FAILED_UP, HELD_DOWN, HELD_UP)
from . import derivatives
from .board import WEIGHTS, Board, Evidence, clip

CROSS_EVENTS = ("cross", "knife", "fail", "contested")

_SIDE = {
    "up": {"sign": 1, "opt": "call", "crossed": CROSSED_UP, "full": (HELD_UP, BROKEN_UP),
           "half": (FAILED_DOWN,), "events": (CROSSED_UP, HELD_UP, BROKEN_UP, FAILED_DOWN)},
    "down": {"sign": -1, "opt": "put", "crossed": CROSSED_DOWN, "full": (HELD_DOWN, BROKEN_DOWN),
             "half": (FAILED_UP,), "events": (CROSSED_DOWN, HELD_DOWN, BROKEN_DOWN, FAILED_UP)},
}


def _slope(tape) -> float:
    """Least-squares slope of spot vs minutes (points per minute)."""
    t0 = tape[0][0]
    xs = [(t - t0).total_seconds() / 60.0 for t, _ in tape]
    ys = [y for _, y in tape]
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx <= 0:
        return 0.0
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx


def _lvl(c) -> dict | None:
    return None if c is None else {"id": c.id, "name": c.name, "price": c.price}


class Watcher:
    def __init__(self, side: str) -> None:
        if side not in _SIDE:
            raise ValueError(side)
        self.side = side
        self.other = "down" if side == "up" else "up"
        self.name = f"{side}_watcher"
        self.other_name = f"{self.other}_watcher"
        self.sign = _SIDE[side]["sign"]
        self.opt = _SIDE[side]["opt"]
        self.anchor: dict | None = None      # {key, spot, ts}
        self.baseline: dict | None = None    # {target_id, strike, opt, key, mid, ts}

    # ---- scenario accessors ----
    def target(self, sc):
        return sc.up_target if self.side == "up" else sc.down_target

    def origin(self, sc):
        return sc.invalidation_up if self.side == "up" else sc.invalidation_down

    # ---- pass-0 state ----
    def update(self, ctx, board: Board) -> None:
        sc, spot = ctx.scenario, float(ctx.spot)
        T, O = self.target(sc), self.origin(sc)
        key = (T.id if T else None, O.id if O else None)
        if self.anchor is None or self.anchor["key"] != key:
            self.anchor = {"key": key, "spot": spot, "ts": ctx.ts}
        crossed = any(((tr.get("detail") or {}).get("event") in CROSS_EVENTS)
                      for tr in (ctx.transitions or []))
        if T is None:
            self.baseline = None
            return
        if self.baseline is not None and self.baseline["target_id"] != T.id:
            self.baseline = None
        if crossed and self.baseline is None:
            k = strike_key(T.strike)
            mid = premium_map((ctx.feed or {}).get("contracts") or [], [T.strike], self.opt).get(k)
            if mid:
                self.baseline = {"target_id": T.id, "strike": T.strike, "opt": self.opt,
                                 "key": k, "mid": mid, "ts": ctx.ts.isoformat()}

    def state(self) -> dict:
        a = self.anchor
        return {"anchor": None if a is None else {"spot": a["spot"], "ts": a["ts"].isoformat()},
                "baseline": self.baseline}

    # ---- the pass ----
    def _pace(self, ctx, board: Board, T, window_min: float) -> dict:
        spot, s = float(ctx.spot), self.sign
        tape = board.spots(window_min)
        remaining = max(s * (T.price - spot), 0.0)
        reach = _reach(remaining / spot) if spot > 0 else 0.0
        out = {"pace_ppm": None, "needed_ppm": None, "pace_ratio": None, "reach": reach,
               "pace_moved": None, "pace_real": False,
               "window_min": window_min, "window_full": False, "remaining": remaining}
        if not tape:
            return out
        t0 = tape[0][0]
        elapsed = (ctx.ts - t0).total_seconds() / 60.0
        out["window_full"] = elapsed >= window_min - 2.0 * ctx.cfg.tick_sec / 60.0
        if elapsed < WEIGHTS["min_pace_min"] or len(tape) < 3:
            return out
        pace = s * _slope(tape)
        needed = max(remaining / window_min, WEIGHTS["min_needed_ppm"])
        out.update(pace_ppm=round(pace, 6), needed_ppm=round(needed, 6),
                   pace_ratio=round(clip(pace / needed), 6),
                   pace_moved=round(abs(pace) * elapsed, 6),
                   pace_real=abs(pace) * elapsed > ctx.cfg.buffer_pts)
        return out

    def _hold_f(self, ctx, O) -> tuple[float, str | None, float]:
        if O is None:
            return 0.0, None, 0.0
        tr = ctx.level_map.tracker(O.id)
        if tr is None or tr.state is None:
            return 0.0, None, 0.0
        cfg, sd = ctx.cfg, _SIDE[self.side]
        if tr.state == sd["crossed"]:
            return clip(tr.hold_sec / (cfg.hold_min * 60.0), 0.0, 1.0), tr.state, tr.hold_sec
        if tr.state in sd["full"]:
            return 1.0, tr.state, tr.hold_sec
        if tr.state in sd["half"]:
            return WEIGHTS["hold_failed_opposite"], tr.state, tr.hold_sec
        return 0.0, tr.state, tr.hold_sec

    def run(self, ctx, board: Board, window_min: float, pass_no: int) -> list[Evidence]:
        cfg, sc, spot, s = ctx.cfg, ctx.scenario, float(ctx.spot), self.sign
        T, O = self.target(sc), self.origin(sc)
        posts: list[Evidence] = []
        if T is None:
            return posts
        side, me = self.side, self.name

        # progress + pace
        a = self.anchor or {"spot": spot}
        span = max(s * (T.price - a["spot"]), cfg.buffer_pts)
        moved = s * (spot - a["spot"])
        eff = math.copysign(max(abs(moved) - cfg.buffer_pts, 0.0), moved)   # band = noise
        frac = clip(eff / span)
        pace = self._pace(ctx, board, T, window_min)
        pr = pace["pace_ratio"]
        # gated pace: a real move (regressed move over the span > BUFFER_PTS) and not a stall
        gpr = pr if (pr is not None and pace["pace_real"]
                     and abs(pr) >= WEIGHTS["stall_ratio"]) else 0.0
        pace_term = gpr * pace["reach"]
        w = WEIGHTS["progress_max"] * clip(0.5 * frac + 0.5 * pace_term)
        pace_txt = f", pace {pace['pace_ppm']:+.2f}/{pace['needed_ppm']:.2f} pt/min" \
            if pr is not None else ""
        posts.append(Evidence(
            me, side, w, "progress",
            {"target": _lvl(T), "origin": _lvl(O), "anchor_spot": a["spot"], "spot": spot,
             "progress_frac": round(frac, 6), **pace},
            f"{frac * 100:+.0f}% of the way to {T.name} {T.price:g}{pace_txt}", pass_no))

        # hold
        f, ostate, hold_sec = self._hold_f(ctx, O)
        if f > 0:
            posts.append(Evidence(
                me, side, WEIGHTS["hold_max"] * f, "hold",
                {"origin": _lvl(O), "state": ostate, "hold_sec": hold_sec, "f": round(f, 6)},
                f"{O.name} {ostate} (hold {f * 100:.0f}%)", pass_no))

        # premium_build
        bl = self.baseline
        if bl is not None and bl["target_id"] == T.id:
            live = premium_map((ctx.feed or {}).get("contracts") or [], [bl["strike"]], bl["opt"])
            trend, ratio = classify_premium({bl["key"]: bl["mid"]}, live)
            if trend in ("building", "fading") and ratio is not None:
                pw = WEIGHTS["premium_max"] * clip((ratio - 1.0) / WEIGHTS["premium_full"])
                posts.append(Evidence(
                    me, side, pw, "premium_build",
                    {"strike": bl["strike"], "opt": bl["opt"], "baseline_mid": bl["mid"],
                     "live_mid": live.get(bl["key"]), "ratio": round(ratio, 6), "trend": trend,
                     "baseline_ts": bl["ts"]},
                    f"{bl['opt']} {bl['strike']:g} mid {trend} {(ratio - 1) * 100:+.0f}% vs baseline",
                    pass_no))

        # retouch
        if (O is not None and sc.reference_level is not None and sc.reference_level.id == O.id
                and sc.reference_event in _SIDE[side]["events"]
                and abs(spot - O.price) <= cfg.buffer_pts):
            posts.append(Evidence(
                me, side, -WEIGHTS["retouch"], "retouch",
                {"origin": _lvl(O), "event": sc.reference_event, "d": round(spot - O.price, 4)},
                f"price back at origin {O.name} {O.price:g}", pass_no))

        # derivatives_agreement — read the derivatives agent's posts (same pass)
        dposts = board.this_tick(agent=derivatives.AGENT, kind=derivatives.DIRECTION_KINDS,
                                 pass_no=pass_no)
        D = sum(e.weight for e in dposts if e.scenario == side) \
            - sum(e.weight for e in dposts if e.scenario == self.other)
        if D > 1e-9:
            confirm = max(clip(gpr, 0.0, 1.0), f)
            factor = confirm * (1.0 + WEIGHTS["agree_amp"]) - 1.0
            adj = D * factor
            if abs(adj) > 1e-6:
                mode = "amplify" if factor > 0 else "discount"
                posts.append(Evidence(
                    me, side, adj, "derivatives_agreement",
                    {"D": round(D, 6), "confirm": round(confirm, 6), "mode": mode,
                     "n_posts": len(dposts)},
                    f"derivatives point {side} (net {D:+.2f}); price "
                    f"{'confirms' if mode == 'amplify' else 'does not confirm'} → {mode}",
                    pass_no))

        # stall — needs the OTHER watcher's progress post from this pass
        other = board.this_tick(agent=self.other_name, kind="progress", pass_no=pass_no)
        if other and pr is not None and pace["window_full"]:
            od = other[-1].detail
            opr = od.get("pace_ratio")
            if opr is not None and od.get("window_full") \
                    and pr < WEIGHTS["stall_ratio"] and opr < WEIGHTS["stall_ratio"]:
                posts.append(Evidence(
                    me, "range", WEIGHTS["stall"], "stall",
                    {f"{side}_pace_ratio": pr, f"{self.other}_pace_ratio": opr,
                     "window_min": window_min},
                    f"no pace toward either target over {window_min:g} min → range", pass_no))
        return posts

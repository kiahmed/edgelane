"""Event-based grading by level (§12, binary — revised 2026-11-23).

A leg opens when the manager makes a call (up / down / range). A leg also opens
with pick=None (no strategy available) — price-only shadow grading.

Directional leg (σ = +1 up, −1 down), levels fixed at open. Binary: result is
`win` or `loss`, close_reason records which event ended it:
  win  / target         spot came within HIT_TOL_PTS of the target level
                        (σ·(spot − target) ≥ −HIT_TOL_PTS)
  loss / invalidation   spot leaves through the far side of the invalidation band
                        (σ·(spot − inv) < −BUFFER_PTS — the state machine's "fail" side)
  loss / flipped        the manager's call changes (to anything) before a hit
  loss / session_close  still open at session close (no hit)
Range leg (zone = [lower, upper] = the call's down / up targets at open):
  loss / zone_exit      spot leaves the zone (beyond either level's band)
  win  / zone_held      still inside when the call changes or at session close
exit_spot = spot on the closing tick. Target hit and invalidation are checked
on the same tick in that order (the hit wins: it was reached first or together).
Old rows (pre-binary result 'flipped' / 'unresolved') are tolerated on restore
and counted as losses by the stats (they never reached the target).

Recorded: progress_pct (current, toward target, capped at 100; None for range),
mae_pts (max adverse excursion; range: max points outside the zone), the time
to target (wins only), strategy_pnl.

Decisions (spec ambiguities, resolved conservatively)
-----------------------------------------------------
* Price is graded BEFORE the tick's decision, so a leg opened on tick t is
  first graded on tick t+1 and the opening tick never resolves it.
* Leg episodes: a call change starts an episode. A leg opens on the episode's
  first tick with usable levels. After that, a new leg in the same episode
  opens only as a CONTINUATION — the previous leg won and the call's target
  has moved on. After a loss the call keeps running but no new leg opens until
  the call changes (no re-entering a thesis the market just invalidated).
* A directional leg needs both a target and an invalidation level; a range
  leg needs both bounds. Missing → no leg. A directional leg whose target is
  already within max(BUFFER_PTS, HIT_TOL_PTS) of spot (or behind it) does not
  open — it would "win" on noise (seen when a flip lands while spot sits on
  the target).
* strategy_pnl ($ per 1-lot) = (mark_now − mark_at_open) × 100 for debits,
  (mark_at_open − mark_now) × 100 for credits, marks from
  strategy.reprice_pick on the feed's contracts. The entry mark is taken at
  open (mid-based, like every other mark here). No pick / no mark → None.
* No leg opens on the session-close tick (it could only close at once).
* Leg id = f"{trade_date}:{n}" (n = 1, 2, … per day) — deterministic, replay-safe.
* Restart: legs are rebuilt from lvl_legs; an open leg whose levels are no
  longer in today's clusters (and not in its pick) is graded only by call
  change / session close. Its entry mark is not stored, so strategy_pnl of a
  restored leg stays at its last persisted value.
* leg_updates are emitted on close and, while open, only when a recorded
  field changed (rounded: progress 0.1 %, mae / pnl 0.01).
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime

DIRECTIONAL = ("up", "down")
CALLS = ("up", "down", "range")


@dataclass
class Leg:
    id: str
    call: str
    opened_at: datetime
    entry_spot: float
    target_id: str | None
    target_price: float | None
    target_name: str | None
    inv_id: str | None
    inv_price: float | None
    inv_name: str | None
    pick: dict | None = None
    entry_mark: float | None = None
    closed_at: datetime | None = None
    result: str | None = None
    close_reason: str | None = None
    exit_spot: float | None = None
    progress_pct: float | None = None
    mae_pts: float = 0.0
    time_to_target_min: float | None = None
    strategy_pnl: float | None = None
    _emitted: tuple | None = None

    @property
    def open(self) -> bool:
        return self.result is None

    def fields(self) -> dict:
        return {"closed_at": self.closed_at, "result": self.result,
                "close_reason": self.close_reason, "exit_spot": self.exit_spot,
                "progress_pct": self.progress_pct, "mae_pts": self.mae_pts,
                "time_to_target_min": self.time_to_target_min,
                "strategy_pnl": self.strategy_pnl}

    def row(self, trade_date) -> dict:
        """lvl_legs row (insert)."""
        return {"id": self.id, "trade_date": trade_date, "opened_at": self.opened_at,
                "call": self.call, "target_level_id": self.target_id,
                "invalidation_level_id": self.inv_id, "entry_spot": self.entry_spot,
                "target_level": _lv(self.target_price, self.target_name),
                "invalidation_level": _lv(self.inv_price, self.inv_name),
                "pick": self.pick, **self.fields()}

    def summary(self) -> dict:
        return {"id": self.id, "call": self.call, "result": self.result,
                "close_reason": self.close_reason, "exit_spot": self.exit_spot,
                "target": self.target_name, "invalidation": self.inv_name,
                "progress_pct": self.progress_pct, "mae_pts": self.mae_pts,
                "time_to_target_min": self.time_to_target_min,
                "strategy_pnl": self.strategy_pnl}


def _lv(price, name) -> dict | None:
    return None if price is None and name is None else {"price": price, "name": name}


def _num(v) -> float | None:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def _obj(v) -> dict | None:
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except ValueError:
            return None
    return v if isinstance(v, dict) else None


def _ts(v) -> datetime | None:
    if v is None or isinstance(v, datetime):
        return v
    return datetime.fromisoformat(str(v))


def _is_credit(pick: dict | None) -> bool:
    return bool(pick) and (pick.get("spread_type") or pick.get("type")) == "credit"


class Grader:
    def __init__(self, cfg, reprice=None) -> None:
        self.cfg = cfg
        self.reprice = reprice          # strategy.reprice_pick or None
        self.legs: list[Leg] = []
        self.n = 0
        self.episode_call: str | None = None
        self.episode_legs = 0

    @property
    def current(self) -> Leg | None:
        return self.legs[-1] if self.legs and self.legs[-1].open else None

    # ---- marks ----
    def _mark(self, leg: Leg, contracts) -> float | None:
        if leg.pick is None or self.reprice is None:
            return None
        try:
            v = self.reprice(leg.pick, contracts)
        except Exception:               # a strategy bug must not stop grading
            return None
        return None if v is None else float(v)

    def _pnl(self, leg: Leg, contracts) -> float | None:
        if leg.entry_mark is None:
            return leg.strategy_pnl
        m = self._mark(leg, contracts)
        if m is None:
            return leg.strategy_pnl
        d = (leg.entry_mark - m) if _is_credit(leg.pick) else (m - leg.entry_mark)
        return round(d * 100.0, 2)

    def _close(self, leg: Leg, ctx, result: str, reason: str) -> None:
        ts = ctx.ts
        leg.closed_at, leg.result, leg.close_reason = ts, result, reason
        leg.exit_spot = float(ctx.spot)
        if result == "win" and leg.call in DIRECTIONAL:
            leg.time_to_target_min = round((ts - leg.opened_at).total_seconds() / 60.0, 4)

    def _update_row(self, leg: Leg, force: bool = False) -> dict | None:
        key = (leg.result, None if leg.progress_pct is None else round(leg.progress_pct, 1),
               round(leg.mae_pts, 2), None if leg.strategy_pnl is None else round(leg.strategy_pnl, 2))
        if not force and key == leg._emitted:
            return None
        leg._emitted = key
        return {"id": leg.id, **leg.fields()}

    def mark(self, ctx) -> list[dict]:
        """Grade the open leg on this tick's spot (before the decision)."""
        leg = self.current
        if leg is None:
            return []
        spot, buf = float(ctx.spot), ctx.cfg.buffer_pts
        contracts = (ctx.feed or {}).get("contracts") or []
        if leg.target_price is None or leg.inv_price is None:
            pass                        # restored leg whose levels vanished: close/flip only
        elif leg.call in DIRECTIONAL:
            s = 1 if leg.call == "up" else -1
            span = abs(leg.target_price - leg.entry_spot) or buf
            leg.progress_pct = round(min(100.0, 100.0 * s * (spot - leg.entry_spot) / span), 4)
            leg.mae_pts = round(max(leg.mae_pts, s * (leg.entry_spot - spot)), 4)
            if s * (spot - leg.target_price) >= -float(ctx.cfg.hit_tol_pts):
                self._close(leg, ctx, "win", "target")
            elif s * (spot - leg.inv_price) < -buf:
                self._close(leg, ctx, "loss", "invalidation")
        else:
            lo, hi = leg.inv_price, leg.target_price
            leg.mae_pts = round(max(leg.mae_pts, spot - hi, lo - spot, 0.0), 4)
            if spot > hi + buf or spot < lo - buf:
                self._close(leg, ctx, "loss", "zone_exit")
        leg.strategy_pnl = self._pnl(leg, contracts)
        row = self._update_row(leg, force=not leg.open)
        return [row] if row else []

    # ---- decisions ----
    def on_call(self, ctx, call: str, target, invalidation, pick: dict | None,
                allow_open: bool = True) -> tuple[dict | None, list[dict]]:
        """Apply the manager's call for this tick. Returns (leg_open row, updates)."""
        updates: list[dict] = []
        if call != self.episode_call:
            leg = self.current
            if leg is not None:
                if leg.call == "range":
                    self._close(leg, ctx, "win", "zone_held")
                else:
                    self._close(leg, ctx, "loss", "flipped")
                updates.append(self._update_row(leg, force=True))
            self.episode_call, self.episode_legs = call, 0
        if not allow_open or call not in CALLS or self.current is not None:
            return None, updates
        if target is None or invalidation is None:
            return None, updates
        if call in DIRECTIONAL:
            sg = 1 if call == "up" else -1
            if sg * (target.price - float(ctx.spot)) <= max(ctx.cfg.buffer_pts, ctx.cfg.hit_tol_pts):
                return None, updates        # already at / past the target: nothing to grade
        if self.episode_legs > 0:
            last = self.legs[-1]
            if not (last.result == "win" and last.target_id != target.id):
                return None, updates
        self.n += 1
        leg = Leg(id=f"{ctx.trade_date.isoformat()}:{self.n}", call=call, opened_at=ctx.ts,
                  entry_spot=float(ctx.spot), target_id=target.id, target_price=target.price,
                  target_name=target.name, inv_id=invalidation.id,
                  inv_price=invalidation.price, inv_name=invalidation.name, pick=pick,
                  progress_pct=0.0 if call in DIRECTIONAL else None)
        leg.entry_mark = self._mark(leg, (ctx.feed or {}).get("contracts") or [])
        if leg.entry_mark is not None:
            leg.strategy_pnl = 0.0
        self.legs.append(leg)
        self.episode_legs += 1
        row = leg.row(ctx.trade_date)
        leg._emitted = (leg.result, leg.progress_pct, round(leg.mae_pts, 2), leg.strategy_pnl)
        return row, updates

    def close_session(self, ctx) -> list[dict]:
        leg = self.current
        if leg is None:
            return []
        if leg.call == "range":
            self._close(leg, ctx, "win", "zone_held")
        else:
            self._close(leg, ctx, "loss", "session_close")
        return [self._update_row(leg, force=True)]

    # ---- restart ----
    def restore(self, rows: list[dict], clusters, current_call: str | None) -> None:
        """Rebuild from the day's lvl_legs rows (restart). Open legs resolve their
        level prices from today's clusters (else the pick's level dicts)."""
        by_id = {c.id: c for c in clusters or []}
        for r in rows:
            try:
                n = int(str(r.get("id", "")).rsplit(":", 1)[-1])
            except ValueError:
                n = 0
            self.n = max(self.n, n)
            pick = r.get("pick") or None

            def price(lid, key):
                c = by_id.get(lid)
                if c is not None:
                    return c.price, c.name
                lv = _obj(r.get(key)) or (pick or {}).get(key) or {}
                return lv.get("price"), lv.get("name")

            tp, tn = price(r.get("target_level_id"), "target_level")
            ip, iname = price(r.get("invalidation_level_id"), "invalidation_level")
            leg = Leg(id=str(r.get("id")), call=r.get("call"), opened_at=_ts(r.get("opened_at")),
                      entry_spot=_num(r.get("entry_spot")) or 0.0,
                      target_id=r.get("target_level_id"), target_price=_num(tp), target_name=tn,
                      inv_id=r.get("invalidation_level_id"), inv_price=_num(ip), inv_name=iname,
                      pick=pick, closed_at=_ts(r.get("closed_at")), result=r.get("result"),
                      close_reason=r.get("close_reason"), exit_spot=_num(r.get("exit_spot")),
                      progress_pct=_num(r.get("progress_pct")),
                      mae_pts=_num(r.get("mae_pts")) or 0.0,
                      time_to_target_min=_num(r.get("time_to_target_min")),
                      strategy_pnl=_num(r.get("strategy_pnl")))
            self.legs.append(leg)
        if self.legs and current_call in CALLS and self.legs[-1].call == current_call:
            self.episode_call, self.episode_legs = current_call, 1
        else:
            self.episode_call = current_call

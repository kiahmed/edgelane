"""Shared helpers for the tick / read-API / replay tests: a fake wall clock
(wired into the MemoryStore's audit timestamps and the runner), synthetic
feed dicts with explicit ts 20 s apart, and a stub brain."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from level_engine import store as store_mod
from level_engine.clock import ET
from level_engine.config import LevelConfig
from level_engine.context import BrainOutput
from level_engine.store import MemoryStore
from level_engine.tick import TickRunner

TD = date(2026, 11, 20)              # a Friday
CFG = LevelConfig()


def et(h: int, m: int, s: int = 0, d: date = TD) -> datetime:
    return datetime(d.year, d.month, d.day, h, m, s, tzinfo=ET).astimezone(timezone.utc)


class Clock:
    """Wall clock for the store's audit `at` and the runner's `now`."""

    def __init__(self, t: datetime) -> None:
        self.t = t

    def __call__(self) -> datetime:
        return self.t


def make_store(monkeypatch, clock: Clock) -> MemoryStore:
    monkeypatch.setattr(store_mod, "_now", clock)
    return MemoryStore()


def contracts(lo: int = 6800, hi: int = 7200, step: int = 5) -> list[dict]:
    out = []
    for k in range(lo, hi + 1, step):
        for side in ("call", "put"):
            out.append({"symbol": f"SPXW{side[0].upper()}{k}", "strike": float(k), "side": side,
                        "expiration": "2026-11-20", "bid": 1.0, "ask": 1.2, "mid": 1.1,
                        "last": 1.1, "delta": 0.5, "gamma": 0.01, "theta": -0.5, "vega": 0.1,
                        "iv": 0.2, "open_interest": 100, "volume": 10,
                        "trade_date": "x", "last_volume": 3})          # extra cols → dropped
    return out


CHAIN = contracts()


def feed(ts: datetime, spot: float, *, spy: float | None = None, market_open: bool = True,
         em: float | None = 20.0, spx_pc: float | None = 6990.0, spy_pc: float | None = 699.0,
         bias: dict | None = None) -> dict:
    return {"symbol": "SPX", "ts": ts.isoformat().replace("+00:00", "Z"), "spot": spot,
            "expiration": "2026-11-20", "dte": 0.3, "expected_move": em, "contracts": CHAIN,
            "bias": bias if bias is not None else {"direction": "up", "score": 0.4},
            "engine_pick": {"name": "legacy"}, "spy_spot": spy if spy is not None else spot / 10.0,
            "spx_prevclose": spx_pc, "spy_prevclose": spy_pc, "market_open": market_open,
            "market_reason": "open" if market_open else "closed"}


class Driver:
    """Feeds a runner one tick per 20 s; the wall clock trails the feed ts by 2 s."""

    def __init__(self, runner: TickRunner, clock: Clock, t0: datetime, dt: int = 20) -> None:
        self.runner, self.clock, self.ts, self.dt = runner, clock, t0, dt
        self.outs: list = []

    def tick(self, spot: float, **kw):
        self.clock.t = self.ts + timedelta(seconds=2)
        out = self.runner.process(feed(self.ts, spot, **kw))
        self.outs.append(out)
        self.ts = self.ts + timedelta(seconds=self.dt)
        return out

    def run(self, spots, **kw):
        return [self.tick(s, **kw) for s in spots]


def path(a: float, b: float, n: int) -> list[float]:
    """n spots from a to b inclusive (linear)."""
    if n == 1:
        return [b]
    return [round(a + (b - a) * i / (n - 1), 2) for i in range(n)]


class StubBrain:
    """Deterministic brain: belief every tick, a pick that changes id when the
    zone changes, a leg opened on the first held_up, a day summary at close."""

    def __init__(self) -> None:
        self.ctxs: list = []
        self.resets: list = []
        self.leg_id = None

    def reset_day(self, trade_date) -> None:
        self.resets.append(trade_date)
        self.leg_id = None

    def __call__(self, ctx) -> BrainOutput:
        self.ctxs.append(ctx)
        zone = ctx.scenario.zone
        pid = f"{ctx.trade_date}:{zone.kind}:{zone.lower.id if zone.lower else '-'}"
        out = BrainOutput(
            evidence=[{"agent": "stub", "scenario": "up", "weight": 0.1, "kind": "test",
                       "detail": {"spot": ctx.spot}, "reason": "stub"}],
            belief={"p_up": 0.5, "p_down": 0.3, "p_range": 0.2, "call": "wait",
                    "confidence": 0.5, "trace": f"spot {ctx.spot}"},
            pick={"pick_id": pid, "source": "levels", "call": "wait"},
            decision={"call": "wait", "ts": ctx.ts, "pick_id": pid,
                      "session_close": ctx.session_close})
        if self.leg_id is None and any(t["state"] == "held_up" for t in ctx.transitions):
            self.leg_id = f"leg:{ctx.ts.isoformat()}"
            out.leg_open = {"id": self.leg_id, "call": "up", "entry_spot": ctx.spot}
        elif self.leg_id and ctx.session_close:
            out.leg_updates = [{"id": self.leg_id, "result": "unresolved", "closed_at": ctx.ts}]
        if ctx.session_close:
            out.day_summary = {"close": ctx.spot, "final_zone": zone.to_dict(),
                               "levels_outcome": {}, "legs": {"n": 1 if self.leg_id else 0}}
        return out


def spx_levels(store: MemoryStore, prices=(7000.0, 7050.0), d: date = TD) -> list[dict]:
    return [store.add_level(d, {"symbol": "SPX", "price": p, "name": f"L{int(p)}"}, d)
            for p in prices]

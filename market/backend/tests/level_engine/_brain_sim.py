"""Hermetic driver for the brain tests: drives the REAL LevelMap + map_scenario
with synthetic 20 s SPX paths, builds TickContexts, persists BrainOutputs into
an in-memory day memory (so restart can be exercised), and keeps every output.

Map (default): W 6980, L 7000, U 7020, Y 7040. 14:40 UTC = 09:40 ET.
"""
from __future__ import annotations

import copy
import json
import math
from datetime import date, datetime, timedelta, timezone

from level_engine import state_machine as sm
from level_engine.brain import create_brain
from level_engine.config import LevelConfig
from level_engine.context import TickContext
from level_engine.levels import Cluster
from level_engine.scenario import map_scenario

# Pinned to the pre-2026-11-24 HIT_TOL_PTS (2.0): the scripted price paths are
# built around that tolerance. The 5.0 default is tested in test_levels_switch.
CFG = LevelConfig(hit_tol_pts=2.0)
TD = date(2026, 11, 20)
T0 = datetime(2026, 11, 20, 14, 40, tzinfo=timezone.utc)
LEVELS = ((6980.0, "W"), (7000.0, "L"), (7020.0, "U"), (7040.0, "Y"))


def cl(price, name):
    return Cluster(id=f"c:{name}", price=price, name=name, kind=None, level_ids=(name,),
                   names=(name,), strike=price, low=price, high=price)


def chain(spot, lo=6900, hi=7120, step=5):
    """Smooth synthetic 0DTE chain: intrinsic + 8·exp(−|spot−K|/15)."""
    out = []
    for k in range(lo, hi + 1, step):
        tv = 8.0 * math.exp(-abs(spot - k) / 15.0)
        for side in ("call", "put"):
            intr = max(spot - k, 0.0) if side == "call" else max(k - spot, 0.0)
            mid = round(intr + tv, 2)
            out.append({"symbol": f"SPXW{side[0].upper()}{k}", "strike": float(k), "side": side,
                        "bid": max(round(mid - 0.05, 2), 0.05), "ask": round(mid + 0.05, 2),
                        "delta": 0.5, "gamma": 0.01, "theta": -1.0, "vega": 0.1, "iv": 0.15,
                        "open_interest": 100, "volume": 10})
    return out


def bias(flow_call_m=None, flow_put_m=None, net_dex=None, put_wall=None, call_wall=None):
    b = {"net_gex": 0.0, "_facts": {"net_dex": net_dex}}
    if flow_call_m is not None:
        b["_flow_call_premium_m"], b["_flow_put_premium_m"] = flow_call_m, flow_put_m
    if put_wall:
        b.update(put_wall_strike=put_wall[0], put_wall_strength=put_wall[1],
                 put_wall_net_gex=abs(put_wall[2]) if len(put_wall) > 2 else 1e6)
    if call_wall:
        b.update(call_wall_strike=call_wall[0], call_wall_strength=call_wall[1],
                 call_wall_net_gex=-abs(call_wall[2]) if len(call_wall) > 2 else -1e6)
    return b


class Memory:
    """The runtime's persistence, reduced to what day_memory returns."""

    def __init__(self):
        self.rows = {"evidence": [], "beliefs": [], "legs": [], "level_state": [], "picks": []}

    def persist(self, ts, out, transitions):
        for t in transitions:
            self.rows["level_state"].append({**t, "trade_date": TD})
        if out is None:
            return
        for e in out.evidence:
            self.rows["evidence"].append({**e, "trade_date": TD, "ts": ts})
        if out.belief:
            self.rows["beliefs"].append({**out.belief, "trade_date": TD, "ts": ts})
        if out.leg_open:
            self.rows["legs"].append(copy.deepcopy(out.leg_open))
        for u in out.leg_updates:
            for r in self.rows["legs"]:
                if r["id"] == u["id"]:
                    r.update(copy.deepcopy(u))

    def __call__(self, table):
        if table not in self.rows:
            raise ValueError(table)
        return copy.deepcopy(self.rows[table])


class Sim:
    def __init__(self, levels=LEVELS, cfg=CFG, strategy=None, bias_dict=None, start=T0):
        self.cfg = cfg
        self.clusters = [cl(p, n) for p, n in levels]
        self.m = sm.LevelMap(self.clusters, cfg)
        self.brain = create_brain(cfg, strategy=strategy)
        self.ts, self.prev = start, None
        self.bias = bias_dict if bias_dict is not None else bias()
        self.mem = Memory()
        self.outs = []
        self.ctxs = []

    def tick(self, spot, session_close=False, bias_dict=None):
        tr = self.m.step(self.ts, spot, self.prev)
        sc = map_scenario(self.m, spot)
        feed = {"ts": self.ts.isoformat(), "spot": spot, "contracts": chain(spot),
                "bias": bias_dict if bias_dict is not None else self.bias,
                "expiration": "2026-11-20", "dte": 0, "expected_move": 30.0}
        ctx = TickContext(cfg=self.cfg, trade_date=TD, ts=self.ts, spot=spot,
                          prev_spot=self.prev, spy=None, ratio=None, ratio_locked=False,
                          clusters=self.clusters, level_map=self.m, scenario=sc,
                          transitions=tr, feed=feed, levels_changed=False,
                          session_close=session_close, day_memory=self.mem)
        out = self.brain(ctx)
        self.mem.persist(self.ts, out, tr)
        self.outs.append(out)
        self.ctxs.append(ctx)
        self.prev = spot
        self.ts += timedelta(seconds=self.cfg.tick_sec)
        return out

    def run(self, spots, **kw):
        out = None
        for s in spots:
            out = self.tick(s, **kw)
        return out

    def stay(self, spot, seconds, **kw):
        return self.run([spot] * int(seconds // self.cfg.tick_sec), **kw)

    def ramp(self, a, b, seconds, **kw):
        n = int(seconds // self.cfg.tick_sec)
        return self.run([round(a + (b - a) * (i + 1) / n, 2) for i in range(n)], **kw)

    def state(self, name):
        return self.m.tracker(f"c:{name}").state

    # ---- views ----
    @property
    def last(self):
        return self.outs[-1]

    def calls(self):
        return [o.decision["call"] for o in self.outs if o]

    def legs_opened(self):
        return [o.leg_open for o in self.outs if o and o.leg_open]

    def leg(self, leg_id):
        return next(r for r in self.mem.rows["legs"] if r["id"] == leg_id)

    def evidence(self, kind=None, agent=None):
        return [e for o in self.outs if o for e in o.evidence
                if (kind is None or e["kind"] == kind) and (agent is None or e["agent"] == agent)]

    def dump(self):
        return json.dumps([None if o is None else o.__dict__ for o in self.outs],
                          default=str, sort_keys=True)


def drift(center=7010.0, n=60):
    pat = (0.0, -2.0, 2.0, -1.0, 1.0)
    return [center + pat[i % len(pat)] for i in range(n)]


def reclaim_upper(s: Sim, hold_sec=600):
    """Drift in L–U, then reclaim U (cross at 7023) and hold above it, grinding up."""
    s.run(drift(7010.0, 30))                       # 10 min in the zone
    s.run([7012, 7014, 7016, 7018, 7019])
    s.tick(7023)                                   # cross U (Δ4: not a knife)
    s.ramp(7023, 7028, hold_sec)                   # hold above U, grinding toward Y

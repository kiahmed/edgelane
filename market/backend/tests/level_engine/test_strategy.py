"""strategy.py — level-anchored strategy agent + R/R (§8.3, §8.4, §11).

Hermetic: a Black-Scholes synthetic 0DTE chain (fractional IV like Tradier's
mid_iv). Levels 7240 / 7260 / 7280 / 7300 / 7320, spot 7268, 11:00 ET.
"""
from __future__ import annotations

import math
from datetime import date, datetime, timedelta, timezone

import pytest

from level_engine import strategy as st
from level_engine import state_machine as sm
from level_engine.config import LevelConfig
from level_engine.context import TickContext
from level_engine.levels import Cluster
from level_engine.projection import bs_price, _norm_cdf
from level_engine.scenario import map_scenario

CFG = LevelConfig()
TD = date(2026, 11, 20)
EXP = "2026-11-20"
T0 = datetime(2026, 11, 20, 16, 0, tzinfo=timezone.utc)   # 11:00 ET (EST)
LEVELS = ((7240.0, "A"), (7260.0, "B"), (7280.0, "C"), (7300.0, "D"), (7320.0, "E"))
LEGACY_KEYS = ("strategy", "name", "short", "spread_type", "label", "width_preset",
               "composite_score", "composite_verdict", "structure_text", "net_premium",
               "max_profit", "max_loss", "pop_pct", "ev", "ev_adjusted", "health", "liquidity",
               "strikes", "legs", "predicted_direction")


def occ(k, side):
    return f"SPXW261120{'C' if side == 'call' else 'P'}{int(k * 1000):08d}"


def bs_chain(spot=7268.0, lo=7150, hi=7400, step=5, iv=0.15, minutes=300.0, drop=()):
    T = minutes / 1440 / 365
    out = []
    for k in range(lo, hi + 1, step):
        k = float(k)
        d1 = (math.log(spot / k) + (0.05 + 0.5 * iv * iv) * T) / (iv * math.sqrt(T))
        for side in ("call", "put"):
            if (k, side) in drop:
                continue
            mid = bs_price(spot, k, T, 0.05, iv, side)
            delta = _norm_cdf(d1) if side == "call" else _norm_cdf(d1) - 1
            bid = max(0.0, round(mid - 0.05, 2))
            out.append({"symbol": occ(k, side), "strike": k, "side": side, "expiration": EXP,
                        "bid": bid, "ask": round(mid + 0.05, 2), "last": mid, "delta": delta, "gamma": 0.01, "theta": -1.0, "vega": 0.5,
                        "iv": iv, "open_interest": 500, "volume": 100})
    return out


def cl(price, name):
    return Cluster(id=f"c:{name}", price=price, name=name, kind=None, level_ids=(name,),
                   names=(name,), strike=price, low=price, high=price)


CL = [cl(p, n) for p, n in LEVELS]
BY = {c.name: c for c in CL}


def ctx(spot=7268.0, ts=T0, clusters=CL, levels_changed=False, chain=None, trade_date=TD,
        cfg=CFG):
    m = sm.LevelMap(clusters, cfg)
    m.step(ts, spot, None)
    feed = {"contracts": chain if chain is not None else bs_chain(spot),
            "expected_move": 30.0, "dte": 0.2, "expiration": EXP, "spot": spot}
    return TickContext(cfg=cfg, trade_date=trade_date, ts=ts, spot=spot, prev_spot=None, spy=None,
                       ratio=None, ratio_locked=False, clusters=list(clusters), level_map=m,
                       scenario=map_scenario(m, spot), transitions=[], feed=feed,
                       levels_changed=levels_changed)


def up_pick(agent=None, **kw):
    a = agent or st.StrategyAgent()
    return a.pick(ctx(**kw), "up", BY["C"], BY["B"], BY["B"]), a


# ---- legacy shape -------------------------------------------------------
def _legacy_pick():
    from app.engine import compute_engine_output
    from app.mock_tradier import _build_mock_chain
    from app.poller import _normalize_tradier_contract
    ch = [_normalize_tradier_contract(o) for o in _build_mock_chain("SPX", EXP, 7580.0)]
    return compute_engine_output("SPX", 7580.0, ch, EXP, 0.3)["engine_pick"]


def _kind(v):
    return "num" if isinstance(v, (int, float)) and not isinstance(v, bool) else type(v).__name__


@pytest.mark.parametrize("call", ["up", "down", "range"])
def test_pick_has_every_legacy_key_with_legacy_types(call):
    legacy = _legacy_pick()
    assert legacy is not None
    assert set(LEGACY_KEYS) == set(legacy)
    targets = {"up": (BY["C"], BY["B"], BY["B"]), "down": (BY["C"], BY["B"], BY["C"]),
               "range": (BY["C"], BY["B"], None)}[call]
    p = st.StrategyAgent().pick(ctx(), call, *targets)
    assert p is not None
    for k in LEGACY_KEYS:
        assert k in p
        assert _kind(p[k]) == _kind(legacy[k]), k
    assert set(p["strikes"]) == set(legacy["strikes"])
    assert set(p["composite_verdict"]) == set(legacy["composite_verdict"])
    for leg in p["legs"]:
        assert set(leg) == set(legacy["legs"][0])
    assert p["source"] == "levels" and p["call"] == call
    assert p["predicted_direction"] == {"up": "up", "down": "down", "range": "flat"}[call]
    for c in p["candidates"]:
        assert c["pick_id"] != p["pick_id"]


# ---- candidate sets -----------------------------------------------------
def _all(p):
    return [p] + p["candidates"]


def test_up_candidates_snap_to_level_strikes():
    p, _ = up_pick()
    by = {(c["strategy"], c["reach"]): c for c in _all(p)}
    assert set(s for s, _ in by) == {"bull_call", "call_butterfly", "long_call", "straddle",
                                     "strangle"}
    bc = by[("bull_call", "near")]["strikes"]
    assert bc["long_call"] == 7270.0 and bc["short_call"] == 7280.0   # ATM long, T_up short
    assert by[("call_butterfly", "near")]["strikes"]["center"] == 7280.0
    assert by[("long_call", "near")]["strikes"]["long_call"] == 7270.0
    assert by[("long_call", "extreme")]["strikes"]["long_call"] == 7320.0
    assert by[("straddle", "near")]["strikes"]["center"] == 7260.0     # nearest level to 7268
    s = by[("strangle", "near")]["strikes"]
    assert (s["long_put"], s["long_call"]) == (7260.0, 7280.0)
    x = by[("strangle", "extreme")]["strikes"]
    assert (x["long_put"], x["long_call"]) == (7240.0, 7320.0)
    assert p["reach"] == "near"


def test_down_candidates_mirror():
    p = st.StrategyAgent().pick(ctx(), "down", BY["C"], BY["B"], BY["C"])
    by = {(c["strategy"], c["reach"]): c for c in _all(p)}
    assert set(s for s, _ in by) == {"bear_put", "put_butterfly", "long_put", "straddle", "strangle"}
    bp = by[("bear_put", "near")]["strikes"]
    assert bp["long_put"] == 7270.0 and bp["short_put"] == 7260.0
    assert by[("put_butterfly", "near")]["strikes"]["center"] == 7260.0
    assert by[("long_put", "extreme")]["strikes"]["long_put"] == 7240.0


def test_range_condor_shorts_at_levels_longs_one_strike_outside():
    p = st.StrategyAgent().pick(ctx(), "range", BY["C"], BY["B"], None)
    by = {c["strategy"]: c for c in _all(p)}
    assert set(by) == {"iron_condor", "call_butterfly", "put_butterfly"}
    k = by["iron_condor"]["strikes"]
    assert (k["long_put"], k["short_put"], k["short_call"], k["long_call"]) == (7255, 7260, 7280, 7285)
    assert by["call_butterfly"]["strikes"]["center"] == 7270.0          # zone midpoint
    assert p["rr"]["extreme"] is None and p["rr"]["near"]["target"] == 7270.0


def test_condor_only_on_range_and_never_credit_verticals():
    for call, tg in (("up", (BY["C"], BY["B"], BY["B"])), ("down", (BY["C"], BY["B"], BY["C"])),
                     ("range", (BY["C"], BY["B"], None))):
        p = st.StrategyAgent().pick(ctx(), call, *tg)
        strats = {c["strategy"] for c in _all(p)}
        assert not strats & {"bull_put", "bear_call"}
        assert ("iron_condor" in strats) == (call == "range")
        if call != "range":
            assert p["spread_type"] == "debit"


@pytest.mark.parametrize("call", ["wait", "done", "none", None])
def test_no_pick_without_a_call(call):
    assert st.StrategyAgent().pick(ctx(), call, BY["C"], BY["B"], BY["B"]) is None


def test_best_is_top_composite_among_near():
    p, _ = up_pick()
    near = [c for c in p["candidates"] if c["reach"] == "near"]
    assert all(p["composite_score"] >= c["composite_score"] for c in near)
    assert p["composite_verdict"] == st.composite_verdict(p["composite_score"], p["ev_adjusted"])


def test_skips_leg_missing_from_chain_or_unquoted():
    chain = bs_chain(drop={(7280.0, "call")})
    p, _ = up_pick(chain=chain)
    strats = {(c["strategy"], c["reach"]) for c in _all(p)}
    assert ("bull_call", "near") not in strats and ("strangle", "near") not in strats
    chain = bs_chain()
    for c in chain:
        if c["strike"] == 7280.0 and c["side"] == "call":
            c["bid"] = 0.0
    p, _ = up_pick(chain=chain)
    assert ("bull_call", "near") not in {(c["strategy"], c["reach"]) for c in _all(p)}


def test_snaps_to_nearest_listed_strike_not_grid():
    # 10-pt listed grid: level 7280 → 7280, spot 7268 → 7270; level at 7284 → 7280
    clusters = [cl(7240.0, "A"), cl(7260.0, "B"), cl(7284.0, "C"), cl(7320.0, "E")]
    c = ctx(clusters=clusters, chain=bs_chain(step=10, lo=7150, hi=7400))
    p = st.StrategyAgent().pick(c, "up", clusters[2], clusters[1], clusters[1])
    bc = next(x for x in _all(p) if x["strategy"] == "bull_call")
    assert (bc["strikes"]["long_call"], bc["strikes"]["short_call"]) == (7270.0, 7280.0)


# ---- strike plan --------------------------------------------------------
def test_strikes_fixed_across_ticks():
    a = st.StrategyAgent()
    p1, _ = up_pick(a)
    p2, _ = up_pick(a, spot=7276.0, ts=T0 + timedelta(minutes=5), chain=bs_chain(7276.0))
    assert p1["pick_id"] == p2["pick_id"] or p2["strikes"] in [c["strikes"] for c in _all(p1)]
    ids1 = {(c["strategy"], c["reach"]): c["strikes"] for c in _all(p1)}
    ids2 = {(c["strategy"], c["reach"]): c["strikes"] for c in _all(p2)}
    assert ids1 == ids2
    assert a.rebuilds == 0 and p2["plan"]["anchor_spot"] == 7268.0


def test_rebuild_on_levels_changed():
    a = st.StrategyAgent()
    up_pick(a)
    p, _ = up_pick(a, spot=7276.0, ts=T0 + timedelta(minutes=5), chain=bs_chain(7276.0),
                   levels_changed=True)
    assert a.rebuilds == 1 and p["plan"]["reason"] == "levels_changed"
    bc = next(c for c in _all(p) if c["strategy"] == "bull_call")
    assert bc["strikes"]["long_call"] == 7275.0


def test_rebuild_when_cluster_set_differs_even_without_flag():
    a = st.StrategyAgent()
    up_pick(a)
    clusters = CL + [cl(7340.0, "F")]
    a.pick(ctx(clusters=clusters, ts=T0 + timedelta(minutes=1)), "up", BY["C"], BY["B"], BY["B"])
    assert a.rebuilds == 1


def test_rebuild_when_every_level_on_one_side_passed():
    a = st.StrategyAgent()
    p = a.pick(ctx(spot=7250.0, chain=bs_chain(7250.0)), "up", BY["B"], BY["A"], BY["A"])
    # 7240 is the only level below the anchor; still inside its band → no rebuild
    a.pick(ctx(spot=7238.0, chain=bs_chain(7238.0), ts=T0 + timedelta(minutes=1)),
           "up", BY["B"], BY["A"], BY["A"])
    assert a.rebuilds == 0
    p2 = a.pick(ctx(spot=7236.0, chain=bs_chain(7236.0), ts=T0 + timedelta(minutes=2)),
                "up", BY["A"], None, None)
    # below_all → brain says done, but the agent itself still rebuilds on the passed side
    assert a.rebuilds == 1 and p2["plan"]["reason"] == "below_passed"
    assert p["plan"]["anchor_spot"] == 7250.0 and p2["plan"]["anchor_spot"] == 7236.0


def test_new_day_and_reset_day():
    a = st.StrategyAgent()
    up_pick(a)
    p, _ = up_pick(a, trade_date=date(2026, 11, 23))
    assert p["plan"]["reason"] == "new_day"
    a.reset_day()
    assert a.plan is None


def test_module_wrapper_uses_default_agent():
    st._DEFAULT_AGENT.reset_day()
    p = st.build_level_picks(ctx(), "up", BY["C"], BY["B"], BY["B"])
    assert p is not None and st._DEFAULT_AGENT.plan is not None


# ---- pick_id ------------------------------------------------------------
def test_pick_id_deterministic():
    p1, _ = up_pick()
    p2, _ = up_pick()
    assert p1["pick_id"] == p2["pick_id"]
    syms = sorted(l["symbol"] for l in p1["legs"])
    import hashlib
    exp = hashlib.sha1(f"{TD}|{p1['strategy']}|{','.join(syms)}".encode()).hexdigest()[:16]
    assert p1["pick_id"] == exp
    p3, _ = up_pick(trade_date=date(2026, 11, 23))
    assert p3["pick_id"] != p1["pick_id"]
    assert len({c["pick_id"] for c in _all(p1)}) == len(_all(p1))


# ---- R/R + rr_line ------------------------------------------------------
def test_rr_near_and_extreme_targets():
    p, _ = up_pick()
    assert p["rr"]["near"]["target"] == 7280.0 and p["rr"]["extreme"]["target"] == 7320.0
    for r in (p["rr"]["near"], p["rr"]["extreme"]):
        assert set(r) >= {"cost", "value", "multiple", "target"}
        assert r["cost"] > 0 and r["multiple"] == round(r["value"] / r["cost"], 2)
        assert r["minutes_to_expiry"] == 150.0               # midpoint of 300 min, on 15-min grid
    lc = next(c for c in p["candidates"] + [p] if c["strategy"] == "long_call" and c["reach"] == "near")
    lcx = next(c for c in p["candidates"] if c["strategy"] == "long_call" and c["reach"] == "extreme")
    assert lc["rr"]["extreme"]["pick_id"] == lcx["pick_id"]    # extreme sibling used
    assert lc["rr"]["near"]["pick_id"] == lc["pick_id"]


def test_rr_respects_cfg_reach():
    a = st.StrategyAgent()
    p = a.pick(ctx(cfg=CFG.with_overrides(reach="near")), "up", BY["C"], BY["B"], BY["B"])
    assert p["rr"]["extreme"] is None and p["rr_line"].startswith("Near: ") and "Extreme" not in p["rr_line"]
    p = st.StrategyAgent().pick(ctx(cfg=CFG.with_overrides(reach="extreme")), "up",
                                BY["C"], BY["B"], BY["B"])
    assert p["rr"]["near"] is None and p["rr_line"].startswith("Extreme: ")


def test_rr_line_exact_format():
    rr = {"near": {"cost": 310.0, "value": 1210.0, "pnl": 900.0, "target": 7280.0},
          "extreme": {"cost": 85.0, "value": 1485.0, "pnl": 1400.0, "target": 7320.0}}
    assert st.format_rr_line(rr) == ("Near: risk $310 → +$900 at 7,280 · "
                                     "Extreme: risk $85 → +$1,400 at 7,320")
    rr = {"near": {"cost": 1234.5, "pnl": -120.4, "target": 7282.5}, "extreme": None}
    assert st.format_rr_line(rr) == "Near: risk $1,235 → −$120 at 7,283"
    assert st.format_rr_line({"near": None, "extreme": None}) == ""


def test_pick_rr_line_matches_rr():
    p, _ = up_pick()
    assert p["rr_line"] == st.format_rr_line(p["rr"])
    assert p["rr_line"].count(" · ") == 1 and "at 7,280" in p["rr_line"] and "at 7,320" in p["rr_line"]


# ---- reprice ------------------------------------------------------------
def test_reprice_debit_and_credit():
    p, _ = up_pick()
    chain = bs_chain()
    mark = st.reprice_pick(p, chain)
    assert mark == pytest.approx(p["net_premium"], abs=1e-9)   # same chain → mark == entry
    r = st.StrategyAgent().pick(ctx(), "range", BY["C"], BY["B"], None)
    cond = next(c for c in _all(r) if c["strategy"] == "iron_condor")
    # compact candidates carry no legs → reprice explicit condor legs
    legs = [{"symbol": occ(7255, "put"), "strike": 7255.0, "side": "put", "long_short": 1},
            {"symbol": occ(7260, "put"), "strike": 7260.0, "side": "put", "long_short": -1},
            {"symbol": occ(7280, "call"), "strike": 7280.0, "side": "call", "long_short": -1},
            {"symbol": occ(7285, "call"), "strike": 7285.0, "side": "call", "long_short": 1}]
    m = st.reprice_pick({"legs": legs, "spread_type": "credit"}, chain)
    assert m == pytest.approx(cond["net_premium"], abs=1e-9) and m > 0
    assert st.reprice_pick(p, [c for c in chain if c["symbol"] != p["legs"][0]["symbol"]
                               and not (c["strike"] == p["legs"][0]["strike"]
                                        and c["side"] == p["legs"][0]["side"])]) is None


# ---- R/R gate -----------------------------------------------------------
def test_rr_gate_rejects_structures_that_lose_at_the_target():
    p = st.StrategyAgent().pick(ctx(), "down", BY["C"], BY["B"], BY["C"])
    assert p["rr_gate"] == "passed" and p["rr"]["near"]["pnl"] >= 0
    strad = next(c for c in p["candidates"] if c["strategy"] == "straddle")
    assert strad["rr"]["near"]["pnl"] < 0            # straddle struck AT the down target
    near = [c for c in p["candidates"] if c["reach"] == "near" and c["rr"]["near"]["pnl"] >= 0]
    assert all(p["composite_score"] >= c["composite_score"] for c in near)


def test_rr_gate_falls_back_to_top_composite(monkeypatch):
    real = st.risk_reward

    def losing(*a, **k):
        r = real(*a, **k)
        return None if r is None else dict(r, pnl=-1.0)
    monkeypatch.setattr(st, "risk_reward", losing)
    p = st.StrategyAgent().pick(ctx(), "up", BY["C"], BY["B"], BY["B"])
    assert p["rr_gate"] == "none_passed"
    assert all(p["composite_score"] >= c["composite_score"] for c in p["candidates"]
               if c["reach"] == "near")

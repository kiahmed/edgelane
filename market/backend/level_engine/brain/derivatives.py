"""[2] Derivatives agent (§8.1) — reads the feed's `bias` dict, never decides.

Posts `wall_pull`, `dex_skew`, `flow` (direction claims) and `pin` (range). The
up/down watchers read these posts and confirm or discount them against price
action (§8.2 derivatives_agreement) — this agent's weights alone never move the
belief once the watchers have discounted them.

Reuses app.bias_engine: `_reach` (proximity tiers) and
`compute_flow_direction_score` (the provider's $5M dead-band flow rule).

Decisions (spec ambiguities, resolved conservatively)
-----------------------------------------------------
* Wall sign follows the codebase's LOCKED GEX convention (bias_engine
  compute_profile_score / compute_magnet_direction_score): net GEX > 0 (the put
  wall) is a MAGNET that pulls price toward it, net GEX < 0 (the call wall) is a
  BARRIER that repels. A wall in a scenario's path — strictly between spot and
  that scenario's target, the target's band included — posts + (magnet) or −
  (barrier) on that scenario. Walls beyond the targets are ignored. A missing
  net_gex falls back to the wall's type (put → magnet, call → barrier).
* dex_skew direction mirrors compute_confidence: net DEX > 0 → down, < 0 → up,
  0/missing → no post. Fixed small weight (no cross-day DEX scale exists).
* flow / dex use the MEAN of this agent's own per-tick readings over the pass
  window (DETAIL_WINDOW_MIN on pass 0, wider on re-passes, §8.5), so a re-pass
  is a genuinely wider view, not a repeat. Walls and pin are structural
  snapshots and read the current bias only.
* flow inputs: bias `_flow_call_premium_m` / `_flow_put_premium_m` ($M, set by
  app/engine.py) or raw `call_premium` / `put_premium` ($). Missing → no flow.
* pin: spot within BUFFER_PTS of a positive-GEX wall with strength 'high',
  the wall inside the current zone (between its levels, bands included).
"""
from __future__ import annotations

from app.bias_engine import _reach, compute_flow_direction_score

from .board import WEIGHTS, Board, Evidence

AGENT = "derivatives"
DIRECTION_KINDS = ("wall_pull", "dex_skew", "flow")


def _f(v) -> float | None:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if x == x else None


def _bias(ctx) -> dict:
    return (ctx.feed or {}).get("bias") or {}


def _flow_score(bias: dict) -> float | None:
    cp_m, pp_m = _f(bias.get("_flow_call_premium_m")), _f(bias.get("_flow_put_premium_m"))
    if cp_m is not None and pp_m is not None:
        cp, pp = cp_m * 1e6, pp_m * 1e6
    else:
        cp, pp = _f(bias.get("call_premium")), _f(bias.get("put_premium"))
    score, used = compute_flow_direction_score(cp, pp)
    return float(score) if used and score is not None else None


def _net_dex(bias: dict) -> float | None:
    facts = bias.get("_facts") or {}
    v = _f(facts.get("net_dex"))
    return v if v is not None else _f(bias.get("net_dex"))


def observe(ctx, board: Board) -> None:
    """Pass-0 bookkeeping: remember this tick's raw readings (the agent's memory)."""
    b = _bias(ctx)
    fs = _flow_score(b)
    if fs is not None:
        board.observe("flow_score", fs)
    nd = _net_dex(b)
    if nd is not None:
        board.observe("net_dex", nd)


def _walls(bias: dict) -> list[dict]:
    out = []
    for typ in ("put", "call"):
        k = _f(bias.get(f"{typ}_wall_strike"))
        if k is None:
            continue
        g = _f(bias.get(f"{typ}_wall_net_gex"))
        sign = (1 if g > 0 else -1) if g else (1 if typ == "put" else -1)
        out.append({"type": typ, "strike": k, "net_gex": g, "sign": sign,
                    "strength": bias.get(f"{typ}_wall_strength") or "low"})
    return out


def run(ctx, board: Board, window_min: float, pass_no: int) -> list[Evidence]:
    cfg, sc, spot = ctx.cfg, ctx.scenario, float(ctx.spot)
    bias = _bias(ctx)
    buf = cfg.buffer_pts
    posts: list[Evidence] = []
    if spot <= 0:
        return posts

    # wall_pull — a wall in a scenario's path
    for w in _walls(bias):
        k = w["strike"]
        for scen, tgt in (("up", sc.up_target), ("down", sc.down_target)):
            if tgt is None:
                continue
            in_path = (spot < k <= tgt.price + buf) if scen == "up" else (tgt.price - buf <= k < spot)
            if not in_path:
                continue
            mult = WEIGHTS["wall_strength"].get(w["strength"], WEIGHTS["wall_strength"]["low"])
            reach = _reach(abs(k - spot) / spot)
            wt = w["sign"] * WEIGHTS["wall_pull_max"] * mult * reach
            role = "magnet" if w["sign"] > 0 else "barrier"
            verb = "pulls toward" if w["sign"] > 0 else "resists"
            posts.append(Evidence(
                AGENT, scen, wt, "wall_pull",
                {"wall": w["type"], "strike": k, "strength": w["strength"], "role": role,
                 "net_gex": w["net_gex"], "reach": reach, "target_id": tgt.id,
                 "target": tgt.price, "window_min": window_min},
                f"{w['type']} wall {k:g} ({w['strength']}, {role}) {verb} {tgt.name} {tgt.price:g}",
                pass_no))

    # dex_skew — sign of the windowed mean net DEX
    dex = board.observations("net_dex", window_min)
    if dex:
        m = sum(dex) / len(dex)
        if m != 0:
            scen = "down" if m > 0 else "up"
            posts.append(Evidence(
                AGENT, scen, WEIGHTS["dex_skew"], "dex_skew",
                {"net_dex_mean": m, "samples": len(dex), "window_min": window_min},
                f"net DEX {'positive' if m > 0 else 'negative'} → {scen}", pass_no))

    # flow — windowed mean of the flow-direction score
    fl = board.observations("flow_score", window_min)
    if fl:
        m = sum(fl) / len(fl)
        if m != 0:
            scen = "up" if m > 0 else "down"
            posts.append(Evidence(
                AGENT, scen, WEIGHTS["flow_max"] * abs(m) / 100.0, "flow",
                {"score_mean": m, "score_now": fl[-1], "samples": len(fl),
                 "window_min": window_min},
                f"flow {'calls' if m > 0 else 'puts'} lead (score {m:+.0f}) → {scen}", pass_no))

    # pin — spot sitting on a strong positive-gamma wall inside the zone
    z = sc.zone
    if z.lower is not None and z.upper is not None:
        lo, hi = z.lower.price - buf, z.upper.price + buf
        for w in _walls(bias):
            if w["sign"] > 0 and w["strength"] == "high" and lo <= w["strike"] <= hi \
                    and abs(spot - w["strike"]) <= buf:
                posts.append(Evidence(
                    AGENT, "range", WEIGHTS["pin"], "pin",
                    {"wall": w["type"], "strike": w["strike"], "strength": w["strength"],
                     "window_min": window_min},
                    f"spot pinned at {w['type']} wall {w['strike']:g} (high, +GEX) inside the zone",
                    pass_no))
                break
    return posts

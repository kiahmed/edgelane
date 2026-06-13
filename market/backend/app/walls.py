"""Wall finding + wall-penalty math — Python port of:
    _findGexWall, _findGexWallsBilateral, computeWallPenalty
from spread_optimizer_v4_7_html.jsx (v4.7.65 + v4.7.67 + v4.7.59).
"""
from __future__ import annotations
from typing import Any, Iterable
from .lookup_tables import WALL_STRENGTH_MULT


def _num(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _strike_rows_from(exposures_for_chosen: dict | None) -> list[dict]:
    if not exposures_for_chosen:
        return []
    rows = exposures_for_chosen.get("by_strike") or []
    return [r for r in rows if isinstance(r, dict)]


def _gex_from_row(row: dict) -> float:
    """Pull the net_gex value from a strike row, tolerant of field-name variants
    (matches JSX `_gexFromRow` behavior)."""
    for k in ("net_gex", "gex", "GEX", "gamma_exposure"):
        if k in row and row[k] is not None:
            return float(row[k])
    return 0.0


def _dex_from_row(row: dict) -> float:
    for k in ("net_dex", "dex", "DEX", "delta_exposure"):
        if k in row and row[k] is not None:
            return float(row[k])
    return 0.0


def _strike_from_row(row: dict) -> float | None:
    return _num(row.get("strike"))


def find_gex_wall(greeks_raw: dict | None, exposures_for_chosen: dict | None) -> dict:
    """Return the single dominant wall (max-|GEX| strike) with type label.

    Mirrors JSX `_findGexWall` post-v4.7.65:
      • Prefer Atlas-supplied key_levels if present.
      • Otherwise pick max-|net_gex| strike from per-strike rows.
      • Classify type = 'put'|'call'|'mixed' based on which side dominates GEX
        at that strike. Used by UI labeling.
    """
    kl = (greeks_raw or {}).get("key_levels") or {}
    atlas_call_wall = _num(kl.get("call_wall") or (kl.get("callWall") or {}).get("strike") if isinstance(kl.get("callWall"), dict) else kl.get("callWall")) or None
    atlas_put_wall  = _num(kl.get("put_wall")  or (kl.get("putWall")  or {}).get("strike") if isinstance(kl.get("putWall"),  dict) else kl.get("putWall"))  or None
    # If Atlas returns object form, extract strike
    if isinstance(kl.get("call_wall"), dict):
        atlas_call_wall = _num(kl["call_wall"].get("strike"))
    if isinstance(kl.get("put_wall"), dict):
        atlas_put_wall = _num(kl["put_wall"].get("strike"))

    rows = _strike_rows_from(exposures_for_chosen)
    enriched: list[dict] = []
    for r in rows:
        s = _strike_from_row(r)
        if s is None:
            continue
        enriched.append({
            "strike": s,
            "gex": _gex_from_row(r),
            "call_gex": float(r.get("call_gex") or r.get("callGex") or 0.0),
            "put_gex":  float(r.get("put_gex")  or r.get("putGex")  or 0.0),
        })

    best: dict | None = None
    for o in enriched:
        if best is None or abs(o["gex"]) > abs(best["gex"]):
            best = o

    sorted_abs = sorted((abs(o["gex"]) for o in enriched), reverse=True)
    strength = "low"
    if len(sorted_abs) >= 2 and sorted_abs[1] > 0:
        ratio = sorted_abs[0] / sorted_abs[1]
        strength = "high" if ratio > 2 else "medium" if ratio > 1.2 else "low"
    elif len(sorted_abs) == 1 and sorted_abs[0] > 0:
        strength = "high"

    # Classify wall type
    wall_type: str | None = None
    if best:
        cgex = abs(best["call_gex"])
        pgex = abs(best["put_gex"])
        if cgex + pgex > 0:
            dominance = max(cgex, pgex) / max(min(cgex, pgex), 1e-9)
            if dominance < 1.3:
                wall_type = "mixed"
            elif pgex > cgex:
                wall_type = "put"
            else:
                wall_type = "call"

    wall_strike = best["strike"] if best else (atlas_call_wall or atlas_put_wall)
    if best is None and wall_strike is not None:
        wall_type = "call" if wall_strike == atlas_call_wall else "put" if wall_strike == atlas_put_wall else None

    return {
        "strike": wall_strike,
        "strength": strength,
        "type": wall_type,
        "atlas_call_wall": atlas_call_wall,
        "atlas_put_wall": atlas_put_wall,
        "computed_top_gex": best["gex"] if best else 0.0,
    }


def find_gex_walls_bilateral(greeks_raw: dict | None, exposures_for_chosen: dict | None) -> dict:
    """Returns dominant put wall + call wall SEPARATELY (EdgelaneProvider-style).

    Mirrors JSX `_findGexWallsBilateral` (v4.7.67):
      • put wall  = highest POSITIVE net_gex strike (puts dominate)
      • call wall = most NEGATIVE net_gex strike (calls dominate)
    """
    rows = _strike_rows_from(exposures_for_chosen)
    enriched: list[dict] = []
    for r in rows:
        s = _strike_from_row(r)
        if s is None:
            continue
        enriched.append({
            "strike": s,
            "net_gex": _gex_from_row(r),
            "call_gex": float(r.get("call_gex") or r.get("callGex") or 0.0),
            "put_gex":  float(r.get("put_gex")  or r.get("putGex")  or 0.0),
        })
    if not enriched:
        return {"put_wall": None, "call_wall": None}

    total_abs = sum(abs(o["net_gex"]) for o in enriched)

    def classify(gex_abs: float) -> str:
        if not gex_abs or not total_abs:
            return "low"
        share = gex_abs / total_abs
        return "high" if share > 0.20 else "medium" if share > 0.10 else "low"

    put_wall: dict | None = None
    for o in enriched:
        if o["net_gex"] > 0 and (put_wall is None or o["net_gex"] > put_wall["net_gex"]):
            put_wall = dict(o)
    if put_wall is not None:
        put_wall["strength"] = classify(abs(put_wall["net_gex"]))

    call_wall: dict | None = None
    for o in enriched:
        if o["net_gex"] < 0 and (call_wall is None or o["net_gex"] < call_wall["net_gex"]):
            call_wall = dict(o)
    if call_wall is not None:
        call_wall["strength"] = classify(abs(call_wall["net_gex"]))

    return {"put_wall": put_wall, "call_wall": call_wall}


def compute_wall_penalty(
    strategy: str,
    strikes: dict,
    breakevens: dict | None,
    wall_strike: float | None,
    wall_strength: str | None,
    net_gex: float = 0.0,
    dte: float | None = None,
) -> dict:
    """Strategy-aware GEX wall penalty.

    Mirrors JSX `computeWallPenalty` post-v4.7.59:
      • DTE-gated: intraday (DTE < 1.5) treats wall as magnet (force longGamma=False)
      • Multi-day long-gamma + supportive wall geometry → factor 1.0
      • Otherwise penalize by proximity × strength × side
    Returns {factor, reason, verdict}.
    """
    if not wall_strike or not strikes:
        return {"factor": 1.0, "reason": None, "verdict": "neutral"}

    w = wall_strike
    s = WALL_STRENGTH_MULT.get(wall_strength or "low", 0.5)
    fmt = lambda v: f"{float(v):.2f}"
    intraday = dte is not None and dte < 1.5
    long_gamma = (not intraday) and (net_gex > 0)

    if strategy == "bull_put":
        sp = strikes.get("short_put")
        if sp is None:
            return {"factor": 1.0, "reason": None, "verdict": "neutral"}
        if w > sp:
            return {"factor": 1.0, "reason": f"Wall {fmt(w)} above short put — acts as upward support.", "verdict": "good"}
        if abs(w - sp) < 0.01:
            return {"factor": 1 - 0.4 * s, "reason": f"Wall AT short put {fmt(sp)} — high pin risk on the short strike.", "verdict": "bad"}
        if long_gamma:
            return {
                "factor": 1.0,
                "reason": f"Wall {fmt(w)} below short put {fmt(sp)} in long-gamma regime — put wall acts as support floor below the spread, stabilizing.",
                "verdict": "good",
            }
        dist_pct = (sp - w) / sp
        proximity = max(0.0, 1 - dist_pct * 10)
        return {
            "factor": 1 - 0.4 * proximity * s,
            "reason": f"Wall {fmt(w)} below short put {fmt(sp)} (short-gamma) — no upward pull; price can drift through.",
            "verdict": "bad" if proximity > 0.5 else "warn",
        }

    if strategy == "bear_call":
        sc = strikes.get("short_call")
        if sc is None:
            return {"factor": 1.0, "reason": None, "verdict": "neutral"}
        if w < sc:
            return {"factor": 1.0, "reason": f"Wall {fmt(w)} below short call — caps upside as resistance.", "verdict": "good"}
        if abs(w - sc) < 0.01:
            return {"factor": 1 - 0.4 * s, "reason": f"Wall AT short call {fmt(sc)} — high pin risk on the short strike.", "verdict": "bad"}
        if long_gamma:
            return {
                "factor": 1.0,
                "reason": f"Wall {fmt(w)} above short call {fmt(sc)} in long-gamma regime — call wall acts as resistance ceiling above the spread, stabilizing.",
                "verdict": "good",
            }
        dist_pct = (w - sc) / sc
        proximity = max(0.0, 1 - dist_pct * 10)
        return {
            "factor": 1 - 0.4 * proximity * s,
            "reason": f"Wall {fmt(w)} above short call {fmt(sc)} (short-gamma) — no downward push; price can drift through.",
            "verdict": "bad" if proximity > 0.5 else "warn",
        }

    if strategy == "iron_condor":
        sp = strikes.get("short_put")
        sc = strikes.get("short_call")
        if sp is None or sc is None:
            return {"factor": 1.0, "reason": None, "verdict": "neutral"}
        if sp <= w <= sc:
            mid = (sp + sc) / 2
            half = (sc - sp) / 2 or 1
            off_center = abs(w - mid) / half
            centered = 1 - off_center
            if centered > 0.7:
                return {"factor": 1.0, "reason": f"Wall {fmt(w)} centered between shorts {fmt(sp)}/{fmt(sc)} — ideal pin.", "verdict": "good"}
            skew_reason = (
                f"Wall {fmt(w)} inside body {fmt(sp)}/{fmt(sc)} but skewed (long-gamma) — anchors price toward nearer short."
                if long_gamma
                else f"Wall {fmt(w)} inside body {fmt(sp)}/{fmt(sc)} but skewed — pinning will pull price toward nearer short."
            )
            return {"factor": 1 - 0.3 * (1 - centered) * s, "reason": skew_reason, "verdict": "warn"}
        dist = (sp - w) if w < sp else (w - sc)
        dist_pct = dist / ((sp + sc) / 2)
        proximity = max(0.0, 1 - dist_pct * 10)
        side_desc = "below body" if w < sp else "above body"
        reason = (
            f"Wall {fmt(w)} {side_desc} {fmt(sp)}/{fmt(sc)} in long-gamma regime — anchors price {dist_pct*100:.1f}% from the profit zone."
            if long_gamma
            else f"Wall {fmt(w)} OUTSIDE shorts {fmt(sp)}/{fmt(sc)} (short-gamma) — pinning drags price out of profit zone."
        )
        return {"factor": 1 - 0.5 * proximity * s, "reason": reason, "verdict": "bad"}

    if strategy in ("iron_butterfly", "call_butterfly", "put_butterfly"):
        center = strikes.get("center")
        if center is None:
            return {"factor": 1.0, "reason": None, "verdict": "neutral"}
        offset_pct = abs(w - center) / center
        if offset_pct < 0.005:
            return {"factor": 1.0, "reason": f"Wall {fmt(w)} pinned at center {fmt(center)} — ideal alignment.", "verdict": "good"}
        proximity = max(0.0, 1 - offset_pct * 30)
        return {
            "factor": 1 - 0.5 * (1 - proximity) * s,
            "reason": f"Wall {fmt(w)} {offset_pct*100:.1f}% off center {fmt(center)} — pin misses target.",
            "verdict": "bad" if proximity < 0.3 else "warn",
        }

    if strategy == "bull_call":
        # Attractor: price drifts toward wall. Need price ↑ past breakeven.
        be = (breakevens or {}).get("breakeven", 0)
        sc = strikes.get("short_call")
        if sc is None:
            return {"factor": 1.0, "reason": None, "verdict": "neutral"}
        if w >= sc:
            return {"factor": 1.0, "reason": f"Wall {fmt(w)} above short call — pulls past max-profit (capped, good).", "verdict": "good"}
        if be is not None and be <= w < sc:
            return {"factor": 1.0, "reason": f"Wall {fmt(w)} between breakeven and short call — pulls into profit zone.", "verdict": "good"}
        return {"factor": 1 - 0.3 * s, "reason": f"Wall {fmt(w)} below breakeven — pulls price away from profit zone.", "verdict": "bad"}

    if strategy == "bear_put":
        be = (breakevens or {}).get("breakeven", 0)
        sp = strikes.get("short_put")
        if sp is None:
            return {"factor": 1.0, "reason": None, "verdict": "neutral"}
        if w <= sp:
            return {"factor": 1.0, "reason": f"Wall {fmt(w)} below short put — pulls past max-profit (capped, good).", "verdict": "good"}
        if be is not None and sp < w <= be:
            return {"factor": 1.0, "reason": f"Wall {fmt(w)} between short put and breakeven — pulls into profit zone.", "verdict": "good"}
        return {"factor": 1 - 0.3 * s, "reason": f"Wall {fmt(w)} above breakeven — pulls price away from profit zone.", "verdict": "bad"}

    return {"factor": 1.0, "reason": None, "verdict": "neutral"}

"""Long-vol / single-leg scorers for the level engine's strategy agent (§8.4).

Spec: docs/level_engine.md §8.4 ("New: a small score_straddle_strangle producing
the same fields … so composite_score works"). The existing scorers in
`app/strategy_engine.py` are reused UNCHANGED for verticals / flies / condors;
this module only adds the structures they don't cover:

    score_straddle_strangle(call_c, put_c, spot, dte, expected_move)
    score_long_option(c, spot, dte, expected_move)        # long call / long put

Both return the exact candidate-dict shape `score_vertical` returns
(structure_text, width, type, net_premium, max_profit, max_loss,
capital_required, rr_ratio, pop_pct, ev, breakevens, liquidity, short_delta,
long_delta, net_spread_delta, net_theta_dollar, strikes, dte, legs, health,
health_explanation), so `compute_limit_premiums`, `composite_score` and
`composite_verdict` run on them as-is. Values are per share (points), like the
legacy scorers.

Method decisions (mirroring the legacy scorers)
-----------------------------------------------
* net_premium = Σ leg mids (legacy `_get_mid`); a debit <= 0.01 → None (legacy
  "doesn't price" rule).
* max_loss = debit. max_profit is CAPPED at the structure's intrinsic value at
  a ±1 expected-move finish (spot ± EM_CAP_MULT·EM, the better side) minus the
  debit, floored at 0 — a long option's payoff is unbounded, so a finite cap is
  needed for compute_limit_premiums' width (= max_profit + max_loss) and the EV
  formula. Calibrated, not arbitrary: with the delta-POP below, a fairly priced
  (Black-Scholes) ATM option scores EV ≈ 0 at 1·EM, whereas a 2·EM cap gives
  every long option / straddle a systematic +EV (on a 0DTE SPX test chain the
  straddle then out-ranked every directional structure on an 'up' call).
* POP: the legacy scorers use DELTA AS PROBABILITY (credit POP = 1 − |Δshort|;
  the debit blend is a delta heuristic). Mirrored here: P(finish beyond a
  breakeven) = |Δ| of the same-side option at the breakeven strike, linearly
  interpolated in strike over the listed chain (`chain`), clamped to the end
  strikes; without a chain, the leg's own |Δ| is used. Straddle/strangle POP =
  P(above upper BE) + P(below lower BE), capped at 100.
* EV = pop·max_profit − (1 − pop)·max_loss (the legacy formula).
* health: the legacy DEBIT classifier (`_classify_health("debit", …)`) — like
  score_vertical's debit branch. NOTE: it returns 'broken' for dte <= 2, so on
  0DTE every debit structure (these AND bull call / bear put) gets the −30
  badge; flies/condors use the credit classifier. That is the reused scorer's
  behaviour, kept unchanged.
* liquidity: legacy `_classify_liquidity` (min OI across legs).
* health_explanation: legacy `_health_explanation("credit", …)` — score_vertical
  passes 'credit' for debits too (JSX parity quirk); mirrored for consistency.
* legs: legacy leg shape {strike, side, long_short, iv (= contract iv / 100,
  the legacy convention), symbol}.
"""
from __future__ import annotations

from app.strategy_engine import (_classify_health, _classify_liquidity, _get_mid,
                                 _health_explanation)

EM_CAP_MULT = 1.0


def _leg(c: dict, ls: int) -> dict:
    iv = c.get("iv") or 0
    return {"strike": float(c["strike"]), "side": c.get("side"), "long_short": ls,
            "iv": (float(iv) / 100) if iv > 0 else None, "symbol": c.get("symbol")}


def prob_beyond(chain: list[dict] | None, side: str, strike: float, fallback_delta: float) -> float:
    """|Δ| of a `side` option struck at `strike`, interpolated over `chain`."""
    pts = sorted((float(c["strike"]), abs(float(c.get("delta") or 0.0)))
                 for c in (chain or []) if c.get("side") == side and c.get("delta") is not None)
    if not pts:
        return max(0.0, min(1.0, abs(fallback_delta)))
    if strike <= pts[0][0]:
        return pts[0][1]
    if strike >= pts[-1][0]:
        return pts[-1][1]
    for (k0, d0), (k1, d1) in zip(pts, pts[1:]):
        if k0 <= strike <= k1:
            if k1 == k0:
                return d0
            return d0 + (d1 - d0) * (strike - k0) / (k1 - k0)
    return pts[-1][1]  # unreachable


def _finish(*, structure_text, net, max_profit, pop, breakevens, legs_c, legs, strikes, dte,
            short_delta, long_delta, net_delta, net_theta) -> dict:
    max_loss = net
    pop_pct = max(0.0, min(100.0, pop * 100))
    ev = (pop_pct / 100) * max_profit - (1 - pop_pct / 100) * max_loss
    health = _classify_health("debit", net_delta, dte, max_loss, max_profit)
    return {
        "structure_text": structure_text,
        "width": max_profit + max_loss,
        "type": "debit",
        "net_premium": net,
        "max_profit": max_profit,
        "max_loss": max_loss,
        "capital_required": max_loss,
        "rr_ratio": max_profit / max_loss if max_loss else 0.0,
        "pop_pct": pop_pct,
        "ev": ev,
        "breakevens": breakevens,
        "liquidity": _classify_liquidity(*legs_c),
        "short_delta": short_delta,
        "long_delta": long_delta,
        "net_spread_delta": net_delta,
        "net_theta_dollar": net_theta,
        "strikes": strikes,
        "dte": dte,
        "legs": legs,
        "health": health,
        "health_explanation": _health_explanation("credit", health, net_delta, dte, max_loss, max_profit),
    }


def score_straddle_strangle(call_c: dict | None, put_c: dict | None, spot: float, dte: float,
                            expected_move: float, chain: list[dict] | None = None) -> dict | None:
    """Long straddle (same strike) / long strangle (put strike < call strike)."""
    if not call_c or not put_c or call_c.get("side") != "call" or put_c.get("side") != "put":
        return None
    kc, kp = float(call_c["strike"]), float(put_c["strike"])
    if kp > kc:
        return None
    net = _get_mid(call_c) + _get_mid(put_c)
    if net <= 0.01:
        return None
    em = max(0.0, float(expected_move or 0.0))
    up_fin, dn_fin = spot + EM_CAP_MULT * em, spot - EM_CAP_MULT * em
    best_intr = max(max(0.0, up_fin - kc) + max(0.0, kp - up_fin),
                    max(0.0, dn_fin - kc) + max(0.0, kp - dn_fin))
    max_profit = max(0.0, best_intr - net)
    be_up, be_dn = kc + net, kp - net
    cd, pd = abs(float(call_c.get("delta") or 0.0)), abs(float(put_c.get("delta") or 0.0))
    pop = min(1.0, prob_beyond(chain, "call", be_up, cd) + prob_beyond(chain, "put", be_dn, pd))
    straddle = kc == kp
    text = (f"Long {call_c['strike']} straddle" if straddle
            else f"Long {put_c['strike']}P / {call_c['strike']}C strangle")
    return _finish(
        structure_text=text, net=net, max_profit=max_profit, pop=pop,
        breakevens=[be_dn, be_up], legs_c=(call_c, put_c),
        legs=[_leg(put_c, 1), _leg(call_c, 1)],
        strikes={"short_put": None, "long_put": kp, "short_call": None, "long_call": kc,
                 "center": kc if straddle else None},
        dte=dte, short_delta=0.0, long_delta=(cd + pd) / 2,
        net_delta=abs(float(call_c.get("delta") or 0.0) + float(put_c.get("delta") or 0.0)),
        net_theta=float(call_c.get("theta") or 0.0) + float(put_c.get("theta") or 0.0))


def score_long_option(c: dict | None, spot: float, dte: float, expected_move: float,
                      chain: list[dict] | None = None) -> dict | None:
    """Long call / long put — same field set, same method as above."""
    if not c or c.get("side") not in ("call", "put"):
        return None
    side, k = c["side"], float(c["strike"])
    net = _get_mid(c)
    if net <= 0.01:
        return None
    em = max(0.0, float(expected_move or 0.0))
    fin = spot + EM_CAP_MULT * em if side == "call" else spot - EM_CAP_MULT * em
    intr = max(0.0, fin - k) if side == "call" else max(0.0, k - fin)
    max_profit = max(0.0, intr - net)
    be = k + net if side == "call" else k - net
    d = abs(float(c.get("delta") or 0.0))
    pop = prob_beyond(chain, side, be, d)
    ch = side[:1].upper()
    return _finish(
        structure_text=f"Long {c['strike']}{ch}", net=net, max_profit=max_profit, pop=pop,
        breakevens=[be], legs_c=(c,), legs=[_leg(c, 1)],
        strikes={"short_put": None, "long_put": k if side == "put" else None,
                 "short_call": None, "long_call": k if side == "call" else None, "center": None},
        dte=dte, short_delta=0.0, long_delta=d, net_delta=d,
        net_theta=float(c.get("theta") or 0.0))

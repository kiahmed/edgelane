"""Overnight catalyst + premium combiner — pure, deterministic (docs/
simmer_overnight_llm_spec.md §6).

Produces the final overnight call from THREE deterministic inputs:
  * the premium engine's own call + trends (simmer_premium_tracker) — price/
    premium confirmation,
  * the LLM catalyst read (sustain / deserved / sentiment / its own call) — the
    "why it moved / will it hold" signal,
  * the RelativeQs crowd tag.

The LLM NEVER computes a number here and NEVER sizes/prices a trade — it only
supplies a side + a sustain probability the combiner weighs. Two policies:

  * conservative (`promote=False`, Simmer's "catalyst never promotes"): the
    premium call is the gate. The catalyst can only CONFIRM it (raise
    confidence), VETO a side conflict (→ wait), or trim confidence when unsure.
    It can never turn a premium wait/skip into a trade.
  * blended (`promote=True`): additionally lets a HIGH-confidence, trend-agreeing
    catalyst promote a premium `wait`/`skip` into a directional sell — for the
    down-gap case where the premium engine is too timid to commit.

Pure function of its inputs → fully unit-testable. No network, no DB.
"""
from __future__ import annotations

from typing import Any, Optional

_SIDES = ("sell_bear_call", "sell_bull_put")
_PROMOTE_MIN_CONF = 70
_PROMOTE_SUSTAIN = ("extend", "hold_to_close")


def combine(premium: dict, catalyst: Optional[dict], *, promote: bool = False) -> dict[str, Any]:
    """premium: {call, premium_trend, underlying_trend, confidence}. catalyst:
    {call, sustain, confidence, deserved, web_social_sentiment} or None (no LLM
    read yet → fall straight back to the premium call). Returns {call, confidence,
    source, reasons}."""
    pcall = str(premium.get("call") or "wait")
    pconf = int(premium.get("confidence") or 0)
    if not catalyst:
        return {"call": pcall, "confidence": pconf, "source": "premium_only",
                "reasons": ["no catalyst read"]}

    ccall = str(catalyst.get("call") or "wait")
    cconf = int(catalyst.get("confidence") or 0)
    sustain = str(catalyst.get("sustain") or "")
    reasons: list[str] = []

    # 1) both name the same side → confirmed; blend + a small agreement bonus.
    if pcall in _SIDES and ccall == pcall:
        reasons.append(f"premium and catalyst agree on {pcall}")
        return {"call": pcall, "confidence": min(100, (pconf + cconf) // 2 + 10),
                "source": "agree", "reasons": reasons}

    # 2) both committed but to OPPOSITE sides → never trade into the conflict.
    if pcall in _SIDES and ccall in _SIDES and ccall != pcall:
        reasons.append(f"premium {pcall} vs catalyst {ccall} conflict — stand down")
        return {"call": "wait", "confidence": 30, "source": "conflict", "reasons": reasons}

    # 3) premium is neutral (wait/skip).
    if pcall in ("wait", "skip"):
        if promote and ccall in _SIDES and cconf >= _PROMOTE_MIN_CONF and sustain in _PROMOTE_SUSTAIN:
            reasons.append(f"premium {pcall}; catalyst promotes {ccall} "
                           f"(conf {cconf}, sustain {sustain})")
            return {"call": ccall, "confidence": max(0, cconf - 10),
                    "source": "catalyst_promote", "reasons": reasons}
        reasons.append(f"premium {pcall}; catalyst not strong enough to promote")
        return {"call": pcall, "confidence": pconf, "source": "premium_hold", "reasons": reasons}

    # 4) premium committed, catalyst unsure (wait/skip) → keep the premium call but
    #    trim confidence (the catalyst couldn't corroborate it).
    reasons.append(f"premium {pcall}; catalyst unsure ({ccall}) — confidence trimmed")
    return {"call": pcall, "confidence": max(0, pconf - 20),
            "source": "premium_catalyst_unsure", "reasons": reasons}


# ── Phase A: premium-is-master drift override (docs/simmer_overnight_rq_impl_spec.md) ──
_SIDE_OPPOSITE = {"sell_bear_call": "sell_bull_put", "sell_bull_put": "sell_bear_call"}

# Calibrated defaults (DB-held in the `overnight_drift` settings section; grade them
# against simmer_overnight_analysis, don't fiddle by hand). `flip_ratio` is how far the
# toward-move premium must travel vs its 09:25 baseline (mean ratio) before a FROZEN
# scored call is allowed to flip — deliberately ABOVE the premium engine's own 0.08
# dead-band so an intraday scored flip needs real, not marginal, divergence.
DRIFT_DEFAULTS = {"enabled": True, "flip_ratio": 0.12, "require_underlying": True,
                  "min_premium_conf": 45, "reclaim_frac": 0.6, "skew_flip": 0.10}


def reactive_call(frozen: dict, live_premium: dict, *, drift_cfg: dict | None = None) -> dict[str, Any]:
    """Premium-is-master override. The scored call (`combiner.final`) freezes at
    10:00 so the bake-off isn't contaminated by late-session premiums — but that
    same freeze is exactly what leaves a name stuck on its pre-market/09:25 read
    when the tape turns against it intraday. This recomputes a SEPARATE, never-frozen
    user-facing call each refresh from the LIVE premium verdict:

      * live premium AGREES with / is quiet on the frozen call → hold the frozen call.
      * live premium MATERIALLY contradicts it (opposite side, or a reversing+collapsing
        `skip`) past `flip_ratio` AND, if required, the underlying retracing → premium
        CANCELS the stale catalyst/pre-market read and the call flips to the tape.
      * contradiction present but NOT yet material → stand down to `wait` (don't keep
        holding a decaying thesis, don't commit to the flip on a wiggle either).

    Pure fn: `frozen` = {call, confidence}; `live_premium` = a simmer_premium_tracker
    verdict {call, premium_trend, underlying_trend, confidence, mean_ratio}."""
    cfg = {**DRIFT_DEFAULTS, **(drift_cfg or {})}
    fcall = str(frozen.get("call") or "wait")
    fconf = int(frozen.get("confidence") or 0)
    base = {"call": fcall, "confidence": fconf, "source": "frozen_hold", "reasons": []}
    if not cfg.get("enabled"):
        return {**base, "source": "disabled"}

    lcall = str(live_premium.get("call") or "wait")
    lconf = int(live_premium.get("confidence") or 0)
    ratio = live_premium.get("mean_ratio")
    utrend = live_premium.get("underlying_trend")
    retraced = live_premium.get("gap_retraced")       # leading: price reclaiming the gap
    skew = live_premium.get("skew_shift")             # leading: opposite side bid up vs toward

    # Only a FROZEN directional call can go stale; a frozen wait/skip has nothing to
    # protect, so just surface the live premium read.
    if fcall not in _SIDE_OPPOSITE:
        return {"call": lcall, "confidence": lconf, "source": "premium_live",
                "reasons": ["no committed scored call to protect — tracking live premium"]}

    # Leading reversal: price has reclaimed a big chunk of the gap, OR the two-sided
    # option skew has tipped to the reversal side — EITHER fires before the toward-side
    # premium has fully deflated (the fast-news-reversal case the magnitude bar misses).
    leading = ((retraced is not None and float(retraced) >= float(cfg["reclaim_frac"]))
               or (skew is not None and float(skew) >= float(cfg["skew_flip"])))

    # The live premium still names the SAME side. Hold — UNLESS price/skew are already
    # reversing ahead of the verdict, in which case stand down early (don't wait for
    # the premium verdict to catch up — that's what made it late before).
    if lcall == fcall:
        if leading:
            return {"call": "wait", "confidence": 30, "source": "premium_leading_standdown",
                    "reasons": [f"{fcall} still named, but price/skew turning "
                                f"(retraced {retraced}, skew {skew}) — stand down early"]}
        return {**base, "reasons": [f"live premium still backs the scored {fcall}"]}

    # The live premium no longer backs the frozen side (it went wait / skip / the
    # opposite side). How far has the toward-move premium travelled from baseline?
    # mean_ratio is latest/baseline on the toward side; |ratio-1| is the drift size.
    drift = abs(float(ratio) - 1.0) if ratio is not None else 0.0
    toward_material = drift >= float(cfg["flip_ratio"])
    flip = (lcall == _SIDE_OPPOSITE[fcall]
            and (toward_material or leading)       # magnitude OR a leading signal
            and lconf >= int(cfg["min_premium_conf"]))
    if cfg.get("require_underlying"):
        flip = flip and utrend in ("retracing", "holding")

    if flip:
        # Premium / tape materially committed to the OTHER side → it cancels the stale
        # catalyst / pre-market read and the call flips.
        why = "drift" if toward_material else "price/skew reversal"
        return {"call": lcall, "confidence": lconf, "source": "premium_override",
                "reasons": [f"premium flipped to {lcall} ({why}; drift {drift:.0%}, "
                            f"retraced {retraced}, skew {skew}, {utrend}) — cancels the stale {fcall}"]}
    # Premium stopped backing the thesis but hasn't decisively flipped → stand down
    # rather than hold a decaying call or chase a wiggle.
    return {"call": "wait", "confidence": 30, "source": "premium_drift_standdown",
            "reasons": [f"premium no longer backs the scored {fcall} "
                        f"(now {lcall}, drift {drift:.0%}) — stand down, not yet a flip"]}


# ── Single user-facing verdict: every factor folded in sequence, ONE call out ──
_CALL_WORDS = {"sell_bear_call": "Sell bear-call", "sell_bull_put": "Sell bull-put",
               "wait": "Wait", "skip": "Skip"}


def reconcile(premium: dict, llm: Optional[dict], combiner: Optional[dict], *,
              drift_cfg: dict | None = None) -> dict[str, Any]:
    """Fold EVERY overnight factor into ONE call, and emit the ordered steps that
    produced it — so the card shows a single verdict with its reasoning, never two
    competing opinions. Pure fn; the bake-off candidates in `combiner` are untouched.

    Sequence (each step feeds the next, nothing branches into its own verdict):
      1. Gap + peer context  (the setup)
      2. Premium/price read   (building/holding/fading · extending/holding/retracing)
      3. Catalyst read        (why it moved / deserved / sustain — confirms or trims)
      4. Intraday drift        (premium-is-master override: hold / stand down / flip)
    The step-4 override of the catalyst-gated call IS the final call."""
    premium = premium or {}
    base = (combiner or {}).get("final") or {
        "call": premium.get("call"), "confidence": premium.get("confidence")}
    final = reactive_call(base, premium, drift_cfg=drift_cfg)

    steps: list[dict] = []
    # 1) setup
    dir_ = str(premium.get("dir") or "").lower() or None
    steps.append({"step": "setup", "label": "Overnight gap",
                  "detail": _premium_setup_line(premium)})
    # 2) premium / price read
    pt_, ut_ = premium.get("premium_trend"), premium.get("underlying_trend")
    if pt_ or ut_:
        steps.append({"step": "premium", "label": "Premium & tape",
                      "detail": f"premiums {pt_ or '—'}, underlying {ut_ or '—'} → "
                                f"{_CALL_WORDS.get(str(premium.get('call')), premium.get('call') or '—')}"})
    # 3) catalyst (reasoning only — never its own call)
    if llm and llm.get("call"):
        bits = [b for b in (llm.get("catalyst_type"), llm.get("deserved"),
                            (f"vibe: {llm.get('web_social_sentiment')}" if llm.get("web_social_sentiment") else None),
                            (f"sustain: {llm.get('sustain')}" if llm.get("sustain") else None)) if b]
        steps.append({"step": "catalyst", "label": "Catalyst read",
                      "detail": (llm.get("why_moved") or llm.get("one_liner") or "")
                                + ((" · " + ", ".join(bits)) if bits else "")})
    # 4) intraday drift → the override that decides the final call
    steps.append({"step": "intraday", "label": "Intraday drift",
                  "detail": (final.get("reasons") or ["premium backs the call — holding"])[0]})

    return {"call": final["call"], "confidence": final["confidence"],
            "source": final["source"], "steps": steps}


def _premium_setup_line(premium: dict) -> str:
    gap = premium.get("gap")
    d = str(premium.get("dir") or "").lower()
    move = f"{'down' if d == 'down' else 'up'} {abs(float(gap)) * 100:.1f}%" if gap not in (None, "") else "overnight"
    crowd = premium.get("crowd")
    peer = ("sector-wide (more durable)" if crowd in ("sector_wide", "with_market")
            else "lone move (revert-prone)" if crowd in ("lone", "against_market")
            else "mixed peer signal")
    return f"gapped {move} · {peer}"

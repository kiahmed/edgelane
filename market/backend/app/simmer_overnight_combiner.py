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

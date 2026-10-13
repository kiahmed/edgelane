"""The two decision-first prompts, provider-agnostic.

Wording is the proven text from the Oct 2026 grounded-LLM experiments
(docs/simmer_overnight_llm_spec.md). STEP1 carries the anti-"down-gap bounce"
line that fixes the one repeatable Gemini bias (it assumed gaps mean-revert).
"""
from __future__ import annotations

from .base import CatalystCtx


def _bits(ctx: CatalystCtx) -> tuple[str, float]:
    """(UP/DOWN word, gap percent magnitude) from a signed-gap ctx."""
    updown = "UP" if (ctx.dir or "").lower() == "up" else "DOWN"
    pct = abs(ctx.gap) * 100.0
    return updown, pct


def step1(ctx: CatalystCtx) -> str:
    """Call 1 — web-grounded analyst note (<=120 words)."""
    updown, pct = _bits(ctx)
    crowd = ctx.crowd if ctx.crowd else "n/a"
    premium = (ctx.premium_trend_line.strip() + ". ") if ctx.premium_trend_line else ""
    return (
        f"Today is {ctx.day_label} (US equity market). You are a fast equities "
        f"catalyst analyst. Use web search for CURRENT news dated around "
        f"{ctx.day_label} (last 24h) and web/social chatter. {ctx.symbol} gapped "
        f"{updown} {pct:.1f}% overnight into that day's open (peer context: "
        f"{crowd}). {premium}In a short analyst note (<=120 words): WHY it moved, "
        f"the catalyst's nature, whether the move looks DESERVED or an "
        f"overreaction, the web/social VIBE (holders embracing/adding, or doubting "
        f"'undeserved'?), and whether it will HOLD/EXTEND or FADE through the "
        f"session, with reasoning. Decision-first. "
        f"Do NOT assume a gap mean-reverts. On a fresh, fundamental, or sector-wide "
        f"catalyst, weight CONTINUATION — the move more often extends or holds than "
        f"snaps back. Reserve 'overreaction/fade' for thin, lone, low-conviction "
        f"moves with no fresh news."
    )


def step2(note: str, ctx: CatalystCtx) -> str:
    """Call 2 — strict-schema structured extract of the analyst note."""
    updown, pct = _bits(ctx)
    return (
        f"From this analyst note on {ctx.symbol} (gapped {updown} {pct:.1f}%), "
        f"extract the structured read. `call` is the credit spread to sell: "
        f"sell_bull_put if it should hold/rise, sell_bear_call if it should "
        f"fade/fall, else wait/skip. `sustain`: fade=reverses, hold_*=stays at "
        f"level, extend=keeps going. NOTE:\n{note}"
    )

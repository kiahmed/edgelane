"""Typed models + the provider protocol for the overnight catalyst analyzer.

`CatalystVerdict` is the one place `confidence` is validated as int 0..100 —
the Gemini extraction bug (confidence on a 0-1 or 0-10 scale, or out of range)
is fixed here by construction, so no call site has to second-guess the model.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel, Field, field_validator


# --- Call 1 input ----------------------------------------------------------

@dataclass
class CatalystCtx:
    """Everything the grounded read needs about one overnight mover.

    `gap` is signed (e.g. -0.047 == down 4.7%); `dir` is the human direction
    ('up'/'down'). `premium_trend_line` is a one-line, premium-aware context
    string so the sustain judgment is premium-aware — the LLM reasons over it,
    it never computes premium math (CLAUDE.md: never delegate math to the LLM).
    """
    symbol: str
    gap: float
    dir: str
    crowd: str | None
    day_label: str
    premium_trend_line: str | None = None


# --- Call 1 output ---------------------------------------------------------

@dataclass
class GroundedNote:
    """The prose analyst note from call 1, plus grounding/usage metadata."""
    text: str
    queries: list[str] | None
    latency_ms: int
    tokens: dict  # keys: "in", "out", "think"


# --- Call 2 output ---------------------------------------------------------

class CatalystVerdict(BaseModel):
    """Strict-schema extraction of the analyst note (call 2).

    The 7 model fields are what the LLM fills; `provider`/`model`/`latency_ms`/
    `tokens` are stamped by the adapter afterward and are deliberately excluded
    from the response schema sent to the LLM.
    """
    catalyst_type: Literal[
        "earnings", "guidance", "analyst", "M&A", "product",
        "legal", "macro_sector", "other",
    ]
    deserved: Literal["deserved", "overreaction", "unclear"]
    web_social_sentiment: Literal["embrace", "doubt", "mixed"]
    sustain: Literal["fade", "hold_to_midday", "hold_to_close", "extend"]
    confidence: int
    call: Literal["sell_bull_put", "sell_bear_call", "wait", "skip"]
    one_liner: str

    # Adapter-filled meta (NOT part of the LLM response schema).
    provider: str = ""
    model: str = ""
    latency_ms: int = 0
    tokens: dict = Field(default_factory=dict)

    @field_validator("confidence", mode="before")
    @classmethod
    def _clamp_confidence(cls, v):
        """Coerce the model's confidence onto a 0..100 int scale.

        Gemini repeatably emits confidence on the wrong scale: a 0-1 fraction or
        a 0-10 score. Rescale (<=1 -> *100, <=10 -> *10), then clamp 0..100 and
        round. Garbage -> 0 (best-effort; never raise out of a background loop).
        """
        try:
            f = float(v)
        except (TypeError, ValueError):
            return 0
        if f <= 1.0:
            f *= 100.0
        elif f <= 10.0:
            f *= 10.0
        f = max(0.0, min(100.0, f))
        return int(round(f))


# --- Protocol --------------------------------------------------------------

@runtime_checkable
class OvernightLLM(Protocol):
    """Two-method provider seam. Swapping provider = change the factory string;
    no call-site changes. Both methods are best-effort: None on any failure."""
    provider: str
    model: str

    async def grounded_read(self, ctx: CatalystCtx) -> GroundedNote | None: ...

    async def structured_read(
        self, note: str, ctx: CatalystCtx
    ) -> CatalystVerdict | None: ...

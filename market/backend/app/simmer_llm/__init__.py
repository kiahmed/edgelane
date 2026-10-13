"""Provider-agnostic LLM adapter for the Simmer overnight catalyst analyzer.

A thin internal seam (NOT LangChain/LangGraph — see
`docs/simmer_overnight_llm_spec.md` §4). Two calls per read:

    1. grounded_read   — web-grounded prose analyst note (call 1)
    2. structured_read — strict-schema verdict extract (call 2)

Grounding and strict response-schema can't be combined in one call on Gemini,
so the two-call shape is required regardless of provider. Everything here is
best-effort: on ANY network/parse failure the adapters LOG a warning and return
None — never raise. This runs in an off-hours loop that must not crash.

Keys are passed in (factory args); this package never imports app config, so it
stays import-safe and unit-testable with no credentials.
"""
from __future__ import annotations

from .base import (
    CatalystCtx,
    CatalystVerdict,
    GroundedNote,
    OvernightLLM,
)
from .factory import make_llm
from .gemini import GeminiOvernightLLM
from .openai import OpenAIOvernightLLM

__all__ = [
    "CatalystCtx",
    "CatalystVerdict",
    "GroundedNote",
    "OvernightLLM",
    "GeminiOvernightLLM",
    "OpenAIOvernightLLM",
    "make_llm",
]

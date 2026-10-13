"""provider string -> adapter. Keys are passed in (no app/config import), so
this module stays import-safe and unit-testable without credentials.

Unknown provider or a missing key -> log a warning and return None (best-effort:
the caller treats None as "catalyst read unavailable", never a crash).
"""
from __future__ import annotations

import logging

from .base import OvernightLLM
from .gemini import GeminiOvernightLLM
from .openai import OpenAIOvernightLLM

log = logging.getLogger("edgelane.simmer.llm.factory")


def make_llm(
    provider: str,
    *,
    model: str | None = None,
    thinking: str = "medium",
    context_size: str = "medium",
    gemini_key: str = "",
    openai_key: str = "",
) -> OvernightLLM | None:
    p = (provider or "").strip().lower()
    if p == "gemini":
        if not gemini_key:
            log.warning("make_llm: gemini selected but no gemini_key provided")
            return None
        return GeminiOvernightLLM(gemini_key, model or "gemini-3.8-flash", thinking)
    if p == "openai":
        if not openai_key:
            log.warning("make_llm: openai selected but no openai_key provided")
            return None
        return OpenAIOvernightLLM(openai_key, model or "gpt-6-luna",
                                  thinking, context_size)
    log.warning("make_llm: unknown provider %r", provider)
    return None

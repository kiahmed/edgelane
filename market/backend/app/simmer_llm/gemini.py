"""Gemini overnight adapter — google_search grounding (call 1) + responseSchema
strict JSON (call 2).

Grounding and a strict responseSchema cannot be combined in one Gemini call, so
call 2 sends NO tools. Best-effort throughout: `_post` is the single network
seam and returns None on any failure; every method is build-body -> _post ->
parse, so a None short-circuits to a None return (never raises).
"""
from __future__ import annotations

import logging
import time

import httpx

from .base import CatalystCtx, CatalystVerdict, GroundedNote
from .prompts import step1, step2

log = logging.getLogger("edgelane.simmer.llm.gemini")

_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

# The call-2 response schema: the 7 model fields of CatalystVerdict as an
# OpenAPI-subset object. Meta fields (provider/model/...) are excluded — the
# adapter stamps those, the model never sees them.
_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "catalyst_type": {"type": "string", "enum": [
            "earnings", "guidance", "analyst", "M&A", "product",
            "legal", "macro_sector", "other"]},
        "deserved": {"type": "string", "enum": ["deserved", "overreaction", "unclear"]},
        "web_social_sentiment": {"type": "string", "enum": ["embrace", "doubt", "mixed"]},
        "sustain": {"type": "string", "enum": [
            "fade", "hold_to_midday", "hold_to_close", "extend"]},
        "confidence": {"type": "integer"},
        "call": {"type": "string", "enum": [
            "sell_bull_put", "sell_bear_call", "wait", "skip"]},
        "one_liner": {"type": "string"},
    },
    "required": [
        "catalyst_type", "deserved", "web_social_sentiment", "sustain",
        "confidence", "call", "one_liner",
    ],
    "propertyOrdering": [
        "catalyst_type", "deserved", "web_social_sentiment", "sustain",
        "confidence", "call", "one_liner",
    ],
}


class GeminiOvernightLLM:
    def __init__(self, api_key: str, model: str = "gemini-3.8-flash",
                 thinking: str = "medium"):
        self.provider = "gemini"
        self.api_key = api_key
        self.model = model
        self.thinking = thinking

    @property
    def _url(self) -> str:
        return _ENDPOINT.format(model=self.model)

    @property
    def _headers(self) -> dict:
        return {"x-goog-api-key": self.api_key, "Content-Type": "application/json"}

    def _thinking_config(self, *, grounded: bool) -> dict:
        """includeThoughts off always; gemini-3 uses thinkingLevel, older uses
        a -1 ('dynamic') thinkingBudget. Call 2 wants minimal thinking."""
        tc: dict = {"includeThoughts": False}
        if self.model.startswith("gemini-3"):
            tc["thinkingLevel"] = self.thinking if grounded else "low"
        else:
            tc["thinkingBudget"] = -1
        return tc

    async def _post(self, url: str, headers: dict, body: dict) -> dict | None:
        """Single network seam. POST JSON; return parsed dict or None on ANY
        network/parse failure (logged). Never raises."""
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                resp = await client.post(url, headers=headers, json=body)
            return resp.json()
        except Exception as e:  # network, timeout, non-JSON body — all best-effort
            log.warning("gemini _post failed: %s", e)
            return None

    async def grounded_read(self, ctx: CatalystCtx) -> GroundedNote | None:
        body = {
            "contents": [{"role": "user", "parts": [{"text": step1(ctx)}]}],
            "tools": [{"google_search": {}}],
            "generationConfig": {"thinkingConfig": self._thinking_config(grounded=True)},
        }
        t0 = time.monotonic()
        j = await self._post(self._url, self._headers, body)
        latency_ms = int((time.monotonic() - t0) * 1000)
        if j is None:
            return None
        try:
            cand = (j.get("candidates") or [{}])[0]
            parts = ((cand.get("content") or {}).get("parts")) or []
            # Join only the answer parts — skip any thought (reasoning) parts.
            text = "".join(
                p.get("text", "") for p in parts if not p.get("thought")
            ).strip()
            queries = (cand.get("groundingMetadata") or {}).get("webSearchQueries")
            usage = j.get("usageMetadata") or {}
            tokens = {
                "in": usage.get("promptTokenCount") or 0,
                "out": usage.get("candidatesTokenCount") or 0,
                "think": usage.get("thoughtsTokenCount") or 0,
            }
            return GroundedNote(text=text, queries=queries,
                                latency_ms=latency_ms, tokens=tokens)
        except Exception as e:
            log.warning("gemini grounded parse failed: %s", e)
            return None

    async def structured_read(self, note: str, ctx: CatalystCtx) -> CatalystVerdict | None:
        body = {
            "contents": [{"role": "user", "parts": [{"text": step2(note, ctx)}]}],
            # NO tools: grounding + responseSchema can't be combined on Gemini.
            "generationConfig": {
                "responseMimeType": "application/json",
                "responseSchema": _RESPONSE_SCHEMA,
                "thinkingConfig": self._thinking_config(grounded=False),
            },
        }
        t0 = time.monotonic()
        j = await self._post(self._url, self._headers, body)
        latency_ms = int((time.monotonic() - t0) * 1000)
        if j is None:
            return None
        try:
            import json as _json

            cand = (j.get("candidates") or [{}])[0]
            parts = ((cand.get("content") or {}).get("parts")) or []
            raw = "".join(
                p.get("text", "") for p in parts if not p.get("thought")
            ).strip()
            data = _json.loads(raw)
            usage = j.get("usageMetadata") or {}
            tokens = {
                "in": usage.get("promptTokenCount") or 0,
                "out": usage.get("candidatesTokenCount") or 0,
                "think": usage.get("thoughtsTokenCount") or 0,
            }
            verdict = CatalystVerdict(**data)  # validator clamps confidence
            verdict.provider = self.provider
            verdict.model = self.model
            verdict.latency_ms = latency_ms
            verdict.tokens = tokens
            return verdict
        except Exception as e:
            log.warning("gemini structured parse failed: %s", e)
            return None

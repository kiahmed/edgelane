"""OpenAI overnight adapter — Responses API web_search (call 1) + strict
json_schema (call 2).

web_search bills retrieved pages as input tokens (~25-40K/call — far costlier
than Gemini grounding); budget for it if OpenAI is live. Best-effort throughout:
`_post` is the single network seam returning None on any failure, so every
method is build-body -> _post -> parse and a None short-circuits to None.
"""
from __future__ import annotations

import json
import logging
import time

import httpx

from .base import CatalystCtx, CatalystVerdict, GroundedNote
from .prompts import step1, step2

log = logging.getLogger("edgelane.simmer.llm.openai")

_ENDPOINT = "https://api.openai.com/v1/responses"

# Call-2 strict json_schema: the 7 model fields of CatalystVerdict. Meta fields
# are stamped by the adapter, never sent to the model.
_JSON_SCHEMA = {
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
    "additionalProperties": False,
    "required": [
        "catalyst_type", "deserved", "web_social_sentiment", "sustain",
        "confidence", "call", "one_liner",
    ],
}


class OpenAIOvernightLLM:
    def __init__(self, api_key: str, model: str = "gpt-6-luna",
                 effort: str = "medium", context_size: str = "medium"):
        self.provider = "openai"
        self.api_key = api_key
        self.model = model
        self.effort = effort
        self.context_size = context_size

    @property
    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json"}

    async def _post(self, url: str, headers: dict, body: dict) -> dict | None:
        """Single network seam. POST JSON; return parsed dict or None on ANY
        network/parse failure (logged). Never raises."""
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                resp = await client.post(url, headers=headers, json=body)
            return resp.json()
        except Exception as e:
            log.warning("openai _post failed: %s", e)
            return None

    @staticmethod
    def _output_text(j: dict) -> str:
        """Join all output_text content across the Responses API message items."""
        chunks: list[str] = []
        for item in (j.get("output") or []):
            if item.get("type") != "message":
                continue
            for c in (item.get("content") or []):
                if c.get("type") == "output_text":
                    chunks.append(c.get("text", ""))
        return "".join(chunks).strip()

    @staticmethod
    def _tokens(j: dict) -> dict:
        usage = j.get("usage") or {}
        details = usage.get("output_tokens_details") or {}
        return {
            "in": usage.get("input_tokens") or 0,
            "out": usage.get("output_tokens") or 0,
            "think": details.get("reasoning_tokens") or 0,
        }

    async def grounded_read(self, ctx: CatalystCtx) -> GroundedNote | None:
        def _body(tool_type: str) -> dict:
            return {
                "model": self.model,
                "input": step1(ctx),
                "tools": [{"type": tool_type, "search_context_size": self.context_size}],
                "reasoning": {"effort": self.effort},
                "max_output_tokens": 2000,
            }

        t0 = time.monotonic()
        j = await self._post(self._ENDPOINT_URL, self._headers, _body("web_search"))
        # Retry once with the legacy tool name if the model rejects "web_search".
        if j is not None:
            err = j.get("error") if isinstance(j, dict) else None
            if err and "web_search" in str(err.get("message", "")):
                j = await self._post(self._ENDPOINT_URL, self._headers,
                                     _body("web_search_preview"))
        latency_ms = int((time.monotonic() - t0) * 1000)
        if j is None:
            return None
        try:
            text = self._output_text(j)
            return GroundedNote(text=text, queries=None,
                                latency_ms=latency_ms, tokens=self._tokens(j))
        except Exception as e:
            log.warning("openai grounded parse failed: %s", e)
            return None

    async def structured_read(self, note: str, ctx: CatalystCtx) -> CatalystVerdict | None:
        body = {
            "model": self.model,
            "input": step2(note, ctx),
            # NO web tool on the extract pass.
            "reasoning": {"effort": "low"},
            "text": {"format": {
                "type": "json_schema",
                "name": "verdict",
                "strict": True,
                "schema": _JSON_SCHEMA,
            }},
        }
        t0 = time.monotonic()
        j = await self._post(self._ENDPOINT_URL, self._headers, body)
        latency_ms = int((time.monotonic() - t0) * 1000)
        if j is None:
            return None
        try:
            raw = self._output_text(j)
            data = json.loads(raw)
            verdict = CatalystVerdict(**data)  # validator clamps confidence
            verdict.provider = self.provider
            verdict.model = self.model
            verdict.latency_ms = latency_ms
            verdict.tokens = self._tokens(j)
            return verdict
        except Exception as e:
            log.warning("openai structured parse failed: %s", e)
            return None

    # Endpoint as an attribute so the network seam stays trivially mockable.
    _ENDPOINT_URL = _ENDPOINT

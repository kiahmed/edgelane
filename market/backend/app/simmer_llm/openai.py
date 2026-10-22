"""OpenAI overnight adapter — Responses API web_search (call 1) + strict
json_schema (call 2).

web_search bills retrieved pages as input tokens (~25-40K/call — far costlier
than Gemini grounding); budget for it if OpenAI is live. Best-effort throughout:
`_post` is the single network seam returning None on any failure, so every
method is build-body -> _post -> parse and a None short-circuits to None.
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import time

import httpx

from .base import CatalystCtx, CatalystVerdict, GroundedNote
from .prompts import step1, step2

log = logging.getLogger("edgelane.simmer.llm.openai")

_ENDPOINT = "https://api.openai.com/v1/responses"

# Rate-limit handling per OpenAI's guidance (developers.openai.com/api/docs/guides/
# rate-limits): a 429/5xx is retried with exponential backoff + jitter, honoring the
# Retry-After header when present. web_search calls are ~30K input tokens each, so a
# burst trips the per-minute token limit — the backoff spaces them out. A 402
# (quota/credit depleted) or 401/403 is NOT retried (it won't recover) and is logged
# loudly instead of being silently swallowed as an empty read.
_RETRY_STATUS = {429, 500, 502, 503, 504}
_MAX_RETRIES = 3
_TIMEOUT_SEC = 90.0          # a grounded read takes ~26–30s; 60s was clipping the slow ones
_MAX_RETRY_WAIT = 30.0       # cap any single backoff/Retry-After — the catalyst runs INLINE
                            # in overnight_loop, so a 60–3600s Retry-After would stall the
                            # whole loop (readings / refresh / alerts). Longer than this →
                            # give up this tick; the per-name attempt cap retries next tick.

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
        """Single network seam. POST JSON; return parsed dict, or None on failure.
        Retries 429/5xx up to _MAX_RETRIES with exponential backoff + jitter,
        honoring Retry-After. A 402/401/403 is logged and NOT retried. Never raises."""
        delay = 2.0
        for attempt in range(_MAX_RETRIES + 1):
            try:
                async with httpx.AsyncClient(timeout=_TIMEOUT_SEC) as client:
                    resp = await client.post(url, headers=headers, json=body)
            except (httpx.ConnectError, httpx.ConnectTimeout) as e:
                # The request never reached the server → safe to retry.
                log.warning("openai _post connect error (attempt %d/%d): %s",
                            attempt + 1, _MAX_RETRIES + 1, e)
                if attempt < _MAX_RETRIES:
                    await asyncio.sleep(min(delay, _MAX_RETRY_WAIT) + random.uniform(0, 1.0))
                    delay *= 2
                    continue
                return None
            except Exception as e:
                # Read timeout / transport error AFTER sending: the request may already
                # be processing (and billed), and retrying inline would both re-pay and
                # block the loop. Give up — the per-name attempt cap retries next tick.
                log.warning("openai _post read/transport error (no inline retry): %s", e)
                return None
            if resp.status_code < 300:
                try:
                    return resp.json()
                except Exception as e:
                    log.warning("openai _post: 2xx but unparseable body: %s", e)
                    return None
            detail = resp.text[:300]
            if resp.status_code in _RETRY_STATUS and attempt < _MAX_RETRIES:
                ra = (resp.headers.get("retry-after") or "").strip()
                if ra.replace(".", "", 1).isdigit():
                    ra_f = float(ra)
                    if ra_f > _MAX_RETRY_WAIT:
                        # too long to wait inline → give up this tick (attempt cap retries later)
                        log.warning("openai %s Retry-After %.0fs exceeds the %.0fs cap — giving "
                                    "up this tick: %s", resp.status_code, ra_f, _MAX_RETRY_WAIT, detail)
                        return None
                    wait = ra_f
                else:
                    wait = min(delay, _MAX_RETRY_WAIT)
                wait += random.uniform(0, 1.0)          # jitter — don't retry in lockstep
                log.warning("openai %s (attempt %d/%d) — retrying in %.1fs: %s",
                            resp.status_code, attempt + 1, _MAX_RETRIES + 1, wait, detail)
                await asyncio.sleep(wait); delay *= 2
                continue
            # 402/401/403/400, or retries exhausted → give up loudly (was silently empty)
            log.warning("openai _post failed HTTP %s (no retry): %s", resp.status_code, detail)
            return None
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

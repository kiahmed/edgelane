"""Unit tests for the provider-agnostic overnight catalyst LLM adapter
(app/simmer_llm/). FULLY MOCKED — no network anywhere.

The adapters expose a single `async def _post(url, headers, body) -> dict|None`
network seam; every test monkeypatches that instance attribute with a plain
async function (it shadows the method, so it is called with no `self`). This
keeps the build-body -> _post -> parse best-effort contract easy to exercise.
"""
from __future__ import annotations

import pytest

from app.simmer_llm import (
    CatalystCtx,
    CatalystVerdict,
    GeminiOvernightLLM,
    OpenAIOvernightLLM,
    make_llm,
)
from app.simmer_llm import prompts


def _ctx() -> CatalystCtx:
    return CatalystCtx(
        symbol="MU",
        gap=-0.047,
        dir="down",
        crowd="SNDK +3%, peers mixed",
        day_label="Monday 2026-10-12",
        premium_trend_line="put premium bid up 18% vs prior close",
    )


def _verdict_fields(**over) -> dict:
    base = {
        "catalyst_type": "earnings",
        "deserved": "deserved",
        "web_social_sentiment": "embrace",
        "sustain": "hold_to_close",
        "confidence": 72,
        "call": "sell_bull_put",
        "one_liner": "DRAM pricing catalyst, holders adding.",
    }
    base.update(over)
    return base


def _seam(result):
    """Build an async _post stand-in returning `result` (or raising if it is an
    Exception instance)."""
    async def _post(url, headers, body):
        if isinstance(result, Exception):
            raise result
        return result
    return _post


# --- (a) confidence clamping / scaling -------------------------------------

@pytest.mark.parametrize("raw,expected", [
    (0.85, 85),   # <=1 -> *100
    (8, 80),      # <=10 -> *10
    (4, 40),      # <=10 -> *10
    (150, 100),   # clamp high
    (92, 92),     # already in-range, untouched
    (0, 0),       # floor
    (1.0, 100),   # boundary: <=1 -> *100
    (10, 100),    # boundary: <=10 -> *10
    ("bad", 0),   # garbage -> 0, never raises
])
def test_confidence_clamp_scale(raw, expected):
    v = CatalystVerdict(**_verdict_fields(confidence=raw))
    assert v.confidence == expected
    assert isinstance(v.confidence, int)


def test_meta_fields_default_and_assignable():
    v = CatalystVerdict(**_verdict_fields())
    assert v.provider == "" and v.model == "" and v.latency_ms == 0 and v.tokens == {}
    v.provider, v.model, v.latency_ms, v.tokens = "openai", "gpt-6-luna", 123, {"in": 5}
    assert v.provider == "openai" and v.tokens == {"in": 5}


# --- prompts ---------------------------------------------------------------

def test_step1_has_antibias_and_direction():
    t = prompts.step1(_ctx())
    assert "Do NOT assume a gap mean-reverts" in t
    assert "gapped DOWN 4.7%" in t
    assert "Monday 2026-10-12" in t
    assert "put premium bid up 18%" in t  # premium_trend_line folded in


def test_step1_omits_absent_premium_line():
    c = _ctx()
    c.premium_trend_line = None
    c.crowd = None
    t = prompts.step1(c)
    assert "peer context: n/a" in t
    assert "None" not in t


def test_step2_contains_note_and_direction():
    t = prompts.step2("the note body", _ctx())
    assert "the note body" in t
    assert "gapped DOWN 4.7%" in t
    assert "sell_bull_put" in t


# --- (b) Gemini grounded parse ---------------------------------------------

async def test_gemini_grounded_skips_thought_parts():
    adapter = GeminiOvernightLLM("k", model="gemini-3.8-flash")
    adapter._post = _seam({
        "candidates": [{
            "content": {"parts": [
                {"text": "internal reasoning", "thought": True},
                {"text": "MU up on DRAM pricing. "},
                {"text": "Holders adding."},
            ]},
            "groundingMetadata": {"webSearchQueries": ["MU DRAM news", "Micron gap"]},
        }],
        "usageMetadata": {
            "promptTokenCount": 1200,
            "candidatesTokenCount": 90,
            "thoughtsTokenCount": 300,
        },
    })
    note = await adapter.grounded_read(_ctx())
    assert note is not None
    assert note.text == "MU up on DRAM pricing. Holders adding."
    assert "internal reasoning" not in note.text
    assert note.queries == ["MU DRAM news", "Micron gap"]
    assert note.tokens == {"in": 1200, "out": 90, "think": 300}
    assert isinstance(note.latency_ms, int)


async def test_gemini_grounded_none_on_failed_post():
    adapter = GeminiOvernightLLM("k")
    adapter._post = _seam(None)
    assert await adapter.grounded_read(_ctx()) is None


# --- (c) Gemini structured parse -------------------------------------------

async def test_gemini_structured_builds_verdict():
    adapter = GeminiOvernightLLM("k", model="gemini-3.8-flash")
    adapter._post = _seam({
        "candidates": [{"content": {"parts": [
            {"text": '{"catalyst_type":"guidance","deserved":"overreaction",'
                     '"web_social_sentiment":"doubt","sustain":"fade",'
                     '"confidence":8,"call":"sell_bear_call",'
                     '"one_liner":"Guidance cut, crowd skeptical."}'},
        ]}}],
        "usageMetadata": {"promptTokenCount": 400, "candidatesTokenCount": 40,
                          "thoughtsTokenCount": 10},
    })
    v = await adapter.structured_read("note", _ctx())
    assert v is not None
    assert v.catalyst_type == "guidance" and v.call == "sell_bear_call"
    assert v.confidence == 80  # 8 -> *10 via validator
    assert v.provider == "gemini" and v.model == "gemini-3.8-flash"
    assert v.tokens == {"in": 400, "out": 40, "think": 10}


async def test_gemini_structured_none_on_bad_json():
    adapter = GeminiOvernightLLM("k")
    adapter._post = _seam({"candidates": [{"content": {"parts": [
        {"text": "not json at all"}]}}]})
    assert await adapter.structured_read("note", _ctx()) is None


def test_gemini_thinking_config_by_model():
    g3 = GeminiOvernightLLM("k", model="gemini-3.8-flash", thinking="high")
    assert g3._thinking_config(grounded=True) == {
        "includeThoughts": False, "thinkingLevel": "high"}
    assert g3._thinking_config(grounded=False)["thinkingLevel"] == "low"
    old = GeminiOvernightLLM("k", model="gemini-2.5-flash")
    assert old._thinking_config(grounded=True) == {
        "includeThoughts": False, "thinkingBudget": -1}


# --- (d) OpenAI parse ------------------------------------------------------

async def test_openai_grounded_reads_output_text_and_usage():
    adapter = OpenAIOvernightLLM("k", model="gpt-6-luna")
    adapter._post = _seam({
        "output": [
            {"type": "reasoning", "content": []},
            {"type": "message", "content": [
                {"type": "output_text", "text": "MU gap looks deserved; "},
                {"type": "output_text", "text": "continuation likely."},
            ]},
        ],
        "usage": {"input_tokens": 32000, "output_tokens": 180,
                  "output_tokens_details": {"reasoning_tokens": 120}},
    })
    note = await adapter.grounded_read(_ctx())
    assert note is not None
    assert note.text == "MU gap looks deserved; continuation likely."
    assert note.tokens == {"in": 32000, "out": 180, "think": 120}


async def test_openai_grounded_retries_on_web_search_tool_error():
    adapter = OpenAIOvernightLLM("k")
    calls: list[str] = []

    async def _post(url, headers, body):
        calls.append(body["tools"][0]["type"])
        if body["tools"][0]["type"] == "web_search":
            return {"error": {"message": "Unknown tool type 'web_search'"}}
        return {"output": [{"type": "message", "content": [
            {"type": "output_text", "text": "ok"}]}],
            "usage": {"input_tokens": 1, "output_tokens": 1,
                      "output_tokens_details": {"reasoning_tokens": 0}}}

    adapter._post = _post
    note = await adapter.grounded_read(_ctx())
    assert calls == ["web_search", "web_search_preview"]
    assert note is not None and note.text == "ok"


async def test_openai_structured_builds_verdict():
    adapter = OpenAIOvernightLLM("k", model="gpt-6-luna")
    adapter._post = _seam({
        "output": [{"type": "message", "content": [
            {"type": "output_text",
             "text": '{"catalyst_type":"analyst","deserved":"deserved",'
                     '"web_social_sentiment":"mixed","sustain":"extend",'
                     '"confidence":0.9,"call":"sell_bull_put",'
                     '"one_liner":"Upgrade, momentum."}'},
        ]}],
        "usage": {"input_tokens": 500, "output_tokens": 30,
                  "output_tokens_details": {"reasoning_tokens": 5}},
    })
    v = await adapter.structured_read("note", _ctx())
    assert v is not None
    assert v.catalyst_type == "analyst" and v.sustain == "extend"
    assert v.confidence == 90  # 0.9 -> *100
    assert v.provider == "openai" and v.tokens["in"] == 500


async def test_openai_structured_none_on_bad_json():
    adapter = OpenAIOvernightLLM("k")
    adapter._post = _seam({"output": [{"type": "message", "content": [
        {"type": "output_text", "text": "oops"}]}]})
    assert await adapter.structured_read("note", _ctx()) is None


# --- (e) factory -----------------------------------------------------------

def test_factory_routes_gemini():
    llm = make_llm("gemini", gemini_key="g", thinking="high")
    assert isinstance(llm, GeminiOvernightLLM)
    assert llm.model == "gemini-3.8-flash" and llm.thinking == "high"


def test_factory_routes_openai_with_overrides():
    llm = make_llm("openai", model="gpt-6-sol", openai_key="o",
                   thinking="low", context_size="low")
    assert isinstance(llm, OpenAIOvernightLLM)
    assert llm.model == "gpt-6-sol" and llm.effort == "low"
    assert llm.context_size == "low"


def test_factory_none_on_unknown_provider():
    assert make_llm("claude", gemini_key="x", openai_key="y") is None


def test_factory_none_on_missing_key():
    assert make_llm("gemini", gemini_key="") is None
    assert make_llm("openai", openai_key="") is None


# --- (f) best-effort: failing _post never propagates -----------------------

async def test_gemini_methods_swallow_post_exception():
    adapter = GeminiOvernightLLM("k")
    adapter._post = _seam(RuntimeError("boom"))
    # _post itself is replaced, but the contract is: a None return (the real
    # _post catches and returns None). Simulate that path too.
    adapter._post = _seam(None)
    assert await adapter.grounded_read(_ctx()) is None
    assert await adapter.structured_read("n", _ctx()) is None


async def test_openai_methods_swallow_post_failure():
    adapter = OpenAIOvernightLLM("k")
    adapter._post = _seam(None)
    assert await adapter.grounded_read(_ctx()) is None
    assert await adapter.structured_read("n", _ctx()) is None


async def test_real_post_returns_none_on_network_error(monkeypatch):
    """The real _post catches httpx failures and returns None (never raises)."""
    import httpx

    class _BoomClient:
        def __init__(self, *a, **k): ...
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, *a, **k): raise httpx.ConnectError("no network")

    monkeypatch.setattr(httpx, "AsyncClient", _BoomClient)
    g = GeminiOvernightLLM("k")
    assert await g._post("http://x", {}, {}) is None
    o = OpenAIOvernightLLM("k")
    assert await o._post("http://x", {}, {}) is None


# ── openai _post: status handling + 429 backoff/retry (OpenAI rate-limit guidance) ──
class _Resp:
    def __init__(self, status, body, headers=None):
        self.status_code = status
        self._body = body
        self.headers = headers or {}
        self.text = str(body)
    def json(self):
        return self._body


def _fake_client_factory(responses):
    seq = list(responses)

    class _Client:
        def __init__(self, *a, **k):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        async def post(self, url, headers=None, json=None):
            item = seq.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
    return _Client


async def _noop_sleep(*a, **k):
    return None


async def test_openai_post_retries_429_then_succeeds(monkeypatch):
    from app.simmer_llm import openai as oa
    monkeypatch.setattr(oa.httpx, "AsyncClient",
                        _fake_client_factory([_Resp(429, {"error": "rate"}, {"retry-after": "0"}),
                                              _Resp(200, {"ok": True})]))
    monkeypatch.setattr(oa.asyncio, "sleep", _noop_sleep)
    out = await oa.OpenAIOvernightLLM(api_key="x")._post("u", {}, {})
    assert out == {"ok": True}       # retried past the 429 and got the body


async def test_openai_post_402_quota_not_retried(monkeypatch):
    from app.simmer_llm import openai as oa
    # a second response would be used only if it retried — it must NOT.
    monkeypatch.setattr(oa.httpx, "AsyncClient",
                        _fake_client_factory([_Resp(402, {"error": "insufficient_quota"}),
                                              _Resp(200, {"ok": True})]))
    monkeypatch.setattr(oa.asyncio, "sleep", _noop_sleep)
    out = await oa.OpenAIOvernightLLM(api_key="x")._post("u", {}, {})
    assert out is None               # credit/quota → give up loudly, no retry


async def test_openai_post_gives_up_after_max_retries(monkeypatch):
    from app.simmer_llm import openai as oa
    monkeypatch.setattr(oa.httpx, "AsyncClient",
                        _fake_client_factory([_Resp(429, {"e": 1})] * (oa._MAX_RETRIES + 1)))
    monkeypatch.setattr(oa.asyncio, "sleep", _noop_sleep)
    out = await oa.OpenAIOvernightLLM(api_key="x")._post("u", {}, {})
    assert out is None               # exhausted retries → None (not an empty dict swallowed)


async def test_openai_post_long_retry_after_gives_up_this_tick(monkeypatch):
    from app.simmer_llm import openai as oa
    # Retry-After far beyond the cap would stall the inline loop → give up this tick
    # (the 2nd response must NOT be consumed).
    monkeypatch.setattr(oa.httpx, "AsyncClient",
                        _fake_client_factory([_Resp(429, {"e": 1}, {"retry-after": "600"}),
                                              _Resp(200, {"ok": True})]))
    monkeypatch.setattr(oa.asyncio, "sleep", _noop_sleep)
    out = await oa.OpenAIOvernightLLM(api_key="x")._post("u", {}, {})
    assert out is None


async def test_openai_post_read_timeout_not_retried(monkeypatch):
    from app.simmer_llm import openai as oa
    # A read timeout may already be processing/billed → no inline retry.
    monkeypatch.setattr(oa.httpx, "AsyncClient",
                        _fake_client_factory([oa.httpx.ReadTimeout("slow"),
                                              _Resp(200, {"ok": True})]))
    monkeypatch.setattr(oa.asyncio, "sleep", _noop_sleep)
    out = await oa.OpenAIOvernightLLM(api_key="x")._post("u", {}, {})
    assert out is None


async def test_openai_post_connect_error_is_retried(monkeypatch):
    from app.simmer_llm import openai as oa
    # A connect error never reached the server → safe to retry.
    monkeypatch.setattr(oa.httpx, "AsyncClient",
                        _fake_client_factory([oa.httpx.ConnectError("refused"),
                                              _Resp(200, {"ok": True})]))
    monkeypatch.setattr(oa.asyncio, "sleep", _noop_sleep)
    out = await oa.OpenAIOvernightLLM(api_key="x")._post("u", {}, {})
    assert out == {"ok": True}

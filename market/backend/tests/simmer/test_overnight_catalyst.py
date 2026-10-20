"""Overnight catalyst analyzer orchestration (docs/simmer_overnight_llm_spec.md).
Fully mocked — no network, no DB. Verifies: read-only persistence to the SEPARATE
analysis table, live+shadow logging, all combiner modes computed, the enabled gate,
and readings/outcome stamping."""
from __future__ import annotations

import types

import pytest

from app import simmer_overnight_catalyst as cat
from app.simmer_llm import CatalystVerdict, GroundedNote


class _FakeLLM:
    def __init__(self, provider, call):
        self.provider, self.model, self._call = provider, f"{provider}-x", call

    async def grounded_read(self, ctx):
        return GroundedNote(text=f"note for {ctx.symbol}", queries=["q"], latency_ms=100,
                            tokens={"in": 10, "out": 5, "think": 3})

    async def structured_read(self, note, ctx):
        return CatalystVerdict(catalyst_type="analyst", deserved="overreaction",
                               web_social_sentiment="doubt", sustain="extend", confidence=82,
                               call=self._call, one_liner="x")


@pytest.fixture
def wired(monkeypatch):
    saved = {}

    async def _list(session):
        return [{"symbol": "MU", "expiration": "2026-11-13", "session_date": session,
                 "dir": "down", "gap": -0.047, "crowd": "sector_wide",
                 "strikes": [100.0], "side": "put", "spot_baseline": 100.0,
                 "verdict": {"call": "wait", "premium_trend": "holding",
                             "underlying_trend": "holding", "confidence": 50}}]

    async def _upsert(row):
        saved.setdefault(row["symbol"], {}).update(row)
        return True

    async def _get(sym, session):
        return saved.get(sym)

    async def _refresh():    # keep run_catalyst's pre-read hermetic (no network)
        return True
    cat._catalyst_attempts.clear()   # module-level retry counter — start each test clean
    monkeypatch.setattr(cat.simmer_config, "refresh_setup", _refresh)
    monkeypatch.setattr(cat.supabase_admin, "list_simmer_overnight", _list)
    monkeypatch.setattr(cat.supabase_admin, "upsert_simmer_overnight_analysis", _upsert)
    monkeypatch.setattr(cat.supabase_admin, "get_simmer_overnight_analysis", _get)
    monkeypatch.setattr(cat, "get_settings",
                        lambda: types.SimpleNamespace(gemini_api_key="g", openai_api_key="o"))
    # live=openai says sell_bear_call, shadow=gemini says sell_bull_put
    monkeypatch.setattr(cat, "_make_llm", lambda provider, model, s, ovn: _FakeLLM(
        provider, "sell_bear_call" if provider == "openai" else "sell_bull_put"))
    return saved


def _ovn(monkeypatch, **over):
    base = {"enabled": True, "provider": "openai", "model": "gpt-6-luna", "thinking": "medium",
            "context_size": "medium", "premium_context_to_llm": True,
            "decision_mode": "combiner_blended", "shadow_provider": "gemini",
            "shadow_model": "gemini-3.8-flash"}
    base.update(over)
    monkeypatch.setattr(cat.simmer_config, "overnight_llm", lambda: base)


async def test_disabled_is_noop(wired, monkeypatch):
    _ovn(monkeypatch, enabled=False)
    res = await cat.run_catalyst("2026-10-12")
    assert res == {"enabled": False} and not wired


async def test_run_persists_live_shadow_and_all_combiner_modes(wired, monkeypatch):
    _ovn(monkeypatch)
    res = await cat.run_catalyst("2026-10-12")
    assert res["analyzed"] == 1
    row = wired["MU"]
    assert row["llm"]["provider"] == "openai" and row["llm"]["call"] == "sell_bear_call"
    assert row["llm_shadow"]["provider"] == "gemini" and row["llm_shadow"]["call"] == "sell_bull_put"
    comb = row["combiner"]
    # the two candidate modes present; blended promotes the premium `wait` to bear_call
    assert set(comb) >= {"llm_only", "combiner_blended"}
    assert comb["mode"] == "combiner_blended"
    assert comb["final"]["call"] == "sell_bear_call"
    assert comb["llm_only"]["call"] == "sell_bear_call"
    assert comb["combiner_blended"]["call"] == "sell_bear_call"
    # the CORE table was never written (separate-table guarantee)
    assert "verdict" not in row       # we only ever wrote analysis fields


def test_step1_prompt_carries_antibounce_and_premium_line():
    from app.simmer_llm import CatalystCtx
    from app.simmer_llm.prompts import step1
    base = dict(symbol="MU", gap=-0.05, dir="down", crowd="sector_wide",
                day_label="Monday 2026-10-12")
    p = step1(CatalystCtx(premium_trend_line=None, **base))
    assert "mean-revert" in p.lower() and "continuation" in p.lower()   # the Gemini-bias fix
    p2 = step1(CatalystCtx(premium_trend_line="Option tape: premium holding.", **base))
    assert "Option tape: premium holding." in p2                        # premium context folded in


async def test_premium_context_flag_controls_grounded_input(wired, monkeypatch):
    """premium_context_to_llm gates whether the premium-trend line reaches call 1."""
    seen: list = []

    class _Rec:
        provider, model = "openai", "x"
        async def grounded_read(self, ctx):
            seen.append(ctx.premium_trend_line)
            return GroundedNote(text="n", queries=None, latency_ms=1, tokens={})
        async def structured_read(self, note, ctx):
            return CatalystVerdict(catalyst_type="other", deserved="unclear",
                                   web_social_sentiment="mixed", sustain="hold_to_close",
                                   confidence=50, call="wait", one_liner="x")
    monkeypatch.setattr(cat, "_make_llm", lambda *a, **k: _Rec())

    _ovn(monkeypatch, premium_context_to_llm=True, shadow_provider="")
    await cat.run_catalyst("2026-10-12")
    assert seen[-1] and "premium" in seen[-1].lower()

    seen.clear(); wired.clear()         # clear so the skip guard doesn't short-circuit
    _ovn(monkeypatch, premium_context_to_llm=False, shadow_provider="")
    await cat.run_catalyst("2026-10-12")
    assert seen[-1] is None


async def test_run_catalyst_skips_already_analyzed_today(wired, monkeypatch):
    """Restart-safe: a name with a PERSISTED llm read is skipped, so a mid-session
    restart neither re-pays for the LLM nor overwrites the 09:25 prediction."""
    _ovn(monkeypatch)
    wired["MU"] = {"symbol": "MU", "session_date": "2026-10-12",
                   "llm": {"provider": "openai", "call": "sell_bull_put"}}  # from this morning
    res = await cat.run_catalyst("2026-10-12")
    assert res["analyzed"] == 0 and res["skipped"] == 1
    assert wired["MU"]["llm"]["call"] == "sell_bull_put"   # untouched (no overwrite)


async def test_run_catalyst_caps_retries_on_persistent_failure(wired, monkeypatch):
    """Cost guard: a name whose read keeps failing is retried a few times, then
    left — so one broken name can't rack up paid LLM calls every tick to 10:30."""
    _ovn(monkeypatch)
    cat._catalyst_attempts.clear()
    calls = {"n": 0}

    async def _boom(mv, premium, session, s, ovn):
        calls["n"] += 1
        raise RuntimeError("grounded read failed")
    monkeypatch.setattr(cat, "analyze_one", _boom)

    res = None
    for _ in range(5):                     # loop would retry every tick
        res = await cat.run_catalyst("2026-10-12")
    assert calls["n"] == cat._MAX_CATALYST_ATTEMPTS     # tried 3×, then stopped
    assert res["analyzed"] == 0 and res["gave_up"] == 1  # capped on the later passes


async def test_capture_reading_idempotent_per_label(wired, monkeypatch):
    _ovn(monkeypatch)

    class _Prov:
        async def chain(self, sym, exp):
            return [{"side": "put", "strike": 100.0, "bid": 1.0, "ask": 1.2, "volume": 50}]
        async def quote(self, sym):
            return {"last": 99.0}

    assert await cat.capture_reading(_Prov(), "10:00", "2026-10-12") == 1
    assert len(wired["MU"]["readings"]) == 1
    # a restart re-calling the same label must NOT append a duplicate
    assert await cat.capture_reading(_Prov(), "10:00", "2026-10-12") == 0
    assert len(wired["MU"]["readings"]) == 1


async def test_refresh_combiner_recombines_against_live_premium_no_llm(wired, monkeypatch):
    """The catalyst read is frozen; the combiner re-runs against the LIVE premium
    verdict each refresh — pure math, no new LLM call."""
    _ovn(monkeypatch, decision_mode="combiner_blended")
    monkeypatch.setattr(cat, "_make_llm",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no LLM in refresh")))
    wired["MU"] = {"symbol": "MU", "session_date": "2026-10-12",
                   "llm": {"call": "sell_bear_call", "sustain": "extend", "confidence": 82}}

    async def _list_an(session):
        return [wired["MU"]]
    monkeypatch.setattr(cat.supabase_admin, "list_simmer_overnight_analysis", _list_an)

    # fixture's list_simmer_overnight returns MU with a live premium verdict of `wait`;
    # blended promotes the frozen bear_call → the combiner tracks the live premium.
    n = await cat.refresh_combiner("2026-10-12")
    assert n == 1
    assert wired["MU"]["premium"]["call"] == "wait"              # live premium snapshot persisted
    assert wired["MU"]["combiner"]["final"]["call"] == "sell_bear_call"


async def test_refresh_combiner_freezes_decision_at_cutoff(wired, monkeypatch):
    """Past the cutoff (lock=True) the scored decision is stamped `decided_at` and
    frozen — a later refresh (even with the live premium flipped) must NOT change
    the scored call, so the bake-off has no late-session lookahead."""
    _ovn(monkeypatch, decision_mode="combiner_blended")
    monkeypatch.setattr(cat, "_make_llm",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no LLM in refresh")))
    wired["MU"] = {"symbol": "MU", "session_date": "2026-10-12",
                   "llm": {"call": "sell_bear_call", "sustain": "extend", "confidence": 82}}

    async def _list_an(session):
        return [wired["MU"]]
    monkeypatch.setattr(cat.supabase_admin, "list_simmer_overnight_analysis", _list_an)

    # lock at the cutoff → decision frozen (premium `wait` + strong catalyst → promote)
    assert await cat.refresh_combiner("2026-10-12", lock=True) == 1
    frozen = wired["MU"]["combiner"]["final"]["call"]
    assert frozen == "sell_bear_call" and wired["MU"]["combiner"].get("decided_at")

    # the live premium now flips to the opposite committed side…
    async def _list_ov(session):
        return [{"symbol": "MU", "expiration": "2026-11-13",
                 "verdict": {"call": "sell_bull_put", "premium_trend": "fading",
                             "underlying_trend": "holding", "confidence": 80}}]
    monkeypatch.setattr(cat.supabase_admin, "list_simmer_overnight", _list_ov)
    # …but a frozen row is skipped, so the scored call is unchanged.
    assert await cat.refresh_combiner("2026-10-12", lock=True) == 0
    assert wired["MU"]["combiner"]["final"]["call"] == frozen


async def test_capture_reading_disabled_is_noop(wired, monkeypatch):
    _ovn(monkeypatch, enabled=False)

    class _Prov:
        async def chain(self, sym, exp):
            raise AssertionError("must not hit the provider when disabled")
        async def quote(self, sym):
            raise AssertionError("must not hit the provider when disabled")

    assert await cat.capture_reading(_Prov(), "10:00", "2026-10-12") == 0


async def test_shadow_disabled_leaves_shadow_empty(wired, monkeypatch):
    _ovn(monkeypatch, shadow_provider="")
    await cat.run_catalyst("2026-10-12")
    assert wired["MU"]["llm_shadow"] == {}


async def test_capture_reading_appends_and_stamps_outcome(wired, monkeypatch):
    _ovn(monkeypatch)

    class _Prov:
        async def chain(self, sym, exp):
            return [{"side": "put", "strike": 100.0, "bid": 1.0, "ask": 1.2, "volume": 50}]
        async def quote(self, sym):
            return {"last": 96.0}         # 4% below the 100.0 premarket level → extended down

    # seed a premarket anchor + both candidate calls as the 09:25/10:00 steps would:
    # llm_only says bull_put (wrong — name fell), combiner_blended says bear_call (right).
    wired["MU"] = {"symbol": "MU", "session_date": "2026-10-12", "gap": -0.047,
                   "premarket": {"spot": 100.0}, "readings": [],
                   "combiner": {"final": {"call": "sell_bear_call"},
                                "llm_only": {"call": "sell_bull_put"},
                                "combiner_blended": {"call": "sell_bear_call"}}}
    n = await cat.capture_reading(_Prov(), "16:00", "2026-10-12")
    assert n == 1
    row = wired["MU"]
    assert row["readings"][-1]["t"] == "16:00" and row["readings"][-1]["spot"] == 96.0
    assert row["close"]["intraday_pct"] == -4.0
    assert row["outcome"] == "extended"            # kept falling in the gap direction
    assert row["direction_right"] is True          # final (bear_call) correct: name did not rally
    # BOTH modes scored independently for the bake-off:
    assert row["close"]["combiner_blended_call"] == "sell_bear_call"
    assert row["close"]["combiner_blended_right"] is True
    assert row["close"]["llm_only_call"] == "sell_bull_put"
    assert row["close"]["llm_only_right"] is False   # bull_put wrong: name broke down

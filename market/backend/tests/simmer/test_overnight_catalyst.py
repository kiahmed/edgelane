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

    # the live premium now flips HARD to the opposite committed side (big drift)…
    async def _list_ov(session):
        return [{"symbol": "MU", "expiration": "2026-11-13",
                 "verdict": {"call": "sell_bull_put", "premium_trend": "fading",
                             "underlying_trend": "holding", "confidence": 80,
                             "mean_ratio": 0.85}}]   # toward-side premium down 15% → material
    monkeypatch.setattr(cat.supabase_admin, "list_simmer_overnight", _list_ov)
    # The FROZEN scored call is never re-scored (bake-off integrity preserved)…
    assert await cat.refresh_combiner("2026-10-12", lock=True) == 1
    assert wired["MU"]["combiner"]["final"]["call"] == frozen
    assert wired["MU"]["combiner"].get("decided_at")
    # …but the Phase A reactive overlay DOES flip the user-facing call to the tape.
    assert wired["MU"]["combiner"]["premium_reactive"]["call"] == "sell_bull_put"
    assert wired["MU"]["combiner"]["premium_reactive"]["source"] == "premium_override"


async def test_premium_drift_simulation_reactive_tracks_the_tape(wired, monkeypatch):
    """Phase A end-to-end: a down-gap name is scored sell_bear_call and FROZEN at
    10:00. Then drive the live premium through a realistic intraday path and assert
    the never-frozen reactive overlay reacts the way a market-maker would — holds
    while the tape confirms, stands down on mild drift, FLIPS to bull_put when the
    premium materially cancels the thesis, and re-commits when the tape comes back.
    Exercises the real refresh_combiner wiring (not just the pure fn)."""
    _ovn(monkeypatch, decision_mode="combiner_blended")
    monkeypatch.setattr(cat, "_make_llm",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no LLM in refresh")))
    # a scored bear-call, already frozen at the cutoff
    wired["MU"] = {"symbol": "MU", "session_date": "2026-10-12",
                   "llm": {"call": "sell_bear_call", "sustain": "extend", "confidence": 80},
                   "combiner": {"mode": "combiner_blended", "decided_at": "2026-10-12T10:00:00-04:00",
                                "final": {"call": "sell_bear_call", "confidence": 78}}}

    async def _list_an(session):
        return [wired["MU"]]
    monkeypatch.setattr(cat.supabase_admin, "list_simmer_overnight_analysis", _list_an)

    async def tick(call, premium_trend, underlying_trend, conf, mean_ratio):
        async def _list_ov(session):
            return [{"symbol": "MU", "expiration": "2026-11-13",
                     "verdict": {"call": call, "premium_trend": premium_trend,
                                 "underlying_trend": underlying_trend, "confidence": conf,
                                 "mean_ratio": mean_ratio}}]
        monkeypatch.setattr(cat.supabase_admin, "list_simmer_overnight", _list_ov)
        await cat.refresh_combiner("2026-10-12", lock=True)
        return wired["MU"]["combiner"]["premium_reactive"]

    # t0 — tape CONFIRMS the drop (premium building, still extending down) → hold bear_call
    r = await tick("sell_bear_call", "building", "extending", 75, 1.18)
    assert r["call"] == "sell_bear_call" and r["source"] == "frozen_hold"

    # t1 — MILD drift against us (fading, stock holding, only 6% premium decay) → stand down
    r = await tick("wait", "fading", "holding", 50, 0.94)
    assert r["call"] == "wait" and r["source"] == "premium_drift_standdown"

    # t2 — MATERIAL flip: premium now names the opposite side, 16% decay, tape retracing
    #      up, real conviction → premium CANCELS the stale bear_call, flip to bull_put
    r = await tick("sell_bull_put", "fading", "retracing", 72, 0.84)
    assert r["call"] == "sell_bull_put" and r["source"] == "premium_override"

    # t3 — the move resumes down, premium rebuilds → the overlay re-commits to bear_call
    r = await tick("sell_bear_call", "building", "extending", 70, 1.15)
    assert r["call"] == "sell_bear_call" and r["source"] == "frozen_hold"

    # the FROZEN scored call never moved through any of it (bake-off integrity)
    assert wired["MU"]["combiner"]["final"]["call"] == "sell_bear_call"
    assert wired["MU"]["combiner"]["decided_at"] == "2026-10-12T10:00:00-04:00"


async def test_reactive_scored_call_freezes_at_noon_no_lookahead(wired, monkeypatch):
    """The SCORED reactive call freezes at the noon checkpoint. A later flip toward
    the realized move must NOT become the scored call — otherwise the close grade
    gets lookahead and flatters the reactive candidate. The late flip is still logged
    in premium_reactive_history (non-predictive), just never graded as the decision."""
    _ovn(monkeypatch, decision_mode="combiner_blended")
    monkeypatch.setattr(cat, "_make_llm",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no LLM in refresh")))
    wired["MU"] = {"symbol": "MU", "session_date": "2026-10-12",
                   "llm": {"call": "sell_bear_call", "sustain": "extend", "confidence": 80},
                   "combiner": {"mode": "combiner_blended", "decided_at": "2026-10-12T10:00:00-04:00",
                                "final": {"call": "sell_bear_call", "confidence": 78}}}

    async def _list_an(session):
        return [wired["MU"]]
    monkeypatch.setattr(cat.supabase_admin, "list_simmer_overnight_analysis", _list_an)

    async def set_live(call, premium_trend, underlying_trend, conf, mean_ratio):
        async def _list_ov(session):
            return [{"symbol": "MU", "expiration": "2026-11-13",
                     "verdict": {"call": call, "premium_trend": premium_trend,
                                 "underlying_trend": underlying_trend, "confidence": conf,
                                 "mean_ratio": mean_ratio}}]
        monkeypatch.setattr(cat.supabase_admin, "list_simmer_overnight", _list_ov)

    # noon checkpoint: tape still backs the scored bear_call → reactive freezes holding it
    await set_live("sell_bear_call", "building", "extending", 75, 1.15)
    await cat.refresh_combiner("2026-10-12", lock=True, reactive_lock=True)
    reactive = wired["MU"]["combiner"]["premium_reactive"]
    assert reactive["call"] == "sell_bear_call" and reactive.get("decided_at")

    # AFTER noon the tape flips hard to the (realized) opposite side — a late flip
    await set_live("sell_bull_put", "fading", "holding", 80, 0.82)
    await cat.refresh_combiner("2026-10-12", lock=True, reactive_lock=True)
    # scored call is UNCHANGED (frozen at noon) — no lookahead credit for the late flip
    assert wired["MU"]["combiner"]["premium_reactive"]["call"] == "sell_bear_call"
    # …but the live flip is captured in the (non-predictive) history
    assert wired["MU"]["combiner"]["premium_reactive_history"][-1]["call"] == "sell_bull_put"


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


async def test_reactive_graded_from_freeze_spot_not_premarket(wired, monkeypatch):
    """A noon flip after a MORNING reversal must not be credited for the morning move.
    Down-gap name reversed UP to 106 by noon; the reactive call flipped to bull_put and
    froze there (spot_at=106). It then FELL to 103 into the close. Graded from 09:25
    (100→103, +3%) bull_put would look RIGHT — lookahead for the pre-noon rip. Graded
    from the freeze spot (106→103, −2.8%) it is correctly WRONG."""
    _ovn(monkeypatch)

    class _Prov:
        async def chain(self, sym, exp):
            return [{"side": "put", "strike": 100.0, "bid": 1.0, "ask": 1.2, "volume": 50}]
        async def quote(self, sym):
            return {"last": 103.0}          # closed at 103, below the noon freeze of 106

    wired["MU"] = {"symbol": "MU", "session_date": "2026-10-12", "gap": -0.05,
                   "premarket": {"spot": 100.0}, "readings": [],
                   "combiner": {"final": {"call": "sell_bear_call"},
                                "llm_only": {"call": "sell_bear_call"},
                                "combiner_blended": {"call": "sell_bear_call"},
                                "premium_reactive": {"call": "sell_bull_put",
                                                     "decided_at": "2026-10-12T12:00:00-04:00",
                                                     "spot_at": 106.0},
                                "premium_reactive_history": [{"call": "sell_bull_put"}]}}
    assert await cat.capture_reading(_Prov(), "16:00", "2026-10-12") == 1
    close = wired["MU"]["close"]
    assert close["premium_reactive_from"] == 106.0          # graded from the freeze, not 100
    assert close["premium_reactive_right"] is False         # 106→103 fell → bull_put wrong
    # a 09:25-based grade would have (wrongly) called it right:
    from app.simmer_overnight_catalyst import _direction_right
    assert _direction_right("sell_bull_put", -0.05, 100.0, 103.0) is True


# ── Phase B: _recall_target (premium-triggered LLM recall) ──────────────────────
def test_recall_target_only_on_reversal_against_the_09_25_read():
    from app.simmer_overnight_catalyst import _recall_target
    a = {"llm": {"call": "sell_bear_call"}}
    # tape overrode to the OPPOSITE side → re-validate that reversal
    assert _recall_target(a, {"call": "sell_bull_put", "source": "premium_override"}) == "sell_bull_put"
    # override but SAME side as the 09:25 read → nothing to re-check
    assert _recall_target(a, {"call": "sell_bear_call", "source": "premium_override"}) is None
    # not an override (holding / standing down) → no recall
    assert _recall_target(a, {"call": "sell_bull_put", "source": "frozen_hold"}) is None
    assert _recall_target(a, {"call": "wait", "source": "premium_drift_standdown"}) is None
    # already recalled for this reversed call → don't re-spend
    a2 = {"llm": {"call": "sell_bear_call"}, "llm_recall": {"for_call": "sell_bull_put"}}
    assert _recall_target(a2, {"call": "sell_bull_put", "source": "premium_override"}) is None
    # a DIFFERENT reversed call than last time → recall again
    assert _recall_target({"llm": {"call": "sell_bull_put"}, "llm_recall": {"for_call": "wait"}},
                          {"call": "sell_bear_call", "source": "premium_override"}) == "sell_bear_call"


async def test_run_recall_revalidates_a_reversal_then_dedupes(monkeypatch):
    """Phase B: an intraday reversal against the 09:25 read triggers ONE grounded
    recall that annotates the catalyst reasoning; a second pass for the same reversal
    does not re-spend."""
    _ovn(monkeypatch, recall_enabled=True)
    saved: dict = {}

    async def _list_an(session):
        row = {"symbol": "MU", "session_date": "2026-10-12", "dir": "down", "gap": -0.05,
               "llm": {"call": "sell_bear_call", "why_moved": "DRAM glut"},
               "combiner": {"final": {"call": "sell_bear_call", "confidence": 80},
                            "decided_at": "2026-10-12T10:00:00-04:00"}}
        if "MU" in saved and saved["MU"].get("llm_recall"):
            row["llm_recall"] = saved["MU"]["llm_recall"]       # carry the prior recall
        return [row]

    async def _list_ov(session):
        return [{"symbol": "MU", "expiration": "2026-11-13", "dir": "down", "gap": -0.05,
                 "crowd": "sector_wide",
                 "verdict": {"call": "sell_bull_put", "premium_trend": "fading",
                             "underlying_trend": "retracing", "confidence": 72, "mean_ratio": 0.84}}]

    async def _upsert(row):
        saved.setdefault(row["symbol"], {}).update(row); return True

    async def _refresh():
        return True
    monkeypatch.setattr(cat.simmer_config, "refresh_setup", _refresh)
    monkeypatch.setattr(cat.supabase_admin, "list_simmer_overnight_analysis", _list_an)
    monkeypatch.setattr(cat.supabase_admin, "list_simmer_overnight", _list_ov)
    monkeypatch.setattr(cat.supabase_admin, "upsert_simmer_overnight_analysis", _upsert)
    monkeypatch.setattr(cat, "get_settings",
                        lambda: types.SimpleNamespace(gemini_api_key="g", openai_api_key="o"))

    class _Rec:
        provider, model = "openai", "gpt-6-luna"
        async def grounded_read(self, ctx):
            assert "REVERSAL" in (ctx.premium_trend_line or "")   # recall context reached the LLM
            return GroundedNote(text="fears overblown", queries=["mu"], latency_ms=1, tokens={})
        async def structured_read(self, note, ctx):
            return CatalystVerdict(catalyst_type="other", deserved="overreaction",
                                   web_social_sentiment="doubt", sustain="hold_to_close",
                                   confidence=68, call="sell_bull_put", one_liner="overblown")
    monkeypatch.setattr(cat, "_make_llm", lambda *a, **k: _Rec())

    assert await cat.run_recall("2026-10-12") == {"enabled": True, "recalled": 1}
    rec = saved["MU"]["llm_recall"]
    assert rec["for_call"] == "sell_bull_put" and rec["deserved"] == "overreaction" and rec["count"] == 1

    # same reversal still standing → no second paid call
    monkeypatch.setattr(cat, "_make_llm",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not recall twice")))
    assert (await cat.run_recall("2026-10-12"))["recalled"] == 0


async def test_run_recall_disabled_is_noop(monkeypatch):
    _ovn(monkeypatch, recall_enabled=False)

    async def _refresh():
        return True
    monkeypatch.setattr(cat.simmer_config, "refresh_setup", _refresh)
    assert await cat.run_recall("2026-10-12") == {"enabled": False}


async def test_run_recall_caps_failed_attempts(monkeypatch):
    """A recall whose grounded read keeps FAILING persists nothing (dedup never sets),
    so it must be attempt-capped — otherwise it re-calls (pays) every tick while the
    reversal holds."""
    _ovn(monkeypatch, recall_enabled=True)
    cat._recall_attempts.clear()
    calls = {"n": 0}

    async def _list_an(session):
        return [{"symbol": "MU", "session_date": "2026-10-12", "dir": "down", "gap": -0.05,
                 "llm": {"call": "sell_bear_call"},
                 "combiner": {"final": {"call": "sell_bear_call", "confidence": 80},
                              "decided_at": "2026-10-12T10:00:00-04:00"}}]

    async def _list_ov(session):
        return [{"symbol": "MU", "expiration": "2026-11-13", "dir": "down", "gap": -0.05,
                 "crowd": "sector_wide",
                 "verdict": {"call": "sell_bull_put", "premium_trend": "fading",
                             "underlying_trend": "retracing", "confidence": 72, "mean_ratio": 0.84}}]

    async def _refresh():
        return True
    monkeypatch.setattr(cat.simmer_config, "refresh_setup", _refresh)
    monkeypatch.setattr(cat.supabase_admin, "list_simmer_overnight_analysis", _list_an)
    monkeypatch.setattr(cat.supabase_admin, "list_simmer_overnight", _list_ov)
    monkeypatch.setattr(cat, "get_settings",
                        lambda: types.SimpleNamespace(gemini_api_key="g", openai_api_key="o"))

    class _Boom:
        provider, model = "openai", "gpt-6-luna"
        async def grounded_read(self, ctx):
            calls["n"] += 1
            raise RuntimeError("provider down")
        async def structured_read(self, note, ctx):
            return None
    monkeypatch.setattr(cat, "_make_llm", lambda *a, **k: _Boom())

    res = None
    for _ in range(6):                     # the loop would retry every tick
        res = await cat.run_recall("2026-10-12")
    assert calls["n"] == cat._MAX_RECALL_ATTEMPTS     # tried 3×, then stopped paying
    assert res["recalled"] == 0


async def test_run_catalyst_reports_complete_when_all_resolved(wired, monkeypatch):
    """`complete` drives the loop's catalyst wind-down (no fixed 10:30 cap): False
    while a name was just analyzed, True once every name is resolved (skipped/capped)."""
    _ovn(monkeypatch)
    res = await cat.run_catalyst("2026-10-12")
    assert res["analyzed"] == 1 and res["complete"] is False      # just analyzed → one more pass
    res2 = await cat.run_catalyst("2026-10-12")
    assert res2["skipped"] == 1 and res2["complete"] is True      # now persisted → done for the day


# ── FIX 2: late LLM reads are flagged non-predictive (no bake-off lookahead) ────
def test_read_predictive_cutoff():
    from app.simmer_overnight_catalyst import _read_predictive
    assert _read_predictive("2026-10-12T09:25:00-04:00") is True     # at-open read
    assert _read_predictive("2026-10-12T09:45:00-04:00") is True     # on the cutoff
    assert _read_predictive("2026-10-12T13:00:00-04:00") is False    # late start → non-predictive
    assert _read_predictive(None) is True                            # old rows → assume predictive


async def test_close_excludes_late_llm_from_grading(wired, monkeypatch):
    """A late-start LLM read (llm.at mid-session) must NOT be graded as a pre-open
    call — llm_only/combiner grades become None and late_llm=True (the reactive
    candidate, graded from its own spot, is unaffected)."""
    _ovn(monkeypatch)

    class _Prov:
        async def chain(self, sym, exp):
            return [{"side": "put", "strike": 100.0, "bid": 1.0, "ask": 1.2, "volume": 50}]
        async def quote(self, sym):
            return {"last": 96.0}

    wired["MU"] = {"symbol": "MU", "session_date": "2026-10-12", "gap": -0.047,
                   "premarket": {"spot": 100.0}, "readings": [],
                   "llm": {"call": "sell_bear_call", "at": "2026-10-12T13:00:00-04:00"},  # late read
                   "combiner": {"final": {"call": "sell_bear_call"},
                                "llm_only": {"call": "sell_bear_call"},
                                "combiner_blended": {"call": "sell_bear_call"}}}
    assert await cat.capture_reading(_Prov(), "16:00", "2026-10-12") == 1
    close = wired["MU"]["close"]
    assert close["late_llm"] is True
    assert close["llm_only_right"] is None and close["combiner_blended_right"] is None
    assert close["llm_only_call"] == "sell_bear_call"       # the call is still recorded

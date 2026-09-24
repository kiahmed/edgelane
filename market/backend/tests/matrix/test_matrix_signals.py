"""Matrix transition detection — each of the six states fires once, on a real
change, and never on a repeat poll (docs/matrix_events_update.md §2/§3)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app import matrix_events as me
from app import matrix_signals as ms


# ── fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _clean_state():
    from app.evaluator import state as est
    for d in (est.regime_alert_active_by_symbol, est.consec_wins_by_symbol,
              est.consec_losses_by_symbol):
        d.clear()
    ms.state.reset()
    yield
    ms.state.reset()


@pytest.fixture
def sent(monkeypatch):
    """Capture publishes instead of hitting Pub/Sub."""
    calls: list[dict] = []

    async def _fake(symbol, state, expiry=None, *, day=None, discriminator=None,
                    extra_attributes=None):
        calls.append({"symbol": symbol, "state": state, "expiry": expiry,
                      "disc": discriminator, "attrs": extra_attributes or {},
                      "event_id": me.event_id(symbol, state, None, discriminator)})
        return True

    monkeypatch.setattr(ms.matrix_events, "publish_transition", _fake)
    return calls


def _snap(**over) -> dict:
    base = {
        "symbol": "SPX",
        "expiration": "2026-09-18",
        "spot": 7790.0,
        "bias": {"bias_label": "bearish", "directional_score": -80.0,
                 "confidence": "high", "call_wall_strike": 7800.0,
                 "put_wall_strike": 7700.0},
        "engine_pick": {"strategy": "bear_call", "label": "Aggressive",
                        "legs": [{"strike": 7680.0, "side": "call", "long_short": -1},
                                 {"strike": 7720.0, "side": "call", "long_short": 1}],
                        "strikes": [7680.0, 7720.0], "composite_score": 86.1,
                        "structure_text": "Short 7680C / Long 7720C",
                        "composite_verdict": {"label": "tradeable on limit"}},
        "strategies": {f"s{i}": {"best": {"label": "Balanced", "health": "healthy",
                                          "composite_verdict": {"label": "ok"}}}
                       for i in range(8)},
    }
    base.update(over)
    return base


def _states(calls) -> list[str]:
    return [c["state"] for c in calls]


# ── pick_selected ───────────────────────────────────────────────────────────

async def test_pick_selected_fires_once_then_stays_quiet(sent):
    _good()
    await ms.on_snapshot(_snap(), None)
    await ms.drain()
    assert "pick_selected" in _states(sent)

    sent.clear()
    await ms.on_snapshot(_snap(), None)          # identical pick, next poll
    await ms.drain()
    assert "pick_selected" not in _states(sent)


async def test_a_drifting_score_is_not_a_new_pick(sent):
    """Composite score moves every poll; only the structure defines the call."""
    _good()
    await ms.on_snapshot(_snap(), None)
    await ms.drain()
    sent.clear()
    snap = _snap()
    snap["engine_pick"]["composite_score"] = 71.4      # same legs, new score
    await ms.on_snapshot(snap, None)
    await ms.drain()
    assert "pick_selected" not in _states(sent)


async def test_different_legs_are_a_new_pick(sent):
    _good()
    await ms.on_snapshot(_snap(), None)
    await ms.drain()
    sent.clear()
    snap = _snap()
    snap["engine_pick"]["legs"] = [{"strike": 7690.0, "side": "call", "long_short": -1}]
    await ms.on_snapshot(snap, None)
    await ms.drain()
    assert "pick_selected" in _states(sent)


async def test_pick_attributes_carry_the_copy_fields(sent):
    _good()
    await ms.on_snapshot(_snap(), None)
    await ms.drain()
    attrs = next(c for c in sent if c["state"] == "pick_selected")["attrs"]
    assert attrs["strategy"] == "bear_call"
    assert attrs["verdict"] == "tradeable on limit"
    assert sent[0]["expiry"] == "2026-09-18"


# ── session_open ────────────────────────────────────────────────────────────

async def test_session_open_fires_once_per_day(sent):
    await ms.on_snapshot(_snap(), None)
    await ms.drain()
    assert "session_open" in _states(sent)
    sent.clear()
    await ms.on_snapshot(_snap(), None)
    await ms.drain()
    assert "session_open" not in _states(sent)


async def test_session_open_is_skipped_with_no_walls(sent):
    """§2: skip silently rather than post an empty frame."""
    snap = _snap(bias={"bias_label": "neutral", "directional_score": 0})
    await ms.on_snapshot(snap, None)
    await ms.drain()
    assert "session_open" not in _states(sent)


# ── grid_digest ─────────────────────────────────────────────────────────────

async def test_grid_digest_respects_its_cooldown(sent):
    await ms.on_snapshot(_snap(), None)           # first ever → fires
    await ms.drain()
    assert "grid_digest" in _states(sent)

    sent.clear()
    snap = _snap()                                 # a wholly different grid...
    for i in range(8):
        snap["strategies"][f"s{i}"]["best"]["health"] = "broken"
    await ms.on_snapshot(snap, None)               # ...but minutes later
    await ms.drain()
    assert "grid_digest" not in _states(sent), "cooldown must hold"


async def test_grid_digest_needs_a_real_change_after_the_cooldown(sent):
    await ms.on_snapshot(_snap(), None)
    await ms.drain()
    sent.clear()
    # Pretend the last digest was days ago.
    ms.state.last_digest_at["SPX"] = (
        datetime.now(timezone.utc) - timedelta(hours=ms._DIGEST_MIN_HOURS + 1)).isoformat()

    await ms.on_snapshot(_snap(), None)            # same grid → still quiet
    await ms.drain()
    assert "grid_digest" not in _states(sent)

    snap = _snap()
    for i in range(ms._DIGEST_MIN_CHANGED):
        snap["strategies"][f"s{i}"]["best"]["health"] = "broken"
    await ms.on_snapshot(snap, None)               # enough cards moved → fires
    await ms.drain()
    assert "grid_digest" in _states(sent)


# ── never raises ────────────────────────────────────────────────────────────

async def test_a_publisher_failure_never_reaches_the_poll(monkeypatch, caplog):
    """Publishes are queued, so the poll path returns BEFORE the topic answers.
    A failing publish must therefore surface as a logged background error and
    nothing else — never an exception on the caller, never a stalled poll."""
    async def _boom(*a, **k):
        raise RuntimeError("pubsub down")

    monkeypatch.setattr(ms.matrix_events, "publish_transition", _boom)
    _good()

    # The return value reports what was HANDED OFF, not what was delivered —
    # that is the point of not waiting for the ack.
    handed_off = await ms.on_snapshot(_snap(), None)
    assert "pick_selected" in handed_off

    await ms.drain()          # let the doomed tasks finish; must not raise
    assert "publish task failed" in caplog.text


async def test_the_poll_does_not_wait_for_the_topic(monkeypatch):
    """The whole reason publishing is backgrounded: a slow topic must not add
    its latency to the poll cycle."""
    import asyncio, time

    async def _slow(*a, **k):
        await asyncio.sleep(0.5)
        return True

    monkeypatch.setattr(ms.matrix_events, "publish_transition", _slow)
    _good()
    t0 = time.monotonic()
    await ms.on_snapshot(_snap(), None)
    elapsed = time.monotonic() - t0
    assert elapsed < 0.1, f"on_snapshot waited {elapsed:.2f}s on the publish"
    await ms.drain()


async def test_a_snapshot_without_a_symbol_is_ignored(sent):
    assert await ms.on_snapshot({}, None) == []
    await ms.drain()


# ── evaluator-side: bias + win_rate + recap ────────────────────────────────

class _FakeDB:
    def __init__(self, n=20, wins=14, losses=0, neutrals=6, pct=70.0):
        self._s = {"n": n, "wins": wins, "losses": losses,
                   "neutrals": neutrals, "accuracy_pct": pct}

    def fetch_accuracy(self, sym, window, min_dwell=1):
        return dict(self._s)


class _FakePoller:
    def __init__(self, snap):
        self.latest_by_symbol = {"SPX": snap}


class _Settings:
    eval_min_graded = 10
    pill_green_pct = 60.0
    pill_red_pct = 40.0
    eval_rolling_window = 20
    regime_clear_consec_wins = 2
    regime_alert_consec_losses = 3
    auth_enabled = False


@pytest.fixture
def evaluator_state(monkeypatch):
    from app.evaluator import state as est
    est.regime_alert_active_by_symbol.clear()
    est.consec_wins_by_symbol.clear()
    est.consec_losses_by_symbol.clear()
    yield est
    est.regime_alert_active_by_symbol.clear()


async def test_bias_transition_fires_only_on_a_flip(sent, evaluator_state):
    db, poller, cfg = _FakeDB(), _FakePoller(_snap()), _Settings()

    await ms.on_evaluation(db, poller, cfg)        # first pass seeds the baseline
    await ms.drain()
    sent.clear()
    await ms.on_evaluation(db, poller, cfg)        # unchanged → silent
    await ms.drain()
    assert not [s for s in _states(sent) if s.startswith("bias_")]


async def test_win_rate_notable_on_recovery(sent, evaluator_state):
    """Rule 1 (§3): the regime alert clearing IS 'recovered from losses'."""
    db, poller, cfg = _FakeDB(), _FakePoller(_snap()), _Settings()
    evaluator_state.regime_alert_active_by_symbol["SPX"] = True
    await ms.on_evaluation(db, poller, cfg)        # seed: alert active
    await ms.drain()
    sent.clear()

    evaluator_state.regime_alert_active_by_symbol["SPX"] = False   # recovered
    await ms.on_evaluation(db, poller, cfg)
    await ms.drain()
    hit = [c for c in sent if c["state"] == "win_rate_notable"]
    assert hit and hit[0]["attrs"]["reason"] == "recovery"


async def test_win_rate_notable_on_crossing_into_green(sent, evaluator_state):
    """Rule 2 (§3): crossing pill_green_pct from below, with enough graded."""
    poller, cfg = _FakePoller(_snap()), _Settings()
    await ms.on_evaluation(_FakeDB(pct=45.0), poller, cfg)   # seed below green
    await ms.drain()
    sent.clear()

    await ms.on_evaluation(_FakeDB(pct=72.0), poller, cfg)   # crosses up
    await ms.drain()
    hit = [c for c in sent if c["state"] == "win_rate_notable"]
    assert hit and hit[0]["attrs"]["reason"] == "win_streak"
    assert hit[0]["attrs"]["win_rate"] == "72"


async def test_a_small_sample_cannot_trigger_a_win_streak(sent, evaluator_state):
    """Below eval_min_graded it is noise, not an achievement."""
    poller, cfg = _FakePoller(_snap()), _Settings()
    await ms.on_evaluation(_FakeDB(n=4, pct=25.0), poller, cfg)
    await ms.drain()
    sent.clear()
    await ms.on_evaluation(_FakeDB(n=4, pct=100.0), poller, cfg)
    await ms.drain()
    assert "win_rate_notable" not in _states(sent)


async def test_daily_recap_fires_once_for_the_finished_day(sent, evaluator_state):
    db, poller, cfg = _FakeDB(), _FakePoller(_snap()), _Settings()
    # A day's worth of picks, recorded by the poll side.
    await ms.on_snapshot(_snap(), None)
    await ms.drain()
    low = _snap()
    low["engine_pick"] = dict(low["engine_pick"], strikes=[1.0, 2.0], composite_score=12.0)
    await ms.on_snapshot(low, None)
    await ms.drain()
    ms.state.day_date["SPX"] = "2026-09-12"        # that day is now over
    sent.clear()

    await ms.on_evaluation(db, poller, cfg)
    await ms.drain()
    hit = [c for c in sent if c["state"] == "daily_recap"]
    assert hit, "a finished day must recap"
    attrs = hit[0]["attrs"]
    assert attrs["session_date"] == "2026-09-12"
    assert attrs["best_composite_score"] == "86.1"
    assert attrs["worst_composite_score"] == "12.0"

    sent.clear()
    await ms.on_evaluation(db, poller, cfg)        # same day must not recap twice
    await ms.drain()
    assert "daily_recap" not in _states(sent)


async def test_on_evaluation_swallows_a_broken_db(sent, evaluator_state):
    class _Boom:
        def fetch_accuracy(self, *a):
            raise RuntimeError("db gone")

    assert await ms.on_evaluation(_Boom(), _FakePoller(_snap()), _Settings()) == []
    await ms.drain()


# ── dwell: the chip and the win rate count the same picks ──────────────────

class _Dwell3:
    pick_min_dwell_polls = 3


async def test_a_flicker_is_never_announced(sent):
    """The engine's top pick changes and changes back inside a minute. A run
    that short is not graded, so it must not be posted either."""
    _good()
    a, b = _snap(), _snap()
    b["engine_pick"] = dict(b["engine_pick"], legs=[{"strike": 1}])

    await ms.on_snapshot(a, _Dwell3()); await ms.drain()      # poll 1 of A
    await ms.on_snapshot(b, _Dwell3()); await ms.drain()      # A flickered away
    await ms.on_snapshot(a, _Dwell3()); await ms.drain()      # and back
    assert "pick_selected" not in _states(sent), "no run reached the dwell"


async def test_a_pick_that_holds_is_announced_once(sent):
    _good()
    snap = _snap()
    for _ in range(6):                       # six consecutive polls, same pick
        await ms.on_snapshot(snap, _Dwell3())
        await ms.drain()
    assert _states(sent).count("pick_selected") == 1, "announce once, on earning it"


async def test_the_announcement_waits_for_the_dwell(sent):
    _good()
    snap = _snap()
    await ms.on_snapshot(snap, _Dwell3()); await ms.drain()
    assert "pick_selected" not in _states(sent)      # poll 1
    await ms.on_snapshot(snap, _Dwell3()); await ms.drain()
    assert "pick_selected" not in _states(sent)      # poll 2
    await ms.on_snapshot(snap, _Dwell3()); await ms.drain()
    assert "pick_selected" in _states(sent)          # poll 3 — earned


async def test_pick_identity_is_the_legs_the_grader_uses(sent):
    """Keyed on legs, matching db._EPISODE_CTE — so a chip and a graded episode
    describe the same pick. Strategy/label churn alone is not a new pick."""
    _good()
    a = _snap()
    b = _snap()
    b["engine_pick"] = dict(b["engine_pick"], label="Balanced")   # same legs

    for _ in range(3):
        await ms.on_snapshot(a, _Dwell3()); await ms.drain()
    sent.clear()
    for _ in range(3):
        await ms.on_snapshot(b, _Dwell3()); await ms.drain()
    assert "pick_selected" not in _states(sent), "same legs = same pick"


# ── event_id granularity ───────────────────────────────────────────────────

async def test_each_distinct_pick_gets_its_own_event_id(sent):
    """Without this, every pick after the day's first deduped away downstream
    and was never posted."""
    _good()
    a, b = _snap(), _snap()
    b["engine_pick"] = dict(b["engine_pick"],
                            legs=[{"strike": 7690.0, "side": "call", "long_short": -1}])
    await ms.on_snapshot(a, None); await ms.drain()
    await ms.on_snapshot(b, None); await ms.drain()

    ids = [c["event_id"] for c in sent if c["state"] == "pick_selected"]
    assert len(ids) == 2 and ids[0] != ids[1], ids


async def test_the_same_pick_keeps_one_event_id(sent):
    """A retry of the SAME pick must still dedupe — that is why the
    discriminator is a hash of the pick, not a counter."""
    key = ms._pick_key(_snap()["engine_pick"])
    first = me.event_id("SPX", "pick_selected", None, me.discriminator(key))
    again = me.event_id("SPX", "pick_selected", None, me.discriminator(key))
    assert first == again


async def test_once_a_day_states_keep_a_bare_id(sent):
    """session_open really does happen once a day — it must NOT gain a
    discriminator, or a restart would re-announce the open."""
    _good()
    await ms.on_snapshot(_snap(), None); await ms.drain()
    ev = next(c for c in sent if c["state"] == "session_open")
    assert ev["disc"] is None
    assert ev["event_id"].endswith("-session_open")


# ── only a pick worth showing gets posted ──────────────────────────────────
#
# The 2026-09-17 incident: SPX sat in a losing Bear Put with bias diverged, the
# engine re-struck new legs every poll, and each re-strike posted as a "new
# pick" carrying health=BROKEN. pick_selected is a claim the tool found
# something sharp — these tests pin that it only makes that claim when true.

def _good(sym="SPX"):
    """Baseline: bias in sync, and a confirming win behind it."""
    from app.evaluator import state as est
    ms.state.last_trust_state[sym] = "in_sync"
    ms.state.last_win_rate[sym] = 64.0        # a record worth showing (> 55%)
    ms.state.last_graded[sym] = 20
    est.regime_alert_active_by_symbol[sym] = False
    est.consec_wins_by_symbol[sym] = 1


@pytest.mark.parametrize("health", ["broken", "BROKEN", "capital_trap"])
async def test_a_broken_pick_is_never_announced(sent, evaluator_state, health):
    _good()
    snap = _snap()
    snap["engine_pick"] = dict(snap["engine_pick"], health=health)
    await ms.on_snapshot(snap, None); await ms.drain()
    assert "pick_selected" not in _states(sent)


async def test_a_do_not_trade_verdict_is_never_announced(sent, evaluator_state):
    _good()
    snap = _snap()
    snap["engine_pick"] = dict(snap["engine_pick"],
                               composite_verdict={"label": "do not trade"})
    await ms.on_snapshot(snap, None); await ms.drain()
    assert "pick_selected" not in _states(sent)


async def test_a_diverged_bias_suppresses_the_pick(sent, evaluator_state):
    """The incident's other half: don't post a call while the bias that
    produced it is out of sync."""
    _good()
    ms.state.last_trust_state["SPX"] = "paused"
    await ms.on_snapshot(_snap(), None); await ms.drain()
    assert "pick_selected" not in _states(sent)


async def test_an_unknown_bias_suppresses_the_pick(sent, evaluator_state):
    """Before the grader has an opinion there is no performance to show."""
    ms.state.last_trust_state.pop("SPX", None)
    await ms.on_snapshot(_snap(), None); await ms.drain()
    assert "pick_selected" not in _states(sent)


async def test_a_healthy_in_sync_pick_is_announced(sent, evaluator_state):
    _good()
    await ms.on_snapshot(_snap(), None); await ms.drain()
    assert "pick_selected" in _states(sent)


async def test_recovery_needs_a_confirming_win_not_just_a_flag_flip(
        sent, evaluator_state):
    """A bias that re-syncs on one poll can un-sync on the next. Announcing on
    that edge is how the same broken idea gets posted over and over."""
    # Suppressed while broken.
    _good()
    broken = _snap()
    broken["engine_pick"] = dict(broken["engine_pick"], health="broken")
    await ms.on_snapshot(broken, None); await ms.drain()
    assert ms.state.pick_blocked["SPX"] is True

    # Structure recovers, but nothing has been graded a win since.
    evaluator_state.consec_wins_by_symbol["SPX"] = 0
    fresh = _snap()
    fresh["engine_pick"] = dict(fresh["engine_pick"],
                                legs=[{"strike": 7700.0, "side": "call"}])
    await ms.on_snapshot(fresh, None); await ms.drain()
    assert "pick_selected" not in _states(sent), "flag flip alone is not recovery"

    # A real graded win lands → the next pick may be announced.
    evaluator_state.consec_wins_by_symbol["SPX"] = 1
    fresh2 = _snap()
    fresh2["engine_pick"] = dict(fresh2["engine_pick"],
                                 legs=[{"strike": 7710.0, "side": "call"}])
    await ms.on_snapshot(fresh2, None); await ms.drain()
    assert "pick_selected" in _states(sent)


async def test_a_regime_pause_blocks_recovery(sent, evaluator_state):
    _good()
    broken = _snap()
    broken["engine_pick"] = dict(broken["engine_pick"], health="broken")
    await ms.on_snapshot(broken, None); await ms.drain()

    evaluator_state.regime_alert_active_by_symbol["SPX"] = True
    evaluator_state.consec_wins_by_symbol["SPX"] = 5      # wins, but still paused
    nxt = _snap()
    nxt["engine_pick"] = dict(nxt["engine_pick"], legs=[{"strike": 7730.0}])
    await ms.on_snapshot(nxt, None); await ms.drain()
    assert "pick_selected" not in _states(sent)


async def test_a_suppressed_pick_can_still_be_announced_if_it_recovers(
        sent, evaluator_state):
    """`last_pick_key` means 'last ANNOUNCED'. A pick held back while broken
    must not be swallowed later as already-said."""
    _good()
    snap = _snap()
    snap["engine_pick"] = dict(snap["engine_pick"], health="broken")
    await ms.on_snapshot(snap, None); await ms.drain()
    assert "pick_selected" not in _states(sent)
    assert ms.state.last_pick_key.get("SPX") is None

    await ms.on_snapshot(_snap(), None); await ms.drain()   # same legs, healthy
    assert "pick_selected" in _states(sent)


async def test_the_chip_carries_the_win_rate_as_its_takeaway(sent, evaluator_state):
    """A pick with no track record beside it is a signal, not a takeaway."""
    _good()
    ms.state.last_win_rate["SPX"] = 64.0
    ms.state.last_graded["SPX"] = 22
    await ms.on_snapshot(_snap(), None); await ms.drain()
    attrs = next(c for c in sent if c["state"] == "pick_selected")["attrs"]
    assert attrs["win_rate"] == "64" and attrs["graded"] == "22"
    assert attrs["engine_state"] == "active"
    assert "trust_state" not in attrs


async def test_the_recap_leads_with_the_best_and_keeps_the_worst_as_context(
        sent, evaluator_state):
    """Composite SCORES, so 'worst' is the weakest idea surfaced, not the
    biggest loss — it stays available without becoming the story."""
    _good()
    await ms.on_snapshot(_snap(), None)
    low = _snap()
    low["engine_pick"] = dict(low["engine_pick"], legs=[{"strike": 1.0}],
                              composite_score=12.0)
    await ms.on_snapshot(low, None)
    await ms.drain()
    ms.state.day_date["SPX"] = "2026-09-17"
    sent.clear()

    await ms.on_evaluation(_FakeDB(), _FakePoller(_snap()), _Settings())
    await ms.drain()
    attrs = next(c for c in sent if c["state"] == "daily_recap")["attrs"]
    assert attrs["headline"] == "best"
    assert attrs["best_composite_score"] == "86.1"
    assert attrs["worst_composite_score"] == "12.0"


async def test_a_day_with_no_best_does_not_recap(sent, evaluator_state):
    """No headline, no post — the same silence session_open keeps when there
    are no walls worth showing."""
    ms.state.day_date["SPX"] = "2026-09-17"
    ms.state.day_best.pop("SPX", None)
    ms.state.day_worst["SPX"] = {"composite_score": "12.0"}

    await ms.on_evaluation(_FakeDB(), _FakePoller(_snap()), _Settings())
    await ms.drain()
    assert "daily_recap" not in _states(sent)


# ── win_rate_notable: the recovery must show a number worth showing ─────────
#
# 2026-09-22: three posts went out headlined 45% / 15% / 10%, all via the
# recovery branch — two wins cleared the pause while the 20-pick window stayed
# terrible. The post displays the rolling rate, so that rate is the gate.

async def _recover_at(evaluator_state, pct, n=20):
    poller, cfg = _FakePoller(_snap()), _Settings()
    evaluator_state.regime_alert_active_by_symbol["SPX"] = True
    await ms.on_evaluation(_FakeDB(n=n, pct=pct), poller, cfg)      # seed: paused
    evaluator_state.regime_alert_active_by_symbol["SPX"] = False    # pause lifts


@pytest.mark.parametrize("pct", [10.0, 15.0, 45.0, 55.0])
async def test_a_recovery_with_a_bad_record_is_not_notable(sent, evaluator_state, pct):
    await _recover_at(evaluator_state, pct)
    sent.clear()
    await ms.on_evaluation(_FakeDB(pct=pct), _FakePoller(_snap()), _Settings())
    await ms.drain()
    assert "win_rate_notable" not in _states(sent), f"{pct}% must not be 'notable'"


async def test_a_recovery_with_a_good_record_is_notable(sent, evaluator_state):
    await _recover_at(evaluator_state, 60.0)
    sent.clear()
    await ms.on_evaluation(_FakeDB(pct=60.0), _FakePoller(_snap()), _Settings())
    await ms.drain()
    hit = [c for c in sent if c["state"] == "win_rate_notable"]
    assert hit and hit[0]["attrs"]["reason"] == "recovery"


async def test_a_recovery_on_a_tiny_sample_is_not_notable(sent, evaluator_state):
    await _recover_at(evaluator_state, 80.0, n=4)
    sent.clear()
    await ms.on_evaluation(_FakeDB(n=4, pct=80.0), _FakePoller(_snap()), _Settings())
    await ms.drain()
    assert "win_rate_notable" not in _states(sent)



# ── one quality bar: > 55% on a real sample, for anything showing a record ──

async def test_a_pick_beside_a_losing_record_is_not_announced(sent, evaluator_state):
    _good()
    ms.state.last_win_rate["SPX"] = 45.0
    await ms.on_snapshot(_snap(), None); await ms.drain()
    assert "pick_selected" not in _states(sent)


async def test_exactly_55_is_not_over_the_bar(sent, evaluator_state):
    _good()
    ms.state.last_win_rate["SPX"] = 55.0
    await ms.on_snapshot(_snap(), None); await ms.drain()
    assert "pick_selected" not in _states(sent)


async def _flip(evaluator_state, prev, pct, cur_alert=False, prev_wr=None, n=20):
    """Seed a previous trust state, then run one sweep that lands on the new one."""
    ms.state.last_trust_state["SPX"] = prev
    if prev_wr is not None:
        ms.state.last_win_rate["SPX"] = prev_wr
        ms.state.last_graded["SPX"] = n
    evaluator_state.regime_alert_active_by_symbol["SPX"] = cur_alert
    await ms.on_evaluation(_FakeDB(n=n, pct=pct), _FakePoller(_snap()), _Settings())
    await ms.drain()


async def test_bias_aligned_at_45_percent_is_not_posted(sent, evaluator_state):
    """The 2026-09-23 card: 'the bias now agrees' beside a 45% record."""
    await _flip(evaluator_state, "calibrating", 45.0)
    assert "bias_aligned" not in _states(sent)


async def test_bias_aligned_with_a_strong_record_is_posted(sent, evaluator_state):
    await _flip(evaluator_state, "calibrating", 64.0)
    assert "bias_aligned" in _states(sent)


async def test_a_low_confidence_wobble_is_never_posted(sent, evaluator_state):
    """Not every divergence — a confidence dip is noise, not news."""
    from app.poller import state as ps
    await _flip(evaluator_state, "in_sync", 64.0, prev_wr=64.0)   # baseline in sync
    sent.clear()
    snap = _snap()
    snap["bias"] = dict(snap["bias"], confidence="low")
    ps.latest_by_symbol["SPX"] = snap
    try:
        await ms.on_evaluation(_FakeDB(pct=64.0), _FakePoller(snap), _Settings())
        await ms.drain()
    finally:
        ps.latest_by_symbol.pop("SPX", None)
    assert "bias_diverged" not in _states(sent)


async def test_a_real_pause_off_a_shown_record_is_posted(sent, evaluator_state):
    """The one divergence worth saying out loud: we were showing a good record
    and the engine has now paused itself on a genuine loss streak."""
    await _flip(evaluator_state, "in_sync", 30.0, cur_alert=True, prev_wr=64.0)
    assert "bias_diverged" in _states(sent)


async def test_a_pause_with_no_shown_record_is_not_posted(sent, evaluator_state):
    await _flip(evaluator_state, "in_sync", 30.0, cur_alert=True, prev_wr=40.0)
    assert "bias_diverged" not in _states(sent)



# ── engine vs market: never mixed ──────────────────────────────────────────

async def test_a_low_bias_confidence_never_reaches_a_pick_event(sent, evaluator_state):
    """low_conf is the BIAS engine's confidence — market data. On a pick event
    the engine is simply active."""
    _good()
    ms.state.last_trust_state["SPX"] = "low_conf"
    ms.state.pick_blocked["SPX"] = False
    attrs = ms._pick_summary(_snap()["engine_pick"], "SPX")
    assert attrs["engine_state"] == "active"
    assert "low" not in " ".join(attrs.values()).lower()


async def test_session_open_carries_the_market_read(sent):
    await ms.on_snapshot(_snap(), None); await ms.drain()
    attrs = next(c for c in sent if c["state"] == "session_open")["attrs"]
    assert attrs["bias_direction"] == "bearish" and attrs["bias_strength"] == "80"
    assert attrs["confidence"] == "high"
    assert "composite_score" not in attrs            # no engine data on the market read

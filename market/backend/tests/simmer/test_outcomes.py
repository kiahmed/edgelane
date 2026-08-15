"""Paper outcomes: held/touched determination, the pending-outcome query, and
engine-verdict independence from user settings."""
from __future__ import annotations

import json
from datetime import date, datetime, timezone

import pytest

from app import simmer_watcher as sw

from .conftest import EXP, FakeSimmerTradier

PAST_EXP = "2026-08-07"      # already expired relative to today
FUTURE_EXP = "2026-12-18"


def _naive_utc() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _readiness_row(symbol="NVDA", expiration=PAST_EXP, structure="bull_put",
                   short=96.0, long=92.0, score=74.5, strikes=None) -> dict:
    return {
        "ts": _naive_utc(),
        "symbol": symbol,
        "expiration": expiration,
        "spot": 100.0,
        "score": score,
        "vetoed": False,
        "veto_reasons": "[]",
        "components": json.dumps({"components": {"structural_safety": {"score": 0.8}},
                                  "strikes": strikes or {"short": short, "long": long,
                                                         "width": abs(short - long)}}),
        "regime": "contango",
        "structure": structure,
        "short_strike": short,
        "long_strike": long,
        "width": abs(short - long),
        "credit_mid": 0.8,
        "credit_fill": 0.75,
        "max_loss": 3.2,
        "pop_breakeven": 0.78,
        "expected_value": 0.05,
        "alpha": 0.016,
        "engine_version": "simmer-engine-test",
    }


# ── Pending-outcome query ───────────────────────────────────────────────────
def test_pending_returns_only_expired_rows_without_outcomes(fresh_db):
    expired_id = fresh_db.insert_simmer_readiness(_readiness_row(expiration=PAST_EXP))
    fresh_db.insert_simmer_readiness(_readiness_row(symbol="AMD", expiration=FUTURE_EXP))
    pending = fresh_db.fetch_pending_simmer_outcomes(date.today())
    assert [r["id"] for r in pending] == [expired_id]

    fresh_db.insert_simmer_outcome({
        "readiness_id": expired_id, "symbol": "NVDA", "expiration": PAST_EXP,
        "evaluated_at": _naive_utc(), "spot_at_expiry": 100.0,
        "short_strike": 96.0, "held": True, "touched": None,
        "score_at_entry": 74.5, "components": "{}", "engine_version": "x",
    })
    assert fresh_db.fetch_pending_simmer_outcomes(date.today()) == []


def test_pending_picks_latest_recommendation_per_key(fresh_db):
    fresh_db.insert_simmer_readiness(_readiness_row(short=95.0))
    latest_id = fresh_db.insert_simmer_readiness(_readiness_row(short=97.0))
    pending = fresh_db.fetch_pending_simmer_outcomes(date.today())
    assert len(pending) == 1
    assert pending[0]["id"] == latest_id
    assert pending[0]["short_strike"] == 97.0


def test_vetoed_rows_without_strikes_are_never_pending(fresh_db):
    row = _readiness_row()
    row.update(score=None, vetoed=True, structure=None,
               short_strike=None, long_strike=None)
    fresh_db.insert_simmer_readiness(row)
    assert fresh_db.fetch_pending_simmer_outcomes(date.today()) == []


# ── held / touched determination ────────────────────────────────────────────
async def test_bull_put_held_when_spot_above_short(fresh_db):
    rid = fresh_db.insert_simmer_readiness(_readiness_row(short=96.0))
    tradier = FakeSimmerTradier(quotes={"NVDA": 100.0})
    n = await sw.evaluate_paper_outcomes(tradier, fresh_db)
    assert n == 1
    summary = fresh_db.fetch_simmer_outcome_summary()
    assert summary["n"] == 1 and summary["held"] == 1
    assert summary["by_structure"]["bull_put"]["held_pct"] == 100.0
    assert fresh_db.fetch_pending_simmer_outcomes(date.today()) == []


async def test_bull_put_breached_when_spot_below_short(fresh_db):
    fresh_db.insert_simmer_readiness(_readiness_row(short=96.0))
    tradier = FakeSimmerTradier(quotes={"NVDA": 90.0})
    await sw.evaluate_paper_outcomes(tradier, fresh_db)
    summary = fresh_db.fetch_simmer_outcome_summary()
    assert summary["held"] == 0


async def test_bear_call_held_direction_is_inverted(fresh_db):
    fresh_db.insert_simmer_readiness(_readiness_row(
        structure="bear_call", short=106.0, long=110.0))
    tradier = FakeSimmerTradier(quotes={"NVDA": 100.0})
    await sw.evaluate_paper_outcomes(tradier, fresh_db)
    assert fresh_db.fetch_simmer_outcome_summary()["held"] == 1


async def test_iron_condor_needs_both_shorts_to_hold(fresh_db):
    strikes = {"put": {"short": 96.0, "long": 92.0},
               "call": {"short": 106.0, "long": 110.0}}
    fresh_db.insert_simmer_readiness(_readiness_row(
        structure="iron_condor", short=96.0, long=92.0, strikes=strikes))
    tradier = FakeSimmerTradier(quotes={"NVDA": 108.0})    # breaches the CALL side
    await sw.evaluate_paper_outcomes(tradier, fresh_db)
    assert fresh_db.fetch_simmer_outcome_summary()["held"] == 0


async def test_touched_is_null_without_intraday_history(fresh_db):
    """TradierClient exposes no daily-history endpoint, so intraday touch is
    not determinable — the gap is recorded as NULL, never guessed."""
    rid = fresh_db.insert_simmer_readiness(_readiness_row())
    await sw.evaluate_paper_outcomes(FakeSimmerTradier(quotes={"NVDA": 100.0}),
                                     fresh_db)
    conn = fresh_db.connect()
    with fresh_db._lock:
        touched, held = conn.execute(
            "SELECT touched, held FROM simmer_outcomes WHERE readiness_id = ?",
            [rid]).fetchone()
    assert touched is None
    assert held is True


async def test_outcome_snapshots_engine_verdict(fresh_db):
    rid = fresh_db.insert_simmer_readiness(_readiness_row(score=74.5))
    await sw.evaluate_paper_outcomes(FakeSimmerTradier(quotes={"NVDA": 100.0}),
                                     fresh_db)
    conn = fresh_db.connect()
    with fresh_db._lock:
        score, comps, ver = conn.execute(
            "SELECT score_at_entry, components, engine_version FROM simmer_outcomes "
            "WHERE readiness_id = ?", [rid]).fetchone()
    assert score == 74.5
    assert "structural_safety" in comps
    assert ver == "simmer-engine-test"


async def test_settlement_read_failure_leaves_row_pending(fresh_db):
    fresh_db.insert_simmer_readiness(_readiness_row())
    tradier = FakeSimmerTradier(fail_symbols={"NVDA"})
    n = await sw.evaluate_paper_outcomes(tradier, fresh_db)
    assert n == 0
    assert len(fresh_db.fetch_pending_simmer_outcomes(date.today())) == 1


# ── Engine-verdict independence from user settings ──────────────────────────
async def test_user_settings_never_reach_the_persisted_verdict(fresh_db):
    """The watcher's persisted rows (and therefore every outcome) are the
    ENGINE's verdict. A personalized on-demand analyze (user_settings set) must
    never write a readiness row, even if asked to persist."""
    tradier = FakeSimmerTradier()
    env = await sw.analyze_symbol(
        tradier, fresh_db, "NVDA", EXP,
        user_settings={"structures_enabled": ["bear_call"]},
        regime=None, persist=True)
    assert env["symbol"] == "NVDA"
    assert "readiness_id" not in env
    assert fresh_db.latest_simmer_readiness("NVDA", EXP) is None


async def test_outcomes_summary_counts_engine_rows_only(fresh_db):
    """Two structures, mixed results — the summary aggregates the persisted
    engine rows and nothing else."""
    fresh_db.insert_simmer_readiness(_readiness_row(short=96.0))                 # holds
    fresh_db.insert_simmer_readiness(_readiness_row(
        symbol="AMD", structure="bear_call", short=95.0, long=99.0))            # breached
    tradier = FakeSimmerTradier(quotes={"NVDA": 100.0, "AMD": 100.0})
    n = await sw.evaluate_paper_outcomes(tradier, fresh_db)
    assert n == 2
    summary = fresh_db.fetch_simmer_outcome_summary()
    assert summary["n"] == 2
    assert summary["held"] == 1
    assert summary["by_structure"]["bull_put"]["held"] == 1
    assert summary["by_structure"]["bear_call"]["held"] == 0

"""Re-price the ORIGINAL saved pick legs even after the bias rotated.

The evaluator must re-mark the legs saved AT DECISION (from bias_decisions),
not whatever pick is on top of the current snapshot. We build a snapshot whose
current engine_pick is a DIFFERENT strategy (with legs that, if mistakenly used,
would grade a LOSS), and assert the outcome reflects the original legs (a WIN).
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app import evaluator
from app.evaluator import evaluate_pending, state as evaluator_state

SYM = "SPX"

# Original pick saved at decision: a debit vertical on strikes X/Y, entry 2.0.
ORIG_LEGS = [
    {"symbol": "X_LONG", "long_short": 1, "side": "call", "strike": 5000.0},
    {"symbol": "Y_SHORT", "long_short": -1, "side": "call", "strike": 5010.0},
]
ORIG_ENTRY = 2.0


class FakeDB:
    def __init__(self, pending):
        self._pending = pending
        self.inserted: list[dict] = []

    def fetch_pending_evaluations(self, eval_window_min):
        return self._pending

    def insert_outcome(self, row):
        self.inserted.append(row)


@pytest.fixture(autouse=True)
def _reset():
    for d in (evaluator_state.consec_losses_by_symbol,
              evaluator_state.consec_wins_by_symbol,
              evaluator_state.regime_alert_active_by_symbol,
              evaluator_state.regime_alert_triggered_at):
        d.pop(SYM, None)
    yield


async def test_reprice_uses_original_legs_not_rotated_pick():
    from datetime import datetime, timezone

    # Pending row tuple shape (matches db.fetch_pending_evaluations):
    # (id, ts, symbol, spot_at_decision, label, score,
    #  pick_legs, pick_entry_mid, pick_spread_type, pick_strategy)
    pending = [(
        101, datetime.now(timezone.utc), SYM, 5000.0, "bullish", 12.0,
        json.dumps(ORIG_LEGS), ORIG_ENTRY, "debit", "bull_call",
    )]
    db = FakeDB(pending)

    # Current snapshot: bias has ROTATED — top pick is now a different strategy
    # (bear_put) on different strikes A/B. Quote map carries BOTH the rotated
    # pick's legs AND the original X/Y legs.
    quote_map = {
        # Original legs now worth net 2.6 → fav +0.6 → WIN.
        "X_LONG": {"bid": 3.0, "ask": 3.2, "mid": 3.1},
        "Y_SHORT": {"bid": 0.4, "ask": 0.6, "mid": 0.5},
        # Rotated pick's legs — if (wrongly) used, net 1.0 vs 2.0 → would be LOSS.
        "A_LONG": {"bid": 1.4, "ask": 1.6, "mid": 1.5},
        "B_SHORT": {"bid": 0.4, "ask": 0.6, "mid": 0.5},
    }
    snapshot = {
        "symbol": SYM,
        "spot": 5004.0,
        "_quote_by_symbol": quote_map,
        "engine_pick": {  # DIFFERENT strategy on top now
            "strategy": "bear_put", "spread_type": "debit", "net_premium": 2.0,
            "legs": [
                {"symbol": "A_LONG", "long_short": 1, "side": "put"},
                {"symbol": "B_SHORT", "long_short": -1, "side": "put"},
            ],
        },
    }
    poller_state = SimpleNamespace(latest_by_symbol={SYM: snapshot})
    settings = SimpleNamespace(eval_window_min=3, regime_alert_consec_losses=3,
                               regime_clear_consec_wins=2)

    n = await evaluate_pending(db, poller_state, settings)
    assert n == 1
    assert len(db.inserted) == 1
    out = db.inserted[0]
    # Graded off the ORIGINAL X/Y legs → net 2.6, WIN (not the rotated pick's loss).
    assert out["spread_type"] == "debit"
    assert round(out["eval_net_premium"], 3) == 2.6
    assert round(out["favorable_delta"], 3) == 0.6
    assert out["result"] == "win"
    assert out["decision_id"] == 101


async def test_unrepriceable_legs_skip_not_graded():
    from datetime import datetime, timezone

    pending = [(
        102, datetime.now(timezone.utc), SYM, 5000.0, "bullish", 12.0,
        json.dumps(ORIG_LEGS), ORIG_ENTRY, "debit", "bull_call",
    )]
    db = FakeDB(pending)
    # Snapshot's quote map lacks the original legs (expiration rotated) → skip.
    snapshot = {"symbol": SYM, "spot": 5004.0, "_quote_by_symbol": {"OTHER": {"bid": 1, "ask": 2, "mid": 1.5}}}
    poller_state = SimpleNamespace(latest_by_symbol={SYM: snapshot})
    settings = SimpleNamespace(eval_window_min=3, regime_alert_consec_losses=3,
                               regime_clear_consec_wins=2)

    n = await evaluate_pending(db, poller_state, settings)
    assert n == 0
    assert db.inserted == []

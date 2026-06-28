"""Spread-outcome grading: favorable-delta + friction-band, credit vs debit.

Covers win / loss / flat across a real mid move AND a pure bid/ask bounce that
must score NEUTRAL (the move sits inside the half-spread noise band).
"""
from __future__ import annotations

from app.evaluator import _grade_spread, _reprice_spread, _FRICTION_EPSILON


def _q(bid: float, ask: float, mid: float | None = None) -> dict:
    return {"bid": bid, "ask": ask, "mid": mid if mid is not None else (bid + ask) / 2}


# A debit vertical: long leg (+1) + short leg (-1). net_premium = long_mid - short_mid.
DEBIT_LEGS = [
    {"symbol": "LONG", "long_short": 1, "side": "call"},
    {"symbol": "SHORT", "long_short": -1, "side": "call"},
]
# A credit vertical: long protective leg (+1) + short leg (-1).
# stored net_premium (credit collected) = short_mid - long_mid = -(signed_open).
CREDIT_LEGS = [
    {"symbol": "LONG", "long_short": 1, "side": "put"},
    {"symbol": "SHORT", "long_short": -1, "side": "put"},
]


# ── DEBIT ────────────────────────────────────────────────────────────────────
def test_debit_win():
    # entry debit 2.0; now long 3.0 / short 0.5 → net 2.5, fav +0.5, tight friction.
    qm = {"LONG": _q(2.9, 3.1), "SHORT": _q(0.4, 0.6)}  # friction = .5*(.2+.2)=.2
    net_now, fav, friction, result = _grade_spread("debit", 2.0, DEBIT_LEGS, qm)
    assert round(net_now, 3) == 2.5
    assert round(fav, 3) == 0.5
    assert round(friction, 3) == 0.2
    assert result == "win"


def test_debit_loss():
    # net drops to 1.6 → fav -0.4 < -friction(.2) → loss.
    qm = {"LONG": _q(2.2, 2.4), "SHORT": _q(0.6, 0.8)}
    net_now, fav, friction, result = _grade_spread("debit", 2.0, DEBIT_LEGS, qm)
    assert round(net_now, 3) == 1.6
    assert result == "loss"


def test_debit_bidask_bounce_is_neutral():
    # Wide markets: fav +0.5 but friction band 1.5 → inside noise → NEUTRAL.
    qm = {"LONG": _q(2.0, 4.0), "SHORT": _q(0.0, 1.0)}  # friction = .5*(2+1)=1.5
    net_now, fav, friction, result = _grade_spread("debit", 2.0, DEBIT_LEGS, qm)
    assert round(fav, 3) == 0.5
    assert round(friction, 3) == 1.5
    assert result == "neutral"


# ── CREDIT ───────────────────────────────────────────────────────────────────
def test_credit_win_decays_toward_zero():
    # entry credit 1.5; now short 1.0 / long 0.2 → net 0.8, fav = 1.5-0.8 = +0.7.
    qm = {"SHORT": _q(0.9, 1.1), "LONG": _q(0.1, 0.3)}
    net_now, fav, friction, result = _grade_spread("credit", 1.5, CREDIT_LEGS, qm)
    assert round(net_now, 3) == 0.8
    assert round(fav, 3) == 0.7
    assert result == "win"


def test_credit_loss_expands():
    # net widens to 1.9 (short 2.0 − long 0.1) → fav = 1.5-1.9 = -0.4 → loss.
    qm = {"SHORT": _q(1.9, 2.1), "LONG": _q(0.0, 0.2)}
    net_now, fav, friction, result = _grade_spread("credit", 1.5, CREDIT_LEGS, qm)
    assert round(net_now, 3) == 1.9
    assert round(fav, 3) == -0.4
    assert result == "loss"


def test_credit_flat_is_neutral():
    # net basically unchanged → fav within friction → neutral.
    qm = {"SHORT": _q(0.9, 1.1), "LONG": _q(0.0, 0.2)}  # net = 1.0-0.1 = 0.9
    net_now, fav, friction, result = _grade_spread("credit", 1.0, CREDIT_LEGS, qm)
    assert abs(fav) <= friction
    assert result == "neutral"


# ── friction band weighting + edge cases ─────────────────────────────────────
def test_friction_weights_butterfly_body_double():
    # 1 long low (+1), 2 short body (-2), 1 long high (+1).
    legs = [
        {"symbol": "LO", "long_short": 1, "side": "call"},
        {"symbol": "MID", "long_short": -2, "side": "call"},
        {"symbol": "HI", "long_short": 1, "side": "call"},
    ]
    qm = {"LO": _q(5.0, 5.2), "MID": _q(3.0, 3.2), "HI": _q(1.0, 1.2)}
    signed, friction = _reprice_spread(legs, qm)
    # widths: LO .2, MID .2 *2 (|−2|), HI .2 → sum .8 → friction .4
    assert round(friction, 3) == 0.4
    # signed = +5.1 -2*3.1 +1.1 = 0.0
    assert round(signed, 3) == 0.0


def test_missing_leg_quote_returns_none():
    qm = {"LONG": _q(2.0, 2.2)}  # SHORT absent
    assert _reprice_spread(DEBIT_LEGS, qm) is None
    assert _grade_spread("debit", 2.0, DEBIT_LEGS, qm) is None


def test_no_recoverable_width_uses_epsilon():
    # bid/ask both zero but explicit mids present → width 0 → epsilon friction.
    qm = {"LONG": {"bid": 0.0, "ask": 0.0, "mid": 3.0},
          "SHORT": {"bid": 0.0, "ask": 0.0, "mid": 0.5}}
    net_now, fav, friction, result = _grade_spread("debit", 2.0, DEBIT_LEGS, qm)
    assert friction == _FRICTION_EPSILON
    assert result == "win"  # clear +0.5 move still scores

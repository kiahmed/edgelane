"""Regression tests for tp's open-position grouping (aggregate_positions).

Run:  python3 tools/test_tp_aggregate.py   (or via pytest)
"""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
import tradier_positions as tp  # noqa: E402

L, S = "NDXP260622C30360000", "NDXP260622C30460000"


def _ml(oid, ts, sides, syms=(L, S)):
    return {"id": oid, "class": "multileg", "status": "filled", "quantity": 2.0,
            "create_date": ts,
            "leg": [{"option_symbol": syms[0], "side": sides[0], "quantity": 1.0, "exec_quantity": 1.0},
                    {"option_symbol": syms[1], "side": sides[1], "quantity": 1.0, "exec_quantity": 1.0}]}


def _bull_call_positions():
    return [
        {"symbol": L, "quantity": 1.0, "cost_basis": 7775.0, "date_acquired": "2026-06-22T15:11:48Z"},
        {"symbol": S, "quantity": -1.0, "cost_basis": -3675.0, "date_acquired": "2026-06-22T15:11:48Z"},
    ]


def test_reused_strikes_collapse_to_one_row():
    """One live spread, but open→close→re-open of the SAME strikes in history
    must NOT produce a row per order (the real '3 positions instead of 1' bug)."""
    orders = [
        _ml(1, "2026-06-22T15:11:48Z", ("buy_to_open", "sell_to_open")),
        _ml(2, "2026-06-22T15:30:00Z", ("sell_to_close", "buy_to_close")),
        _ml(3, "2026-06-22T18:00:00Z", ("buy_to_open", "sell_to_open")),  # latest open
    ]
    rows = tp.aggregate_positions(_bull_call_positions(), orders)
    assert len(rows) == 1, f"expected 1 row, got {len(rows)}"
    assert rows[0].kind == "multileg" and rows[0].label == "vertical"


def test_closing_order_alone_does_not_match_open_legs():
    """A pure closing order must never represent a held position."""
    orders = [_ml(2, "2026-06-22T15:30:00Z", ("sell_to_close", "buy_to_close"))]
    rows = tp.aggregate_positions(_bull_call_positions(), orders)
    # falls through to singletons (Pass 2), never a phantom multileg row
    assert all(r.kind == "single" for r in rows)
    assert len(rows) == 2


def test_normal_single_open_still_groups():
    orders = [_ml(1, "2026-06-22T15:11:48Z", ("buy_to_open", "sell_to_open"))]
    rows = tp.aggregate_positions(_bull_call_positions(), orders)
    assert len(rows) == 1 and rows[0].kind == "multileg"


def test_two_distinct_spreads_stay_two_rows():
    L2, S2 = "NDXP260622C30560000", "NDXP260622C30660000"
    positions = _bull_call_positions() + [
        {"symbol": L2, "quantity": 1.0, "cost_basis": 5000.0, "date_acquired": "2026-06-22T16:00:00Z"},
        {"symbol": S2, "quantity": -1.0, "cost_basis": -2000.0, "date_acquired": "2026-06-22T16:00:00Z"},
    ]
    orders = [
        _ml(1, "2026-06-22T15:11:48Z", ("buy_to_open", "sell_to_open")),
        _ml(4, "2026-06-22T16:00:00Z", ("buy_to_open", "sell_to_open"), syms=(L2, S2)),
    ]
    rows = tp.aggregate_positions(positions, orders)
    assert len([r for r in rows if r.kind == "multileg"]) == 2


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print("PASS", fn.__name__)
    print(f"\n{len(fns)} passed")

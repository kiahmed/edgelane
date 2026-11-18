"""tools/torque_ticker.py — value typing/validation (no network)."""
import importlib.util
from pathlib import Path

import pytest

_P = Path(__file__).resolve().parents[3] / "tools" / "torque_ticker.py"
_spec = importlib.util.spec_from_file_location("torque_ticker", _P)
tt = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tt)

COLS = {"ticker": ("text", False), "enabled": ("boolean", False), "sort_order": ("integer", False),
        "tick": ("numeric", False), "stop_loss_default": ("numeric", True),
        "close_target_default": ("numeric", True), "offsets": ("jsonb", False),
        "allowed_roots": ("_text", True), "notes": ("text", True), "updated_at": ("timestamp with time zone", False)}


@pytest.mark.parametrize("col,val,sql", [
    ("stop_loss_default", "50", "50.0"), ("stop_loss_default", "null", "null"),
    ("close_target_default", "40.5", "40.5"), ("enabled", "FALSE", "false"), ("sort_order", "110", "110"),
    ("offsets", '{"vertical":{"anchor":1}}', "'{\"vertical\": {\"anchor\": 1}}'::jsonb"),
    ("allowed_roots", "djxw,DJX", "array['DJXW','DJX']::text[]"),
    ("notes", "it's; drop table x", "'it''s; drop table x'"),
])
def test_to_sql_types_values(col, val, sql):
    assert tt.to_sql(col, val, COLS) == sql


@pytest.mark.parametrize("col,val", [
    ("stop_loss_default", "150"), ("stop_loss_default", "0"), ("close_target_default", "abc"),
    ("close_target_default", "nan"), ("sort_order", "1.5"), ("enabled", "yes"), ("tick", "null"),
    ("ticker", "ZZZ"), ("updated_at", "r:now()"), ("bogus", "1"), ("offsets", "[1,2]"),
    ("offsets", "{bad"), ("allowed_roots", "x y;z"),
])
def test_to_sql_rejects(col, val):
    with pytest.raises(SystemExit):
        tt.to_sql(col, val, COLS)


def test_ticker_and_pairs_validation():
    assert tt._ticker(" qqq ") == "QQQ"
    for bad in ("", "Q Q", "QQQ'--", "TOOLONGTICKER1"):
        with pytest.raises(SystemExit):
            tt._ticker(bad)
    with pytest.raises(SystemExit):
        tt.parse_pairs(["tick=1", "tick=2"])
    with pytest.raises(SystemExit):
        tt.parse_pairs([])


def test_effective_reflex_exits_and_extended_flag():
    e = tt._effective({"ticker": "DJX", "stop_loss_default": 50, "close_target_default": 60}, {"SPY"})
    assert e == {"extended_hours": "no", "sl": "50%", "tp": "60%"}
    e = tt._effective({"ticker": "SPY", "stop_loss_default": None, "close_target_default": None}, {"SPY"})
    assert e == {"extended_hours": "yes", "sl": "30% (default)", "tp": "30% (default)"}


def test_reflex_aliases_and_percent_values():
    assert tt.parse_pairs(["reflex_tp=15%*", "reflex_sl=40%"]) == [
        ("close_target_default", "15%*"), ("stop_loss_default", "40%")]
    assert tt.to_sql("reflex_tp", "15%*", COLS) == "15.0"
    assert tt.parse_pairs(["tp=15", "sl=40"]) == [("close_target_default", "15"), ("stop_loss_default", "40")]
    assert tt.to_sql("close_target_default", "30%", COLS) == "30.0"
    with pytest.raises(SystemExit):
        tt.parse_pairs(["reflex_tp=15", "close_target_default=20"])   # same column twice

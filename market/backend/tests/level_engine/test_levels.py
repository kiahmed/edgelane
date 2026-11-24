"""levels.py — nearest_strike, SPY→SPX conversion + merge (§6), clusters (§7.0)."""
from __future__ import annotations

import pytest

from level_engine.levels import (Cluster, ResolvedLevel, build_clusters, cluster_id,
                                 convert_levels, nearest_strike)


def row(id, symbol, price, name, kind=None, source="entered"):
    return {"id": id, "symbol": symbol, "price": price, "name": name, "kind": kind,
            "source": source}


def lvl(id, price, name=None, kind=None):
    return ResolvedLevel(id=id, price=price, name=name or id, kind=kind, source="entered",
                         symbol_origin="SPX", names=(name or id,), level_ids=(id,))


# ---- nearest_strike ----
@pytest.mark.parametrize("p,exp", [(7282.4, 7280.0), (7282.5, 7285.0), (7287.49, 7285.0),
                                   (7280.0, 7280.0), (7277.5, 7280.0), (7277.49, 7275.0)])
def test_nearest_strike_half_up(p, exp):
    assert nearest_strike(p) == exp


def test_nearest_strike_other_step():
    assert nearest_strike(595.5, 1.0) == 596.0
    with pytest.raises(ValueError):
        nearest_strike(100, 0)


# ---- convert_levels ----
def test_spx_entered_used_as_is_and_spy_converted():
    rows = [row("a", "SPX", 7300.0, "HOD"), row("b", "SPY", 600.0, "PM high")]
    levels, persist = convert_levels(rows, ratio=10.0, buffer_pts=2.5)
    assert [(l.id, l.price, l.source, l.symbol_origin) for l in levels] == [
        ("b", 6000.0, "converted", "SPY"), ("a", 7300.0, "entered", "SPX")]
    assert persist == [{"price": 6000.0, "name": "PM high", "kind": None, "ratio": 10.0,
                        "converted_from": "b"}]


def test_no_ratio_skips_spy():
    rows = [row("a", "SPX", 7300.0, "HOD"), row("b", "SPY", 600.0, "PM high")]
    levels, persist = convert_levels(rows, ratio=None, buffer_pts=2.5)
    assert [l.id for l in levels] == ["a"] and persist == []


def test_converted_rows_and_bad_rows_ignored():
    rows = [row("a", "SPX", 7300.0, "x"), row("c", "SPX", 6000.0, "old", source="converted"),
            row("n", "SPX", None, "no price"), row("z", "QQQ", 500.0, "nope"),
            row("neg", "SPY", -1, "bad")]
    levels, persist = convert_levels(rows, ratio=10.0, buffer_pts=2.5)
    assert [l.id for l in levels] == ["a"] and persist == []


def test_converted_price_rounded_2dp():
    levels, persist = convert_levels([row("b", "SPY", 595.21, "x")], ratio=10.0123456,
                                     buffer_pts=2.5)
    assert levels[0].price == round(595.21 * 10.0123456, 2)
    assert persist[0]["ratio"] == 10.0123456


def test_merge_within_buffer_spx_price_wins():
    rows = [row("spx", "SPX", 7300.0, "Golden Gate", kind="support"),
            row("spy", "SPY", 730.2, "PM high", kind="trigger")]       # → 7302.0
    levels, persist = convert_levels(rows, ratio=10.0, buffer_pts=2.5)
    assert len(levels) == 1
    m = levels[0]
    assert m.price == 7300.0 and m.source == "entered" and m.symbol_origin == "SPX"
    assert m.names == ("Golden Gate", "PM high") and m.name == "Golden Gate / PM high"
    assert m.level_ids == ("spx", "spy") and m.kind == "trigger"
    # merged SPY row still persisted with its ratio (audit)
    assert persist == [{"price": 7302.0, "name": "PM high", "kind": "trigger", "ratio": 10.0,
                        "converted_from": "spy"}]


def test_merge_boundary_inclusive_and_outside():
    rows = [row("spx", "SPX", 7300.0, "A"), row("in", "SPY", 730.25, "B"),
            row("out", "SPY", 730.26, "C")]
    levels, _ = convert_levels(rows, ratio=10.0, buffer_pts=2.5)
    by = {l.id: l for l in levels}
    assert by["spx"].level_ids == ("spx", "in")
    assert by["out"].price == 7302.6


def test_merge_picks_nearest_spx():
    rows = [row("s1", "SPX", 7300.0, "A"), row("s2", "SPX", 7304.0, "B"),
            row("y", "SPY", 730.3, "C")]                                 # 7303 → s2 (1.0)
    levels, _ = convert_levels(rows, ratio=10.0, buffer_pts=2.5)
    by = {l.id: l for l in levels}
    assert by["s2"].level_ids == ("s2", "y") and by["s1"].level_ids == ("s1",)


# ---- build_clusters ----
def test_two_levels_3pts_apart_one_cluster():
    cl = build_clusters([lvl("a", 7300.0, "PDH", "resistance"),
                         lvl("b", 7303.0, "1 ATR", "pivot")], cluster_pts=5.0)
    assert len(cl) == 1
    c = cl[0]
    assert c.price == 7301.5 and c.names == ("PDH", "1 ATR") and c.name == "PDH / 1 ATR"
    assert c.kind == "pivot" and c.strike == 7300.0 and (c.low, c.high) == (7300.0, 7303.0)


def test_gap_equal_cluster_pts_not_merged_and_sorted():
    cl = build_clusters([lvl("b", 7305.0), lvl("a", 7300.0)], cluster_pts=5.0)
    assert [c.price for c in cl] == [7300.0, 7305.0]


def test_chaining_and_cluster_spacing():
    cl = build_clusters([lvl("a", 7300.0), lvl("b", 7304.0), lvl("c", 7308.0),
                         lvl("d", 7313.0)], cluster_pts=5.0)
    assert len(cl) == 2
    assert cl[0].level_ids == ("a", "b", "c") and cl[0].price == 7304.0
    assert cl[1].price - cl[0].price >= 5.0


def test_kind_strength_and_tie_first():
    cl = build_clusters([lvl("a", 7300.0, kind="support"), lvl("b", 7301.0, kind="resistance")],
                        cluster_pts=5.0)
    assert cl[0].kind == "support"
    cl = build_clusters([lvl("a", 7300.0, kind=None), lvl("b", 7301.0, kind="trigger"),
                         lvl("c", 7302.0, kind="pivot")], cluster_pts=5.0)
    assert cl[0].kind == "trigger"


def test_cluster_id_stable_and_member_sensitive():
    a, b = lvl("a", 7300.0), lvl("b", 7302.0)
    c1 = build_clusters([a, b], 5.0)[0]
    c2 = build_clusters([b, a], 5.0)[0]
    assert c1.id == c2.id == cluster_id(["b", "a"])
    assert c1.id.startswith("c:") and len(c1.id) == 12
    c3 = build_clusters([a, b, lvl("x", 7301.0)], 5.0)[0]
    assert c3.id != c1.id


def test_merged_level_ids_feed_cluster_id():
    rows = [row("spx", "SPX", 7300.0, "A"), row("spy", "SPY", 730.1, "B")]
    levels, _ = convert_levels(rows, ratio=10.0, buffer_pts=2.5)
    c = build_clusters(levels, 5.0)[0]
    assert c.id == cluster_id(["spx", "spy"]) and c.price == 7300.0


def test_cluster_to_dict():
    c = build_clusters([lvl("a", 7300.0)], 5.0)[0]
    assert isinstance(c, Cluster)
    d = c.to_dict()
    assert d["id"] == c.id and d["price"] == 7300.0 and d["level_ids"] == ["a"]

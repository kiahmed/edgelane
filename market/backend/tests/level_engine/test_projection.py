"""projection.py — the Matrix Lookup grid port (§2, §8.3).

JS reference values below were produced by running the page's own
`_normCdf` / `_bsPrice` / `_priceSpreadAtSpot` (copied verbatim from
market/ui/index.html) under node. `test_live_js_parity` re-extracts them from
the page and re-runs node when it is installed (skipped otherwise).
"""
from __future__ import annotations

import json
import math
import pathlib
import shutil
import subprocess
from datetime import datetime, timezone

import pytest

from level_engine import projection as pj

LEGS = [{"strike": 7270.0, "side": "call", "long_short": 1, "iv": 0.15},
        {"strike": 7280.0, "side": "call", "long_short": -1, "iv": 0.14}]
JS = {"n": [0.09680055242003593, 0.5000000005, 0.758036416712803],
      "bs": [10.450575415435083, 5.573517865506496, 5.549435520686075, 0, 0],
      "spread": 5.709804374490886, "anchor": 4.592273156469673}
Y = lambda m: m / 1440 / 365


def test_norm_cdf_is_the_page_approximation():
    for x, v in zip((-1.3, 0.0, 0.7), JS["n"]):
        assert pj._norm_cdf(x) == pytest.approx(v, abs=1e-15)
        assert abs(pj._norm_cdf(x) - 0.5 * (1 + math.erf(x / math.sqrt(2)))) < 2e-7


def test_bs_price_hand_values_and_js():
    # textbook: S=K=100, T=1, r=5%, σ=20% → C 10.4506, P 5.5735
    assert pj.bs_price(100, 100, 1, 0.05, 0.2, "call") == pytest.approx(10.4506, abs=1e-4)
    assert pj.bs_price(100, 100, 1, 0.05, 0.2, "put") == pytest.approx(5.5735, abs=1e-4)
    got = [pj.bs_price(100, 100, 1, 0.05, 0.2, "call"), pj.bs_price(100, 100, 1, 0.05, 0.2, "put"),
           pj.bs_price(7268, 7280, Y(300), 0.05, 0.15, "call"),
           pj.bs_price(7268, 7260, Y(300), 0.05, None, "put"),
           pj.bs_price(7268, 7260, 0, 0.05, 0.2, "put")]
    for g, v in zip(got, JS["bs"]):
        assert g == pytest.approx(v, abs=1e-12)
    # sigma-less fallback = discounted intrinsic; T<=0 = intrinsic
    assert pj.bs_price(7268, 7260, Y(300), 0.05, None, "call") == pytest.approx(
        7268 - 7260 * math.exp(-0.05 * Y(300)))
    assert pj.bs_price(7250, 7260, 0, 0.05, 0.2, "put") == 10
    assert pj.bs_price(0, 7260, 1, 0.05, 0.2, "put") is None


def test_project_value_matches_js_spread():
    assert pj.project_value(LEGS, 7276, 150) == pytest.approx(JS["spread"], abs=1e-12)
    assert pj.project_value(LEGS, 7268, 300) == pytest.approx(JS["anchor"], abs=1e-12)
    # iv_by_leg overrides, percent accepted
    v = pj.project_value(LEGS, 7276, 150, iv_by_leg={(7270.0, "call"): 15.0, (7280.0, "call"): 0.14})
    assert v == pytest.approx(JS["spread"], abs=1e-12)


def test_grid_cell_offset_anchoring_matches_js_by_hand():
    live = 4.70
    js_cell = JS["spread"] + (live - JS["anchor"])        # page: px.total + offset
    assert pj.grid_cell(LEGS, live, False, 7268, 300, 7276, 150) == pytest.approx(js_cell, abs=1e-12)
    # at (spot_now, now) the cell is the live premium, debit and credit
    assert pj.grid_cell(LEGS, live, False, 7268, 300, 7268, 300) == pytest.approx(live)
    credit_legs = [dict(l, long_short=-l["long_short"]) for l in LEGS]
    assert pj.grid_cell(credit_legs, 4.7, True, 7268, 300, 7268, 300) == pytest.approx(4.7)


def test_projected_minutes_snaps_to_quarter_hours():
    assert pj.projected_minutes(300, 0.5) == 150
    assert pj.projected_minutes(307, 0.5) == 150          # 153.5 → 150
    assert pj.projected_minutes(10, 0.5) == 0              # 5 min → nearest quarter-hour = exp
    assert pj.projected_minutes(10, 1.0) == 0
    assert pj.projected_minutes(200, 0.0) == 200


def test_minutes_to_expiry_is_1600_et():
    now = datetime(2026, 11, 20, 16, 0, tzinfo=timezone.utc)   # 11:00 EST
    assert pj.minutes_to_expiry(now, "2026-11-20") == 300
    summer = datetime(2026, 7, 1, 15, 0, tzinfo=timezone.utc)  # 11:00 EDT
    assert pj.minutes_to_expiry(summer, "2026-07-01") == 300
    assert pj.minutes_to_expiry(now, None, 0.25) == 360


def test_time_axis_mirrors_page_columns():
    now = datetime(2026, 11, 20, 16, 7, tzinfo=timezone.utc)   # 11:07 ET
    ax = pj.time_axis(now, "2026-11-20")
    assert ax[0]["label"] == "now" and ax[0]["minutes_to_expiry"] == 293
    assert [c["label"] for c in ax[1:4]] == ["11:15", "11:30", "11:45"]
    assert len(ax) == 10 and ax[-1]["label"] == "exp" and ax[-1]["minutes_to_expiry"] == 0
    late = pj.time_axis(datetime(2026, 11, 20, 20, 40, tzinfo=timezone.utc), "2026-11-20")
    assert [c["label"] for c in late] == ["now", "15:45", "exp"]


def test_risk_reward_debit():
    pick = {"legs": LEGS, "net_premium": 4.70, "max_loss": 4.70, "spread_type": "debit"}
    r = pj.risk_reward(pick, 7276, 0.5, spot=7268, minutes_to_expiry_now=300)
    cell = JS["spread"] + (4.70 - JS["anchor"])
    assert r["cost"] == 470.0
    assert r["pnl"] == round((cell - 4.70) * 100, 2)
    assert r["value"] == round(470 + (cell - 4.70) * 100, 2)
    assert r["multiple"] == round(r["value"] / r["cost"], 2) and r["target"] == 7276
    assert r["minutes_to_expiry"] == 150
    # bare legs form
    r2 = pj.risk_reward(LEGS, 7276, 0.5, spot=7268, minutes_to_expiry_now=300, net_premium=4.70,
                        is_credit=False)
    assert r2["value"] == r["value"]


def test_risk_reward_credit_cost_is_max_loss():
    legs = [{"strike": 7255.0, "side": "put", "long_short": 1, "iv": 0.15},
            {"strike": 7260.0, "side": "put", "long_short": -1, "iv": 0.15},
            {"strike": 7280.0, "side": "call", "long_short": -1, "iv": 0.15},
            {"strike": 7285.0, "side": "call", "long_short": 1, "iv": 0.15}]
    pick = {"legs": legs, "net_premium": 3.0, "max_loss": 2.0, "spread_type": "credit"}
    r = pj.risk_reward(pick, 7268, 1.0, spot=7268, minutes_to_expiry_now=300)
    # page anchoring: the live-vs-model offset persists to expiry, where the
    # model is intrinsic (0 inside the shorts) → close = |−(0 + offset)|
    offset = -3.0 - pj.project_value(legs, 7268, 300)
    assert r["cost"] == 200.0 and r["minutes_to_expiry"] == 0
    assert r["pnl"] == round((3.0 - abs(offset)) * 100, 2)


def test_live_js_parity():
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    html = (pathlib.Path(__file__).resolve().parents[3] / "ui" / "index.html").read_text()
    a = html.index("  function _normCdf(x)")
    b = html.index("  // ── ET clock-time helpers")
    js = html[a:b] + """
const legs=%s;
console.log(JSON.stringify([_normCdf(0.7), _bsPrice(7268,7280,%r,0.05,0.15,'call'),
  _priceSpreadAtSpot(legs,7276,%r,0.05).total]));""" % (json.dumps(LEGS), Y(300), Y(150))
    out = json.loads(subprocess.run([node, "-e", js], capture_output=True, text=True,
                                    check=True, timeout=30).stdout)
    assert out[0] == pytest.approx(pj._norm_cdf(0.7), abs=1e-15)
    assert out[1] == pytest.approx(pj.bs_price(7268, 7280, Y(300), 0.05, 0.15, "call"), abs=1e-12)
    assert out[2] == pytest.approx(pj.project_value(LEGS, 7276, 150), abs=1e-12)

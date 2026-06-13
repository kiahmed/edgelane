"""Parity test: same chain → same outputs from JSX (Node) and Python.

For each synthetic fixture:
  1. Build the chain
  2. Run via Node (calls original JSX functions in jsx_engine.js)
  3. Run via Python (calls the new port in app/*)
  4. Compare bias signals + candidate composite ordering
  5. Fail if any field diverges beyond tolerance

The JSX side is the ground truth — failures should drive Python fixes.
"""
from __future__ import annotations

import json
import math
import subprocess
import sys
from pathlib import Path
import pytest

HERE = Path(__file__).resolve().parent
JSX_RUNNER = HERE / "run_jsx.js"

# Add backend root to path so the Python port modules are importable.
sys.path.insert(0, str(HERE.parent.parent))

from app.dealer_exposures import compute_dealer_exposures  # noqa: E402
from app.bias_engine import compute_bias_signals  # noqa: E402
from app.strategy_engine import generate_candidates, pick_best_candidate  # noqa: E402
from tests.parity.fixtures import ALL_FIXTURES  # noqa: E402


# ─────────────────────────────────────────────────────────────────────────────
# JSX bridge + comparator
# ─────────────────────────────────────────────────────────────────────────────


def run_jsx(op: str, args: dict) -> dict:
    """Invoke node run_jsx.js with the requested op + args, return parsed JSON.

    Raises RuntimeError on any non-zero exit so callers see node-side stack
    traces in the test report.
    """
    proc = subprocess.run(
        ["node", str(JSX_RUNNER)],
        input=json.dumps({"op": op, "args": args}),
        capture_output=True,
        text=True,
        timeout=30,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"node failed: {proc.stderr}")
    return json.loads(proc.stdout)


def approx_eq(a, b, rel_tol: float = 1e-6, abs_tol: float = 1e-3) -> bool:
    """Tolerant numeric comparison handling None, lists, dicts, strings."""
    if a is None and b is None:
        return True
    if isinstance(a, bool) or isinstance(b, bool):
        return a == b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return math.isclose(a, b, rel_tol=rel_tol, abs_tol=abs_tol)
    if isinstance(a, str) and isinstance(b, str):
        return a == b
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(approx_eq(x, y, rel_tol, abs_tol) for x, y in zip(a, b))
    if isinstance(a, dict) and isinstance(b, dict):
        keys = set(a.keys()) | set(b.keys())
        return all(approx_eq(a.get(k), b.get(k), rel_tol, abs_tol) for k in keys)
    return a == b


def _to_camel(snake: str) -> str:
    """snake_case → camelCase for fields where the JSX side uses camelCase."""
    parts = snake.split("_")
    return parts[0] + "".join(p.title() for p in parts[1:])


def _js_get(d: dict, snake: str):
    """Read a field from a JSX response, trying snake_case then camelCase."""
    if snake in d:
        return d[snake]
    return d.get(_to_camel(snake))


# ─────────────────────────────────────────────────────────────────────────────
# Dealer-exposure + bias parity (foundation tests)
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("fixture_fn", ALL_FIXTURES, ids=lambda fn: fn.__name__)
def test_bias_parity(fixture_fn):
    fx = fixture_fn()
    spot = fx["spot"]
    contracts = fx["contracts"]

    # 1. Dealer exposures — bedrock for everything downstream.
    py_de = compute_dealer_exposures(contracts, spot)
    js_de = run_jsx("dealer_exposures", {"spot": spot, "contracts": contracts})

    # Compare per-strike net_gex (the foundation).
    py_exp = py_de["exposures_by_date"][fx["expiration"]]
    js_exp = js_de["exposures_by_date"][fx["expiration"]]
    py_strikes = {r["strike"]: r["net_gex"] for r in py_exp["by_strike"]}
    js_strikes = {r["strike"]: r["net_gex"] for r in js_exp["by_strike"]}
    assert set(py_strikes.keys()) == set(js_strikes.keys()), (
        f"strike mismatch: py={sorted(py_strikes)} js={sorted(js_strikes)}"
    )
    for k in py_strikes:
        assert approx_eq(py_strikes[k], js_strikes[k]), (
            f"{fixture_fn.__name__}: net_gex@{k} py={py_strikes[k]} js={js_strikes[k]}"
        )

    # 2. Bias signals — the real test.
    greeks_raw_py = {
        "exposures_by_date": py_de["exposures_by_date"],
        "portfolio_totals": py_de["portfolio_totals"],
        "key_levels": py_de["key_levels"],
    }
    greeks_raw_js = {
        "exposures_by_date": js_de["exposures_by_date"],
        "portfolio_totals": js_de["portfolio_totals"],
        "key_levels": js_de["key_levels"],
    }

    py_bias = compute_bias_signals(
        "SPX", fx["expiration"], spot, greeks_raw_py, py_exp,
        chosen_dte=fx["chosen_dte"],
    )
    js_bias = run_jsx("bias_signals", {
        "symbol": "SPX",
        "expiration": fx["expiration"],
        "spot": spot,
        "greeks_raw": greeks_raw_js,
        "exposures_for_chosen": js_exp,
        "regime_override": None,
        "chosen_dte": fx["chosen_dte"],
    })

    scalar_fields = [
        "directional_score",
        "bias_label",
        "confidence",
        "gex_wall_strike",
        "gex_wall_strength",
        "gex_wall_type",
        "put_wall_strike",
        "put_wall_strength",
        "call_wall_strike",
        "call_wall_strength",
        "net_gex",
    ]
    for field in scalar_fields:
        js_val = _js_get(js_bias, field)
        py_val = py_bias.get(field)
        assert approx_eq(py_val, js_val), (
            f"{fixture_fn.__name__}: bias.{field} py={py_val!r} js={js_val!r}"
        )

    # Recommended strategies must agree exactly (list-equality, order matters)
    assert py_bias["recommended_strategies"] == js_bias["recommended_strategies"], (
        f"{fixture_fn.__name__}: recommended_strategies "
        f"py={py_bias['recommended_strategies']} js={js_bias['recommended_strategies']}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Candidate parity (per-strategy)
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("fixture_fn", ALL_FIXTURES, ids=lambda fn: fn.__name__)
@pytest.mark.parametrize("strategy", ["bull_put", "bear_call", "iron_condor"])
def test_candidate_parity(fixture_fn, strategy):
    fx = fixture_fn()
    spot = fx["spot"]
    contracts = fx["contracts"]

    py_de = compute_dealer_exposures(contracts, spot)
    py_bias = compute_bias_signals(
        "SPX", fx["expiration"], spot,
        {
            "exposures_by_date": py_de["exposures_by_date"],
            "portfolio_totals": py_de["portfolio_totals"],
            "key_levels": py_de["key_levels"],
        },
        py_de["exposures_by_date"][fx["expiration"]],
        chosen_dte=fx["chosen_dte"],
    )

    if py_bias["gex_wall_strike"] is None:
        walls_py = None
        walls_jsx = None
    else:
        walls_py = {
            "strike": py_bias["gex_wall_strike"],
            "strength": py_bias["gex_wall_strength"],
            "type": py_bias["gex_wall_type"],
            "net_gex": py_bias["net_gex"],
            "dte": fx["chosen_dte"],
        }
        # JSX-side walls dict uses camelCase netGex
        walls_jsx = {**walls_py, "netGex": walls_py["net_gex"]}

    expected_move = 30.0
    target_delta = 0.20
    width_factor = 1.0

    py_cands = generate_candidates(
        strategy, contracts, fx["chosen_dte"], expected_move,
        target_delta, width_factor, walls_py,
    )
    js_cands = run_jsx("generate_candidates", {
        "strategy": strategy,
        "contracts": contracts,
        "dte": fx["chosen_dte"],
        "expected_move": expected_move,
        "target_delta": target_delta,
        "width_factor": width_factor,
        "walls": walls_jsx,
    })

    assert len(py_cands) == len(js_cands), (
        f"{fixture_fn.__name__}/{strategy}: candidate count py={len(py_cands)} js={len(js_cands)}"
    )

    if not py_cands:
        return  # nothing more to compare

    numeric_fields = [
        "composite_score",
        "ev",
        "ev_adjusted",
        "pop_pct",
        "max_profit",
        "max_loss",
        "net_premium",
    ]
    string_fields = ["health", "liquidity", "label"]

    for py_c, js_c in zip(py_cands, js_cands):
        for field in numeric_fields:
            js_v = _js_get(js_c, field)
            py_v = py_c.get(field)
            assert approx_eq(py_v, js_v, rel_tol=1e-3, abs_tol=1e-2), (
                f"{fixture_fn.__name__}/{strategy}/{py_c.get('label')}: "
                f"{field} py={py_v!r} js={js_v!r}"
            )
        for field in string_fields:
            js_v = _js_get(js_c, field)
            py_v = py_c.get(field)
            assert py_v == js_v, (
                f"{fixture_fn.__name__}/{strategy}/{py_c.get('label')}: "
                f"{field} py={py_v!r} js={js_v!r}"
            )

    # Best-candidate label must match
    py_best = pick_best_candidate(py_cands)
    js_best_obj = run_jsx("composite_pick", {
        "strategy": strategy,
        "contracts": contracts,
        "dte": fx["chosen_dte"],
        "expected_move": expected_move,
        "target_delta": target_delta,
        "width_factor": width_factor,
        "walls": walls_jsx,
    })
    js_best = js_best_obj["best_label"]
    assert py_best == js_best, (
        f"{fixture_fn.__name__}/{strategy}: pick_best py={py_best!r} js={js_best!r}"
    )

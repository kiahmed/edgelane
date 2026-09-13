"""Matrix read-only API + snap surface (docs/matrix_events_update.md §4/§5).

The bearer gate and the "dark until provisioned" default matter as much as the
payloads: this surface carries no user JWT, so an unset token must close it
rather than open it.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import config
from app.config import Settings
from app.main import app
from app.poller import state as poller_state

TOKEN = "matrix-test-token"
SYM = "SPX"


def _snap() -> dict:
    return {
        "symbol": SYM,
        "expiration": "2026-09-18",
        "polled_at": "2026-09-13T14:00:00Z",
        "spot": 7790.0,
        "expected_move": 41.2,
        "bias": {"bias_label": "bearish", "directional_score": -80.0,
                 "confidence": "high", "net_gex": -1.2e9,
                 "recommended_strategies": ["bear_call"],
                 "call_wall_strike": 7800.0, "call_wall_strength": "major",
                 "put_wall_strike": 7700.0, "put_wall_strength": "moderate",
                 "vex_wall_strike": 7750.0, "tex_wall_strike": 7760.0,
                 "gex_wall_strike": 7800.0},
        "engine_pick": {"strategy": "bear_call", "name": "Bear Call Spread",
                        "label": "Aggressive", "strikes": [7680.0, 7720.0],
                        "composite_score": 86.1, "health": "healthy",
                        "structure_text": "Short 7680.0C / Long 7720.0C",
                        "net_premium": 7.625, "max_profit": 7.625,
                        "max_loss": 32.375, "pop_pct": 61.0, "ev_adjusted": 1.23,
                        "composite_verdict": {"label": "tradeable on limit"}},
        "strategies": {
            "bear_call": {"best": {"label": "Aggressive", "health": "healthy",
                                   "structure_text": "Short 7680.0C / Long 7720.0C",
                                   "composite_score": 86.1, "liquidity": "high",
                                   "pop_pct": 61.0,
                                   "composite_verdict": {"label": "tradeable on limit"}}},
            "bull_put": {"best": None},
        },
    }


@pytest.fixture(autouse=True)
def _wired():
    prev = dict(poller_state.latest_by_symbol)
    poller_state.latest_by_symbol[SYM] = _snap()
    config._cached = Settings(matrix_api_token=TOKEN, auth_enabled=False)
    yield
    poller_state.latest_by_symbol.clear()
    poller_state.latest_by_symbol.update(prev)
    config._cached = None


@pytest.fixture
def client():
    return TestClient(app)


def _auth(tok: str = TOKEN) -> dict:
    return {"Authorization": f"Bearer {tok}"}


# ── the gate ────────────────────────────────────────────────────────────────

def test_no_token_is_rejected(client):
    assert client.get(f"/matrix/state/{SYM}").status_code == 401


def test_a_wrong_token_is_rejected(client):
    assert client.get(f"/matrix/state/{SYM}", headers=_auth("nope")).status_code == 401


def test_the_surface_is_dark_until_provisioned(client):
    """A blank server token must CLOSE the endpoint, never open it."""
    config._cached = Settings(matrix_api_token="", auth_enabled=False)
    assert client.get(f"/matrix/state/{SYM}", headers=_auth()).status_code == 401


# ── /matrix/state ───────────────────────────────────────────────────────────

def test_pick_block(client):
    r = client.get(f"/matrix/state/{SYM}", headers=_auth(), params={"block": "pick"})
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["pick"]["strategy"] == "bear_call"
    assert data["expiration"] == "2026-09-18"


def test_grid_block_keeps_empty_slots_visible(client):
    r = client.get(f"/matrix/state/{SYM}", headers=_auth(), params={"block": "grid"})
    grid = r.json()["data"]["grid"]
    assert grid["bear_call"]["composite_score"] == 86.1
    assert grid["bull_put"] is None       # "no candidate" is itself information


def test_walls_block_maps_the_key_levels(client):
    r = client.get(f"/matrix/state/{SYM}", headers=_auth(), params={"block": "walls"})
    levels = r.json()["data"]["key_levels"]
    assert levels["call_wall"] == 7800.0 and levels["put_wall"] == 7700.0
    assert levels["vex_wall"] == 7750.0 and levels["tex_wall"] == 7760.0


def test_bias_block(client):
    r = client.get(f"/matrix/state/{SYM}", headers=_auth(), params={"block": "bias"})
    data = r.json()["data"]
    assert data["bias_label"] == "bearish" and data["confidence"] == "high"


def test_win_eval_block_survives_no_db(client):
    """_accuracy_view degrades to empty rather than 500ing when the DB is absent."""
    r = client.get(f"/matrix/state/{SYM}", headers=_auth(), params={"block": "win_eval"})
    assert r.status_code == 200
    assert set(r.json()["data"]) == {"trust", "stats"}


def test_an_unknown_block_is_422(client):
    r = client.get(f"/matrix/state/{SYM}", headers=_auth(), params={"block": "nope"})
    assert r.status_code == 422


def test_an_unknown_symbol_is_404(client):
    assert client.get("/matrix/state/ZZZZ", headers=_auth()).status_code == 404


def test_a_malformed_symbol_is_422(client):
    assert client.get("/matrix/state/not_a_symbol!", headers=_auth()).status_code == 422


# ── /matrix/snap ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("view", ["engine_pick", "strategy_grid", "bias_chip",
                                  "walls_chip", "win_eval_grid"])
def test_every_view_renders_a_standalone_crop(client, view):
    r = client.get(f"/matrix/snap/{SYM}", headers=_auth(), params={"view": view})
    assert r.status_code == 200
    html = r.text
    assert f'data-snap="{view}"' in html, "the crop target must be present"
    assert html.startswith("<!doctype html>")
    assert "<style>" in html and "</html>" in html
    # No SPA, no user session — the whole point of this endpoint.
    assert "matrix.facades.trade" in html
    assert "<script" not in html


def test_snap_defaults_to_the_engine_pick(client):
    r = client.get(f"/matrix/snap/{SYM}", headers=_auth())
    assert 'data-snap="engine_pick"' in r.text
    assert "Short 7680.0C" in r.text


def test_snap_requires_the_bearer(client):
    assert client.get(f"/matrix/snap/{SYM}").status_code == 401


def test_an_unknown_view_is_422(client):
    r = client.get(f"/matrix/snap/{SYM}", headers=_auth(), params={"view": "nope"})
    assert r.status_code == 422


def test_a_symbol_with_no_pick_still_renders(client):
    """An empty pick is a legitimate state — the card says so instead of 500ing."""
    poller_state.latest_by_symbol[SYM] = dict(_snap(), engine_pick={})
    r = client.get(f"/matrix/snap/{SYM}", headers=_auth(), params={"view": "engine_pick"})
    assert r.status_code == 200
    assert "No engine pick" in r.text


def test_rendered_values_are_escaped(client):
    """Snapshot text reaches the card as data; it must never become markup."""
    snap = _snap()
    snap["engine_pick"]["structure_text"] = '<img src=x onerror=alert(1)>'
    poller_state.latest_by_symbol[SYM] = snap
    r = client.get(f"/matrix/snap/{SYM}", headers=_auth())
    assert "<img src=x" not in r.text
    assert "&lt;img" in r.text

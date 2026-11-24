"""GET /internal/feed/{symbol} — the gate (fail closed, tunnel refusal, generic
404) and the payload (market data only, no `_` internals)."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import config
from app.config import Settings
from app.main import app
from app.poller import state as poller_state

TOKEN = "feed-test-token"
SYM = "SPX"
URL = f"/internal/feed/{SYM}"

EXPECTED_KEYS = {
    "symbol", "ts", "spot", "expiration", "dte", "expected_move", "contracts",
    "bias", "engine_pick", "spy_spot", "spx_prevclose", "spy_prevclose",
    "market_open", "market_reason",
}


def _contract(strike: float, side: str, bid: float, ask: float) -> dict:
    return {"symbol": f"SPXW{side[0].upper()}{int(strike)}", "strike": strike,
            "side": side, "expiration": "2026-11-23", "delta": 0.5, "gamma": 0.01,
            "theta": -1.0, "vega": 1.0, "iv": 0.15, "open_interest": 100,
            "volume": 10, "bid": bid, "ask": ask, "last": (bid + ask) / 2}


def _seed() -> None:
    poller_state.latest_by_symbol[SYM] = {
        "symbol": SYM, "spot": 6650.0, "expiration": "2026-11-23", "dte": 0.5,
        "expected_move": 38.5,
        "bias": {"bias_label": "bullish", "put_wall_strike": 6600.0},
        "strategies": {"bull_put": {"best": None}},
        "engine_pick": {"strategy": "bull_put", "decision_id": 7},
        "_quote_by_symbol": {"X": {"bid": 1, "ask": 2, "mid": 1.5}},
        "market_open": True, "evaluation_active": True,
        "ts": "2026-11-23T15:00:00Z",
    }
    poller_state.latest_contracts_by_symbol[SYM] = [
        _contract(6650, "call", 10.0, 11.0),
        _contract(6700, "call", 0.0, 0.5),     # one-sided → mid None
    ]
    poller_state.latest_quote_by_symbol.update({
        "SPX": {"last": 6650.0, "prevclose": 6620.0},
        "SPY": {"last": 663.2, "prevclose": 660.1},
    })
    poller_state.latest_spy_spot = 663.2
    poller_state.market_open = True
    poller_state.market_reason = "open"


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def armed(monkeypatch):
    monkeypatch.setattr(config, "_cached", Settings(internal_feed_token=TOKEN))
    _seed()


def _h(tok: str = TOKEN, **extra) -> dict:
    return {"X-Internal-Token": tok, **extra}


def _is_generic_404(resp) -> bool:
    return resp.status_code == 404 and resp.json() == {"detail": "Not Found"}


# ── the gate ────────────────────────────────────────────────────────────────

def test_fails_closed_when_token_unset(client):
    _seed()   # data present, but the server token is blank
    assert _is_generic_404(client.get(URL))
    assert _is_generic_404(client.get(URL, headers=_h("")))
    assert _is_generic_404(client.get(URL, headers=_h("anything")))


def test_refusal_matches_an_unknown_route(client, armed):
    missing = client.get("/internal/does-not-exist")
    refused = client.get(URL, headers=_h("nope"))
    assert missing.status_code == refused.status_code == 404
    assert missing.json() == refused.json()


def test_missing_token_is_404(client, armed):
    assert _is_generic_404(client.get(URL))


def test_wrong_token_is_404(client, armed):
    assert _is_generic_404(client.get(URL, headers=_h("wrong")))
    assert _is_generic_404(client.get(URL, headers=_h(TOKEN + "x")))


@pytest.mark.parametrize("hdr", [{"Cf-Ray": "8abc-IAD"},
                                 {"Cf-Connecting-Ip": "203.0.113.9"},
                                 {"cf-ray": "1", "cf-connecting-ip": "1.2.3.4"}])
def test_tunnel_traffic_refused_even_with_valid_token(client, armed, hdr):
    assert _is_generic_404(client.get(URL, headers=_h(**hdr)))


def test_not_in_openapi(client, armed):
    assert "/internal/feed/{symbol}" not in client.get("/openapi.json").json()["paths"]


# ── payload ─────────────────────────────────────────────────────────────────

def test_valid_token_returns_feed(client, armed):
    r = client.get(URL, headers=_h())
    assert r.status_code == 200
    body = r.json()
    assert set(body) == EXPECTED_KEYS
    assert body["ts"] == "2026-11-23T15:00:00Z"
    assert body["spot"] == 6650.0
    assert body["expiration"] == "2026-11-23" and body["dte"] == 0.5
    assert body["expected_move"] == 38.5
    assert body["spy_spot"] == 663.2
    assert body["spx_prevclose"] == 6620.0 and body["spy_prevclose"] == 660.1
    assert body["market_open"] is True and body["market_reason"] == "open"
    assert body["bias"]["bias_label"] == "bullish"
    assert body["engine_pick"]["strategy"] == "bull_put"
    mids = [c["mid"] for c in body["contracts"]]
    assert mids == [10.5, None]


def test_lowercase_symbol_ok(client, armed):
    assert client.get("/internal/feed/spx", headers=_h()).status_code == 200


def test_cached_contracts_not_mutated(client, armed):
    client.get(URL, headers=_h())
    assert "mid" not in poller_state.latest_contracts_by_symbol[SYM][0]


def test_payload_has_no_internals_or_account_data(client, armed):
    body = client.get(URL, headers=_h()).json()
    assert not [k for k in body if k.startswith("_")]
    forbidden = ("account", "user", "broker", "email", "token", "order", "position")
    flat = " ".join(body.keys()).lower()
    assert not [f for f in forbidden if f in flat]
    assert "_quote_by_symbol" not in body and "strategies" not in body


def test_no_snapshot_yet_is_503_after_auth(client, monkeypatch):
    monkeypatch.setattr(config, "_cached", Settings(internal_feed_token=TOKEN))
    poller_state.latest_by_symbol.pop(SYM, None)
    poller_state.latest_contracts_by_symbol.pop(SYM, None)
    r = client.get(URL, headers=_h())
    assert r.status_code == 503 and r.json() == {"error": "no snapshot yet"}
    # ...but an unauthenticated caller still can't tell it apart from a 404 route.
    assert _is_generic_404(client.get(URL, headers=_h("wrong")))


def test_snapshot_without_cached_chain_is_503(client, armed):
    poller_state.latest_contracts_by_symbol.pop(SYM, None)   # LEVEL_FEED_ENABLED off
    assert client.get(URL, headers=_h()).status_code == 503

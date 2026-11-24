"""The order dialog places the pick that is shown (§3.4): level picks resolve by
pick_id from the level engine, never from the legacy strategies."""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.routes import orders as O

from .conftest import (CAND_PICK_ID, EXPIRATION, LEVEL_PICK_ID, decision_wait,
                       decision_with_pick, fake_decision, use_settings)


class FakeTradier:
    def __init__(self):
        self.placed: list[dict] = []
        self.chain_calls = 0

    async def resolve_account_id(self):
        return "VA12345678"

    async def options_chain(self, symbol, expiration, greeks=True):
        self.chain_calls += 1
        return []

    async def place_order(self, account_id, payload):
        self.placed.append(payload)
        return {"order": {"id": 1, "status": "ok"}}


def _level_req(**over) -> O.OrderRequest:
    base = dict(symbol="SPX", strategy="call_butterfly", candidate_label="Near",
                quantity=2, limit_price=None, duration="day",
                pick_id=LEVEL_PICK_ID, source="levels")
    base.update(over)
    return O.OrderRequest(**base)


@pytest.fixture(autouse=True)
def _levels(monkeypatch):
    use_settings(monkeypatch, matrix_decision_source="levels")


async def test_level_pick_resolves_by_pick_id_and_builds_its_legs(monkeypatch):
    calls = fake_decision(monkeypatch, decision_with_pick())
    t = FakeTradier()
    out = await O._submit_order(_level_req(), t, preview=True, per_user=True)
    assert calls == [("SPX", 0)]                     # fresh read, not the 5 s cache
    p = out["payload"]
    assert [p[f"option_symbol[{i}]"] for i in range(3)] == [
        "SPXW261123C07260000", "SPXW261123C07280000", "SPXW261123C07300000"]
    assert [p[f"side[{i}]"] for i in range(3)] == ["buy_to_open", "sell_to_open", "buy_to_open"]
    assert [p[f"quantity[{i}]"] for i in range(3)] == ["2", "4", "2"]
    assert out["order_type"] == "debit"              # from the pick's spread_type
    assert out["limit_price"] == 3.2                 # net_premium fallback
    assert out["expiration"] == EXPIRATION
    assert t.chain_calls == 0                        # OCC symbols carried on the legs


async def test_source_levels_alone_is_enough_to_route_to_the_level_engine(monkeypatch):
    fake_decision(monkeypatch, decision_with_pick())
    out = await O._submit_order(_level_req(source=None), FakeTradier(), preview=True,
                                per_user=True)
    assert out["strategy"] == "call_butterfly"


@pytest.mark.parametrize("decision", [decision_wait(), decision_wait("done"),
                                      {**decision_with_pick(), "pick": {
                                          **decision_with_pick()["pick"], "pick_id": "other"}}])
async def test_stale_or_flipped_pick_is_409_never_a_legacy_fallback(monkeypatch, decision):
    fake_decision(monkeypatch, decision)
    t = FakeTradier()
    # bull_call / "Balanced" exists in the legacy strategies — must NOT be used.
    with pytest.raises(HTTPException) as ei:
        await O._submit_order(_level_req(strategy="bull_call", candidate_label="Balanced"),
                              t, preview=True, per_user=True)
    assert ei.value.status_code == 409
    assert "level pick no longer current — refresh" in ei.value.detail
    assert t.placed == []


async def test_source_levels_without_pick_id_is_409(monkeypatch):
    calls = fake_decision(monkeypatch, decision_with_pick())
    with pytest.raises(HTTPException) as ei:
        await O._submit_order(_level_req(pick_id=None), FakeTradier(), preview=True,
                              per_user=True)
    assert ei.value.status_code == 409 and calls == []


async def test_a_compact_candidate_without_legs_is_409(monkeypatch):
    fake_decision(monkeypatch, decision_with_pick())
    with pytest.raises(HTTPException) as ei:
        await O._submit_order(_level_req(pick_id=CAND_PICK_ID, strategy="bull_call"),
                              FakeTradier(), preview=True, per_user=True)
    assert ei.value.status_code == 409


async def test_strategy_mismatch_is_409(monkeypatch):
    fake_decision(monkeypatch, decision_with_pick())
    with pytest.raises(HTTPException) as ei:
        await O._submit_order(_level_req(strategy="put_butterfly"), FakeTradier(),
                              preview=True, per_user=True)
    assert ei.value.status_code == 409


async def test_level_engine_down_is_503_and_places_nothing(monkeypatch):
    fake_decision(monkeypatch, None)
    t = FakeTradier()
    with pytest.raises(HTTPException) as ei:
        await O._submit_order(_level_req(), t, preview=False, per_user=True)
    assert ei.value.status_code == 503 and t.placed == []


async def test_level_pick_id_in_engine_mode_is_409(monkeypatch):
    use_settings(monkeypatch, matrix_decision_source="engine")
    calls = fake_decision(monkeypatch, decision_with_pick())
    with pytest.raises(HTTPException) as ei:
        await O._submit_order(_level_req(), FakeTradier(), preview=True, per_user=True)
    assert ei.value.status_code == 409 and calls == []


async def test_levels_mode_refuses_a_legacy_spx_order(monkeypatch):
    # §3.4 revised: levels mode shows nothing legacy, so an SPX order must name
    # the level pick it came from.
    use_settings(monkeypatch, matrix_decision_source="levels")
    calls = fake_decision(monkeypatch, decision_with_pick())
    req = O.OrderRequest(symbol="SPX", strategy="bull_call", candidate_label="Balanced",
                         quantity=1, limit_price=4.0)
    with pytest.raises(HTTPException) as ei:
        await O._submit_order(req, FakeTradier(), preview=True, per_user=True)
    assert ei.value.status_code == 409 and calls == []


@pytest.mark.parametrize("mode", ["engine"])
async def test_legacy_order_path_is_unchanged(monkeypatch, mode):
    use_settings(monkeypatch, matrix_decision_source=mode)
    calls = fake_decision(monkeypatch, decision_with_pick())
    req = O.OrderRequest(symbol="SPX", strategy="bull_call", candidate_label="Balanced",
                         quantity=1, limit_price=4.0)
    out = await O._submit_order(req, FakeTradier(), preview=True, per_user=True)
    assert calls == []
    p = out["payload"]
    assert p["option_symbol[0]"] == "SPXW261123C07270000"
    assert p["option_symbol[1]"] == "SPXW261123C07280000"
    assert out["label"] == "Balanced" and out["limit_price"] == 4.0
    # and the legacy lookup still 404s on unknown labels, as before
    with pytest.raises(HTTPException) as ei:
        await O._submit_order(O.OrderRequest(symbol="SPX", strategy="bull_call",
                                             candidate_label="Nope"),
                              FakeTradier(), preview=True, per_user=True)
    assert ei.value.status_code == 404


async def test_webull_path_also_places_the_level_pick(monkeypatch):
    fake_decision(monkeypatch, decision_with_pick())

    class FakeWebull:
        async def first_account_id(self):
            return "WB1"

        async def preview_option(self, account_id, orders):
            return {"ok": True}

    out = await O._submit_webull(_level_req(), FakeWebull(), preview=True)
    assert out["broker"] == "webull" and out["order_type"] == "debit"
    assert out["expiration"] == EXPIRATION

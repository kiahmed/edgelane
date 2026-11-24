"""app.level_pick: bounded, cached, never raises; config keys (§3.4)."""
from __future__ import annotations

import logging

import httpx

from app import level_pick
from app.config import Settings, _coerce

from .conftest import CAND_PICK_ID, LEVEL_PICK_ID, decision_with_pick, use_settings


def _upstream(monkeypatch, status=200, body=None, exc=None):
    calls = []

    async def _get(url, params, timeout):
        calls.append(url)
        if exc is not None:
            raise exc
        return status, body

    monkeypatch.setattr(level_pick, "_http_get", _get)
    return calls


async def test_fetch_decision_returns_inner_decision_plus_status(monkeypatch):
    s = use_settings(monkeypatch, level_engine_read_url="http://le:8791")
    dec = decision_with_pick()
    dec.pop("engine_status")
    calls = _upstream(monkeypatch, body={"symbol": "SPX", "decision": dec,
                                         "status": {"feed": "ok"}})
    out = await level_pick.fetch_decision(s, "spx")
    assert calls == ["http://le:8791/levels/decision/SPX"]
    assert out["pick"]["pick_id"] == LEVEL_PICK_ID
    assert out["engine_status"] == {"feed": "ok"}


async def test_cache_absorbs_polling_and_max_age_zero_forces_a_read(monkeypatch):
    s = use_settings(monkeypatch)
    calls = _upstream(monkeypatch, body={"decision": decision_with_pick(), "status": {}})
    for _ in range(5):
        await level_pick.fetch_decision(s, "SPX")
    assert len(calls) == 1
    await level_pick.fetch_decision(s, "SPX", max_age=0)
    assert len(calls) == 2


async def test_errors_are_none_and_never_raise(monkeypatch):
    s = use_settings(monkeypatch)
    _upstream(monkeypatch, exc=httpx.ConnectError("down"))
    assert await level_pick.fetch_decision(s, "SPX") is None
    level_pick.clear_cache()
    _upstream(monkeypatch, exc=RuntimeError("anything"))
    assert await level_pick.fetch_decision(s, "SPX") is None
    level_pick.clear_cache()
    _upstream(monkeypatch, status=503, body=None)
    assert await level_pick.fetch_decision(s, "SPX") is None
    level_pick.clear_cache()
    _upstream(monkeypatch, status=404, body={"detail": "Not Found"})
    assert await level_pick.fetch_decision(s, "SPX") is None


async def test_reachable_without_a_decision_is_not_unavailable(monkeypatch):
    s = use_settings(monkeypatch)
    _upstream(monkeypatch, body={"symbol": "SPX", "decision": None, "status": {"feed": "down"}})
    out = await level_pick.fetch_decision(s, "SPX")
    assert out["pick"] is None and out["call"] is None and out["status"] == "no_decision"
    assert level_pick.compact_decision(out)["status"] == "no_decision"


def test_find_level_pick_searches_pick_and_candidates():
    dec = decision_with_pick()
    assert level_pick.find_level_pick(dec, LEVEL_PICK_ID)["strategy"] == "call_butterfly"
    assert level_pick.find_level_pick(dec, CAND_PICK_ID)["strategy"] == "bull_call"
    assert level_pick.find_level_pick(dec, "nope") is None
    assert level_pick.find_level_pick(dec, None) is None
    assert level_pick.find_level_pick(None, LEVEL_PICK_ID) is None
    assert level_pick.find_level_pick({"pick": None}, LEVEL_PICK_ID) is None


def test_compact_decision_unavailable():
    assert level_pick.compact_decision(None) == {"status": "unavailable"}


def test_config_keys_and_validation(caplog):
    assert Settings().matrix_decision_source == "engine"
    assert Settings().level_engine_read_url == "http://facades-levels-engine:8791"
    assert _coerce({"MATRIX_DECISION_SOURCE": "Levels"})["matrix_decision_source"] == "levels"
    assert _coerce({"MATRIX_DECISION_SOURCE": "engine"})["matrix_decision_source"] == "engine"
    with caplog.at_level(logging.WARNING):
        assert _coerce({"MATRIX_DECISION_SOURCE": "lvls"})["matrix_decision_source"] == "engine"
    assert "MATRIX_DECISION_SOURCE" in caplog.text
    assert _coerce({"LEVEL_ENGINE_READ_URL": "http://x:1/ "})["level_engine_read_url"] == "http://x:1"
    assert "level_engine_read_url" not in _coerce({"LEVEL_ENGINE_READ_URL": "  "})

"""Saved exit settings per account per ticker (torque_sl_settings): stop-loss
and take-profit, and that a TP-only save never reads as stop-loss saved."""
import pytest
from fastapi import HTTPException

from app import torque_guardian as g
from app import torque_config as tcfg
from app.routes import torque as troute
from tests.torque.conftest import FakeRequest, FakeTradier

USER = {"id": "u1", "auth": "dev"}


@pytest.fixture
def store(monkeypatch):
    rows = {}

    async def _key(request, user, account_id=None):
        return "live:ACC"
    async def _get(key):
        return [dict(r) for (k, _), r in rows.items() if k == key]
    async def _upsert(row):
        cur = rows.setdefault((row["account_key"], row["ticker"]),
                              {"enabled": True, "sl_saved": False})   # the table's column defaults
        cur.update(row)
        return True
    monkeypatch.setattr(troute, "_sl_account_key", _key)
    monkeypatch.setattr(troute.supabase_admin, "get_sl_settings", _get)
    monkeypatch.setattr(troute.supabase_admin, "upsert_sl_setting", _upsert)
    return rows


async def _get_setting(sym):
    return await troute.torque_sl_setting_get(sym, FakeRequest(FakeTradier()), user=USER)


async def test_never_saved_returns_both_defaults(store):
    r = await _get_setting("QQQ")
    assert r["saved"] is False and r["enabled"] is True
    assert r["tp_saved"] is False and r["tp_enabled"] is True
    assert r["tp_pct"] == tcfg.close_target_default("QQQ")


async def test_tp_save_persists_and_does_not_mark_stop_loss_saved(store):
    await troute.torque_tp_setting_set(troute.TPSettingRequest(symbol="QQQ", enabled=False, pct=15),
                                       FakeRequest(FakeTradier()), user=USER)
    r = await _get_setting("QQQ")
    assert r["tp_saved"] is True and r["tp_enabled"] is False and r["tp_pct"] == 15
    assert r["saved"] is False                     # stop-loss half still "never saved"
    row = store[("live:ACC", "QQQ")]
    assert "pct" not in row and row["sl_saved"] is False


async def test_sl_save_then_tp_save_keeps_both(store):
    await troute.torque_sl_setting_set(troute.SLSettingRequest(symbol="QQQ", enabled=True, pct=20),
                                       FakeRequest(FakeTradier()), user=USER)
    await troute.torque_tp_setting_set(troute.TPSettingRequest(symbol="QQQ", enabled=True, pct=40),
                                       FakeRequest(FakeTradier()), user=USER)
    r = await _get_setting("QQQ")
    assert (r["saved"], r["enabled"], r["pct"]) == (True, True, 20)
    assert (r["tp_saved"], r["tp_enabled"], r["tp_pct"]) == (True, True, 40)


async def test_guardian_ignores_a_tp_only_row(store, monkeypatch):
    await troute.torque_tp_setting_set(troute.TPSettingRequest(symbol="QQQ", enabled=True, pct=40),
                                       FakeRequest(FakeTradier()), user=USER)
    g._SETTINGS_CACHE.pop("live:ACC", None)
    monkeypatch.setattr(g.supabase_admin, "get_sl_settings", troute.supabase_admin.get_sl_settings)
    settings = await g._sl_settings("live:ACC")
    assert "QQQ" not in settings                   # outside positions stay unguarded until SL is saved


async def test_tp_off_refused_where_auto_close_is_required(store, monkeypatch):
    monkeypatch.setattr(troute.tcfg, "close_target_required", lambda t: True)
    with pytest.raises(HTTPException) as ei:
        await troute.torque_tp_setting_set(troute.TPSettingRequest(symbol="DJX", enabled=False),
                                           FakeRequest(FakeTradier()), user=USER)
    assert ei.value.status_code == 400


async def test_tp_save_unknown_ticker_refused(store):
    with pytest.raises(HTTPException):
        await troute.torque_tp_setting_set(troute.TPSettingRequest(symbol="ZZZZ", enabled=True),
                                           FakeRequest(FakeTradier()), user=USER)


@pytest.mark.parametrize("row,expected", [
    ({"sl_saved": True, "tp_enabled": None}, True),
    ({"sl_saved": True, "tp_enabled": True}, True),
    ({"sl_saved": False, "tp_enabled": None}, True),     # saved by a pre-0032 backend in the deploy gap
    ({"sl_saved": False, "tp_enabled": True}, False),    # TP-only row
    ({"tp_enabled": None}, True),                        # column not there yet
])
def test_sl_saved_row(row, expected):
    assert g.sl_saved_row(row) is expected


async def test_deploy_gap_sl_save_still_reads_as_saved(store):
    store[("live:ACC", "SPY")] = {"account_key": "live:ACC", "ticker": "SPY", "enabled": True,
                                  "pct": 25, "sl_saved": False}     # old backend: no sl_saved sent
    r = await _get_setting("SPY")
    assert r["saved"] is True and r["pct"] == 25

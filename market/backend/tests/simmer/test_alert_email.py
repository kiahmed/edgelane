"""Readiness-alert email: the body mirrors the card, and fan-out emails ONLY
opted-in users (notify_email), best-effort (a send failure never breaks fanout)."""
from __future__ import annotations

import pytest

from app import emailer, simmer_email
from app import simmer_watcher as sw
from app import supabase_admin

from .conftest import EXP, readiness_env


def _ready(structure="bull_put", score=80.0):
    env = readiness_env(score=score, structure=structure)
    env["symbol"] = "MSTR"
    env["expiration"] = EXP
    env["structure"] = structure
    env["strikes"] = {"short": 340, "long": 330, "width": 10}
    env["credit_fill"] = 1.15
    env["credit_mid"] = 1.28
    env["max_loss"] = 8.85
    env["pop_breakeven"] = 0.82
    env["expected_value"] = 0.121
    env["alpha"] = 0.0137
    env["management"] = {"profit_target_pct": 50.0, "manage_dte": 21, "stop_credit_multiple": 2.0}
    return env


# ── Renderer ────────────────────────────────────────────────────────────────
def test_render_has_all_card_fields():
    subj, html = simmer_email.render_readiness_email(_ready(), app_url="https://x.test")
    assert "MSTR" in subj and "Bull Put" in subj and "80/100" in subj
    for token in ("340 / 330", "10 wide", "achievable", "advertised", "$8.85",
                  "82%", "Take profit", "21 DTE", "2× credit", "Open in Simmer",
                  f"exp {EXP}", "Expiry"):   # expiry must be present + prominent
        assert token in html, token


def test_render_condor_strikes():
    env = _ready(structure="iron_condor")
    env["strikes"] = {"put": {"short": 320, "long": 310}, "call": {"short": 380, "long": 390}}
    _, html = simmer_email.render_readiness_email(env)
    assert "P 320/310" in html and "C 380/390" in html


def test_render_earnings_note_when_in_window():
    env = _ready()
    env["earnings"] = {"in_window": True, "direction": "bullish", "confidence": 0.7,
                       "close_before_print": True}
    _, html = simmer_email.render_readiness_email(env)
    assert "Earnings in the tenor" in html and "CLOSE BEFORE THE PRINT" in html


# ── Fan-out email gating ────────────────────────────────────────────────────
@pytest.fixture()
def wired(monkeypatch):
    """One watcher; toggle notify_email per test. Captures emails + inserts."""
    sent: list[tuple[str, str]] = []
    inserts: list[dict] = []
    cfg = {"notify_email": True}
    open_alert = {"rows": []}   # what _has_open_alert sees (empty = none outstanding)

    async def fake_select_many(table, select="*", filters=None, **kw):
        if table == "simmer_alerts":
            return open_alert["rows"]          # dedup probe
        return [{"user_id": "u1", "expiration": None}]   # watchlist

    async def fake_select_one(table, key, value, select):
        return {"min_score": 0, **cfg}

    async def fake_insert(table, row):
        inserts.append(row)
        return True

    async def fake_email(user_id):
        return "trader@example.com"

    async def fake_send(to, subject, html, **kw):
        sent.append((to, kw.get("from_email", "")))
        return True

    monkeypatch.setattr(supabase_admin, "select_many", fake_select_many)
    monkeypatch.setattr(supabase_admin, "_select_one", fake_select_one)
    monkeypatch.setattr(supabase_admin, "insert_row", fake_insert)
    monkeypatch.setattr(supabase_admin, "get_user_email", fake_email)
    monkeypatch.setattr(emailer, "send_email", fake_send)
    return {"sent": sent, "inserts": inserts, "cfg": cfg, "open_alert": open_alert}


async def test_optin_user_gets_email_with_product_sender(wired):
    await sw.fanout_alert(_ready(), {"state": "contango", "calm": True})
    assert len(wired["sent"]) == 1
    to, frm = wired["sent"][0]
    assert to == "trader@example.com"
    assert "solutionjet.net" in frm and "Simmer" in frm   # product-specific sender


async def test_optout_user_gets_no_email(wired):
    wired["cfg"]["notify_email"] = False
    await sw.fanout_alert(_ready(), {"state": "contango", "calm": True})
    assert wired["sent"] == []
    assert len(wired["inserts"]) == 1                     # in-app row still written


async def test_email_failure_never_breaks_fanout(wired, monkeypatch):
    async def boom(user_id):
        raise RuntimeError("gotrue down")
    monkeypatch.setattr(supabase_admin, "get_user_email", boom)
    n = await sw.fanout_alert(_ready(), {"state": "contango", "calm": True})
    assert n == 1                                         # insert still counted
    assert wired["sent"] == []


async def test_open_alert_suppresses_duplicate_and_email(wired):
    # User already has an UNACKNOWLEDGED alert for this symbol+expiry (e.g. after
    # a watcher restart re-fired). No second row, no repeat email.
    wired["open_alert"]["rows"] = [{"id": "existing"}]
    n = await sw.fanout_alert(_ready(), {"state": "contango", "calm": True})
    assert n == 0
    assert wired["inserts"] == []
    assert wired["sent"] == []


# ── Escaping (defense-in-depth for future untrusted fields) ─────────────────
def test_render_escapes_untrusted_text():
    env = _ready()
    env["symbol"] = "AB<x>"                    # uppercased → AB<X> before escaping
    env["earnings"] = {"in_window": True, "direction": "<script>alert(1)</script>",
                       "confidence": 0.5, "close_before_print": True}
    _, html = simmer_email.render_readiness_email(env)
    # direction (not uppercased) proves tag escaping end-to-end
    assert "<script>" not in html and "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    # symbol angle brackets escaped too (case-insensitive on the letter)
    assert "AB<" not in html and "AB&lt;" in html

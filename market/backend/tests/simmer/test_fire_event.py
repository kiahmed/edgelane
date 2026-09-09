"""Admin end-to-end fire tool (tools/simmer_fire_event.py). No network: the
publish + email paths are stubbed; we assert ticker/expiry selection, a valid
renderable envelope, and that --no-publish / --no-email are honored."""
from __future__ import annotations

from datetime import date

import pytest

from app import simmer_email
import tools.simmer_fire_event as fx


# ── ticker + expiry selection ───────────────────────────────────────────────
def test_pick_ticker_db_first_else_nvda():
    assert fx.pick_ticker(["AMD", "NVDA"]) == "AMD"
    assert fx.pick_ticker([]) == "NVDA"
    assert fx.pick_ticker(None) == "NVDA"
    assert fx.pick_ticker(["", "  "]) == "NVDA"


def test_nearest_expiry_prefers_listed_future():
    t = date(2026, 9, 9)
    assert fx.nearest_expiry(["2026-08-01", "2026-09-18", "2026-10-16"], today=t) == "2026-09-18"
    assert fx.nearest_expiry([], today=t) == fx.synthetic_expiry(today=t)
    assert date.fromisoformat(fx.synthetic_expiry(today=t)).weekday() == 4   # Friday


# ── envelope ────────────────────────────────────────────────────────────────
def test_build_envelope_is_valid_and_marked():
    env = fx.build_envelope("NVDA", "2026-09-18", "ready", today=date(2026, 9, 9))
    assert env["_test_fire"] is True
    assert env["symbol"] == "NVDA" and env["decision"] == "ready"
    for k in ("score", "structure", "strikes", "metrics", "management"):
        assert k in env
    assert env["metrics"]["vrp"] and env["metrics"]["iv_percentile_effective"]
    # renders through the REAL email renderer without error
    subject, html = simmer_email.render_readiness_email(env, app_url="x")
    assert "NVDA" in subject and "<" in html


def test_watch_state_maps_to_watch_entered():
    env = fx.build_envelope("NVDA", "2026-09-18", "watch", today=date(2026, 9, 9))
    assert env["decision"] == "watch"


# ── fire orchestration ──────────────────────────────────────────────────────
@pytest.fixture
def spies(monkeypatch):
    calls = {"publish": [], "email": []}

    async def _pub(symbol, state, expiry=None, **kw):
        calls["publish"].append({"symbol": symbol, "state": state,
                                 "expiry": expiry, **kw})
        return True

    async def _email(to, subject, html, **kw):
        calls["email"].append({"to": to, "subject": subject, **kw})
        return True

    monkeypatch.setattr(fx.simmer_events, "publish_transition", _pub)
    monkeypatch.setattr(fx.emailer, "send_email", _email)
    return calls


async def test_fire_publishes_forced_with_test_attr_and_emails(spies, monkeypatch):
    monkeypatch.setattr(fx, "resolve_tickers", lambda db=None: ["AMD", "NVDA"])
    summary = await fx.fire("ready", today=date(2026, 9, 9))

    assert summary["symbol"] == "AMD"               # DB/config first
    assert summary["state"] == "ready"
    assert summary["published"] is True and summary["email_sent"] is True

    pub = spies["publish"][0]
    assert pub["state"] == "ready" and pub["force"] is True
    assert pub["extra_attributes"]["test"] == "true"
    assert spies["email"][0]["to"] == fx.DEFAULT_ADMIN_EMAIL


async def test_fire_defaults_to_nvda_when_no_tickers(spies, monkeypatch):
    monkeypatch.setattr(fx, "resolve_tickers", lambda db=None: [])
    summary = await fx.fire("watch", today=date(2026, 9, 9))
    assert summary["symbol"] == "NVDA"
    assert summary["state"] == "watch_entered"      # mapped


async def test_fire_no_publish_honored(spies, monkeypatch):
    monkeypatch.setattr(fx, "resolve_tickers", lambda db=None: ["NVDA"])
    summary = await fx.fire("ready", publish=False, today=date(2026, 9, 9))
    assert spies["publish"] == []                   # topic untouched
    assert summary["published"] is False
    assert spies["email"] and summary["email_sent"] is True   # email still fires


async def test_fire_no_email_honored(spies, monkeypatch):
    monkeypatch.setattr(fx, "resolve_tickers", lambda db=None: ["NVDA"])
    summary = await fx.fire("ready", email=False, today=date(2026, 9, 9))
    assert spies["email"] == []                     # no email
    assert summary["email_sent"] is False
    assert spies["publish"] and summary["published"] is True


async def test_fire_uses_provider_expiry(spies, monkeypatch):
    monkeypatch.setattr(fx, "resolve_tickers", lambda db=None: ["NVDA"])

    class _Prov:
        async def expirations(self, symbol):
            return ["2099-01-15"]                   # a listed future expiry

    summary = await fx.fire("ready", provider=_Prov(), today=date(2026, 9, 9))
    assert summary["expiry"] == "2099-01-15"

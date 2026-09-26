"""POST /webhook/news_signal authentication.

This endpoint places real trades. Every other check on the route constrains
WHAT gets traded (ticker, market hours, entitlement, spread) — none of them
authenticate WHO is calling it. Verified end-to-end with TestClient against
the real mounted app, same as the finding this fixes: an anonymous POST used
to reach the handler and place an order.
"""
from __future__ import annotations

import hashlib
import hmac
import time

import pytest
from fastapi.testclient import TestClient

from app import config
from app.config import Settings
from app.main import app

SECRET = "news-reactor-test-secret"
BODY = (
    b'{"source_event_id":"attacker-1","headline":"x","symbol":"SPY",'
    b'"direction":"bullish","confidence":0.9}'
)


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture(autouse=True)
def _configured(monkeypatch):
    config._cached = Settings(news_reactor_webhook_secret=SECRET, auth_enabled=False)
    yield
    config._cached = None


def _sign(body: bytes, secret: str = SECRET, ts: int | None = None) -> dict[str, str]:
    ts = ts if ts is not None else int(time.time())
    mac = hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    return {"X-Signal-Timestamp": str(ts), "X-Signal-Signature": mac}


def test_unauthenticated_request_is_rejected(client):
    r = client.post("/webhook/news_signal", content=BODY,
                    headers={"Content-Type": "application/json"})
    assert r.status_code == 401


def test_wrong_secret_is_rejected(client):
    headers = _sign(BODY, secret="not-the-real-secret")
    headers["Content-Type"] = "application/json"
    r = client.post("/webhook/news_signal", content=BODY, headers=headers)
    assert r.status_code == 401


def test_stale_timestamp_is_rejected(client):
    headers = _sign(BODY, ts=int(time.time()) - 3600)   # 1h old, well past the window
    headers["Content-Type"] = "application/json"
    r = client.post("/webhook/news_signal", content=BODY, headers=headers)
    assert r.status_code == 401


def test_tampered_body_is_rejected(client):
    """Signature is computed over the ORIGINAL body; changing even one field
    after signing must invalidate it — proves the signature actually binds
    to the payload, not just presence of a valid-looking header."""
    headers = _sign(BODY)   # signed for "bullish"
    headers["Content-Type"] = "application/json"
    tampered = BODY.replace(b"bullish", b"bearish")
    r = client.post("/webhook/news_signal", content=tampered, headers=headers)
    assert r.status_code == 401


def test_no_secret_configured_fails_closed(client, monkeypatch):
    """Unconfigured must mean closed, never open-to-anyone — even a
    well-formed signature (computed with whatever the caller wants) must not
    pass when the server has no secret set at all."""
    config._cached = Settings(news_reactor_webhook_secret="", auth_enabled=False)
    headers = _sign(BODY, secret="anything")
    headers["Content-Type"] = "application/json"
    r = client.post("/webhook/news_signal", content=BODY, headers=headers)
    assert r.status_code == 401


def test_valid_signature_reaches_the_handler(client):
    """A correctly authenticated request must pass the auth gate — verified
    by observing it reach the ordinary (feature-off) drop response rather
    than a 401. Execution behavior past this point (fan-out, placement,
    idempotency) is covered directly against news_signal() in
    test_news_signal.py, which needs FakeTradier/FakeNewsDB wiring this
    lightweight TestClient app instance doesn't have."""
    headers = _sign(BODY)
    headers["Content-Type"] = "application/json"
    r = client.post("/webhook/news_signal", content=BODY, headers=headers)
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["accepted"] is False
    assert "ACCEPT_NEWS_REACTOR_SIGNALS" in body["reason"]   # reached real route logic, not the auth gate

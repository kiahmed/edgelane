"""The order path must always come back with an answer.

A stalled broker call used to leave /orders/preview hanging indefinitely: the
browser sat on "Previewing…" forever, and because uvicorn only writes its access
line once a response exists, the stall left no trace in the log either. These
tests pin the guarantee that every remote stage is bounded and reports which
stage gave up.
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException

from app.poller import state as poller_state
from app.routes import orders as O
from app.webull_client import WebullClient, WebullError


# --- helpers ---------------------------------------------------------------

SYMBOL = "SPX"
EXPIRATION = "2026-08-21"
LABEL = "Conservative"


def _snapshot() -> dict:
    """A snapshot shaped like the poller's, with OCC symbols already on the legs
    (the normal case — the chain ingest carries them through)."""
    return {
        "symbol": SYMBOL,
        "expiration": EXPIRATION,
        "strategies": {
            "bull_call": {
                "best": {
                    "label": LABEL,
                    "type": "debit",
                    "net_premium": 5.0,
                    "legs": [
                        {"side": "call", "strike": 7780.0, "long_short": 1,
                         "symbol": "SPXW260821C07780000"},
                        {"side": "call", "strike": 7785.0, "long_short": -1,
                         "symbol": "SPXW260821C07785000"},
                    ],
                }
            }
        },
    }


@pytest.fixture(autouse=True)
def _seed_snapshot():
    prev = dict(poller_state.latest_by_symbol)
    poller_state.latest_by_symbol[SYMBOL] = _snapshot()
    yield
    poller_state.latest_by_symbol.clear()
    poller_state.latest_by_symbol.update(prev)


@pytest.fixture(autouse=True)
def _fast_deadlines(monkeypatch):
    """Shrink the budgets so a 'hang' is exercised in milliseconds."""
    monkeypatch.setattr(O, "ACCOUNT_RESOLVE_TIMEOUT_SEC", 0.15)
    monkeypatch.setattr(O, "CHAIN_RESOLVE_TIMEOUT_SEC", 0.15)
    monkeypatch.setattr(O, "BROKER_RESOLVE_TIMEOUT_SEC", 0.15)
    monkeypatch.setattr(O, "PREVIEW_DEADLINE_SEC", 0.30)


def _req(**over) -> O.OrderRequest:
    base = dict(symbol=SYMBOL, strategy="bull_call", candidate_label=LABEL,
                quantity=1, limit_price=5.0, duration="day")
    base.update(over)
    return O.OrderRequest(**base)


class FakeTradier:
    """Per-user execution client. Any stage can be told to hang forever."""

    def __init__(self, account="VA12345678", hang_account=False, hang_chain=False):
        self.account = account
        self.hang_account = hang_account
        self.hang_chain = hang_chain
        self.placed: list[dict] = []
        self.resolve_calls = 0

    async def resolve_account_id(self):
        self.resolve_calls += 1
        if self.hang_account:
            await asyncio.sleep(3600)
        return self.account

    async def options_chain(self, symbol, expiration, greeks=True):
        if self.hang_chain:
            await asyncio.sleep(3600)
        return []

    async def place_order(self, account_id, payload):
        self.placed.append(payload)
        return {"order": {"id": 1, "status": "ok"}}

    async def close(self):
        pass


# --- _stage ----------------------------------------------------------------

async def test_stage_turns_a_hang_into_504():
    async def never():
        await asyncio.sleep(3600)

    with pytest.raises(HTTPException) as ei:
        await O._stage("account-resolve", never(), 0.05)
    assert ei.value.status_code == 504
    # The message must name the stage, so the UI can say what stalled.
    assert "account-resolve" in ei.value.detail
    assert "Nothing was placed" in ei.value.detail


async def test_stage_passes_through_a_fast_result():
    async def quick():
        return "ok"

    assert await O._stage("quick", quick(), 5.0) == "ok"


# --- the preview path ------------------------------------------------------

async def test_preview_returns_504_when_the_broker_stalls():
    """The reported symptom: a broker that never answers. It must surface as an
    error in bounded time rather than an open-ended wait."""
    client = FakeTradier(hang_account=True)
    with pytest.raises(HTTPException) as ei:
        await asyncio.wait_for(
            O._submit_order(_req(), client, preview=True, per_user=True),
            timeout=5.0,          # the test itself fails if the code hangs
        )
    assert ei.value.status_code == 504
    assert "account-resolve" in ei.value.detail
    assert client.placed == []    # nothing reached the broker


async def test_preview_returns_504_when_the_chain_fetch_stalls():
    snap = _snapshot()
    for leg in snap["strategies"]["bull_call"]["best"]["legs"]:
        leg.pop("symbol")          # force the chain lookup
    poller_state.latest_by_symbol[SYMBOL] = snap

    client = FakeTradier(hang_chain=True)
    with pytest.raises(HTTPException) as ei:
        await asyncio.wait_for(
            O._submit_order(_req(), client, preview=True, per_user=True),
            timeout=5.0,
        )
    assert ei.value.status_code == 504
    assert "chain-fetch" in ei.value.detail


async def test_preview_succeeds_and_marks_the_payload_as_preview():
    client = FakeTradier()
    out = await O._submit_order(_req(), client, preview=True, per_user=True)
    assert out["preview"] is True
    # Tradier's dry-run flag must be set — this is what keeps a preview from
    # ever becoming a live order.
    assert client.placed[0]["preview"] == "true"
    assert out["account_id"] == "VA12345678"


async def test_email_shaped_account_id_is_ignored_and_resolved_from_the_token():
    """A browser-autofilled email in the account box must not reach the broker;
    the real account_number is resolved from the user's own token instead."""
    client = FakeTradier(account="VA99999999")
    out = await O._submit_order(_req(account_id="user@example.com"),
                                client, preview=True, per_user=True)
    assert out["account_id"] == "VA99999999"
    assert client.resolve_calls == 1
    assert "@" not in client.placed[0]["account_id"] if "account_id" in client.placed[0] else True


# --- the route itself ------------------------------------------------------
#
# Calling the route fn directly (as the Torque route tests do) exercises the
# real handler — entry logging, stage deadlines, client cleanup — while a dev
# user routes to the house client, bypassing dependency injection.

DEV = {"id": "dev-local", "email": "dev@local", "auth": "dev"}


class FakeRequest:
    def __init__(self, client):
        self.app = type("A", (), {"state": type("S", (), {"tradier": client})()})()


async def test_preview_route_answers_instead_of_hanging():
    """End-to-end guarantee: a wedged broker yields a 504 the browser can render,
    never an open-ended request."""
    client = FakeTradier(hang_chain=True)
    snap = _snapshot()
    for leg in snap["strategies"]["bull_call"]["best"]["legs"]:
        leg.pop("symbol")
    poller_state.latest_by_symbol[SYMBOL] = snap

    with pytest.raises(HTTPException) as ei:
        await asyncio.wait_for(
            O.preview_order(_req(account_id="VA12345678"), FakeRequest(client), user=DEV),
            timeout=5.0,
        )
    assert ei.value.status_code == 504
    assert client.placed == []


async def test_preview_route_returns_a_preview():
    client = FakeTradier()
    out = await O.preview_order(_req(account_id="VA12345678"), FakeRequest(client), user=DEV)
    assert out["preview"] is True
    assert client.placed[0]["preview"] == "true"


# --- Webull ----------------------------------------------------------------

async def test_webull_sdk_call_is_bounded():
    """The SDK is synchronous and driven from a worker thread; without a bound a
    wedged call meant a request that never completed."""
    import app.webull_client as W

    import threading

    wc = WebullClient(app_key="k", app_secret="s")
    wc._trade = object()                      # skip the init handshake

    # A stoppable stand-in for a wedged SDK call. It must be releasable: the
    # timeout leaves the thread running detached (by design — a sync SDK call
    # can't be killed), and a thread parked for an hour would stall interpreter
    # shutdown long after the assertion has been made.
    release = threading.Event()

    def wedged():
        release.wait(30)

    W._CALL_TIMEOUT_SEC = 0.1
    try:
        with pytest.raises(WebullError) as ei:
            await asyncio.wait_for(wc._call(wedged), timeout=5.0)
    finally:
        W._CALL_TIMEOUT_SEC = 25
        release.set()                         # let the detached thread finish
    assert "did not respond" in str(ei.value)
    assert "Nothing was placed" in str(ei.value)

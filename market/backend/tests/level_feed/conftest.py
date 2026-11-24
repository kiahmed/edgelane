"""Level-feed tests are hermetic: safe default settings (no real config, no
keys), poller state saved/restored around every test, no network."""
from __future__ import annotations

import pytest

from app import config
from app.config import Settings
from app.poller import state as poller_state

_FEED_ATTRS = ("latest_by_symbol", "latest_contracts_by_symbol",
               "latest_quote_by_symbol", "_pending_feed", "last_poll_at", "last_error")


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    monkeypatch.setattr(config, "_cached", Settings())
    saved = {a: dict(getattr(poller_state, a)) for a in _FEED_ATTRS}
    saved_spy = poller_state.latest_spy_spot
    saved_mkt = (poller_state.market_open, poller_state.market_reason)
    yield
    for a, v in saved.items():
        d = getattr(poller_state, a)
        d.clear()
        d.update(v)
    poller_state.latest_spy_spot = saved_spy
    poller_state.market_open, poller_state.market_reason = saved_mkt

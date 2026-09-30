"""Matrix tests must never touch real services.

get_settings() reads the developer's real edgelane_market.config, which has
MATRIX_EVENTS_ENABLED=true, a GCP project and the Supabase service key. On
2026-09-29 that let the test suite write 14 fixture rows into the LIVE
matrix_event_ledger (Pub/Sub was stubbed, so nothing was published — but the
next change might not be). So for every test in this package:

  * settings start from safe defaults (events off, no project, no keys) — a
    test that needs something sets config._cached itself, which still wins;
  * the lowest real layers — the Pub/Sub client and the Supabase HTTP write —
    are replaced with stubs that FAIL the test if reached. Everything above
    them (publish_transition, matrix_ledger.record) still runs for real, so
    their own tests keep working by stubbing the same layer themselves.
"""
from __future__ import annotations

import pytest

from app import config, emailer, matrix_events, matrix_signals, supabase_admin
from app.config import Settings

# The real session-open window, captured before the autouse fixture below
# replaces it — so its own test can exercise it without undoing the guards.
REAL_WITHIN_OPEN_WINDOW = matrix_signals._within_open_window


@pytest.fixture(autouse=True)
def _no_real_services(monkeypatch):
    monkeypatch.setattr(config, "_cached", Settings())

    async def _no_supabase(*a, **k):
        raise AssertionError("test reached a REAL Supabase write — stub it")

    def _no_pubsub():
        raise AssertionError("test reached the REAL Pub/Sub client — stub it")

    monkeypatch.setattr(supabase_admin, "insert_row_ignore_duplicates", _no_supabase)
    monkeypatch.setattr(supabase_admin, "insert_row", _no_supabase)
    monkeypatch.setattr(matrix_events, "_get_publisher", _no_pubsub)

    async def _no_email(*a, **k):
        raise AssertionError("test reached the REAL email send — stub it")

    monkeypatch.setattr(emailer, "send_email", _no_email)

    # session_open only fires in the first hour of the session; tests run at any
    # wall-clock time, so they start "inside" it. Its own tests override this.
    monkeypatch.setattr(matrix_signals, "_within_open_window", lambda settings: True)
    yield

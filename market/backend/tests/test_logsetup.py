"""LOG_LEVEL: quiet keeps transactions + warnings/errors and drops chatter."""
from __future__ import annotations

import logging

import pytest

from app import logsetup


@pytest.fixture(autouse=True)
def _restore_levels():
    names = ["", "uvicorn", "uvicorn.error", "uvicorn.access", "edgelane.txn"]
    saved = {n: logging.getLogger(n).level for n in names}
    yield
    for n, lvl in saved.items():
        logging.getLogger(n).setLevel(lvl)


def _enabled(name, level):
    return logging.getLogger(name).isEnabledFor(level)


def test_quiet_keeps_transactions_and_errors_but_drops_chatter():
    assert logsetup.configure_logging("quiet") == "quiet"
    assert _enabled("edgelane.txn", logging.INFO)            # transactions visible
    assert _enabled("edgelane.market.torque", logging.WARNING)
    assert not _enabled("edgelane.market.poller", logging.INFO)   # "poll OK" chatter gone
    assert not _enabled("httpx", logging.INFO)                     # per-call HTTP lines gone
    assert not _enabled("uvicorn.access", logging.INFO)            # per-request access log gone
    assert _enabled("uvicorn.error", logging.ERROR)


def test_info_restores_everything():
    logsetup.configure_logging("info")
    for name in ("edgelane.market.poller", "httpx", "uvicorn.access", "edgelane.txn"):
        assert _enabled(name, logging.INFO)
    assert not _enabled("httpx", logging.DEBUG)


def test_debug_is_debug():
    logsetup.configure_logging("debug")
    assert _enabled("httpx", logging.DEBUG) and _enabled("edgelane.txn", logging.DEBUG)


@pytest.mark.parametrize("raw", [None, "", "LOUD", "  INFO  "])
def test_normalize_unknown_falls_back_to_quiet(raw):
    expected = "info" if raw and raw.strip().lower() == "info" else "quiet"
    assert logsetup.normalize(raw) == expected


def test_txn_formats_fields_and_omits_none(caplog):
    logsetup.configure_logging("quiet")
    with caplog.at_level(logging.INFO, logger="edgelane.txn"):
        logsetup.txn("order submitted", order_id=7, price=None, tag="torqueNews")
    assert "order submitted order_id=7 tag=torqueNews" in caplog.text
    assert "price" not in caplog.text


def test_log_level_is_read_from_config_and_defaults_to_quiet():
    from app.config import Settings, _coerce
    assert _coerce({"LOG_LEVEL": " INFO "})["log_level"] == "info"
    assert Settings(auth_enabled=False).log_level == "quiet"

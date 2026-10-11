"""Backend-wide test fixtures.

Torque's per-ticker setup lives in Supabase (public.torque_tickers_setup) and
is held in memory by app.torque_config. Every test starts from exactly the
rows migration 0021 seeds — parsed from the migration file itself, so the
tests and the database can never drift apart.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from app import torque_config as tcfg

_SEED_SQL = (Path(__file__).resolve().parents[3]
             / "supabase" / "migrations" / "0021_torque_tickers_setup.sql")


def torque_seed_rows() -> list[dict]:
    """What the migrations install in torque_tickers_setup: migration 0021's
    `$seed$` rows, with every later torque migration's `$seed$` patch rows
    (e.g. 0023's max_spread_pct) merged in by ticker, in file order."""
    def _block(path: Path) -> list[dict]:
        m = re.search(r"\$seed\$(\[.*?\])\$seed\$", path.read_text(), re.S)
        assert m, f"no $seed$ block in {path}"
        return json.loads(m.group(1))

    rows = {r["ticker"]: dict(r) for r in _block(_SEED_SQL)}
    for path in sorted(_SEED_SQL.parent.glob("*_torque_*.sql")):
        text = path.read_text()
        if path.name <= _SEED_SQL.name or "torque_tickers_setup" not in text or "$seed$" not in text:
            continue
        for patch in _block(path):
            rows.setdefault(patch["ticker"], {"ticker": patch["ticker"]}).update(patch)
    return list(rows.values())


@pytest.fixture(autouse=True)
def _torque_ticker_setup():
    saved_rows, saved_state = dict(tcfg._SETUP), dict(tcfg._SETUP_STATE)
    tcfg.apply_ticker_setup(torque_seed_rows())
    yield
    tcfg._SETUP.clear()
    tcfg._SETUP.update(saved_rows)
    tcfg._SETUP_STATE.clear()
    tcfg._SETUP_STATE.update(saved_state)

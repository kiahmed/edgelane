"""Overnight movers: RelativeQs client parsing, the 2%/eligibility filter, the
frozen-baseline capture, and the 09:25 import orchestration (mocked Supabase +
a fake provider) — docs/simmer.md → "Overnight movers"."""
from __future__ import annotations

import pytest

from app import simmer_overnight as ov
from app import simmer_relativeqs as rq
from app import supabase_admin


# ── RelativeQs client parse ─────────────────────────────────────────────────
_BOARD = {
    "status": "ok",
    "movers": [
        {"symbol": "nvda", "gap": -0.063, "dir": "down", "crowd": "sector_wide", "sector": "semis"},
        {"symbol": "AMD", "gap": 0.041, "dir": "up", "crowd": "lone"},
        {"symbol": "MSFT", "gap": 0.012, "dir": "up", "crowd": "mixed"},      # < 2%
        {"symbol": "SPX", "gap": -0.05, "dir": "down"},                        # blocked
        {"gap": -0.1},                                                          # no symbol → dropped
    ],
}


def test_normalize_movers_parses_and_uppercases():
    out = rq.normalize_movers(_BOARD)
    syms = [m["symbol"] for m in out]
    assert syms == ["NVDA", "AMD", "MSFT", "SPX"]            # the no-symbol row dropped
    nvda = out[0]
    assert nvda["gap"] == -0.063 and nvda["dir"] == "down" and nvda["crowd"] == "sector_wide"


def test_normalize_movers_tolerates_junk():
    assert rq.normalize_movers(None) == []
    assert rq.normalize_movers({"movers": "nope"}) == []
    assert rq.normalize_movers({"movers": [{"symbol": "X", "gap": "bad"}]}) == []


# ── 2% + eligibility filter (uses the seeded ticker setup) ──────────────────
def test_eligible_movers_drops_small_and_ineligible():
    out = ov.eligible_movers(rq.normalize_movers(_BOARD))
    syms = [m["symbol"] for m in out]
    assert "NVDA" in syms and "AMD" in syms      # ≥2% and tradeable
    assert "MSFT" not in syms                     # moved < 2%
    assert "SPX" not in syms                      # blocked index


def test_eligible_movers_imports_movers_dropping_only_blocked_and_small():
    """Import whatever RelativeQs surfaces (we trust it as the universe) — incl.
    names Simmer has no per-ticker row for (they use the base rule). Only drop
    blocked index products and sub-2% moves. The junk/injection defense lives in
    normalize_movers (see below), not here."""
    movers = [
        {"symbol": "SPX", "gap": -0.05, "dir": "down"},       # blocked index → drop
        {"symbol": "MU", "gap": -0.01, "dir": "down"},        # < 2% → drop
        {"symbol": "ADBE", "gap": -0.09, "dir": "down"},      # not configured → imported (base rule)
        {"symbol": "NVDA", "gap": -0.06, "dir": "down"},      # configured → imported
    ]
    assert [m["symbol"] for m in ov.eligible_movers(movers)] == ["ADBE", "NVDA"]


def test_normalize_movers_enforces_symbol_format_finite_and_cap():
    board = {"movers": [
        {"symbol": "<b>x</b>", "gap": -0.1},     # junk/HTML → dropped by regex
        {"symbol": "TOOLONGSYM", "gap": -0.1},    # >6 chars → dropped
        {"symbol": "nan", "gap": float("inf")},   # non-finite gap → dropped
        {"symbol": "NVDA", "gap": -0.06},         # ok
    ]}
    out = rq.normalize_movers(board)
    assert [m["symbol"] for m in out] == ["NVDA"]
    # cap at _MAX_MOVERS
    big = {"movers": [{"symbol": "AAAA", "gap": -0.05} for _ in range(rq._MAX_MOVERS + 50)]}
    assert len(rq.normalize_movers(big)) == rq._MAX_MOVERS


# ── Fake provider for baseline/live ─────────────────────────────────────────
class _FakeProvider:
    def __init__(self, spot=100.0):
        self._spot = spot

    async def chain(self, symbol, expiration):
        rows = []
        for k in (90.0, 92.0, 94.0, 96.0, 98.0):
            rows.append({"side": "put", "strike": k, "bid": 1.0, "ask": 1.2, "volume": 50})
        for k in (102.0, 104.0, 106.0, 108.0):
            rows.append({"side": "call", "strike": k, "bid": 0.8, "ask": 1.0, "volume": 60})
        return rows

    async def quote(self, symbol):
        return {"last": self._spot}

    async def expirations(self, symbol):
        return ["2026-11-20", "2026-12-18"]


async def test_capture_baseline_freezes_strikes_and_premiums():
    snap = await ov.capture_baseline(_FakeProvider(100.0), "NVDA", "2026-11-20", "down")
    assert snap["side"] == "put"
    assert snap["strikes"] == [98.0, 96.0, 94.0]              # OTM puts below spot
    assert snap["baseline"] == {"98.0": 1.1, "96.0": 1.1, "94.0": 1.1}
    assert snap["spot_baseline"] == 100.0


async def test_research_thesis_templated_and_injected():
    t = await ov.research_thesis("NVDA", -0.06, "down", "sector_wide")
    assert "NVDA" in t and "6.0%" in t and "sector-wide" in t

    async def _summ(**kw):
        return "Guidance cut; whole semi complex down.\nsecond line ignored"
    t2 = await ov.research_thesis("NVDA", -0.06, "down", "sector_wide", summarize=_summ)
    assert t2 == "Guidance cut; whole semi complex down."


# ── 09:25 import orchestration (mocked Supabase) ────────────────────────────
@pytest.fixture
def mock_supabase(monkeypatch):
    state = {"overnight": {}, "watchlist": [], "config": {"relativeqs_api_base": "http://rq.test"}}

    async def get_app_config(key):
        return state["config"].get(key)

    async def users_with_overnight_import():
        return ["user-1", "user-2"]

    async def get_simmer_overnight(sym, exp, session):
        return state["overnight"].get((sym, exp, session))

    async def upsert_simmer_overnight(row):
        # emulate partial upsert/merge on the PK
        key = (row["symbol"], str(row["expiration"])[:10], str(row["session_date"])[:10])
        state["overnight"].setdefault(key, {}).update(row)
        return True

    async def get_watchlist_row(uid, sym):
        for r in state["watchlist"]:
            if r["user_id"] == uid and r["symbol"] == sym:
                return r
        return None

    async def upsert_row(table, row, on_conflict):
        if table == "simmer_watchlist":
            for r in state["watchlist"]:
                if r["user_id"] == row["user_id"] and r["symbol"] == row["symbol"]:
                    r.update(row)      # on-conflict updates ONLY provided columns
                    return True
            state["watchlist"].append(dict(row))
        return True

    for name, fn in [("get_app_config", get_app_config),
                     ("users_with_overnight_import", users_with_overnight_import),
                     ("get_simmer_overnight", get_simmer_overnight),
                     ("upsert_simmer_overnight", upsert_simmer_overnight),
                     ("get_watchlist_row", get_watchlist_row),
                     ("upsert_row", upsert_row)]:
        monkeypatch.setattr(supabase_admin, name, fn)
    monkeypatch.setattr(ov.supabase_admin, "upsert_row", upsert_row)
    return state


async def test_run_import_filters_imports_and_freezes_baseline(mock_supabase, monkeypatch):
    async def _fetch(base, token=None, timeout=10.0):
        assert base == "http://rq.test"
        return rq.normalize_movers(_BOARD)
    monkeypatch.setattr(ov.simmer_relativeqs, "fetch_movers", _fetch)

    res = await ov.run_import(_FakeProvider(100.0))
    # NVDA + AMD imported (MSFT<2%, SPX blocked), to 2 users each
    assert res["movers"] == 2 and res["users"] == 2 and res["imported"] == 4

    wl = mock_supabase["watchlist"]
    assert len(wl) == 4
    nvda_rows = [r for r in wl if r["symbol"] == "NVDA"]
    assert all(r["source"] == "rq" for r in nvda_rows)
    assert all(r["overrides"]["overnight"]["dir"] == "down" for r in nvda_rows)

    # overnight analysis row exists with a FROZEN baseline + thesis
    key = ("NVDA", "2026-11-20", ov.session_date())
    row = mock_supabase["overnight"][key]
    assert row["baseline"] and row["side"] == "put" and row["thesis"]
    assert row["verdict"]["call"] == "wait"            # baseline only, pre-open


async def test_run_import_is_nondestructive_on_an_existing_user_row(mock_supabase, monkeypatch):
    # a user already watches NVDA with their own pinned expiry + override — the
    # import must ONLY attach the overnight overlay, never touch their choices.
    mock_supabase["watchlist"].append({
        "user_id": "user-1", "symbol": "NVDA", "expiration": "2026-10-16",
        "active": True, "source": "user", "overrides": {"note": "keep me"}})

    async def _fetch(base, token=None, timeout=10.0):
        return [{"symbol": "NVDA", "gap": -0.06, "dir": "down", "crowd": "sector_wide"}]
    monkeypatch.setattr(ov.simmer_relativeqs, "fetch_movers", _fetch)

    await ov.run_import(_FakeProvider(100.0))
    u1 = next(r for r in mock_supabase["watchlist"] if r["user_id"] == "user-1" and r["symbol"] == "NVDA")
    assert u1["source"] == "user"                    # NOT flipped to rq
    assert u1["active"] is True                       # not re-activated/altered
    assert u1["expiration"] == "2026-10-16"           # user's pinned expiry preserved
    assert u1["overrides"]["note"] == "keep me"        # prior override preserved
    assert u1["overrides"]["overnight"]["gap"] == -0.06   # overlay merged in
    # user-2 did NOT already watch NVDA → inserted fresh as an RQ pick
    u2 = next(r for r in mock_supabase["watchlist"] if r["user_id"] == "user-2" and r["symbol"] == "NVDA")
    assert u2["source"] == "rq" and u2["expiration"] == "2026-11-20"


async def test_refresh_analysis_updates_verdict_from_live(mock_supabase, monkeypatch):
    session = ov.session_date()
    mock_supabase["overnight"][("NVDA", "2026-11-20", session)] = {
        "symbol": "NVDA", "expiration": "2026-11-20", "session_date": session,
        "dir": "down", "gap": -0.06, "crowd": "sector_wide", "side": "put",
        "strikes": [98.0, 96.0], "baseline": {"98.0": 1.0, "96.0": 1.0},
        "spot_baseline": 100.0, "latest": {}, "verdict": {"call": "wait"},
    }

    async def list_simmer_overnight(session_date):
        return list(mock_supabase["overnight"].values())
    monkeypatch.setattr(ov.supabase_admin, "list_simmer_overnight", list_simmer_overnight)

    # live chain where put premiums HOLD and spot sits on the level → sell_bear_call
    class _Hold(_FakeProvider):
        async def chain(self, symbol, expiration):
            return [{"side": "put", "strike": 98.0, "bid": 1.0, "ask": 1.1, "volume": 80},
                    {"side": "put", "strike": 96.0, "bid": 1.0, "ask": 1.1, "volume": 80}]
        async def quote(self, symbol):
            return {"last": 99.9}

    n = await ov.refresh_analysis(_Hold(), sentiment_by_symbol={"NVDA": {"score": -0.3}})
    assert n == 1
    v = mock_supabase["overnight"][("NVDA", "2026-11-20", session)]["verdict"]
    assert v["call"] == "sell_bear_call" and v["premium_trend"] == "holding"


# ── /simmer/overnight route ─────────────────────────────────────────────────
def _route_client():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app import config
    from app.config import Settings
    from app.routes import simmer as sroute
    config._cached = Settings(auth_enabled=False)
    app = FastAPI()
    app.include_router(sroute.router)
    return TestClient(app, raise_server_exceptions=False), sroute


def test_overnight_route_active(monkeypatch):
    client, sroute = _route_client()
    monkeypatch.setattr(sroute, "_readiness_for",
                        lambda sym, exp: {"symbol": sym, "expiration": "2026-11-20"})

    async def _row(sym, exp, session):
        return {"symbol": sym, "expiration": exp, "dir": "down", "gap": -0.06,
                "crowd": "sector_wide", "thesis": "guidance cut", "side": "put",
                "strikes": [98.0], "baseline": {"98.0": 1.0}, "latest": {"98.0": 1.05},
                "verdict": {"call": "sell_bear_call", "confidence": 70},
                "updated_at": "2026-10-12T14:00:00Z"}
    monkeypatch.setattr(sroute.supabase_admin, "get_simmer_overnight", _row)

    r = client.get("/simmer/overnight/NVDA")
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["active"] is True and data["verdict"]["call"] == "sell_bear_call"
    assert data["thesis"] == "guidance cut"


def test_overnight_route_inactive_when_no_row(monkeypatch):
    client, sroute = _route_client()
    monkeypatch.setattr(sroute, "_readiness_for",
                        lambda sym, exp: {"symbol": sym, "expiration": "2026-11-20"})

    async def _none(sym, exp, session):
        return None
    monkeypatch.setattr(sroute.supabase_admin, "get_simmer_overnight", _none)
    r = client.get("/simmer/overnight/AMD")
    assert r.status_code == 200 and r.json()["data"]["active"] is False


def test_overnight_route_rejects_bad_symbol():
    client, _ = _route_client()
    assert client.get("/simmer/overnight/nope$bad").status_code in (404, 422)


async def test_run_import_skips_name_without_a_baseline(mock_supabase, monkeypatch):
    """Optional tightening: a mover we can't get a baseline for (e.g. no usable
    chain) is NOT imported into any watchlist."""
    class _NoChain(_FakeProvider):
        async def chain(self, symbol, expiration):
            return []                      # no strikes → capture_baseline returns {}

    async def _fetch(base, token=None, timeout=10.0):
        return [{"symbol": "NVDA", "gap": -0.06, "dir": "down", "crowd": "sector_wide"}]
    monkeypatch.setattr(ov.simmer_relativeqs, "fetch_movers", _fetch)

    res = await ov.run_import(_NoChain(100.0))
    assert res["imported"] == 0
    assert mock_supabase["watchlist"] == []
    assert mock_supabase["overnight"] == {}

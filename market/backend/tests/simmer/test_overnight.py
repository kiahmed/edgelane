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
    assert snap["baseline"] == {"98": 1.1, "96": 1.1, "94": 1.1}
    assert snap["spot_baseline"] == 100.0


async def test_next_expiry_picks_nearest_not_dte_gated():
    """The RQ pin is the NEXT AVAILABLE expiry (nearest future Friday), not the
    engine's DTE-gated pick — so a name isn't pushed out to ~30 DTE past this
    week's weekly (the user pins the near one by hand)."""
    class _P:
        async def expirations(self, sym):
            return ["2099-03-20", "2099-01-16", "2099-02-20"]   # unsorted, all future
    assert await ov._next_expiry(_P(), "ARM") == "2099-01-16"   # nearest, not farthest


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
    state = {"overnight": {}, "watchlist": [], "config": {"relativeqs_api_base": "http://rq.test"},
             "import_users": ["user-1", "user-2"], "analyze_users": []}

    async def get_app_config(key):
        return state["config"].get(key)

    async def users_with_overnight_import():
        return list(state["import_users"])

    async def users_with_overnight_analyze_all():
        return list(state["analyze_users"])

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
                     ("users_with_overnight_analyze_all", users_with_overnight_analyze_all),
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
    assert res["movers"] == 2 and res["import_users"] == 2 and res["imported"] == 4
    assert res["rows"] == 2 and res["analyzed"] == 0     # no analyze-only users here

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


async def test_run_import_pins_expiry_on_a_pending_row_without_one(mock_supabase, monkeypatch):
    """A row already on the list but with NO expiration would sit 'pending' forever;
    the import auto-assigns the overnight next-expiry (without clobbering a real pin)."""
    mock_supabase["watchlist"].append({
        "user_id": "user-1", "symbol": "NVDA", "expiration": None,
        "active": True, "source": "user", "overrides": {}})

    async def _fetch(base, token=None, timeout=10.0):
        return [{"symbol": "NVDA", "gap": -0.06, "dir": "down", "crowd": "sector_wide"}]
    monkeypatch.setattr(ov.simmer_relativeqs, "fetch_movers", _fetch)

    await ov.run_import(_FakeProvider(100.0))
    u1 = next(r for r in mock_supabase["watchlist"] if r["user_id"] == "user-1" and r["symbol"] == "NVDA")
    assert u1["expiration"] == "2026-11-20"      # auto-pinned (was None)
    assert u1["source"] == "user"                # still not flipped to rq
    assert u1["overrides"]["overnight"]["gap"] == -0.06


async def test_run_import_second_pass_does_not_reimport(mock_supabase, monkeypatch):
    """A retry pass re-captures nothing new → writes NO watchlist rows, so a name a
    user deleted at 10:00 is never re-inserted (and overlays aren't re-applied)."""
    async def _fetch(base, token=None, **kw):
        return rq.normalize_movers(_BOARD)
    monkeypatch.setattr(ov.simmer_relativeqs, "fetch_movers", _fetch)

    r1 = await ov.run_import(_FakeProvider(100.0))
    assert r1["rows"] == 2 and r1["imported"] == 4           # first pass: NVDA+AMD × 2 users
    wl_count = len(mock_supabase["watchlist"])

    r2 = await ov.run_import(_FakeProvider(100.0))
    assert r2["rows"] == 2                                    # baselines still present
    assert r2["imported"] == 0 and r2["analyzed"] == 0       # nothing newly created → no writes
    assert len(mock_supabase["watchlist"]) == wl_count       # no watchlist rows re-inserted


async def test_toggle2_analyzes_only_intersection_never_imports(mock_supabase, monkeypatch):
    """Toggle 2 (analyze): for a user who did NOT opt into import, attach the
    overnight overlay ONLY to a mover they ALREADY watch — never add a new name,
    and always create the user-agnostic analysis row."""
    mock_supabase["import_users"] = []            # nobody imports
    mock_supabase["analyze_users"] = ["user-9"]   # one analyze-only user
    # user-9 already watches NVDA (their own pick) but NOT AMD
    mock_supabase["watchlist"].append({
        "user_id": "user-9", "symbol": "NVDA", "expiration": "2026-10-16",
        "active": True, "source": "user", "overrides": {}})

    async def _fetch(base, token=None, timeout=10.0):
        return rq.normalize_movers(_BOARD)        # NVDA + AMD eligible
    monkeypatch.setattr(ov.simmer_relativeqs, "fetch_movers", _fetch)

    res = await ov.run_import(_FakeProvider(100.0))
    assert res["imported"] == 0 and res["analyzed"] == 1 and res["rows"] == 2

    wl = mock_supabase["watchlist"]
    # NVDA (watched) got the overlay, source preserved; AMD (not watched) NOT added
    nvda = next(r for r in wl if r["user_id"] == "user-9" and r["symbol"] == "NVDA")
    assert nvda["source"] == "user" and nvda["expiration"] == "2026-10-16"
    assert nvda["overrides"]["overnight"]["dir"] == "down"
    assert not any(r["user_id"] == "user-9" and r["symbol"] == "AMD" for r in wl)
    # the shared analysis row exists for BOTH movers regardless of users
    assert ("NVDA", "2026-11-20", ov.session_date()) in mock_supabase["overnight"]
    assert ("AMD", "2026-11-20", ov.session_date()) in mock_supabase["overnight"]


async def test_refresh_analysis_updates_verdict_from_live(mock_supabase, monkeypatch):
    session = ov.session_date()
    mock_supabase["overnight"][("NVDA", "2026-11-20", session)] = {
        "symbol": "NVDA", "expiration": "2026-11-20", "session_date": session,
        "dir": "down", "gap": -0.06, "crowd": "sector_wide", "side": "put",
        "strikes": [98.0, 96.0], "baseline": {"98": 1.0, "96": 1.0},
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


# ── toggle-2 opt-in resolver (default ON, fail-closed) ──────────────────────
async def test_analyze_all_default_on_excludes_optouts_and_fails_closed(monkeypatch):
    """Default ON = watchlist universe minus explicit opt-outs; a failed opt-out
    read must fail CLOSED (None → caller skips toggle 2), never write the overlay
    to a user who may have turned it off."""
    async def sm_normal(table, **kw):
        if table == "simmer_watchlist":
            return [{"user_id": "a"}, {"user_id": "b"}, {"user_id": "a"}]  # a,b (deduped)
        if table == "simmer_settings":
            return [{"user_id": "b"}]                                       # b opted out
        return []
    monkeypatch.setattr(supabase_admin, "select_many", sm_normal)
    assert await supabase_admin.users_with_overnight_analyze_all() == ["a"]

    async def sm_optout_err(table, **kw):
        return [{"user_id": "a"}] if table == "simmer_watchlist" else None  # opt-out read fails
    monkeypatch.setattr(supabase_admin, "select_many", sm_optout_err)
    assert await supabase_admin.users_with_overnight_analyze_all() is None

    async def sm_wl_err(table, **kw):
        return None                                                         # watchlist read fails
    monkeypatch.setattr(supabase_admin, "select_many", sm_wl_err)
    assert await supabase_admin.users_with_overnight_analyze_all() is None


# ── RelativeQs fetch: synchronous retry until a COMPLETE board ───────────────
class _Resp:
    def __init__(self, status, body: bytes):
        self.status_code = status; self.content = body; self.headers = {}
    def json(self):
        import json as _j; return _j.loads(self.content)


def _fake_client(script):
    calls = {"n": 0}

    class _Client:
        def __init__(self, timeout=None): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, url, headers=None):
            r = script[min(calls["n"], len(script) - 1)]; calls["n"] += 1
            if isinstance(r, Exception):
                raise r
            return r
    return _Client, calls


async def test_fetch_movers_retries_until_complete(monkeypatch):
    from app import simmer_relativeqs as rq
    # attempt 1: network error; 2: 200 but INCOMPLETE (no movers list); 3: complete
    script = [RuntimeError("drop"),
              _Resp(200, b'{"status":"ok"}'),
              _Resp(200, b'{"movers":[{"symbol":"MU","gap":-0.05,"dir":"down"}]}')]
    Client, calls = _fake_client(script)
    monkeypatch.setattr(rq.httpx, "AsyncClient", Client)
    async def _nosleep(_): return None
    monkeypatch.setattr(rq.asyncio, "sleep", _nosleep)
    out = await rq.fetch_movers("http://rq.test", "tok")
    assert calls["n"] == 3 and [m["symbol"] for m in out] == ["MU"]


async def test_fetch_movers_does_not_retry_on_401(monkeypatch):
    from app import simmer_relativeqs as rq
    Client, calls = _fake_client([_Resp(401, b'{"detail":"no"}')])
    monkeypatch.setattr(rq.httpx, "AsyncClient", Client)
    assert await rq.fetch_movers("http://rq.test", "tok") == [] and calls["n"] == 1


# ── sweep carve-out + EOD RQ reset ──────────────────────────────────────────
async def test_rq_symbols_active_reads_overlay(monkeypatch):
    async def _sm(table, select="*", filters=None, **kw):
        return [{"symbol": "mu", "overrides": {"overnight": {"gap": -0.05}}},
                {"symbol": "AAPL", "overrides": {"note": "x"}},   # no overnight → not RQ
                {"symbol": "stx", "overrides": {"overnight": {"gap": -0.03}}}]
    monkeypatch.setattr(supabase_admin, "select_many", _sm)
    assert await supabase_admin.rq_symbols_active() == {"MU", "STX"}


async def test_reset_rq_end_of_day_deletes_rq_strips_overlay(monkeypatch):
    rows = [
        {"id": 1, "user_id": "u1", "symbol": "MU", "source": "rq",
         "overrides": {"overnight": {"gap": -0.05}}},                     # purely-RQ → delete
        {"id": 2, "user_id": "u2", "symbol": "NVDA", "source": "user",
         "overrides": {"note": "keep", "overnight": {"gap": 0.04}}},       # user row → strip overlay
        {"id": 3, "user_id": "u3", "symbol": "AMD", "source": "user", "overrides": {}},  # untouched
    ]
    deleted_filters = []
    stripped = []

    async def _sm(table, select="*", filters=None, **kw):
        return rows

    async def _del(table, filters):
        deleted_filters.append(filters); return True

    async def _up(table, row, on_conflict):
        stripped.append(row); return True
    monkeypatch.setattr(supabase_admin, "select_many", _sm)
    monkeypatch.setattr(ov.supabase_admin, "delete_rows", _del)
    monkeypatch.setattr(ov.supabase_admin, "upsert_row", _up)

    res = await ov.reset_rq_end_of_day()
    assert res == {"deleted": 1, "stripped": 1}
    assert deleted_filters == [{"source": "eq.rq"}]          # rq rows deleted in one shot
    assert stripped[0]["symbol"] == "NVDA"                    # user row stripped
    assert "overnight" not in stripped[0]["overrides"] and stripped[0]["overrides"]["note"] == "keep"


# ── overnight ≥70 alert fan-out (fire / re-trigger / opt-out) ───────────────
async def test_overnight_alert_fanout_fires_retriggers_and_opts_out(monkeypatch):
    from app import simmer_watcher as sw
    final = {"call": "sell_bear_call", "confidence": 88}
    analyses = [{"symbol": "MU", "dir": "down", "gap": -0.05,
                 "llm": {"call": "sell_bear_call", "why_moved": "CXMT DRAM competition",
                         "provider": "openai", "model": "gpt-6-luna",
                         "tokens": {"in": 30000}, "queries": ["mu stock"]},
                 "combiner": {"final": final, "decided_at": "2026-10-13T10:00:00-04:00"}}]
    open_alert: dict = {}            # the user's last open overnight alert for MU
    settings_mode = {"v": "on"}      # on → no row (default ON) | off → opted out | error → None
    inserted = []

    async def _list_an(session):
        return analyses

    async def _sm(table, select="*", filters=None, **kw):
        if table == "simmer_watchlist":
            return [{"user_id": "u1"}]
        if table == "simmer_settings":
            m = settings_mode["v"]
            if m == "error":
                return None                       # read ERROR (distinct from "no row")
            if m == "off":
                return [{"user_id": "u1", "notify_overnight": False}]
            return []                             # no row → default ON
        if table == "simmer_alerts":
            return [open_alert] if open_alert else []
        return []

    async def _ins(table, row):
        inserted.append(row)
        open_alert.clear(); open_alert.update(score=row["score"], structure=row["structure"])
        return True
    monkeypatch.setattr(sw.supabase_admin, "list_simmer_overnight_analysis", _list_an)
    monkeypatch.setattr(sw.supabase_admin, "select_many", _sm)
    monkeypatch.setattr(sw.supabase_admin, "insert_row", _ins)

    assert await sw.overnight_alert_fanout("2026-10-13") == 1      # first fire (88)
    # payload is SUMMARY only — no provider/model/tokens/queries leak into the row
    pay = inserted[0]["payload"]
    assert pay["why_moved"] == "CXMT DRAM competition" and pay["call"] == "sell_bear_call"
    assert "provider" not in pay and "model" not in pay and "tokens" not in pay and "queries" not in pay
    assert pay["combiner"]["decided_at"] == "2026-10-13T10:00:00-04:00"
    assert await sw.overnight_alert_fanout("2026-10-13") == 0      # same play/conf → no repeat
    final["confidence"] = 95
    assert await sw.overnight_alert_fanout("2026-10-13") == 1      # conviction rose → re-fire
    final["call"] = "sell_bull_put"; final["confidence"] = 72
    assert await sw.overnight_alert_fanout("2026-10-13") == 1      # play flipped → re-fire
    final["call"] = "wait"; final["confidence"] = 99
    assert await sw.overnight_alert_fanout("2026-10-13") == 0      # wait never alerts
    final["call"] = "sell_bear_call"; final["confidence"] = 60
    open_alert.clear()
    assert await sw.overnight_alert_fanout("2026-10-13") == 0      # below 70 → no alert
    final["confidence"] = 99; settings_mode["v"] = "off"
    assert await sw.overnight_alert_fanout("2026-10-13") == 0      # user opted out

    # a settings READ ERROR must FAIL CLOSED (skip the fan-out, no alert) — not be
    # mistaken for "no row → default ON".
    settings_mode["v"] = "error"; open_alert.clear()
    assert await sw.overnight_alert_fanout("2026-10-13") == 0


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

    async def _row(sym, session):
        return {"symbol": sym, "expiration": "2026-11-13", "dir": "down", "gap": -0.06,
                "crowd": "sector_wide", "thesis": "guidance cut", "side": "put",
                "strikes": [98.0], "baseline": {"98.0": 1.0}, "latest": {"98.0": 1.05},
                "verdict": {"call": "sell_bear_call", "confidence": 70},
                "updated_at": "2026-10-12T14:00:00Z"}
    monkeypatch.setattr(sroute.supabase_admin, "get_simmer_overnight_by_symbol", _row)

    async def _an(sym, session):      # the catalyst analysis row (Phase 2 surfacing)
        return {"symbol": sym, "outcome": None,
                "llm": {"provider": "openai", "call": "sell_bear_call", "sustain": "extend",
                        "deserved": "overreaction", "web_social_sentiment": "doubt",
                        "why_moved": "CXMT DRAM competition fears", "confidence": 74},
                "combiner": {"llm_only": {"call": "sell_bear_call"},
                             "combiner_blended": {"call": "wait"}, "decided_at": None}}
    monkeypatch.setattr(sroute.supabase_admin, "get_simmer_overnight_analysis", _an)

    r = client.get("/simmer/overnight/NVDA")
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["active"] is True and data["verdict"]["call"] == "sell_bear_call"
    assert data["thesis"] == "guidance cut"
    # catalyst read surfaced for the UI (both calls + sustain/why)
    cat = data["catalyst"]
    assert cat["sustain"] == "extend" and cat["why_moved"] == "CXMT DRAM competition fears"
    assert cat["llm_only_call"] == "sell_bear_call" and cat["combiner_call"] == "wait"
    assert cat["deserved"] == "overreaction" and cat["sentiment"] == "doubt"


def test_overnight_route_active_regardless_of_readiness_expiry(monkeypatch):
    """The tab follows the name, not the card's expiry: a row captured on
    2026-11-13 must still surface even when the sweep settled the readiness card
    on a different (near-dated) expiration — the panel-visibility bug."""
    client, sroute = _route_client()
    # readiness card sits on a near-dated 0DTE expiry, unlike the import's row
    monkeypatch.setattr(sroute, "_readiness_for",
                        lambda sym, exp: {"symbol": sym, "expiration": "2026-10-12"})

    async def _row(sym, session):
        return {"symbol": sym, "expiration": "2026-11-13", "dir": "up", "gap": 0.05,
                "verdict": {"call": "sell_bull_put", "confidence": 66},
                "updated_at": "2026-10-12T14:00:00Z"}
    monkeypatch.setattr(sroute.supabase_admin, "get_simmer_overnight_by_symbol", _row)

    async def _an(sym, session):
        return None                   # analyzer hasn't run → tab shows premium only
    monkeypatch.setattr(sroute.supabase_admin, "get_simmer_overnight_analysis", _an)

    data = client.get("/simmer/overnight/MU").json()["data"]
    assert data["active"] is True and data["verdict"]["call"] == "sell_bull_put"
    assert "catalyst" not in data


def test_overnight_route_inactive_when_no_row(monkeypatch):
    client, sroute = _route_client()

    async def _none(sym, session):
        return None
    monkeypatch.setattr(sroute.supabase_admin, "get_simmer_overnight_by_symbol", _none)
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

"""Migration 0035 (Level Engine storage) against a real Postgres.

SKIPPED unless LVL_TEST_PG_DSN is set, so the normal suite stays hermetic.
Point it at a THROWAWAY database as a superuser (e.g. the `postgres` user of a
`docker run postgres:16` container) — never at Supabase:

    LVL_TEST_PG_DSN=postgresql://postgres:x@127.0.0.1:55432/postgres \
        .venv/bin/python -m pytest tests/level_engine/test_migration_0035.py -q

The test creates stub `anon` / `authenticated` roles if missing (Supabase has
them), applies the migration twice (idempotency), then checks role isolation
with `SET ROLE` — privileges and RLS are evaluated for the role, so no role
passwords are needed. Rows it writes are tagged and removed afterwards.
"""
from __future__ import annotations

import os
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

DSN = os.environ.get("LVL_TEST_PG_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="LVL_TEST_PG_DSN not set (needs a throwaway Postgres)")

MIGRATION = (Path(__file__).resolve().parents[4]
             / "supabase" / "migrations" / "0035_level_engine.sql")
PUBLIC_TABLES = ("lvl_levels", "lvl_level_audit", "lvl_ticks", "lvl_level_state", "lvl_evidence",
                 "lvl_beliefs", "lvl_picks", "lvl_legs", "lvl_day_summary")
REPLAY_TABLES = ("runs", "level_state", "evidence", "beliefs", "picks", "legs", "day_summary")
TAG = f"pytest-{uuid.uuid4().hex[:8]}"


def _today_et() -> date:
    return datetime.now(ZoneInfo("America/New_York")).date()


def _connect():
    import psycopg
    from psycopg.rows import dict_row
    return psycopg.connect(DSN, autocommit=True, row_factory=dict_row)


@pytest.fixture(scope="module")
def admin():
    psycopg = pytest.importorskip("psycopg")
    conn = _connect()
    conn.execute("""
        do $$ begin
          if not exists (select 1 from pg_roles where rolname = 'anon') then
            create role anon nologin; end if;
          if not exists (select 1 from pg_roles where rolname = 'authenticated') then
            create role authenticated nologin; end if;
        end $$;""")
    sql = MIGRATION.read_text()
    for _ in range(2):                      # idempotent: the second apply must be a no-op
        with conn.transaction():
            conn.execute(sql)
    conn.execute("create table if not exists public.not_lvl_secret (x int)")
    yield conn
    conn.execute("reset role")
    today = _today_et()
    conn.execute("delete from public.lvl_levels where trade_date = %s and name like %s",
                 (today, f"{TAG}%"))
    conn.execute("delete from public.lvl_level_audit where trade_date = %s", (today,))
    conn.execute("delete from public.lvl_ticks where bias->>'tag' = %s", (TAG,))
    conn.execute("delete from public.lvl_evidence where reason = %s", (TAG,))
    conn.execute("delete from lvl_replay.evidence where reason = %s", (TAG,))
    conn.execute("delete from public.lvl_day_summary where notes = %s", (TAG,))
    conn.execute("delete from lvl_replay.day_summary where notes = %s", (TAG,))
    conn.execute("delete from public.lvl_legs where id like %s", (f"{TAG}%",))
    conn.execute("delete from lvl_replay.legs where id like %s", (f"{TAG}%",))
    conn.close()
    del psycopg


def _as(conn, role: str | None):
    conn.execute("reset role" if role is None else f"set role {role}")


def test_tables_exist(admin):
    pub = {r["tablename"] for r in admin.execute(
        "select tablename from pg_tables where schemaname = 'public'").fetchall()}
    rep = {r["tablename"] for r in admin.execute(
        "select tablename from pg_tables where schemaname = 'lvl_replay'").fetchall()}
    assert set(PUBLIC_TABLES) <= pub
    assert set(REPLAY_TABLES) <= rep
    rls_off = admin.execute(
        "select n.nspname, c.relname from pg_class c join pg_namespace n on n.oid = c.relnamespace "
        "where c.relkind = 'r' and not c.relrowsecurity and "
        "((n.nspname = 'public' and c.relname like 'lvl\\_%%') or n.nspname = 'lvl_replay')").fetchall()
    assert rls_off == []


def test_roles_are_least_privilege(admin):
    for role in ("facades_levels_engine_rw", "facades_levels_engine_replay"):
        r = admin.execute("select rolcanlogin, rolsuper, rolcreatedb, rolcreaterole, rolbypassrls "
                          "from pg_roles where rolname = %s", (role,)).fetchone()
        assert r == {"rolcanlogin": True, "rolsuper": False, "rolcreatedb": False,
                     "rolcreaterole": False, "rolbypassrls": False}
    for role in ("anon", "authenticated"):
        for t in PUBLIC_TABLES:
            assert not admin.execute(
                "select has_table_privilege(%s, %s, 'select,insert,update,delete') ok",
                (role, f"public.{t}")).fetchone()["ok"], (role, t)
        assert not admin.execute("select has_schema_privilege(%s, 'lvl_replay', 'usage') ok",
                                 (role,)).fetchone()["ok"]


def test_trigger_refuses_past_date(admin):
    import psycopg
    past = _today_et() - timedelta(days=3)
    with pytest.raises(psycopg.errors.CheckViolation, match="read-only"):
        admin.execute("insert into public.lvl_levels (trade_date, symbol, price, name) "
                      "values (%s, 'SPY', 600, %s)", (past, f"{TAG}-past"))


def test_trigger_allows_today_and_touches_updated_at(admin):
    r = admin.execute("insert into public.lvl_levels (trade_date, symbol, price, name) "
                      "values (%s, 'SPY', 600, %s) returning id, updated_at",
                      (_today_et(), f"{TAG}-today")).fetchone()
    r2 = admin.execute("update public.lvl_levels set price = 601 where id = %s returning updated_at",
                       (r["id"],)).fetchone()
    assert r2["updated_at"] >= r["updated_at"]


def test_rw_role_writes_lvl_but_nothing_else(admin):
    import psycopg
    _as(admin, "facades_levels_engine_rw")
    try:
        admin.execute("insert into public.lvl_ticks (trade_date, ts, spx, spy, ratio, bias) "
                      "values (%s, %s, 6000, 600, 10, %s)",
                      (_today_et(), datetime.now(timezone.utc), f'{{"tag": "{TAG}"}}'))
        assert admin.execute("select count(*) n from public.lvl_ticks where bias->>'tag' = %s",
                             (TAG,)).fetchone()["n"] == 1
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            admin.execute("select * from public.not_lvl_secret")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            admin.execute("select * from lvl_replay.evidence")
    finally:
        _as(admin, None)


def test_replay_role_reads_live_writes_only_replay(admin):
    import psycopg
    _as(admin, "facades_levels_engine_replay")
    try:
        admin.execute("select * from public.lvl_ticks limit 1")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            admin.execute("insert into public.lvl_ticks (trade_date, ts) values (%s, now())",
                          (_today_et(),))
        admin.execute("insert into lvl_replay.evidence (run_id, trade_date, ts, agent, scenario, "
                      "weight, reason) values (%s, %s, now(), 'up', 'up', 0.5, %s)",
                      (str(uuid.uuid4()), _today_et(), TAG))
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            admin.execute("select * from public.not_lvl_secret")
    finally:
        _as(admin, None)


def test_pgstore_contract_live_and_replay(admin):
    """PgStore (level_engine/store.py) round-trips against the real schema."""
    from level_engine.store import PastDateError, PgStore
    today = _today_et()

    live = PgStore(DSN)
    live._conn.execute("set role facades_levels_engine_rw")
    try:
        lv = live.add_level(today, {"symbol": "SPY", "price": 600.5, "name": f"{TAG}-a",
                                    "kind": "support"}, today)
        assert any(r["id"] == lv["id"] for r in live.list_levels(today))
        conv = live.replace_converted(today, [{"price": 6005, "name": f"{TAG}-conv", "ratio": 10,
                                               "converted_from": str(lv["id"])}], today)
        assert conv[0]["source"] == "converted"
        with pytest.raises(PastDateError):
            live.add_level(today - timedelta(days=1), {"symbol": "SPY", "price": 1,
                                                       "name": f"{TAG}-x"}, today)
        live.insert_tick({"trade_date": today, "ts": datetime.now(timezone.utc), "spx": 6000,
                          "spy": 600, "ratio": 10.01, "ratio_locked": True,
                          "chain": [{"strike": 6000, "side": "call", "bid": 1.1}],
                          "bias": {"tag": TAG}})
        assert live.latest_locked_ratio(today) is not None
        live.insert("evidence", {"trade_date": today, "ts": datetime.now(timezone.utc),
                                 "agent": "up", "scenario": "up", "weight": 0.3,
                                 "detail": {"k": 1}, "reason": TAG})
        live.upsert_day_summary({"trade_date": today, "close": 6000, "legs": {"win": 1},
                                 "notes": TAG})
        live.upsert_day_summary({"trade_date": today, "close": 6001, "legs": {"win": 2},
                                 "notes": TAG})
        assert float(live.get_day_summary(today)["close"]) == 6001
    finally:
        live.close()

    run_id = str(uuid.uuid4())
    rep = PgStore(DSN, replay_run_id=run_id)
    rep._conn.execute("set role facades_levels_engine_replay")
    try:
        rep.insert("evidence", {"trade_date": today, "ts": datetime.now(timezone.utc),
                                "agent": "down", "scenario": "down", "weight": -0.2,
                                "reason": TAG})
        assert len(rep.list_rows("evidence", today)) == 1
        assert rep.list_ticks(today)
        with pytest.raises(PermissionError):
            rep.insert_tick({"trade_date": today, "ts": datetime.now(timezone.utc)})
        rep.upsert_day_summary({"trade_date": today, "close": 1, "notes": TAG})
        assert rep.get_day_summary(today)["run_id"] is not None
    finally:
        rep.close()


def test_leg_columns_and_close_reason_check(admin):
    import psycopg
    cols = {r["column_name"] for r in admin.execute(
        "select column_name from information_schema.columns where table_schema = 'public' "
        "and table_name = 'lvl_legs'").fetchall()}
    rcols = {r["column_name"] for r in admin.execute(
        "select column_name from information_schema.columns where table_schema = 'lvl_replay' "
        "and table_name = 'legs'").fetchall()}
    new = {"close_reason", "exit_spot", "target_level", "invalidation_level"}
    assert new <= cols and new <= rcols
    with pytest.raises(psycopg.errors.CheckViolation):
        admin.execute("insert into public.lvl_legs (id, trade_date, close_reason) "
                      "values (%s, %s, 'nope')", (f"{TAG}:bad", _today_et()))
    with pytest.raises(psycopg.errors.CheckViolation):
        admin.execute("insert into public.lvl_legs (id, trade_date, result) "
                      "values (%s, %s, 'maybe')", (f"{TAG}:bad2", _today_et()))


def _leg_rows(today, base):
    t = lambda h: base + timedelta(hours=h)   # noqa: E731
    lv = {"price": 7040.0, "name": "Y"}
    return [
        {"id": f"{TAG}:1", "trade_date": today, "opened_at": t(0), "call": "up",
         "entry_spot": 7010.0, "target_level": lv, "invalidation_level": {"price": 7020.0,
                                                                         "name": "U"},
         "pick": {"pick_id": "p1", "strategy": "bull_call"}},
        {"id": f"{TAG}:2", "trade_date": today, "opened_at": t(1), "call": "range",
         "entry_spot": 7012.0, "target_level": lv, "invalidation_level": None, "pick": None},
        {"id": f"{TAG}:10", "trade_date": today, "opened_at": t(3), "call": "down",
         "entry_spot": 7015.0, "target_level": lv, "invalidation_level": None, "pick": None},
    ]


@pytest.mark.parametrize("replay", [False, True])
def test_pgstore_recent_and_open_legs(admin, replay):
    """list_recent_legs / get_open_leg round-trip (live table and lvl_replay.legs)."""
    from level_engine.store import PgStore
    today = _today_et()
    base = datetime.now(timezone.utc) + timedelta(days=3000)   # newest rows in any test DB
    run_id = str(uuid.uuid4()) if replay else None
    st = PgStore(DSN, replay_run_id=run_id)
    st._conn.execute("set role " + ("facades_levels_engine_replay" if replay
                                    else "facades_levels_engine_rw"))
    try:
        for r in _leg_rows(today, base):
            st.insert("legs", r)
        st.update_leg(f"{TAG}:1", {"closed_at": base + timedelta(hours=2), "result": "win",
                                    "close_reason": "target", "exit_spot": 7038.5})
        st.update_leg(f"{TAG}:2", {"closed_at": base + timedelta(hours=2), "result": "loss",
                                    "close_reason": "zone_exit", "exit_spot": 7030.0})
        recent = [r for r in st.list_recent_legs(200) if str(r["id"]).startswith(TAG)]
        assert [r["id"] for r in recent] == [f"{TAG}:2", f"{TAG}:1"]   # tie → opened_at desc
        w = recent[1]
        assert w["close_reason"] == "target" and w["exit_spot"] == 7038.5
        assert w["target_level"] == {"price": 7040.0, "name": "Y"}
        assert w["pick"]["pick_id"] == "p1" and isinstance(w["id"], str)
        assert len(st.list_recent_legs(1)) == 1
        ol = st.get_open_leg(today)
        assert ol is not None and ol["id"] == f"{TAG}:10" and ol["result"] is None
        assert st.get_open_leg(today - timedelta(days=4000)) is None
        if replay:
            other = PgStore(DSN, replay_run_id=str(uuid.uuid4()))
            other._conn.execute("set role facades_levels_engine_replay")
            try:
                assert not [r for r in other.list_recent_legs(200)
                            if str(r["id"]).startswith(TAG)]
            finally:
                other.close()
    finally:
        st.close()

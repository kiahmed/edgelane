"""Level Engine persistence — the ONE storage contract every component codes against.

Spec: docs/level_engine.md §5 (tables), §5.1 (roles), §14 (replay).

Two implementations:
  * MemoryStore — hermetic: tests, and replay dry-runs with no DB.
  * PgStore     — Postgres via psycopg 3 on a least-privilege DSN
                  (`facades_levels_engine_rw` live, `facades_levels_engine_replay` for replay).
                  Never the Supabase service_role key.

Live tables live in schema `public` as `lvl_<name>`. A replay run
(`PgStore(..., replay_run_id=...)`) READS live `lvl_levels` / `lvl_ticks` and
WRITES its outputs to `lvl_replay.<name>` tagged with `run_id`; the replay role
has no write grant on live tables, so this is enforced by the DB too.

Row shapes are plain dicts whose keys are the table's column names (see
supabase/migrations/0035_level_engine.sql). `ts` values are tz-aware datetimes
(UTC); `trade_date` is a `date` (ET trading day).

Level-edit rule (§5, §10): writes to levels of a trade_date earlier than
`today` raise PastDateError (a DB trigger enforces the same thing).
"""
from __future__ import annotations

import copy
import itertools
import json
import uuid
from datetime import date, datetime, timezone
from typing import Any, Iterable, Protocol

# Output tables (written by the tick loop / agents / manager / grader).
OUTPUT_TABLES = ("level_state", "evidence", "beliefs", "picks", "legs")
LEVEL_FIELDS = ("symbol", "price", "name", "kind", "condition_text", "condition_rule",
                "source", "ratio", "converted_from")
SYMBOLS = ("SPY", "SPX")
KINDS = (None, "support", "resistance", "pivot", "trigger")


class PastDateError(PermissionError):
    """Level writes for a trade_date before today are refused (§5, §10)."""


class LevelValidationError(ValueError):
    pass


def validate_level_fields(f: dict, partial: bool = False) -> dict:
    """Normalise + validate user-supplied level fields. Returns a clean copy."""
    out = {k: f[k] for k in LEVEL_FIELDS if k in f}
    if not partial or "symbol" in out:
        sym = str(out.get("symbol") or "").upper()
        if sym not in SYMBOLS:
            raise LevelValidationError("symbol must be SPY or SPX")
        out["symbol"] = sym
    if not partial or "name" in out:
        name = str(out.get("name") or "").strip()
        if not name:
            raise LevelValidationError("name is required")
        out["name"] = name[:120]
    if "price" in out and out["price"] is not None:
        try:
            p = float(out["price"])
        except (TypeError, ValueError):
            raise LevelValidationError("price must be a number")
        if p <= 0:
            raise LevelValidationError("price must be > 0")
        out["price"] = p
    if "kind" in out:
        k = (out["kind"] or None)
        k = k.lower() if isinstance(k, str) else k
        if k not in KINDS:
            raise LevelValidationError("kind must be support|resistance|pivot|trigger")
        out["kind"] = k
    if not partial:
        out.setdefault("source", "entered")
        if out.get("symbol") == "SPY" and out.get("price") is None:
            raise LevelValidationError("SPY levels need a price")
    if out.get("source") not in (None, "entered", "converted"):
        raise LevelValidationError("source must be entered|converted")
    return out


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Store(Protocol):
    # ---- levels (§6, §10) ----
    def list_levels(self, trade_date: date, include_converted: bool = True) -> list[dict]: ...
    def get_level(self, level_id: str) -> dict | None: ...
    def add_level(self, trade_date: date, fields: dict, today: date) -> dict: ...
    def add_levels(self, trade_date: date, rows: list[dict], today: date) -> list[dict]: ...
    def update_level(self, level_id: str, fields: dict, today: date) -> dict: ...
    def delete_level(self, level_id: str, today: date) -> None: ...
    def replace_converted(self, trade_date: date, rows: list[dict], today: date) -> list[dict]: ...
    def list_audit(self, trade_date: date) -> list[dict]: ...
    def levels_version(self, trade_date: date) -> str: ...
    # ---- ticks (§5 lvl_ticks — the replay source) ----
    def insert_tick(self, row: dict) -> None: ...
    def list_ticks(self, trade_date: date) -> list[dict]: ...
    def latest_locked_ratio(self, trade_date: date) -> float | None: ...
    # ---- outputs ----
    def insert(self, table: str, row: dict) -> None: ...
    def insert_many(self, table: str, rows: Iterable[dict]) -> None: ...
    def list_rows(self, table: str, trade_date: date) -> list[dict]: ...
    def update_leg(self, leg_id: str, fields: dict) -> None: ...
    def list_recent_legs(self, limit: int) -> list[dict]: ...
    def get_open_leg(self, trade_date: date | None = None) -> dict | None: ...
    def upsert_day_summary(self, row: dict) -> None: ...
    def get_day_summary(self, trade_date: date) -> dict | None: ...


_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _leg_n(leg_id) -> tuple:
    """Leg ids are f"{trade_date}:{n}" — order by (date, n) numerically."""
    d, _, n = str(leg_id or "").rpartition(":")
    return (d, int(n) if n.isdigit() else -1)


def _recent_key(r: dict) -> tuple:
    """Newest closed first (closed_at, then opened_at, then id) — same order as
    PgStore.list_recent_legs."""
    return (r.get("closed_at") or _EPOCH, r.get("opened_at") or _EPOCH, _leg_n(r.get("id")))


def _check_date(trade_date: date, today: date) -> None:
    if trade_date < today:
        raise PastDateError(f"levels for {trade_date} are read-only (today is {today})")


# ============================================================================
# MemoryStore
# ============================================================================
class MemoryStore:
    def __init__(self) -> None:
        self.levels: dict[str, dict] = {}
        self.audit: list[dict] = []
        self.ticks: list[dict] = []
        self.rows: dict[str, list[dict]] = {t: [] for t in OUTPUT_TABLES}
        self.day_summary: dict[date, dict] = {}
        self._seq = itertools.count(1)

    def _audit(self, level_id, trade_date, action, before, after):
        self.audit.append({"id": next(self._seq), "level_id": level_id, "trade_date": trade_date,
                           "action": action, "before": before, "after": after, "at": _now()})

    # levels
    def list_levels(self, trade_date, include_converted=True):
        out = [copy.deepcopy(r) for r in self.levels.values() if r["trade_date"] == trade_date
               and (include_converted or r["source"] != "converted")]
        return sorted(out, key=lambda r: (r["created_at"], r["id"]))

    def get_level(self, level_id):
        r = self.levels.get(str(level_id))
        return copy.deepcopy(r) if r else None

    def add_level(self, trade_date, fields, today):
        _check_date(trade_date, today)
        f = validate_level_fields(fields)
        now = _now()
        row = {k: None for k in LEVEL_FIELDS}
        row.update(f)
        row.update(id=str(uuid.uuid4()), trade_date=trade_date, created_at=now, updated_at=now)
        self.levels[row["id"]] = row
        self._audit(row["id"], trade_date, "add", None, copy.deepcopy(row))
        return copy.deepcopy(row)

    def add_levels(self, trade_date, rows, today):
        _check_date(trade_date, today)
        for f in rows:  # validate all first: all-or-nothing
            validate_level_fields(f)
        return [self.add_level(trade_date, f, today) for f in rows]

    def update_level(self, level_id, fields, today):
        cur = self.levels.get(str(level_id))
        if cur is None:
            raise KeyError(level_id)
        _check_date(cur["trade_date"], today)
        f = validate_level_fields(fields, partial=True)
        before = copy.deepcopy(cur)
        cur.update(f)
        cur["updated_at"] = _now()
        self._audit(cur["id"], cur["trade_date"], "edit", before, copy.deepcopy(cur))
        return copy.deepcopy(cur)

    def delete_level(self, level_id, today):
        cur = self.levels.get(str(level_id))
        if cur is None:
            raise KeyError(level_id)
        _check_date(cur["trade_date"], today)
        del self.levels[cur["id"]]
        # converted children go with their parent (FK on delete cascade)
        for k in [k for k, r in self.levels.items() if r.get("converted_from") == cur["id"]]:
            del self.levels[k]
        self._audit(cur["id"], cur["trade_date"], "delete", cur, None)

    def replace_converted(self, trade_date, rows, today):
        _check_date(trade_date, today)
        old = [k for k, r in self.levels.items()
               if r["trade_date"] == trade_date and r["source"] == "converted"]
        for k in old:
            del self.levels[k]
        out = []
        now = _now()
        for f in rows:
            row = {k: None for k in LEVEL_FIELDS}
            row.update(validate_level_fields({**f, "source": "converted", "symbol": "SPX"}))
            row.update(id=str(uuid.uuid4()), trade_date=trade_date, created_at=now, updated_at=now)
            self.levels[row["id"]] = row
            out.append(copy.deepcopy(row))
        self._audit(None, trade_date, "convert", {"removed": len(old)}, {"rows": out})
        return out

    def list_audit(self, trade_date):
        return [copy.deepcopy(a) for a in self.audit if a["trade_date"] == trade_date]

    def levels_version(self, trade_date):
        rows = self.list_levels(trade_date)
        return f"{len(rows)}:{max((r['updated_at'] for r in rows), default='')}"

    # ticks
    def insert_tick(self, row):
        self.ticks.append(copy.deepcopy(row))

    def list_ticks(self, trade_date):
        return sorted((copy.deepcopy(t) for t in self.ticks if t["trade_date"] == trade_date),
                      key=lambda t: t["ts"])

    def latest_locked_ratio(self, trade_date):
        locked = [t for t in self.list_ticks(trade_date) if t.get("ratio_locked")]
        return float(locked[-1]["ratio"]) if locked else None

    # outputs
    def insert(self, table, row):
        if table not in OUTPUT_TABLES:
            raise ValueError(table)
        self.rows[table].append(copy.deepcopy(row))

    def insert_many(self, table, rows):
        for r in rows:
            self.insert(table, r)

    def list_rows(self, table, trade_date):
        if table not in OUTPUT_TABLES:
            raise ValueError(table)
        return sorted((copy.deepcopy(r) for r in self.rows[table] if r["trade_date"] == trade_date),
                      key=lambda r: r.get("ts") or r.get("opened_at") or _now())

    def update_leg(self, leg_id, fields):
        for r in self.rows["legs"]:
            if r.get("id") == leg_id:
                r.update(copy.deepcopy(fields))
                return
        raise KeyError(leg_id)

    def list_recent_legs(self, limit):
        closed = [r for r in self.rows["legs"] if r.get("result") is not None]
        closed.sort(key=_recent_key, reverse=True)
        return [copy.deepcopy(r) for r in closed[:max(int(limit), 0)]]

    def get_open_leg(self, trade_date=None):
        rows = [r for r in self.rows["legs"] if r.get("result") is None
                and (trade_date is None or r.get("trade_date") == trade_date)]
        if not rows:
            return None
        return copy.deepcopy(max(rows, key=lambda r: (r.get("opened_at") or _EPOCH,
                                                      _leg_n(r.get("id")))))

    def upsert_day_summary(self, row):
        self.day_summary[row["trade_date"]] = copy.deepcopy(row)

    def get_day_summary(self, trade_date):
        r = self.day_summary.get(trade_date)
        return copy.deepcopy(r) if r else None


# ============================================================================
# PgStore (psycopg 3)
# ============================================================================
_JSON_COLS = {"condition_rule", "before", "after", "zone", "chain", "bias", "detail",
              "pick", "final_zone", "levels_outcome", "legs", "logodds",
              "target_level", "invalidation_level"}


def _adapt(row: dict) -> dict:
    out = {}
    for k, v in row.items():
        if k in _JSON_COLS and v is not None and not isinstance(v, str):
            out[k] = json.dumps(v, default=str)
        else:
            out[k] = v
    return out


class PgStore:
    """psycopg-3 store. `replay_run_id` set → outputs go to lvl_replay.* (§14)."""

    def __init__(self, dsn: str, replay_run_id: str | None = None) -> None:
        if not dsn:
            raise ValueError("PgStore needs a DSN (LVL_DB_URL / LVL_REPLAY_DB_URL)")
        import psycopg  # lazy: tests and MemoryStore never need it
        from psycopg.rows import dict_row
        self._psycopg = psycopg
        from psycopg.types.numeric import FloatLoader
        from psycopg.types.string import TextLoader
        self._conn = psycopg.connect(dsn, autocommit=True, row_factory=dict_row)
        # Same Python types as MemoryStore: ids as str, numerics as float.
        self._conn.adapters.register_loader("numeric", FloatLoader)
        self._conn.adapters.register_loader("uuid", TextLoader)
        self.run_id = replay_run_id

    def close(self) -> None:
        self._conn.close()

    def register_run(self, run_id: str, trade_date: date, label: str, config: dict) -> None:
        """Insert the lvl_replay.runs row for a replay run (§14). Config must be DSN/token-free."""
        self._q("insert into lvl_replay.runs (run_id, trade_date, label, config) "
                "values (%s, %s, %s, %s)",
                (run_id, trade_date, label, json.dumps(config, default=str)))

    def _q(self, sql: str, params: tuple | dict = ()) -> list[dict]:
        with self._conn.cursor() as cur:
            cur.execute(sql, params)
            return list(cur.fetchall()) if cur.description else []

    def _out_table(self, table: str) -> str:
        if table not in OUTPUT_TABLES and table != "day_summary":
            raise ValueError(table)
        return f"lvl_replay.{table}" if self.run_id else f"public.lvl_{table}"

    def _tag(self, row: dict) -> dict:
        return {**row, "run_id": self.run_id} if self.run_id else row

    def _insert_sql(self, table: str, row: dict) -> tuple[str, dict]:
        row = _adapt(row)
        cols = ", ".join(row)
        vals = ", ".join(f"%({k})s" for k in row)
        return f"insert into {table} ({cols}) values ({vals})", row

    def _audit(self, level_id, trade_date, action, before, after):
        sql, p = self._insert_sql("public.lvl_level_audit", {
            "level_id": level_id, "trade_date": trade_date, "action": action,
            "before": before, "after": after})
        self._q(sql, p)

    # levels
    def list_levels(self, trade_date, include_converted=True):
        extra = "" if include_converted else " and source <> 'converted'"
        return self._q(f"select * from public.lvl_levels where trade_date = %s{extra} "
                       "order by created_at, id", (trade_date,))

    def get_level(self, level_id):
        r = self._q("select * from public.lvl_levels where id = %s", (str(level_id),))
        return r[0] if r else None

    def add_level(self, trade_date, fields, today):
        _check_date(trade_date, today)
        f = validate_level_fields(fields)
        sql, p = self._insert_sql("public.lvl_levels", {"trade_date": trade_date, **f})
        row = self._q(sql + " returning *", p)[0]
        self._audit(row["id"], trade_date, "add", None, row)
        return row

    def add_levels(self, trade_date, rows, today):
        _check_date(trade_date, today)
        with self._conn.transaction():
            return [self.add_level(trade_date, f, today) for f in rows]

    def update_level(self, level_id, fields, today):
        cur = self.get_level(level_id)
        if cur is None:
            raise KeyError(level_id)
        _check_date(cur["trade_date"], today)
        f = _adapt(validate_level_fields(fields, partial=True))
        if not f:
            return cur
        sets = ", ".join(f"{k} = %({k})s" for k in f)
        row = self._q(f"update public.lvl_levels set {sets}, updated_at = now() "
                      "where id = %(id)s returning *", {**f, "id": str(level_id)})[0]
        self._audit(row["id"], row["trade_date"], "edit", cur, row)
        return row

    def delete_level(self, level_id, today):
        cur = self.get_level(level_id)
        if cur is None:
            raise KeyError(level_id)
        _check_date(cur["trade_date"], today)
        self._q("delete from public.lvl_levels where id = %s", (str(level_id),))
        self._audit(cur["id"], cur["trade_date"], "delete", cur, None)

    def replace_converted(self, trade_date, rows, today):
        _check_date(trade_date, today)
        out = []
        with self._conn.transaction():
            gone = self._q("delete from public.lvl_levels where trade_date = %s "
                           "and source = 'converted' returning id", (trade_date,))
            for f in rows:
                f = validate_level_fields({**f, "source": "converted", "symbol": "SPX"})
                sql, p = self._insert_sql("public.lvl_levels", {"trade_date": trade_date, **f})
                out.append(self._q(sql + " returning *", p)[0])
            self._audit(None, trade_date, "convert", {"removed": len(gone)}, {"rows": out})
        return out

    def list_audit(self, trade_date):
        return self._q("select * from public.lvl_level_audit where trade_date = %s order by at, id",
                       (trade_date,))

    def levels_version(self, trade_date):
        r = self._q("select count(*) n, max(updated_at) m from public.lvl_levels "
                    "where trade_date = %s", (trade_date,))[0]
        return f"{r['n']}:{r['m'] or ''}"

    # ticks (always live; the replay role can read them)
    def insert_tick(self, row):
        if self.run_id:
            raise PermissionError("replay never writes lvl_ticks")
        sql, p = self._insert_sql("public.lvl_ticks", row)
        self._q(sql + " on conflict (trade_date, ts) do nothing", p)

    def list_ticks(self, trade_date):
        return self._q("select * from public.lvl_ticks where trade_date = %s order by ts",
                       (trade_date,))

    def latest_locked_ratio(self, trade_date):
        r = self._q("select ratio from public.lvl_ticks where trade_date = %s and ratio_locked "
                    "order by ts desc limit 1", (trade_date,))
        return float(r[0]["ratio"]) if r else None

    # outputs
    def insert(self, table, row):
        sql, p = self._insert_sql(self._out_table(table), self._tag(row))
        self._q(sql, p)

    def insert_many(self, table, rows):
        rows = list(rows)
        if not rows:
            return
        with self._conn.transaction():
            for r in rows:
                self.insert(table, r)

    def list_rows(self, table, trade_date):
        t = self._out_table(table)
        order = "opened_at" if table == "legs" else "ts"
        if self.run_id:
            return self._q(f"select * from {t} where trade_date = %s and run_id = %s "
                           f"order by {order}", (trade_date, self.run_id))
        return self._q(f"select * from {t} where trade_date = %s order by {order}", (trade_date,))

    def update_leg(self, leg_id, fields):
        f = _adapt(fields)
        sets = ", ".join(f"{k} = %({k})s" for k in f)
        extra = " and run_id = %(run_id)s" if self.run_id else ""
        self._q(f"update {self._out_table('legs')} set {sets} where id = %(id)s{extra}",
                {**f, "id": leg_id, "run_id": self.run_id})

    # leg ids are f"{trade_date}:{n}": the numeric suffix breaks closed_at ties
    _LEG_N = "(case when id ~ ':[0-9]+$' then substring(id from ':([0-9]+)$')::int else -1 end)"

    def list_recent_legs(self, limit):
        t = self._out_table("legs")
        run = " and run_id = %(run_id)s" if self.run_id else ""
        return self._q(f"select * from {t} where result is not null{run} "
                       f"order by closed_at desc nulls last, opened_at desc nulls last, "
                       f"trade_date desc, {self._LEG_N} desc limit %(limit)s",
                       {"run_id": self.run_id, "limit": max(int(limit), 0)})

    def get_open_leg(self, trade_date=None):
        t = self._out_table("legs")
        where = ["result is null"]
        if self.run_id:
            where.append("run_id = %(run_id)s")
        if trade_date is not None:
            where.append("trade_date = %(td)s")
        r = self._q(f"select * from {t} where {' and '.join(where)} "
                    f"order by opened_at desc nulls last, {self._LEG_N} desc limit 1",
                    {"run_id": self.run_id, "td": trade_date})
        return r[0] if r else None

    def upsert_day_summary(self, row):
        row = self._tag(row)
        sql, p = self._insert_sql(self._out_table("day_summary"), row)
        key = "(run_id, trade_date)" if self.run_id else "(trade_date)"
        sets = ", ".join(f"{k} = excluded.{k}" for k in row if k not in ("trade_date", "run_id"))
        self._q(f"{sql} on conflict {key} do update set {sets}", p)

    def get_day_summary(self, trade_date):
        t = self._out_table("day_summary")
        if self.run_id:
            r = self._q(f"select * from {t} where trade_date = %s and run_id = %s",
                        (trade_date, self.run_id))
        else:
            r = self._q(f"select * from {t} where trade_date = %s", (trade_date,))
        return r[0] if r else None

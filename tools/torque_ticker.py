#!/usr/bin/env python3
"""Show / edit rows of public.torque_tickers_setup (Torque's per-ticker setup).

Every column of a row can be set. Changes reach every backend container within
TORQUE_TICKERS_REFRESH_SEC (default 60s) — no restart, no redeploy.

  python3 tools/torque_ticker.py show                       # every ticker, key columns
  python3 tools/torque_ticker.py show QQQ                   # one row, every column
  python3 tools/torque_ticker.py set QQQ stop_loss_default=50 close_target_default=40
  python3 tools/torque_ticker.py set QQQ stop_loss_default=null      # back to the code default
  python3 tools/torque_ticker.py set XLE tp=15 sl=40                 # aliases of the two columns above
  python3 tools/torque_ticker.py set NVDA --create tick=0.05 yahoo_symbol=NVDA sort_order=110
  add -n / --dry-run to preview the SQL, -y / --yes to skip the confirmation.

Driven by:  make torque-ticker [T=QQQ] [SET="col=val col=val"] [CREATE=1] [ARGS=-y]

Values are typed from the column, never passed through as raw SQL:
  numeric/integer  a number (range-checked for the % and price-step columns), or null
  boolean          true / false
  jsonb            JSON, e.g. offsets='{"vertical":{"anchor":1,"width":5}}'
  text[]           comma list, e.g. allowed_roots=DJXW,DJX   (or null)
  text             anything (notes, yahoo_symbol)
`null` is accepted only on nullable columns; ticker / created_at / updated_at
are not editable (updated_at is maintained by the table's trigger).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import supabase_admin as sa  # noqa: E402  (load_env, run_sql, render)

TABLE = "public.torque_tickers_setup"
_TICKER = re.compile(r"^[A-Z][A-Z0-9.]{0,9}$")
_LOCKED = {"ticker", "created_at", "updated_at"}
# Sanity ranges (inclusive) — the backend's own validators are the final word,
# this just stops an obvious typo from landing in the live table.
_RANGES = {
    "stop_loss_default": (1, 99), "close_target_default": (1, 500), "close_target_min": (1, 500),
    "max_spread_pct": (1, 1000), "richness_floor": (0, 10),
    "tick": (0.001, 100), "tick_above_3": (0.001, 100),
    "commission_per_contract": (0, 50), "extra_fee_per_contract": (0, 50),
    "sort_order": (-100000, 100000),
}
_SHOW_COLS = ("ticker", "enabled", "sort_order", "tick", "tick_above_3", "stop_loss_default",
              "close_target_default", "close_target_min", "max_spread_pct", "market_orders_allowed")


def _ctx():
    env = sa.load_env(sa.ENV_FILE)
    ref = env.get("SUPABASE_PROJECT_REF") or env.get("SUPABASE_URL", "").split("//")[-1].split(".")[0]
    token = env.get("SUPABASE_ACCESS_TOKEN", "")
    if not ref or not token:
        raise SystemExit("ERROR set SUPABASE_PROJECT_REF and SUPABASE_ACCESS_TOKEN in deploy/.env")
    return ref, token


def _ticker(t: str) -> str:
    t = (t or "").strip().upper()
    if not _TICKER.match(t):
        raise SystemExit(f"ERROR not a ticker: {t!r}")
    return t


def _q(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def _columns(ctx) -> dict[str, tuple[str, bool]]:
    rows = sa.run_sql(*ctx, (
        "select column_name, data_type, udt_name, is_nullable from information_schema.columns "
        "where table_schema = 'public' and table_name = 'torque_tickers_setup';"))
    if not rows:
        raise SystemExit(f"ERROR {TABLE} not found (run make db-push)")
    return {r["column_name"]: (r["udt_name"] if r["data_type"] == "ARRAY" else r["data_type"],
                               r["is_nullable"] == "YES") for r in rows}


# The names `show` prints for the ticker's default stop-loss / take-profit,
# accepted on `set` too (used wherever an account has no saved setting).
ALIASES = {"sl": "stop_loss_default", "tp": "close_target_default",
           "reflex_sl": "stop_loss_default", "reflex_stop": "stop_loss_default",
           "reflex_tp": "close_target_default", "reflex_target": "close_target_default"}


def to_sql(col: str, raw: str, cols: dict[str, tuple[str, bool]]) -> str:
    """One CLI value -> a SQL literal typed by the column. Raises SystemExit."""
    col = ALIASES.get(col, col)
    if col not in cols:
        raise SystemExit(f"ERROR unknown column {col!r}; columns: {', '.join(sorted(cols))}")
    if col in _LOCKED:
        raise SystemExit(f"ERROR {col} is not editable")
    typ, nullable = cols[col]
    v = raw.strip()
    if v.lower() == "null":
        if not nullable:
            raise SystemExit(f"ERROR {col} cannot be null")
        return "null"
    if typ in ("numeric", "integer", "bigint", "smallint", "double precision", "real"):
        v = v.rstrip("*").rstrip("%").strip()      # accept what `show` prints: 15%, 30%*
        try:
            num = float(v)
        except ValueError:
            raise SystemExit(f"ERROR {col} needs a number, got {raw!r}")
        if num != num or num in (float("inf"), float("-inf")):
            raise SystemExit(f"ERROR {col} needs a finite number")
        if typ in ("integer", "bigint", "smallint"):
            if not num.is_integer():
                raise SystemExit(f"ERROR {col} needs a whole number, got {raw!r}")
            num = int(num)
        lo, hi = _RANGES.get(col, (None, None))
        if lo is not None and not (lo <= num <= hi):
            raise SystemExit(f"ERROR {col}={num} is outside {lo}..{hi}")
        return repr(num) if isinstance(num, float) else str(num)
    if typ == "boolean":
        if v.lower() not in ("true", "false"):
            raise SystemExit(f"ERROR {col} needs true or false, got {raw!r}")
        return v.lower()
    if typ == "jsonb":
        try:
            obj = json.loads(v)
        except json.JSONDecodeError as e:
            raise SystemExit(f"ERROR {col} needs JSON: {e}")
        if col == "offsets" and not isinstance(obj, dict):
            raise SystemExit("ERROR offsets must be a JSON object")
        return _q(json.dumps(obj)) + "::jsonb"
    if typ == "_text":
        items = [x.strip().upper() for x in v.split(",") if x.strip()]
        if not items or not all(_TICKER.match(x) for x in items):
            raise SystemExit(f"ERROR {col} needs a comma list of symbols, got {raw!r}")
        return "array[" + ",".join(_q(x) for x in items) + "]::text[]"
    if typ == "text":
        return _q(v)
    raise SystemExit(f"ERROR column {col} has unsupported type {typ}")


def parse_pairs(pairs: list[str]) -> list[tuple[str, str]]:
    out, seen = [], set()
    for p in pairs:
        if "=" not in p:
            raise SystemExit(f"ERROR expected col=value, got {p!r}")
        col, _, val = p.partition("=")
        col = ALIASES.get(col.strip(), col.strip())
        if col in seen:
            raise SystemExit(f"ERROR {col} given twice")
        seen.add(col)
        out.append((col, val))
    if not out:
        raise SystemExit("ERROR nothing to set (give col=value pairs)")
    return out


def _row(ctx, t: str) -> dict | None:
    rows = sa.run_sql(*ctx, f"select * from {TABLE} where ticker = {_q(t)};")
    return rows[0] if rows else None


# Code defaults the backend falls back to (app/torque_config.py) — shown so the
# "effective" Reflex columns match what an order would actually use.
_DEFAULT_STOP, _DEFAULT_TARGET = 30.0, 30.0


def _hours(ctx) -> tuple[str, set[str]]:
    rows = sa.run_sql(*ctx, ("select key, value from public.app_config where key in "
                             "('torque_trading_hours', 'torque_extended_hours_tickers');"))
    kv = {r["key"]: r["value"] for r in rows}
    ext = {t.strip().upper() for t in str(kv.get("torque_extended_hours_tickers") or "").split(",") if t.strip()}
    return kv.get("torque_trading_hours") or "(unset — backend uses edgelane_market.config)", ext


def _effective(r: dict, ext: set[str]) -> dict:
    def pct(v, d):
        try:
            return f"{float(v):g}%" if v is not None else f"{d:g}% (default)"
        except (TypeError, ValueError):
            return f"{d:g}% (default)"
    return {"extended_hours": "yes" if r.get("ticker") in ext else "no",
            "sl": pct(r.get("stop_loss_default"), _DEFAULT_STOP),
            "tp": pct(r.get("close_target_default"), _DEFAULT_TARGET)}


def cmd_show(ctx, a) -> int:
    hours, ext = _hours(ctx)
    print(f"trading window (ET): {hours}   regular session 09:30-16:00")
    print(f"extended-hours tickers ({len(ext)}): {', '.join(sorted(ext)) or '(none)'}")
    if a.ticker:
        r = _row(ctx, _ticker(a.ticker))
        if not r:
            print(f"no row for {a.ticker.upper()} — not a Torque ticker")
            return 1
        print(sa.render([{**r, **_effective(r, ext)}], expanded=True))
        return 0
    rows = sa.run_sql(*ctx, f"select {', '.join(_SHOW_COLS)} from {TABLE} order by sort_order, ticker;")
    out = []
    for r in rows:
        e = _effective(r, ext)
        out.append({"ticker": r["ticker"], "on": "yes" if r.get("enabled") else "no",
                    "ext_hrs": e["extended_hours"],
                    "sl": e["sl"].replace(" (default)", "*"),
                    "tp": e["tp"].replace(" (default)", "*"),
                    "tick": r.get("tick"), "max_spread": r.get("max_spread_pct")})
    print(sa.render(out))
    print("sl / tp = the ticker's default stop-loss / take-profit, used wherever an account has no\n"
          "saved setting for it; * = code default (no value on the row). `T=<ticker>` = every column.")
    return 0


def cmd_set(ctx, a) -> int:
    t = _ticker(a.ticker)
    cols = _columns(ctx)
    # `make torque-ticker SET="…"` hands the pairs over in TORQUE_SET (not argv)
    # so JSON keeps its quotes; pairs are whitespace-separated, JSON has no spaces.
    raw_pairs = a.pairs or os.environ.get("TORQUE_SET", "").split()
    pairs = [(c, to_sql(c, v, cols)) for c, v in parse_pairs(raw_pairs)]
    before = _row(ctx, t)
    if before is None and not a.create:
        raise SystemExit(f"ERROR no row for {t}; pass --create (CREATE=1) to add it")
    if before is not None and a.create:
        print(f"note: {t} already exists — updating it")
    if before is None:
        names = ", ".join(["ticker"] + [c for c, _ in pairs])
        vals = ", ".join([_q(t)] + [v for _, v in pairs])
        sql = f"insert into {TABLE} ({names}) values ({vals}) returning *;"
    else:
        sets = ", ".join(f"{c} = {v}" for c, v in pairs)
        sql = f"update {TABLE} set {sets} where ticker = {_q(t)} returning *;"
        print("before:")
        print(sa.render([{c: before.get(c) for c, _ in pairs}]))
    print(f"SQL> {sql}")
    if a.dry_run:
        print("[dry-run] nothing executed")
        return 0
    if not a.yes:
        if not sys.stdin.isatty():
            raise SystemExit("ERROR refusing to write without confirmation; pass --yes (ARGS=-y)")
        if input(f"apply to {t}? [y/N] ").strip().lower() not in ("y", "yes"):
            print("aborted")
            return 1
    after = sa.run_sql(*ctx, sql)
    print("after:")
    print(sa.render([{c: (after[0] if after else {}).get(c) for c in ["ticker"] + [c for c, _ in pairs]}]))
    print("✓ live in every backend within TORQUE_TICKERS_REFRESH_SEC (default 60s)")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("show", help="list tickers, or every column of one")
    s.add_argument("ticker", nargs="?")
    s.set_defaults(fn=cmd_show)
    u = sub.add_parser("set", help="set columns on one ticker row")
    u.add_argument("ticker")
    u.add_argument("pairs", nargs="*", help="col=value …")
    u.add_argument("--create", action="store_true", help="insert the row if it doesn't exist")
    u.add_argument("--dry-run", "-n", action="store_true")
    u.add_argument("--yes", "-y", action="store_true")
    u.set_defaults(fn=cmd_set)
    return p


def main() -> int:
    a = build_parser().parse_args()
    return a.fn(_ctx(), a)


if __name__ == "__main__":
    raise SystemExit(main())

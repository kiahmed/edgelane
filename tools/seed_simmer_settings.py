#!/usr/bin/env python3
"""Seed / reset the default per-user Simmer settings (public.simmer_settings).

Only the USER-FACING columns are managed here — the alert bar (min_score,
min_iv_percentile), risk_profile, sweep_interval, and notify_email. Engine
behavior (DTE window, strike delta, structures, regime, safety gates) is
admin-global in `simmer/simmer_tickers.json`, NOT per user, so it is deliberately
untouched.

Runs against the Supabase Management API (same path as tools/db_push.py) using
SUPABASE_PROJECT_REF + SUPABASE_ACCESS_TOKEN from deploy/.env.

Edit DEFAULTS below, then:

  python3 tools/seed_simmer_settings.py --list           # show current rows
  python3 tools/seed_simmer_settings.py --all            # give every profile a row (insert MISSING only)
  python3 tools/seed_simmer_settings.py --all --reset    # OVERWRITE every row to DEFAULTS
  python3 tools/seed_simmer_settings.py --user <uuid>    # seed/reset one user to DEFAULTS
  add --dry-run to print the SQL and apply nothing
"""
from __future__ import annotations

import json
import re
import sys
import urllib.request
from pathlib import Path

# ── The defaults. Change these, then re-run. ────────────────────────────────
DEFAULTS: dict[str, object] = {
    "min_score": 70,          # alert bar: only surface score ≥ this
    "min_iv_percentile": 40,  # alert bar: only surface IV percentile ≥ this
    "risk_profile": "balanced",
    "sweep_interval": 5,      # minutes (watcher cadence; min across users wins)
    "notify_email": False,
}
_COLS = list(DEFAULTS.keys())

ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = ROOT / "deploy" / ".env"
_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def _load_env(path: Path) -> dict:
    out: dict[str, str] = {}
    for line in (path.read_text().splitlines() if path.is_file() else []):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def _lit(v: object) -> str:
    """A safe SQL literal for our known value types (numbers/bools/short strings)."""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    return "'" + str(v).replace("'", "''") + "'"


def _run_sql(ref: str, token: str, sql: str) -> tuple[int, str]:
    url = f"https://api.supabase.com/v1/projects/{ref}/database/query"
    req = urllib.request.Request(
        url, method="POST", data=json.dumps({"query": sql}).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json",
                 "User-Agent": _UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.status, r.read().decode()


def _cols_sql() -> str:
    return ", ".join(_COLS)


def _vals_sql() -> str:
    return ", ".join(_lit(DEFAULTS[c]) for c in _COLS)


def _update_set() -> str:
    return ", ".join(f"{c} = excluded.{c}" for c in _COLS)


def _sql_list() -> str:
    return (f"select user_id, {_cols_sql()}, updated_at "
            f"from public.simmer_settings order by updated_at desc;")


def _sql_all(reset: bool) -> str:
    conflict = (f"do update set {_update_set()}" if reset else "do nothing")
    return (f"insert into public.simmer_settings (user_id, {_cols_sql()}) "
            f"select id, {_vals_sql()} from public.profiles "
            f"on conflict (user_id) {conflict};")


def _sql_user(uid: str) -> str:
    return (f"insert into public.simmer_settings (user_id, {_cols_sql()}) "
            f"values ('{uid}', {_vals_sql()}) "
            f"on conflict (user_id) do update set {_update_set()};")


def main() -> int:
    argv = sys.argv[1:]
    dry = "--dry-run" in argv or "-n" in argv
    reset = "--reset" in argv
    env = _load_env(ENV_FILE)
    ref, token = env.get("SUPABASE_PROJECT_REF"), env.get("SUPABASE_ACCESS_TOKEN")

    if "--user" in argv:
        uid = argv[argv.index("--user") + 1] if argv.index("--user") + 1 < len(argv) else ""
        if not re.fullmatch(r"[0-9a-fA-F-]{36}", uid):
            print("--user needs a UUID"); return 2
        sql = _sql_user(uid)
    elif "--all" in argv:
        sql = _sql_all(reset)
    elif "--list" in argv:
        sql = _sql_list()
    else:
        print(__doc__)
        return 0

    print("Defaults:", {c: DEFAULTS[c] for c in _COLS})
    print("SQL:\n  " + sql)
    if dry:
        print("[dry-run] nothing applied")
        return 0
    if not (ref and token):
        print("SUPABASE_PROJECT_REF / SUPABASE_ACCESS_TOKEN missing in deploy/.env")
        return 2
    status, body = _run_sql(ref, token, sql)
    print(f"HTTP {status}")
    print(body[:2000])
    return 0 if status < 400 else 1


if __name__ == "__main__":
    raise SystemExit(main())

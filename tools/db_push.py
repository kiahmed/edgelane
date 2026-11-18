#!/usr/bin/env python3
"""Apply EdgeLane Supabase migrations idempotently.

Runs every `supabase/migrations/*.sql` (sorted) against the project via the
Supabase Management API. The migrations are written to be idempotent
(`create … if not exists`, `drop policy if exists` …), so re-running only
effects real diffs — unchanged objects are no-ops. This is the single source
both `make db-push` and `deploy.sh` (prod) use.

Config comes from deploy/.env:
  SUPABASE_PROJECT_REF   project ref (e.g. wfezpfswpywsmbnjrbri)
  SUPABASE_ACCESS_TOKEN  Management API token (sbp_…)

Then mirrors the APP_CONFIG_SYNC keys of edgelane_market.config (Torque's
trading window + extended-hours tickers) into public.app_config.

Usage:
  python3 tools/db_push.py            apply all migrations
  python3 tools/db_push.py --dry-run  list what would run, apply nothing
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = ROOT / "deploy" / ".env"
MARKET_CONFIG = ROOT / "edgelane_market.config"

# edgelane_market.config keys mirrored into public.app_config on every push, so
# the backend reads them from the DB (it falls back to the same config keys).
# Each value is validated before it is written: key -> (app_config key, regex).
APP_CONFIG_SYNC = {
    "TORQUE_TRADING_HOURS": ("torque_trading_hours",
                             r"^([01]\d|2[0-3]):[0-5]\d-([01]\d|2[0-3]):[0-5]\d$"),
    "TORQUE_EXTENDED_HOURS_TICKERS": ("torque_extended_hours_tickers",
                                      r"^[A-Z][A-Z0-9.]{0,9}(,[A-Z][A-Z0-9.]{0,9})*$"),
    "TORQUE_EXT_STOP_LOSS_PCT": ("torque_ext_stop_loss_pct", r"^\d{1,2}(\.\d{1,2})?$"),
    "TORQUE_EXT_CLOSE_TARGET_PCT": ("torque_ext_close_target_pct", r"^\d{1,3}(\.\d{1,2})?$"),
}
MIGRATIONS_DIR = ROOT / "supabase" / "migrations"
# A browser-ish UA — the default urllib UA gets Cloudflare-1010 blocked.
_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def _load_env(path: Path) -> dict:
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def _run_sql(ref: str, token: str, sql: str) -> tuple[int, str]:
    url = f"https://api.supabase.com/v1/projects/{ref}/database/query"
    req = urllib.request.Request(
        url, method="POST", data=json.dumps({"query": sql}).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json",
                 "User-Agent": _UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.status, r.read().decode()[:400]


def _sync_app_config(ref: str, token: str) -> int:
    """Upsert the APP_CONFIG_SYNC keys from edgelane_market.config into
    public.app_config. A key absent from the config is left as it is in the
    DB; a malformed value fails the push rather than writing garbage."""
    import re
    cfg = _load_env(MARKET_CONFIG)
    for cfg_key, (db_key, rx) in APP_CONFIG_SYNC.items():
        raw = cfg.get(cfg_key, "").split("#", 1)[0].strip()
        if not raw:
            continue
        val = re.sub(r"\s+", "", raw).upper() if cfg_key.endswith("TICKERS") else re.sub(r"\s+", "", raw)
        if not re.match(rx, val):
            print(f"FAILED app_config {db_key}: {cfg_key}={raw!r} is malformed", file=sys.stderr)
            return 1
        sql = ("insert into public.app_config (key, value) values ('%s', '%s') "
               "on conflict (key) do update set value = excluded.value, updated_at = now();"
               % (db_key, val))          # both validated above: no quotes possible
        try:
            st, _ = _run_sql(ref, token, sql)
            print(f"app_config {db_key} = {val} -> HTTP {st}")
        except Exception as exc:
            print(f"FAILED app_config {db_key} -> {exc}", file=sys.stderr)
            return 1
    return 0


def main() -> int:
    dry = "--dry-run" in sys.argv or "-n" in sys.argv
    files = sorted(MIGRATIONS_DIR.glob("*.sql"))
    if not files:
        print(f"no migrations in {MIGRATIONS_DIR}")
        return 0

    print(f"==> migrations ({len(files)}):")
    for f in files:
        print(f"    {f.relative_to(ROOT)}")
    if dry:
        print("[dry-run] nothing applied")
        return 0

    env = _load_env(ENV_FILE)
    ref = env.get("SUPABASE_PROJECT_REF") or env.get("SUPABASE_URL", "").split("//")[-1].split(".")[0]
    token = env.get("SUPABASE_ACCESS_TOKEN", "")
    if not ref or not token:
        print("ERROR: SUPABASE_PROJECT_REF and SUPABASE_ACCESS_TOKEN must be set in deploy/.env",
              file=sys.stderr)
        return 1

    for f in files:
        try:
            st, _ = _run_sql(ref, token, f.read_text())
            print(f"applied {f.name} -> HTTP {st}")
        except urllib.error.HTTPError as e:
            print(f"FAILED {f.name} -> HTTP {e.code}: {e.read().decode()[:300]}", file=sys.stderr)
            return 1
        except Exception as exc:
            print(f"FAILED {f.name} -> {exc}", file=sys.stderr)
            return 1

    rc = _sync_app_config(ref, token)
    if rc:
        return rc

    try:
        _, body = _run_sql(ref, token,
                           "select table_name from information_schema.tables "
                           "where table_schema='public' order by table_name;")
        print(f"public tables: {body}")
    except Exception:
        pass
    print("✓ db push complete")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

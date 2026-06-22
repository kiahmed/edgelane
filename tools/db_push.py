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

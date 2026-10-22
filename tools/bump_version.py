#!/usr/bin/env python3
"""Bump the app version shown in the Simmer UI footer.

ONE version string lives in the DB (`simmer_engine_settings` section `version`),
read by both the backend (`/simmer/version`) and the UI, so front + back always
show the same number. Each deployment bumps it — the Makefile deploy targets call
this — so the footer reflects what's actually live.

Reuses tools/supabase_admin.py for DB access (Supabase Management API via
deploy/.env), so there's no extra connection or credentials here.

Usage:
  python3 tools/bump_version.py           # bump patch  (1.0.3 -> 1.0.4)
  python3 tools/bump_version.py minor      # 1.0.4 -> 1.1.0
  python3 tools/bump_version.py major      # 1.1.0 -> 2.0.0
  python3 tools/bump_version.py --show     # print the current version, change nothing
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

_ADMIN = Path(__file__).with_name("supabase_admin.py")
_SELECT = ("select settings->>'version' as version from "
           "simmer_engine_settings where section='version'")


def _run(sql: str, *, write: bool) -> str:
    cmd = [sys.executable, str(_ADMIN), "sql"]
    if write:
        cmd.append("--yes")
    else:
        cmd.append("--json")
    cmd.append(sql)
    out = subprocess.run(cmd, capture_output=True, text=True)
    if out.returncode != 0:
        sys.stderr.write(out.stderr or out.stdout)
        raise SystemExit(f"bump_version: admin query failed ({out.returncode})")
    return out.stdout


def _current() -> str:
    rows = json.loads(_run(_SELECT, write=False) or "[]")
    v = (rows[0].get("version") if rows else None) or "1.0.0"
    return str(v)


def _next(cur: str, part: str) -> str:
    try:
        major, minor, patch = (int(x) for x in cur.split("."))
    except (ValueError, AttributeError):
        major, minor, patch = 1, 0, 0
    if part == "major":
        return f"{major + 1}.0.0"
    if part == "minor":
        return f"{major}.{minor + 1}.0"
    return f"{major}.{minor}.{patch + 1}"


def main() -> None:
    args = [a for a in sys.argv[1:]]
    if "--show" in args:
        print(_current())
        return
    part = next((a for a in args if a in ("major", "minor", "patch")), "patch")
    cur = _current()
    nxt = _next(cur, part)
    # jsonb_set the single field so nothing else in the row is disturbed.
    _run("update simmer_engine_settings set settings = "
         f"jsonb_set(settings, '{{version}}', '\"{nxt}\"'), updated_at = now() "
         "where section='version'", write=True)
    print(f"version: {cur} -> {nxt}")


if __name__ == "__main__":
    main()

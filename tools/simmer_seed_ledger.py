#!/usr/bin/env python3
"""Seed the Simmer event ledger with a couple of FAKE rows for local dev, and set
the dev ledger token — so `simmer-poster` can claim → compose → post a DRAFT
without the engine or real market data.

What it does (all via PostgREST with the service_role key, which bypasses the
ledger's RLS — the same key the backend writes with):

  1. Upserts sha256(<token>) into `public.simmer_ledger_secret` so the poster's
     ledger token is accepted. The RAW token is what you put in the poster's
     env as SIMMER_LEDGER_TOKEN (printed at the end).
  2. Inserts N fake ledger rows built by the REAL freezing code
     (`app.simmer_ledger.build_row` on a synthetic readiness envelope), so
     `data` (card/score/gates/sentiment/evolution) and `snap_html` are exactly
     the shapes the poster expects. Conflicts on event_id are ignored, so it's
     re-runnable.

Then it prints the ready-to-paste `make simmer-event … MODE=draft` commands
(run those in the soljet-postiz repo) that make the poster post DRAFTS.

Prereqs: migrations 0019/0020 applied (`make db-push`), and
edgelane_market.config has SUPABASE_URL + SUPABASE_SERVICE_KEY.

Safety on the single prod Supabase: the token is NOT a repo constant (reused from
the poster .env or randomly generated); an existing secret row is never
overwritten without --force-token (a deployed poster's real token lives there);
and the fake rows use SMR-SEED-* ids that can't collide with real engine events.

Usage:
  python3 tools/simmer_seed_ledger.py                 # reuse .env token or generate one
  python3 tools/simmer_seed_ledger.py --token XYZ     # use a specific token
  python3 tools/simmer_seed_ledger.py --dry-run       # build + print, write nothing
"""
from __future__ import annotations

import argparse
import hashlib
import json
import secrets
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "market" / "backend"
sys.path.insert(0, str(BACKEND))

# NO committed default token: a value in the repo would be a known secret, and
# claim/done/prune are granted to anon — anyone with it could tombstone rows and
# suppress posts. The token is reused from the poster's .env if already set, else
# a fresh random one is generated per machine.
# The soljet-postiz repo whose .env the local poster reads. Override with
# --postiz-env if yours lives elsewhere.
DEFAULT_POSTIZ_ENV = "/mnt/c/soljet_dev/ai_stack_development/soljet-postiz/.env"


def _read_env_key(env_path: Path, key: str) -> str:
    if not env_path.is_file():
        return ""
    for ln in env_path.read_text().splitlines():
        ln = ln.strip()
        if ln.startswith(f"{key}="):
            return ln.partition("=")[2].strip().strip('"').strip("'")
    return ""


def _ensure_env_line(env_path: Path, key: str, value: str) -> str:
    """Add or update KEY=value in the poster's .env so local `make simmer-event`
    picks up the token with no manual edit. Returns a short status string."""
    if not env_path.is_file():
        return f"! {env_path} not found — add {key}={value} yourself"
    lines = env_path.read_text().splitlines()
    for i, ln in enumerate(lines):
        if ln.strip().startswith(f"{key}="):
            if ln.strip() == f"{key}={value}":
                return f"{key} already set in {env_path.name}"
            lines[i] = f"{key}={value}"
            env_path.write_text("\n".join(lines) + "\n")
            return f"updated {key} in {env_path.name}"
    lines.append(f"{key}={value}")
    env_path.write_text("\n".join(lines) + "\n")
    return f"added {key} to {env_path.name}"

# Two fake events: a fresh NVDA "ready" and an AMD "watch_entered". Minimal
# readiness envelopes carrying just what _state_block + render_snap_card read.
_FAKES = [
    dict(symbol="NVDA", state="ready", score=88.0, decision="ready",
         structure="bull_put", expiry=None),
    dict(symbol="AMD", state="watch_entered", score=57.0, decision="watch",
         structure="bull_put", expiry=None),
]


def _fake_env(symbol, expiry, score, decision, structure):
    return {
        "symbol": symbol, "expiration": expiry, "spot": 180.0,
        "score": score, "decision": decision, "structure": structure,
        "strikes": {"short": 170.0, "long": 165.0, "width": 5.0},
        "credit_mid": 0.85, "credit_fill": 0.80, "max_loss": 4.20,
        "pop_breakeven": 0.74, "expected_value": 0.06, "alpha": 0.02,
        "veto_reasons": [], "avoid_if": [],
        "components": {"structural_safety": {"score": 0.82}},
        "data_quality": {}, "metrics": {"iv_percentile_effective": 55.0,
                                        "atm_iv": 0.42, "vrp": 1.3, "em_1sd": 6.1},
        "regime": {"state": "contango"},
        "engine_version": "simmer-seed", "computed_at": datetime.now(timezone.utc).isoformat(),
    }


def _rest(settings):
    base = settings.supabase_url.rstrip("/") + "/rest/v1"
    key = settings.supabase_service_key
    headers = {"apikey": key, "Authorization": f"Bearer {key}",
               "Content-Type": "application/json"}
    return base, headers


def _post(base, headers, path, body, prefer, params=None):
    url = f"{base}/{path}"
    if params:
        url += "?" + "&".join(f"{k}={v}" for k, v in params.items())
    req = urllib.request.Request(url, method="POST",
                                 data=json.dumps(body).encode(),
                                 headers={**headers, "Prefer": prefer})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.status, r.read().decode()[:200]


def _delete(base, headers, path) -> tuple[int, str]:
    req = urllib.request.Request(f"{base}/{path}", method="DELETE",
                                 headers={**headers, "Prefer": "return=minimal"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.status, r.read().decode()[:200]


# Legacy fake rows an early version of this tool inserted with REAL engine
# event_ids (before the SMR-SEED- prefix existed). They must be purged: a genuine
# same-day event with that expiry would dedupe against them and never publish.
_LEGACY_FAKE_IDS = (
    "SMR-NVDA-260930-261030-ready",
    "SMR-AMD-260930-261030-watch_entered",
)


def _get_stored_hash(base, headers) -> str | None:
    """The token_hash already in simmer_ledger_secret (id=1), or None if the row
    is absent. Used to avoid clobbering a real/deployed poster token."""
    url = f"{base}/simmer_ledger_secret?id=eq.1&select=token_hash"
    req = urllib.request.Request(url, method="GET", headers=headers)
    with urllib.request.urlopen(req, timeout=30) as r:
        rows = json.loads(r.read().decode() or "[]")
    return rows[0]["token_hash"] if rows else None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--token", default=None,
                    help="raw ledger token; default reuses the poster .env's or generates a random one")
    ap.add_argument("--postiz-env", default=DEFAULT_POSTIZ_ENV,
                    help="path to the soljet-postiz .env to write SIMMER_LEDGER_TOKEN into")
    ap.add_argument("--force-token", action="store_true",
                    help="OVERWRITE an existing simmer_ledger_secret row (would break a deployed poster)")
    ap.add_argument("--purge", action="store_true",
                    help="first DELETE all SMR-SEED-* rows and the known legacy fake rows")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    from app.config import get_settings
    from app import simmer_ledger

    settings = get_settings()
    if not (getattr(settings, "supabase_url", "") and getattr(settings, "supabase_service_key", "")):
        print("ERROR: SUPABASE_URL / SUPABASE_SERVICE_KEY missing from edgelane_market.config",
              file=sys.stderr)
        return 1

    postiz_env = Path(args.postiz_env)
    # Token: explicit --token, else whatever the poster .env already carries, else
    # a fresh random one. NEVER a repo-committed constant.
    token = args.token or _read_env_key(postiz_env, "SIMMER_LEDGER_TOKEN") or secrets.token_urlsafe(24)
    token_hash = hashlib.sha256(token.encode()).hexdigest()

    # Pick a near-30-DTE Friday for the expiry so the card looks real.
    exp = datetime.now(timezone.utc).date() + timedelta(days=28)
    while exp.weekday() != 4:
        exp += timedelta(days=1)
    exp_iso = exp.isoformat()
    yy = exp.strftime("%y%m%d")

    rows = []
    for f in _FAKES:
        # SEED-prefixed id: must NEVER equal a real engine event_id
        # (SMR-<SYM>-<day>-<exp>-<state>), or a genuine same-day event would
        # dedupe against this fake row and never publish.
        eid = f"SMR-SEED-{f['symbol']}-{yy}-{f['state']}"
        env = _fake_env(f["symbol"], exp_iso, f["score"], f["decision"], f["structure"])
        row = simmer_ledger.build_row(
            event_id=eid, symbol=f["symbol"], state=f["state"], expiry=exp_iso,
            attrs={"product": "simmer", "symbol": f["symbol"], "state": f["state"],
                   "expiry": exp_iso, "event_id": eid},
            env=env, db=None)
        rows.append(row)

    base, headers = _rest(settings)
    legacy_in = "(" + ",".join(f'"{i}"' for i in _LEGACY_FAKE_IDS) + ")"
    if args.dry_run:
        if args.purge:
            print("[dry-run] would DELETE event_id like 'SMR-SEED-%' and in", _LEGACY_FAKE_IDS)
        print(f"[dry-run] token source: "
              f"{'--token' if args.token else ('.env reuse' if _read_env_key(postiz_env, 'SIMMER_LEDGER_TOKEN') else 'generated')}")
        print("[dry-run] would ensure simmer_ledger_secret token_hash=", token_hash[:12], "…")
        for r in rows:
            print(f"[dry-run] would insert row {r['event_id']} ({r['symbol']} {r['state']}) "
                  f"snap_html={len(r['snap_html'] or '')}b")
    else:
        if args.purge:
            st1, _ = _delete(base, headers, "simmer_event_ledger?event_id=like.SMR-SEED-*")
            st2, _ = _delete(base, headers, f"simmer_event_ledger?event_id=in.{legacy_in}")
            print(f"purged seed rows (HTTP {st1}) + legacy fake rows (HTTP {st2})")
        # Never clobber an existing secret row (a deployed poster's real token
        # lives there) unless explicitly forced.
        stored = _get_stored_hash(base, headers)
        if stored is None:
            _post(base, headers, "simmer_ledger_secret", {"id": 1, "token_hash": token_hash},
                  "return=minimal")
            print("seeded token hash (row was absent)")
        elif stored == token_hash:
            print("token hash already matches — left as is")
        elif args.force_token:
            _post(base, headers, "simmer_ledger_secret", {"id": 1, "token_hash": token_hash},
                  "resolution=merge-duplicates,return=minimal", params={"on_conflict": "id"})
            print("OVERWROTE existing token hash (--force-token)")
        else:
            print("! simmer_ledger_secret already has a DIFFERENT token — NOT overwriting "
                  "(would break a deployed poster).\n"
                  "  Pass --token=<the existing token> so this run matches it, or --force-token "
                  "to replace it.", file=sys.stderr)
            return 1
        st, body = _post(base, headers, "simmer_event_ledger", rows,
                         "resolution=ignore-duplicates,return=minimal",
                         params={"on_conflict": "event_id"})
        print(f"seeded {len(rows)} ledger rows -> HTTP {st} {body}")
        print(_ensure_env_line(postiz_env, "SIMMER_LEDGER_TOKEN", token))

    print("\n--- now, in the soljet-postiz repo, post DRAFTS ---")
    for r in rows:
        evt = {"product": "simmer", "symbol": r["symbol"], "state": r["state"],
               "expiry": r["expiry"], "event_id": r["event_id"], "ledger": "1"}
        print(f"make simmer-event MODE=draft EVENT='{json.dumps(evt)}'")
    print("\n(add DRY=1 to compose+claim-peek without calling Postiz at all)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Cloudflare API-token + Turnstile-widget helper for EdgeLane.

Why this exists
---------------
The tunnel-scoped CF_API_TOKEN in deploy/.env cannot manage Turnstile (it has no
account/Turnstile permissions — `verify` shows 0 accounts), which is why the
Turnstile widget's allowed-hostnames couldn't be fixed via API. This tool:

  verify          Diagnose what a token can actually do (accounts, Turnstile R/W).
  create-token    Mint a NEW token scoped for Turnstile management (+ account read).
  widget          List widgets, or add an allowed hostname to one (fixes error 110200).

Auth (env or flags), resolved in this order per command:
  • verify / widget  → --token  | $CF_API_TOKEN_TURNSTILE | $CF_API_TOKEN
  • create-token     → a BOOTSTRAP credential that itself can create tokens:
                         --admin-token | $CF_API_TOKEN_ADMIN   (a token with the
                           "User API Tokens: Edit" permission), OR
                         --global-key/$CF_GLOBAL_API_KEY + --email/$CF_EMAIL
                       A normal scoped token cannot create tokens — that's a
                       Cloudflare rule, not a limitation of this script.

Examples
  python tools/cf_token.py verify
  python tools/cf_token.py create-token --name edgelane-turnstile
  python tools/cf_token.py widget --sitekey 0x4AAAAAADoUOnE-RLndnqGU \
      --add edgelane-matrix.vercel.app --add localhost

Reads deploy/.env automatically if present. Stdlib only (no pip installs).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

API = "https://api.cloudflare.com/client/v4"
# This util lives in tools/ but reads the deploy credentials in deploy/.env.
ENV_PATH = Path(__file__).resolve().parents[1] / "deploy" / ".env"


# ── tiny .env loader (KEY=VALUE, # comments) ────────────────────────────────
def load_env() -> None:
    if not ENV_PATH.is_file():
        return
    for line in ENV_PATH.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


# ── HTTP ────────────────────────────────────────────────────────────────────
def _req(method: str, path: str, headers: dict, body: dict | None = None) -> dict:
    url = path if path.startswith("http") else f"{API}{path}"
    data = json.dumps(body).encode() if body is not None else None
    h = {"Content-Type": "application/json", **headers}
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode() or "{}")
        except Exception:
            return {"success": False, "errors": [{"message": f"HTTP {e.code}"}]}
    except Exception as e:  # noqa: BLE001
        return {"success": False, "errors": [{"message": str(e)}]}


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _global(email: str, key: str) -> dict:
    return {"X-Auth-Email": email, "X-Auth-Key": key}


def _errs(resp: dict) -> str:
    return "; ".join(e.get("message", str(e)) for e in (resp.get("errors") or [])) or "unknown error"


# ── auth resolution ──────────────────────────────────────────────────────────
def resolve_token(args) -> str | None:
    return (getattr(args, "token", None)
            or os.environ.get("CF_API_TOKEN_TURNSTILE")
            or os.environ.get("CF_API_TOKEN"))


def resolve_bootstrap(args) -> dict | None:
    """Headers for a credential that can CREATE tokens, or None."""
    admin = getattr(args, "admin_token", None) or os.environ.get("CF_API_TOKEN_ADMIN")
    if admin:
        return _bearer(admin)
    key = getattr(args, "global_key", None) or os.environ.get("CF_GLOBAL_API_KEY")
    email = getattr(args, "email", None) or os.environ.get("CF_EMAIL")
    if key and email:
        return _global(email, key)
    return None


def first_account(headers: dict) -> tuple[str | None, str | None]:
    resp = _req("GET", "/accounts", headers)
    if not resp.get("success"):
        return None, _errs(resp)
    res = resp.get("result") or []
    if not res:
        return None, "token can see 0 accounts"
    return res[0]["id"], res[0].get("name")


def resolve_account(headers: dict, args) -> tuple[str | None, str | None]:
    """Account id, preferring an explicit one. A minimal Turnstile token can use
    Turnstile under a known account but CANNOT enumerate /accounts (that needs
    Account Settings: Read) — so an explicit id is the reliable path."""
    explicit = getattr(args, "account_id", None) or os.environ.get("CF_ACCOUNT_ID")
    if explicit:
        return explicit, "explicit (CF_ACCOUNT_ID/--account-id)"
    return first_account(headers)


# ── commands ──────────────────────────────────────────────────────────────────
def cmd_verify(args) -> int:
    token = resolve_token(args)
    if not token:
        print("✗ no token (set CF_API_TOKEN / CF_API_TOKEN_TURNSTILE or pass --token)")
        return 2
    h = _bearer(token)
    v = _req("GET", "/user/tokens/verify", h)
    print(f"token active: {v.get('success') and v.get('result', {}).get('status')}")
    aid, name = resolve_account(h, args)
    if not aid:
        print(f"accounts visible: 0  ({name})")
        print("→ A minimal Turnstile token can't ENUMERATE accounts (that needs")
        print("  Account Settings: Read) — but it CAN use Turnstile if you give it the")
        print("  account id directly. Find it: Cloudflare dashboard → right sidebar")
        print("  'Account ID' (or the dash.cloudflare.com/<ACCOUNT_ID> URL). Then either:")
        print("    • add  CF_ACCOUNT_ID=<id>  to deploy/.env, or")
        print("    • run  python tools/cf_token.py verify --account-id <id>")
        return 1
    print(f"account: {aid}  ({name})")
    # Probe Turnstile read
    w = _req("GET", f"/accounts/{aid}/challenges/widgets", h)
    if w.get("success"):
        widgets = w.get("result") or []
        print(f"Turnstile read: OK ({len(widgets)} widget(s))")
        for wd in widgets:
            print(f"  • {wd.get('sitekey')}  {wd.get('name')!r}  domains={wd.get('domains')}")
    else:
        print(f"Turnstile read: NO ({_errs(w)})")
    return 0


def _turnstile_write_group(headers: dict) -> tuple[str | None, str]:
    """Find the 'Turnstile Sites Write' permission-group id."""
    resp = _req("GET", "/user/tokens/permission_groups", headers)
    if not resp.get("success"):
        return None, _errs(resp)
    want = None
    for g in resp.get("result") or []:
        n = (g.get("name") or "").lower()
        if "turnstile" in n and ("write" in n or "edit" in n):
            want = g["id"]
            break
    return want, "" if want else "no Turnstile write permission group found"


def cmd_create_token(args) -> int:
    boot = resolve_bootstrap(args)
    if not boot:
        print("✗ create-token needs a BOOTSTRAP credential that can create tokens:")
        print("   • CF_API_TOKEN_ADMIN  (a token with 'User → API Tokens → Edit'), or")
        print("   • CF_GLOBAL_API_KEY + CF_EMAIL  (My Profile → API Tokens → Global API Key)")
        print("  A scoped/tunnel token cannot create tokens (Cloudflare rule).")
        return 2
    aid, name = resolve_account(boot, args)
    if not aid:
        print(f"✗ bootstrap can't resolve an account: {name}")
        print("  Set CF_ACCOUNT_ID=<id> in deploy/.env or pass --account-id <id>.")
        return 1
    gid, err = _turnstile_write_group(boot)
    if not gid:
        print(f"✗ {err}")
        return 1
    body = {
        "name": args.name,
        "policies": [{
            "effect": "allow",
            "resources": {f"com.cloudflare.api.account.{aid}": "*"},
            "permission_groups": [{"id": gid}],
        }],
    }
    resp = _req("POST", "/user/tokens", boot, body)
    if not resp.get("success"):
        print(f"✗ token creation failed: {_errs(resp)}")
        return 1
    new = resp["result"]["value"]
    print(f"✓ created token {resp['result'].get('id')} ({args.name}) for account {aid} ({name})")
    print("\n  ── NEW TOKEN (shown once — store it now) ──")
    print(f"  {new}\n")
    print("  Add to deploy/.env:")
    print(f"    CF_API_TOKEN_TURNSTILE={new}")
    print("  Then fix the widget:")
    print("    python tools/cf_token.py widget --sitekey <SITEKEY> --add edgelane-matrix.vercel.app")
    return 0


def cmd_widget(args) -> int:
    token = resolve_token(args)
    if not token:
        print("✗ no Turnstile-capable token (set CF_API_TOKEN_TURNSTILE or pass --token)")
        return 2
    h = _bearer(token)
    aid, name = resolve_account(h, args)
    if not aid:
        print(f"✗ can't resolve an account ({name}).")
        print("  Set CF_ACCOUNT_ID=<id> in deploy/.env or pass --account-id <id>.")
        return 1

    if not args.sitekey:
        # list mode
        w = _req("GET", f"/accounts/{aid}/challenges/widgets", h)
        if not w.get("success"):
            print(f"✗ list failed: {_errs(w)}")
            return 1
        for wd in (w.get("result") or []):
            print(f"{wd.get('sitekey')}  {wd.get('name')!r}  domains={wd.get('domains')}")
        return 0

    cur = _req("GET", f"/accounts/{aid}/challenges/widgets/{args.sitekey}", h)
    if not cur.get("success"):
        print(f"✗ get widget failed: {_errs(cur)}")
        return 1
    wd = cur["result"]
    domains = list(wd.get("domains") or [])
    if not args.add:
        print(f"{wd.get('name')!r}  mode={wd.get('mode')}  domains={domains}")
        return 0
    added = [d for d in args.add if d not in domains]
    if not added:
        print(f"✓ already present: {args.add}  (domains={domains})")
        return 0
    domains.extend(added)
    # Update = full PUT of the widget object (name + domains + mode are required).
    put = {"name": wd.get("name"), "domains": domains, "mode": wd.get("mode")}
    resp = _req("PUT", f"/accounts/{aid}/challenges/widgets/{args.sitekey}", h, put)
    if not resp.get("success"):
        print(f"✗ update failed: {_errs(resp)}")
        return 1
    print(f"✓ added {added} → widget domains now: {resp['result'].get('domains')}")
    print("  Turnstile 110200 should clear within ~1 min; hard-reload the site.")
    return 0


def main() -> int:
    load_env()
    p = argparse.ArgumentParser(description="Cloudflare token + Turnstile helper")
    sub = p.add_subparsers(dest="cmd", required=True)

    v = sub.add_parser("verify", help="diagnose a token's capabilities")
    v.add_argument("--token")
    v.add_argument("--account-id", help="CF account id (else $CF_ACCOUNT_ID / enumerate)")
    v.set_defaults(fn=cmd_verify)

    c = sub.add_parser("create-token", help="create a Turnstile-scoped API token")
    c.add_argument("--name", default="edgelane-turnstile")
    c.add_argument("--account-id", help="CF account id (else $CF_ACCOUNT_ID / enumerate)")
    c.add_argument("--admin-token", help="bootstrap token with 'API Tokens: Edit'")
    c.add_argument("--global-key", help="Global API Key (with --email)")
    c.add_argument("--email", help="Cloudflare account email (with --global-key)")
    c.set_defaults(fn=cmd_create_token)

    w = sub.add_parser("widget", help="list widgets or add an allowed hostname")
    w.add_argument("--token")
    w.add_argument("--account-id", help="CF account id (else $CF_ACCOUNT_ID / enumerate)")
    w.add_argument("--sitekey", help="widget sitekey (omit to list all)")
    w.add_argument("--add", action="append", default=[], metavar="DOMAIN",
                   help="hostname to add to the widget (repeatable)")
    w.set_defaults(fn=cmd_widget)

    args = p.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())

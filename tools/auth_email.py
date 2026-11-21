#!/usr/bin/env python3
"""Invite a user to a Facades product, or send them a password reset — with
the product's redirect, so the email never lands on the portal.

  python3 tools/auth_email.py invite jane@example.com simmer      # invite + grant simmer
  python3 tools/auth_email.py reset  jane@example.com matrix      # password reset → Matrix
  python3 tools/auth_email.py grant  jane@example.com simmer      # grant only (existing user)
  add -n / --dry-run to print what would be sent, nothing else.

Driven by:  make invite EMAIL=… PRODUCT=matrix|simmer|reflex
            make reset-password EMAIL=… PRODUCT=…

Why: an auth email without a redirect sends the user to the Site URL
(facades.trade, the portal), which discards the link. Every Facades product
(Matrix, Simmer, Reflex) signs in against the same Supabase project, so this one
tool serves all three. The redirect must also be on Supabase → Authentication →
URL Configuration → Redirect URLs; the tool checks that before sending.

Grants: Matrix needs `market` and Simmer `simmer` in profiles.tools_enabled or
the product refuses the login; Reflex's login has no tool gate. The invite also
passes signup_product so the signup trigger seeds the tool; the explicit grant
covers an existing user.

Config (deploy/.env): SUPABASE_URL, SUPABASE_SERVICE_KEY, SUPABASE_ANON_KEY,
SUPABASE_PROJECT_REF + SUPABASE_ACCESS_TOKEN (allow-list check and the grant,
which runs as one atomic SQL statement on an exact email / id match).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import supabase_admin as sa  # noqa: E402  (run_sql: Management API SQL)

ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = ROOT / "deploy" / ".env"
_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
# No quotes, spaces, angle brackets or wildcard characters (* %) anywhere.
_EMAIL = re.compile(r"^[^@\s'\"<>*%\\]+@[^@\s'\"<>*%\\]+\.[A-Za-z]{2,}$")
_UUID = re.compile(r"^[0-9a-fA-F-]{36}$")

# product -> (redirect URL, tools_enabled entry the product requires, or None)
PRODUCTS = {
    "matrix": ("https://matrix.facades.trade/", "market"),
    "simmer": ("https://simmer.facades.trade/", "simmer"),
    "reflex": ("https://reflex.facades.trade/", None),
}


def load_env(path: Path) -> dict:
    out: dict[str, str] = {}
    if path.is_file():
        for line in path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def _req(method: str, url: str, headers: dict, body: dict | None = None) -> tuple[int, object]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, method=method, data=data,
                                 headers={"User-Agent": _UA, "Accept": "application/json",
                                          **({"Content-Type": "application/json"} if data else {}),
                                          **headers})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read().decode()
            return r.status, (json.loads(raw) if raw.strip() else {})
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw)
        except json.JSONDecodeError:
            return e.code, raw[:300]


def check_allowlisted(env: dict, url: str) -> str | None:
    """Warning text if `url` isn't on the project's Redirect URLs (None = OK
    or couldn't check)."""
    ref, tok = env.get("SUPABASE_PROJECT_REF"), env.get("SUPABASE_ACCESS_TOKEN")
    if not (ref and tok):
        return None
    st, d = _req("GET", f"https://api.supabase.com/v1/projects/{ref}/config/auth",
                 {"Authorization": f"Bearer {tok}"})
    if st != 200 or not isinstance(d, dict):
        return None
    allow = [u.strip() for u in str(d.get("uri_allow_list") or "").split(",") if u.strip()]
    host = urllib.parse.urlparse(url).netloc
    if any(urllib.parse.urlparse(a.rstrip("*")).netloc == host for a in allow):
        return None
    return (f"{url} is NOT on Supabase → Authentication → URL Configuration → Redirect URLs — "
            f"the email would land on the portal ({d.get('site_url')}). Add it first.")


def _q(text: str) -> str:
    return "'" + str(text).replace("'", "''") + "'"


def grant(env: dict, email: str, tool: str | None, dry: bool, user_id: str | None = None) -> str:
    """Add `tool` to the user's profiles.tools_enabled. The profile is found by
    its id (from the invite) or by an EXACT, case-insensitive email match — never
    a pattern — and must be exactly one row. The add is a single atomic SQL
    array_append guarded by "not already present", so a concurrent change can't
    be overwritten."""
    if not tool:
        return "no grant needed (this product's login has no tool gate)"
    ref, tok = env.get("SUPABASE_PROJECT_REF"), env.get("SUPABASE_ACCESS_TOKEN")
    if not (ref and tok):
        return "grant FAILED: set SUPABASE_PROJECT_REF and SUPABASE_ACCESS_TOKEN in deploy/.env"
    if user_id and _UUID.match(user_id):
        where = f"id = {_q(user_id)}"
    else:
        where = f"lower(email) = lower({_q(email)})"
    rows = sa.run_sql(ref, tok, f"select id, tools_enabled from public.profiles where {where};")
    if not rows:
        return f"grant pending: no profile for {email} yet — run `make grant-product` once the user exists"
    if len(rows) != 1:
        return f"grant REFUSED: {len(rows)} profiles match {email} — grant by hand"
    pid, tools = rows[0]["id"], list(rows[0].get("tools_enabled") or [])
    if tool in tools:
        return f"already has {tool}"
    if dry:
        return f"[dry-run] would add {tool} to tools_enabled {tools}"
    out = sa.run_sql(ref, tok, (
        f"update public.profiles set tools_enabled = array_append(tools_enabled, {_q(tool)}) "
        f"where id = {_q(pid)} and not ({_q(tool)} = any(tools_enabled)) returning tools_enabled;"))
    return f"granted {tool}" if out else f"already has {tool}"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("action", choices=("invite", "reset", "grant"))
    ap.add_argument("email")
    ap.add_argument("product", choices=sorted(PRODUCTS))
    ap.add_argument("--dry-run", "-n", action="store_true")
    a = ap.parse_args()

    email = a.email.strip()
    if not _EMAIL.match(email):
        print(f"ERROR not an email address: {email!r}", file=sys.stderr)
        return 2
    env = load_env(ENV_FILE)
    missing = [k for k in ("SUPABASE_URL", "SUPABASE_SERVICE_KEY", "SUPABASE_ANON_KEY") if not env.get(k)]
    if missing:
        print(f"ERROR set {', '.join(missing)} in deploy/.env", file=sys.stderr)
        return 2
    url, tool = PRODUCTS[a.product]
    base = env["SUPABASE_URL"].rstrip("/")
    redirect = urllib.parse.quote(url, safe="")

    if a.action in ("invite", "reset"):
        warn = check_allowlisted(env, url)
        if warn:
            print(f"ERROR {warn}", file=sys.stderr)
            return 1

    if a.action == "grant":
        print(grant(env, email, tool, a.dry_run))
        return 0

    if a.action == "reset":
        if a.dry_run:
            print(f"[dry-run] POST /auth/v1/recover?redirect_to={url}  email={email}")
            return 0
        st, d = _req("POST", f"{base}/auth/v1/recover?redirect_to={redirect}",
                     {"apikey": env["SUPABASE_ANON_KEY"]}, {"email": email})
        if st in (200, 204):
            print(f"✓ password reset sent to {email} → {url}")
            return 0
        print(f"ERROR reset failed (HTTP {st}): {d}", file=sys.stderr)
        return 1

    # invite
    body = {"email": email, "data": {"signup_product": tool or a.product}}
    if a.dry_run:
        print(f"[dry-run] POST /auth/v1/invite?redirect_to={url}  {json.dumps(body)}")
        print(grant(env, email, tool, True))
        return 0
    h = {"apikey": env["SUPABASE_SERVICE_KEY"], "Authorization": f"Bearer {env['SUPABASE_SERVICE_KEY']}"}
    st, d = _req("POST", f"{base}/auth/v1/invite?redirect_to={redirect}", h, body)
    text = json.dumps(d) if not isinstance(d, str) else d
    if st in (200, 201):
        print(f"✓ invite sent to {email} → {url}")
    elif st == 422 or "already" in text.lower():
        print(f"! {email} already has an account — no invite sent; granting access instead.")
        print(f"  To get them in, send a reset: make reset-password EMAIL={email} PRODUCT={a.product}")
    else:
        print(f"ERROR invite failed (HTTP {st}): {text[:300]}", file=sys.stderr)
        return 1
    uid = d.get("id") if isinstance(d, dict) else None
    print(grant(env, email, tool, False, user_id=uid))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

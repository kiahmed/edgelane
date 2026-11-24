#!/usr/bin/env python3
"""Provision the Level Engine's Postgres roles + secrets (docs/level_engine.md §5.1).

Run by tools/db_push.py after the migrations apply. Migration 0035 creates the
login roles `facades_levels_engine_rw` / `facades_levels_engine_replay` (only
if missing) WITHOUT passwords. Postgres stores only a hash, so a password can
never be read back — the config file is the only copy. Hence, per role:

  * its DSN key (LVL_DB_URL / LVL_REPLAY_DB_URL) in facades_levels_engine.config
    is non-empty  -> left alone, no DB change (idempotent re-runs do nothing);
  * it is empty     -> a fresh random password is set on the role (this also
    covers "role exists but config lost" = rotate) and the DSN is written.

The password leaves this machine only as a SCRAM-SHA-256 verifier (Postgres
accepts pre-hashed passwords), so it never appears in a SQL statement or a
server log. DSN host = the project's Supavisor pooler in SESSION mode on 5432
(psycopg 3 server-side prepares break under transaction-mode 6543), user
`<role>.<ref>`; if the pooler lookup fails, the direct host db.<ref>.supabase.co.

INTERNAL_FEED_TOKEN is kept identical in facades_levels_engine.config and
edgelane_market.config (generated if both are empty; otherwise the set one is
copied to the empty one). No other key of edgelane_market.config is touched.

Secrets are never printed. Config files are written atomically; the levels
config with mode 0600.

Standalone: python3 tools/levels_provision.py [--dry-run]
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import sys
import tempfile
import urllib.request
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parents[1]
LEVELS_DIR = ROOT / "market" / "backend" / "level_engine"
LEVELS_CONFIG = LEVELS_DIR / "facades_levels_engine.config"
LEVELS_EXAMPLE = LEVELS_DIR / "facades_levels_engine.config.example"
MARKET_CONFIG = ROOT / "edgelane_market.config"

# role -> the levels-config key holding its DSN
ROLES = (
    ("facades_levels_engine_rw", "LVL_DB_URL"),
    ("facades_levels_engine_replay", "LVL_REPLAY_DB_URL"),
)
FEED_KEY = "INTERNAL_FEED_TOKEN"

_URLSAFE = re.compile(r"^[A-Za-z0-9_-]{32,}$")          # secrets.token_urlsafe output
_ROLE_RX = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")
_REF_RX = re.compile(r"^[a-z0-9]{6,40}$")
_VERIFIER_RX = re.compile(r"^SCRAM-SHA-256\$\d+:[A-Za-z0-9+/=]+\$[A-Za-z0-9+/=]+:[A-Za-z0-9+/=]+$")
_HOST_RX = re.compile(r"^[A-Za-z0-9.-]+$")
_DB_RX = re.compile(r"^[A-Za-z0-9_]+$")
_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

RunSql = Callable[[str, str, str], "tuple[int, str]"]
GetJson = Callable[[str, str], object]


# ── secrets ──────────────────────────────────────────────────────────────
def gen_secret() -> str:
    return secrets.token_urlsafe(32)


def assert_urlsafe(value: str) -> str:
    """Guard before a secret goes anywhere near SQL or a URL."""
    if not isinstance(value, str) or not _URLSAFE.match(value):
        raise ValueError("secret is not url-safe base64 (refusing to use it)")
    return value


def scram_verifier(password: str, salt: bytes | None = None, iterations: int = 4096) -> str:
    """Postgres SCRAM-SHA-256 verifier (the form pg_authid stores), RFC 5802/7677.
    The password is url-safe ASCII, so SASLprep is the identity."""
    assert_urlsafe(password)
    salt = salt if salt is not None else os.urandom(16)
    salted = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    client_key = hmac.new(salted, b"Client Key", hashlib.sha256).digest()
    stored_key = hashlib.sha256(client_key).digest()
    server_key = hmac.new(salted, b"Server Key", hashlib.sha256).digest()
    b64 = lambda b: base64.b64encode(b).decode()  # noqa: E731
    return f"SCRAM-SHA-256${iterations}:{b64(salt)}${b64(stored_key)}:{b64(server_key)}"


def alter_role_sql(role: str, password: str) -> str:
    if not _ROLE_RX.match(role):
        raise ValueError(f"bad role name {role!r}")
    v = scram_verifier(password)
    if not _VERIFIER_RX.match(v) or "'" in v:
        raise ValueError("verifier has unexpected characters")
    return f"alter role {role} with login password '{v}';"


# ── KEY=VALUE files ──────────────────────────────────────────────────────
_LINE_RX = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$")


def _value(raw: str) -> str:
    v = raw.strip()
    if " #" in v:
        v = v.split(" #", 1)[0].strip()
    elif v.startswith("#"):
        v = ""
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        v = v[1:-1]
    return v.strip()


def read_kv(text: str) -> dict[str, str]:
    """Uncommented KEY=VALUE lines only (a `# KEY=` comment is absent)."""
    out: dict[str, str] = {}
    for line in text.splitlines():
        if line.lstrip().startswith("#"):
            continue
        m = _LINE_RX.match(line)
        if m:
            out[m.group(1).upper()] = _value(m.group(2))
    return out


def update_kv(text: str, updates: dict[str, str], append_comment: str | None = None) -> str:
    """Set each key in place on EVERY uncommented `KEY=` line (comments/order
    kept) — readers take the last occurrence, so a duplicated key must never be
    left with a stale copy. Keys with no such line are appended at the end."""
    lines = text.splitlines()
    done: set[str] = set()
    for i, line in enumerate(lines):
        if line.lstrip().startswith("#"):
            continue
        m = _LINE_RX.match(line)
        if not m:
            continue
        k = m.group(1).upper()
        if k in updates:
            lines[i] = f"{m.group(1)}={updates[k]}"
            done.add(k)
    missing = [k for k in updates if k not in done]
    if missing:
        if lines and lines[-1].strip():
            lines.append("")
        if append_comment:
            lines.append(append_comment)
        lines.extend(f"{k}={updates[k]}" for k in missing)
    return "\n".join(lines) + "\n"


def write_config(path: Path, text: str, mode: int = 0o600) -> None:
    """Write a config file the containers bind-mount as a SINGLE FILE.

    An existing file is rewritten IN PLACE (same inode): a single-file bind
    mount pins the inode at container create, so a replace-by-rename would
    leave running containers reading the old file even after a restart. The
    write is fsync'd and read back to verify. A missing file is created
    atomically (no container can have it mounted yet).
    Note: on WSL drvfs (/mnt/c) chmod is usually ignored, so 0600 is best-effort
    there; the files stay gitignored either way."""
    if not path.exists():
        write_atomic(path, text, mode)
        return
    data = text.encode("utf-8")
    original = path.read_bytes()
    # Keep a copy first (`<file>.bak`, gitignored via *.bak, 0600): the market
    # config holds every live key, so a crash mid-rewrite must stay recoverable.
    write_atomic(path.with_name(path.name + ".bak"), original.decode("utf-8"), 0o600)
    _rewrite_in_place(path, data)
    try:
        os.chmod(path, mode)
    except OSError:
        pass
    if path.read_bytes() != data:
        _rewrite_in_place(path, original)          # restore; the .bak stays as well
        raise OSError(f"verify failed after writing {path.name} — original restored "
                      f"(copy kept at {path.name}.bak)")


def _rewrite_in_place(path: Path, data: bytes) -> None:
    with open(path, "r+b") as f:
        f.seek(0)
        f.write(data)
        f.truncate()
        f.flush()
        os.fsync(f.fileno())


def write_atomic(path: Path, text: str, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        try:
            os.chmod(path, mode)
        except OSError:
            pass
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ── Management API ───────────────────────────────────────────────────────
def _get_json(url: str, token: str) -> object:
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}", "User-Agent": _UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def role_exists(run_sql: RunSql, ref: str, token: str, role: str) -> bool:
    if not _ROLE_RX.match(role):
        raise ValueError(f"bad role name {role!r}")
    _, body = run_sql(ref, token, f"select 1 as ok from pg_roles where rolname = '{role}';")
    try:
        rows = json.loads(body)
    except (TypeError, ValueError):
        return False
    return isinstance(rows, list) and len(rows) > 0


def parse_pooler(payload: object) -> tuple[str, str] | None:
    """(host, db_name) of the PRIMARY database's pooler, from either shape the
    API returns: a list of entries, or one entry object. Host comes from
    `db_host`, else from `connection_string`. Port is ignored (we force 5432)."""
    entries: list = []
    if isinstance(payload, list):
        entries = [e for e in payload if isinstance(e, dict)]
    elif isinstance(payload, dict):
        inner = payload.get("data") if isinstance(payload.get("data"), list) else None
        entries = [e for e in inner if isinstance(e, dict)] if inner else [payload]
    if not entries:
        return None
    primary = [e for e in entries if str(e.get("database_type", "")).upper() == "PRIMARY"]
    for e in (primary or entries):
        host = str(e.get("db_host") or "").strip()
        db = str(e.get("db_name") or "").strip()
        cs = str(e.get("connection_string") or "")
        if not host and cs:
            m = re.search(r"@([^/:@?\s]+)(?::\d+)?(?:/([^?\s]*))?", cs)
            if m:
                host = m.group(1)
                db = db or (m.group(2) or "")
        if host and _HOST_RX.match(host):
            return host, (db if db and _DB_RX.match(db) else "postgres")
    return None


def build_dsn(role: str, ref: str, password: str, pooler: tuple[str, str] | None) -> str:
    assert_urlsafe(password)
    if pooler:
        host, db = pooler
        return f"postgresql://{role}.{ref}:{password}@{host}:5432/{db}?sslmode=require"
    return f"postgresql://{role}:{password}@db.{ref}.supabase.co:5432/postgres?sslmode=require"


# ── provisioning ─────────────────────────────────────────────────────────
def provision(ref: str = "", token: str = "", *, dry_run: bool = False,
              levels_path: Path = LEVELS_CONFIG, example_path: Path = LEVELS_EXAMPLE,
              market_path: Path = MARKET_CONFIG,
              run_sql: RunSql | None = None, get_json: GetJson | None = None,
              out: Callable[[str], None] = print) -> int:
    """Returns 0 on success (incl. clean skips), 1 if a DB change failed."""
    if run_sql is None:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from db_push import _run_sql as run_sql  # noqa: E402  (lazy: db_push imports us)
    get_json = get_json or _get_json
    tag = "[dry-run] " if dry_run else ""
    out(f"==> {tag}level engine provisioning ({levels_path.name})")

    if levels_path.is_file():
        text = levels_path.read_text(encoding="utf-8")
        created = False
    else:
        text = example_path.read_text(encoding="utf-8") if example_path.is_file() else ""
        created = True
    cfg = read_kv(text)
    updates: dict[str, str] = {}
    rc = 0

    # 1-2. role passwords + DSNs
    need = [(role, key) for role, key in ROLES if not cfg.get(key)]
    for role, key in ROLES:
        if cfg.get(key):
            out(f"    {key}: present, unchanged")
    if need and dry_run:
        for role, key in need:
            out(f"    {key}: would set (new password for {role}, if the role exists)")
    elif need:
        if not ref or not token or not _REF_RX.match(ref):
            out("    note: no SUPABASE_PROJECT_REF/ACCESS_TOKEN — role passwords skipped")
            need = []
        pooler = None
        if need:
            try:
                pooler = parse_pooler(get_json(
                    f"https://api.supabase.com/v1/projects/{ref}/config/database/pooler", token))
            except Exception:
                pooler = None
            if pooler is None:
                out(f"    note: pooler lookup failed — using direct host db.{ref}.supabase.co:5432")
        for role, key in need:
            try:
                exists = role_exists(run_sql, ref, token, role)
            except Exception as exc:
                out(f"    {key}: skipped — could not check role {role} ({type(exc).__name__})")
                rc = 1
                continue
            if not exists:
                out(f"    {key}: skipped — role {role} does not exist yet (migration not applied?)")
                continue
            pw = assert_urlsafe(gen_secret())
            try:
                run_sql(ref, token, alter_role_sql(role, pw))
            except Exception as exc:
                out(f"    {key}: FAILED to set password for {role} ({type(exc).__name__})")
                rc = 1
                continue
            updates[key] = build_dsn(role, ref, pw, pooler)
            out(f"    {key}: set (new password for {role})")

    # 3. INTERNAL_FEED_TOKEN (levels config <-> edgelane_market.config)
    lv_tok = cfg.get(FEED_KEY, "")
    mk_text = market_path.read_text(encoding="utf-8") if market_path.is_file() else None
    mk_tok = read_kv(mk_text).get(FEED_KEY, "") if mk_text is not None else ""
    market_write: str | None = None
    if lv_tok and mk_tok:
        out(f"    {FEED_KEY}: present in both, unchanged"
            + ("" if lv_tok == mk_tok else " (WARNING: the two values differ — fix by hand)"))
    elif lv_tok and not mk_tok:
        if mk_text is None:
            out(f"    {FEED_KEY}: present; {market_path.name} not found — not synced")
        else:
            market_write = lv_tok
            out(f"    {FEED_KEY}: {'would copy' if dry_run else 'copied'} to {market_path.name}")
    elif mk_tok:
        updates[FEED_KEY] = mk_tok
        out(f"    {FEED_KEY}: {'would copy' if dry_run else 'copied'} from {market_path.name}")
    else:
        tok = assert_urlsafe(gen_secret())
        updates[FEED_KEY] = tok
        if mk_text is None:
            out(f"    {FEED_KEY}: {'would generate' if dry_run else 'generated'}; "
                f"{market_path.name} not found — set it there by hand")
        else:
            market_write = tok
            out(f"    {FEED_KEY}: {'would generate' if dry_run else 'generated'} "
                f"(both configs)")

    # 4. write
    if dry_run:
        if updates or market_write:
            keys = [k for _, k in need] + sorted(updates) + ([f"{market_path.name}:{FEED_KEY}"] if market_write else [])
            out(f"    would write: {', '.join(keys)}"
                + (f" (creating {levels_path.name} from .example)" if created else ""))
        else:
            out("    nothing to do")
        return rc
    if updates:
        write_config(levels_path, update_kv(text, updates), 0o600)
        out(f"    wrote {levels_path.name}{' (created from .example)' if created else ''}")
    if market_write is not None and mk_text is not None:
        try:
            mode = market_path.stat().st_mode & 0o777
        except OSError:
            mode = 0o600
        write_config(market_path, update_kv(
            mk_text, {FEED_KEY: market_write},
            append_comment="# Level Engine feed secret — written by tools/db_push.py (docs/level_engine.md §3.2)"),
            mode)
        out(f"    wrote {market_path.name} ({FEED_KEY} only)")
    if not updates and market_write is None:
        out("    nothing to do")
    return rc


def main() -> int:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from db_push import ENV_FILE, _load_env, _run_sql  # noqa: E402
    dry = "--dry-run" in sys.argv or "-n" in sys.argv
    env = _load_env(ENV_FILE)
    ref = env.get("SUPABASE_PROJECT_REF") or env.get("SUPABASE_URL", "").split("//")[-1].split(".")[0]
    return provision(ref, env.get("SUPABASE_ACCESS_TOKEN", ""), dry_run=dry, run_sql=_run_sql)


if __name__ == "__main__":
    raise SystemExit(main())

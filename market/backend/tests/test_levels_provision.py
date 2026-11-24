"""tools/levels_provision.py — Level Engine role passwords, DSNs and the feed
token (hermetic: the HTTP layer is faked, configs live in tmp_path)."""
import base64
import hashlib
import hmac
import importlib.util
import json
import os
import stat
from pathlib import Path

import pytest

_P = Path(__file__).resolve().parents[3] / "tools" / "levels_provision.py"
_spec = importlib.util.spec_from_file_location("levels_provision", _P)
lp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lp)

REF = "abcdefghijklmnopqrst"
TOKEN = "sbp_fake"
POOLER_LIST = [
    {"database_type": "READ_REPLICA", "db_host": "replica.pooler.supabase.com", "db_name": "postgres"},
    {"database_type": "PRIMARY", "db_host": "aws-0-us-east-1.pooler.supabase.com", "db_port": 6543,
     "db_name": "postgres", "db_user": f"postgres.{REF}", "pool_mode": "transaction",
     "connection_string": f"postgres://postgres.{REF}:[YOUR-PASSWORD]@aws-0-us-east-1.pooler.supabase.com:6543/postgres"},
]
EXAMPLE = """# Level Engine — template
BACKEND_FEED_URL=http://edgelane-backend:8789/internal/feed
INTERNAL_FEED_TOKEN=
SYMBOL=SPX
# least-privilege Postgres roles
LVL_DB_URL=
LVL_REPLAY_DB_URL=
READ_PORT=8791
"""
MARKET = """# backend config
DEVMODE=true
TRADIER_TOKEN=keepme   # inline comment
# LEVEL_FEED_ENABLED=false
# INTERNAL_FEED_TOKEN=
"""


class Fake:
    """Records every SQL statement / GET; roles in `roles` exist."""

    def __init__(self, roles=("facades_levels_engine_rw", "facades_levels_engine_replay"),
                 pooler=POOLER_LIST, alter_fails=False):
        self.roles, self.pooler, self.alter_fails = set(roles), pooler, alter_fails
        self.sql: list[str] = []
        self.gets: list[str] = []

    def run_sql(self, ref, token, sql):
        assert ref == REF and token == TOKEN
        self.sql.append(sql)
        if "from pg_roles" in sql:
            role = sql.split("rolname = '")[1].split("'")[0]
            return 201, json.dumps([{"ok": 1}] if role in self.roles else [])
        if sql.startswith("alter role"):
            if self.alter_fails:
                raise RuntimeError("boom")
            return 201, "[]"
        raise AssertionError(f"unexpected sql {sql}")

    def get_json(self, url, token):
        self.gets.append(url)
        if isinstance(self.pooler, Exception):
            raise self.pooler
        return self.pooler

    @property
    def alters(self):
        return [s for s in self.sql if s.startswith("alter role")]


@pytest.fixture
def paths(tmp_path):
    ex = tmp_path / "facades_levels_engine.config.example"
    ex.write_text(EXAMPLE)
    mk = tmp_path / "edgelane_market.config"
    mk.write_text(MARKET)
    return {"levels_path": tmp_path / "facades_levels_engine.config", "example_path": ex,
            "market_path": mk}


def _run(paths, fake, dry_run=False, lines=None):
    lines = [] if lines is None else lines
    rc = lp.provision(REF, TOKEN, dry_run=dry_run, run_sql=fake.run_sql, get_json=fake.get_json,
                      out=lines.append, **paths)
    return rc, lines


def _kv(p):
    return lp.read_kv(Path(p).read_text())


# ── fresh provisioning ───────────────────────────────────────────────────
def test_new_passwords_and_dsns_written_when_empty(paths, capsys):
    fake = Fake()
    rc, lines = _run(paths, fake)
    assert rc == 0
    cfg = _kv(paths["levels_path"])
    for role, key in lp.ROLES:
        dsn = cfg[key]
        assert dsn.startswith(f"postgresql://{role}.{REF}:")
        assert dsn.endswith("@aws-0-us-east-1.pooler.supabase.com:5432/postgres?sslmode=require")
    assert len(fake.alters) == 2
    # the plaintext password never goes to the DB — only a SCRAM verifier
    for (role, key), sql in zip(lp.ROLES, fake.alters):
        pw = cfg[key].split(":", 2)[2].split("@")[0]
        assert pw not in sql and "SCRAM-SHA-256$4096:" in sql and f"alter role {role} " in sql
    # template comments/order preserved; created from the example
    text = paths["levels_path"].read_text()
    assert text.startswith("# Level Engine — template\n")
    assert text.index("SYMBOL=SPX") < text.index("LVL_DB_URL=") < text.index("READ_PORT=8791")
    # secrets never printed
    printed = "\n".join(lines) + capsys.readouterr().out
    for v in (cfg["LVL_DB_URL"], cfg["LVL_REPLAY_DB_URL"], cfg["INTERNAL_FEED_TOKEN"]):
        assert v and v.split("@")[0] not in printed
        assert v.split(":", 2)[-1].split("@")[0] not in printed
    assert "    LVL_DB_URL: set (new password for facades_levels_engine_rw)" in lines


def test_secrets_not_in_real_stdout(paths, capsys):
    fake = Fake()
    assert lp.provision(REF, TOKEN, run_sql=fake.run_sql, get_json=fake.get_json, **paths) == 0
    printed = capsys.readouterr()
    cfg = _kv(paths["levels_path"])
    secrets_ = [cfg["INTERNAL_FEED_TOKEN"]] + [cfg[k].split(":", 2)[2].split("@")[0] for _, k in lp.ROLES]
    assert all(s and s not in printed.out + printed.err for s in secrets_)
    assert "LVL_REPLAY_DB_URL: set (new password for facades_levels_engine_replay)" in printed.out


def test_file_mode_0600(paths):
    _run(paths, Fake())
    mode = stat.S_IMODE(os.stat(paths["levels_path"]).st_mode)
    if os.name == "posix" and mode != 0o600 and str(paths["levels_path"]).startswith("/mnt/"):
        pytest.skip("filesystem ignores chmod")
    assert mode == 0o600
    assert not [p for p in paths["levels_path"].parent.iterdir() if p.name.endswith(".tmp")]


# ── idempotency / rotation ───────────────────────────────────────────────
def test_untouched_when_present(paths):
    _run(paths, Fake())
    before = paths["levels_path"].read_text()
    mk_before = paths["market_path"].read_text()
    fake = Fake()
    rc, lines = _run(paths, fake)
    assert rc == 0
    assert fake.sql == [] and fake.gets == []            # no DB contact at all
    assert paths["levels_path"].read_text() == before
    assert paths["market_path"].read_text() == mk_before
    assert "    nothing to do" in lines


def test_rotate_when_role_exists_but_config_empty(paths):
    _run(paths, Fake())
    old = _kv(paths["levels_path"])
    text = paths["levels_path"].read_text().replace(f"LVL_DB_URL={old['LVL_DB_URL']}", "LVL_DB_URL=")
    paths["levels_path"].write_text(text)
    fake = Fake()
    rc, _ = _run(paths, fake)
    new = _kv(paths["levels_path"])
    assert rc == 0 and len(fake.alters) == 1 and "facades_levels_engine_rw" in fake.alters[0]
    assert new["LVL_DB_URL"] and new["LVL_DB_URL"] != old["LVL_DB_URL"]
    assert new["LVL_REPLAY_DB_URL"] == old["LVL_REPLAY_DB_URL"]
    assert new["INTERNAL_FEED_TOKEN"] == old["INTERNAL_FEED_TOKEN"]


def test_skips_cleanly_when_roles_missing(paths):
    fake = Fake(roles=())
    rc, lines = _run(paths, fake)
    assert rc == 0 and fake.alters == []
    cfg = _kv(paths["levels_path"])
    assert cfg["LVL_DB_URL"] == "" and cfg["LVL_REPLAY_DB_URL"] == ""
    assert any("does not exist yet" in line for line in lines)


def test_alter_failure_leaves_dsn_empty_and_fails(paths):
    rc, _ = _run(paths, Fake(alter_fails=True))
    assert rc == 1
    assert _kv(paths["levels_path"])["LVL_DB_URL"] == ""


# ── dry run ──────────────────────────────────────────────────────────────
def test_dry_run_changes_nothing(paths):
    mk_before = paths["market_path"].read_text()
    fake = Fake()
    rc, lines = _run(paths, fake, dry_run=True)
    assert rc == 0
    assert fake.sql == [] and fake.gets == []
    assert not paths["levels_path"].exists()
    assert paths["market_path"].read_text() == mk_before
    joined = "\n".join(lines)
    assert "LVL_DB_URL: would set (new password for facades_levels_engine_rw" in joined
    assert "would write:" in joined


# ── pooler parsing + fallback ────────────────────────────────────────────
def test_pooler_list_shape_picks_primary():
    assert lp.parse_pooler(POOLER_LIST) == ("aws-0-us-east-1.pooler.supabase.com", "postgres")


def test_pooler_object_shape_and_connection_string_only():
    obj = {"connection_string": f"postgresql://postgres.{REF}:[YOUR-PASSWORD]@aws-1-eu.pooler.supabase.com:6543/mydb",
           "pool_mode": "session"}
    assert lp.parse_pooler(obj) == ("aws-1-eu.pooler.supabase.com", "mydb")
    assert lp.parse_pooler({"db_host": "h.pooler.supabase.com"}) == ("h.pooler.supabase.com", "postgres")
    assert lp.parse_pooler({"data": POOLER_LIST})[0] == "aws-0-us-east-1.pooler.supabase.com"


@pytest.mark.parametrize("bad", [None, [], {}, "x", [{"db_host": "bad host;"}], [{"foo": 1}]])
def test_pooler_garbage_returns_none(bad):
    assert lp.parse_pooler(bad) is None


@pytest.mark.parametrize("pooler", [RuntimeError("down"), [], {"nope": 1}])
def test_pooler_failure_falls_back_to_direct_host(paths, pooler):
    rc, lines = _run(paths, Fake(pooler=pooler))
    assert rc == 0
    dsn = _kv(paths["levels_path"])["LVL_DB_URL"]
    assert dsn.startswith("postgresql://facades_levels_engine_rw:")
    assert dsn.endswith(f"@db.{REF}.supabase.co:5432/postgres?sslmode=require")
    assert sum("pooler lookup failed" in line for line in lines) == 1


# ── INTERNAL_FEED_TOKEN sync ─────────────────────────────────────────────
def test_feed_token_generated_into_both(paths):
    _run(paths, Fake())
    lv = _kv(paths["levels_path"])["INTERNAL_FEED_TOKEN"]
    mk = paths["market_path"].read_text()
    assert lv and lp.read_kv(mk)["INTERNAL_FEED_TOKEN"] == lv
    # nothing else in the market config changed; the commented line stays commented
    assert mk.startswith(MARKET)
    assert "LEVEL_FEED_ENABLED" not in lp.read_kv(mk)
    assert lp.read_kv(mk)["TRADIER_TOKEN"] == "keepme"


def test_feed_token_copied_from_market(paths):
    paths["market_path"].write_text(MARKET + "INTERNAL_FEED_TOKEN=" + "m" * 43 + "\n")
    before = paths["market_path"].read_text()
    _run(paths, Fake())
    assert _kv(paths["levels_path"])["INTERNAL_FEED_TOKEN"] == "m" * 43
    assert paths["market_path"].read_text() == before


def test_feed_token_copied_to_market_in_place(paths):
    paths["levels_path"].write_text(EXAMPLE.replace("INTERNAL_FEED_TOKEN=", "INTERNAL_FEED_TOKEN=" + "L" * 43))
    paths["market_path"].write_text("A=1\nINTERNAL_FEED_TOKEN=\nB=2\n")
    _run(paths, Fake())
    assert paths["market_path"].read_text() == f"A=1\nINTERNAL_FEED_TOKEN={'L' * 43}\nB=2\n"


def test_feed_token_market_missing_not_created(paths):
    paths["market_path"].unlink()
    _run(paths, Fake())
    assert _kv(paths["levels_path"])["INTERNAL_FEED_TOKEN"]
    assert not paths["market_path"].exists()


# ── guards ───────────────────────────────────────────────────────────────
@pytest.mark.parametrize("bad", ["short", "a" * 40 + "'", "a" * 40 + " ", "a" * 40 + "$", "a" * 40 + "/", ""])
def test_non_urlsafe_password_rejected(bad):
    with pytest.raises(ValueError):
        lp.alter_role_sql("facades_levels_engine_rw", bad)
    with pytest.raises(ValueError):
        lp.build_dsn("facades_levels_engine_rw", REF, bad, None)


def test_bad_generated_secret_never_reaches_db(paths, monkeypatch):
    monkeypatch.setattr(lp, "gen_secret", lambda: "x'; drop role postgres; --" + "a" * 20)
    fake = Fake()
    with pytest.raises(ValueError):
        _run(paths, fake)
    assert fake.alters == []


def test_bad_role_name_rejected():
    with pytest.raises(ValueError):
        lp.alter_role_sql("x; drop", "a" * 43)


def test_scram_verifier_matches_rfc7677_exchange():
    """The verifier authenticates the RFC 7677 SCRAM-SHA-256 example exchange."""
    salt = base64.b64decode("W22ZaJ0SNY7soEsUEjb6gQ==")
    # RFC password is "pencil" (not url-safe-length): bypass the guard for the math check
    orig = lp.assert_urlsafe
    lp.assert_urlsafe = lambda v: v
    try:
        v = lp.scram_verifier("pencil", salt=salt)
    finally:
        lp.assert_urlsafe = orig
    head, keys = v.split("$", 1)[1].split("$")
    assert head == "4096:W22ZaJ0SNY7soEsUEjb6gQ=="
    stored_key, server_key = (base64.b64decode(k) for k in keys.split(":"))
    auth = ("n=user,r=rOprNGfwEbeRWgbNEkqO,"
            "r=rOprNGfwEbeRWgbNEkqO%hvYDpWUa2RaTCAfuxFIlj)hNlF$k0,s=W22ZaJ0SNY7soEsUEjb6gQ==,i=4096,"
            "c=biws,r=rOprNGfwEbeRWgbNEkqO%hvYDpWUa2RaTCAfuxFIlj)hNlF$k0").encode()
    proof = base64.b64decode("dHzbZapWIk4jUhN+Ute9ytag9zjfMHgsqmmiz7AndVQ=")
    sig = hmac.new(stored_key, auth, hashlib.sha256).digest()
    client_key = bytes(a ^ b for a, b in zip(proof, sig))
    assert hashlib.sha256(client_key).digest() == stored_key
    assert (base64.b64encode(hmac.new(server_key, auth, hashlib.sha256).digest()).decode()
            == "6rriTRBi23WpRR/wtup+mMhUZUn/dB5nLTJRsjl95G4=")


def test_update_kv_appends_missing_and_keeps_comments():
    out = lp.update_kv("# c\nA=1\n# B=old\n", {"A": "2", "B": "3"})
    assert out == "# c\nA=2\n# B=old\n\nB=3\n"


# ── review fixes: same inode (single-file bind mounts) + duplicate keys ──
def test_existing_configs_rewritten_in_place_same_inode(paths):
    paths["levels_path"].write_text(EXAMPLE)                  # exists → mounted in a container
    lv_ino = paths["levels_path"].stat().st_ino
    mk_ino = paths["market_path"].stat().st_ino
    _run(paths, Fake())
    assert paths["levels_path"].stat().st_ino == lv_ino
    assert paths["market_path"].stat().st_ino == mk_ino
    kv = _kv(paths["levels_path"])
    assert kv["LVL_DB_URL"] and kv["LVL_REPLAY_DB_URL"] and kv["INTERNAL_FEED_TOKEN"]


def test_write_config_shrinking_content_truncates(tmp_path):
    p = tmp_path / "c.config"
    p.write_text("A=" + "x" * 200 + "\n")
    lp.write_config(p, "A=1\n")
    assert p.read_text() == "A=1\n"


def test_update_kv_updates_every_duplicate():
    out = lp.update_kv("K=old\n# K=commented\nX=1\nK=older\n", {"K": "new"})
    assert out == "K=new\n# K=commented\nX=1\nK=new\n"
    assert lp.read_kv(out)["K"] == "new"


def test_write_config_keeps_bak_copy(tmp_path):
    p = tmp_path / "c.config"
    p.write_text("SECRET=orig\n")
    lp.write_config(p, "SECRET=new\n")
    assert p.read_text() == "SECRET=new\n"
    bak = tmp_path / "c.config.bak"
    assert bak.read_text() == "SECRET=orig\n"
    if os.name == "posix" and not str(tmp_path).startswith("/mnt/"):
        assert stat.S_IMODE(bak.stat().st_mode) == 0o600


def test_failed_verify_restores_original_and_keeps_bak(tmp_path, monkeypatch):
    p = tmp_path / "c.config"
    p.write_text("TRADIER_TOKEN=live\nX=1\n")
    real = lp._rewrite_in_place
    calls = []

    def short_write(path, data):                   # simulate a short write once
        calls.append(data)
        real(path, data[: len(data) // 2] if len(calls) == 1 else data)

    monkeypatch.setattr(lp, "_rewrite_in_place", short_write)
    with pytest.raises(OSError, match="original restored"):
        lp.write_config(p, "TRADIER_TOKEN=live\nX=1\nINTERNAL_FEED_TOKEN=abc\n")
    assert p.read_text() == "TRADIER_TOKEN=live\nX=1\n"
    assert (tmp_path / "c.config.bak").read_text() == "TRADIER_TOKEN=live\nX=1\n"

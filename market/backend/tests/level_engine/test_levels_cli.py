"""levels_cli — parsers (CSV + bulk text) and the HTTP client, end-to-end against
the admin app through a fake urlopen (no sockets)."""
from __future__ import annotations

import io
import json
import urllib.error
import urllib.parse
from datetime import date

import pytest
from fastapi.testclient import TestClient

from level_engine import levels_cli as cli
from level_engine.admin import create_admin_app
from level_engine.config import LevelConfig
from level_engine.store import MemoryStore

TODAY = date(2026, 11, 20)


# ---- bulk text grammar ------------------------------------------------------
def test_parse_bulk_text_grammar():
    text = ("SPY 595.20 PM high\n"
            "spx 6012.5 Golden Gate\n"
            "\n# skipped\n"
            "SPY 595.2 PM high [Support]\n"
            "SPX $6,001.25 Big round [pivot] | only after 10:00\n"
            "SPY 594 name with [brackets] inside\n")
    rows, errors = cli.parse_bulk_text(text)
    assert errors == []
    assert [(r["line"], r["symbol"], r["price"], r["name"], r["kind"], r["condition_text"]) for r in rows] == [
        (1, "SPY", 595.2, "PM high", None, None),
        (2, "SPX", 6012.5, "Golden Gate", None, None),
        (5, "SPY", 595.2, "PM high", "support", None),
        (6, "SPX", 6001.25, "Big round", "pivot", "only after 10:00"),
        (7, "SPY", 594.0, "name with [brackets] inside", None, None),
    ]


@pytest.mark.parametrize("line,frag", [
    ("QQQ 500 x", "SPY or SPX"),
    ("SPY abc x", "not a number"),
    ("SPY -1 x", "positive"),
    ("SPY 595", "expected"),
    ("SPY 595 x [sideways]", "kind"),
])
def test_parse_bulk_line_errors(line, frag):
    rows, errors = cli.parse_bulk_text("SPY 595 ok\n" + line)
    assert len(rows) == 1 and errors[0]["line"] == 2 and frag in errors[0]["error"]


# ---- CSV -----------------------------------------------------------------------
def test_parse_csv():
    text = ("# levels for friday\n"
            "Symbol,Price,Name,Kind,Condition_Text\n"
            "SPY,595.20,PM high,support,\n"
            "\n"
            "SPX,,Day high boundary,,\n"
            'SPX,"6,012.5","Golden Gate, upper",resistance,"if VIX < 18"\n')
    assert cli.looks_like_csv(text)
    fmt, rows, errors = cli.parse_levels_file(text)
    assert fmt == "csv" and errors == []
    assert [(r["line"], r["symbol"], r["price"], r["name"], r["kind"], r["condition_text"]) for r in rows] == [
        (3, "SPY", 595.2, "PM high", "support", None),
        (5, "SPX", None, "Day high boundary", None, None),
        (6, "SPX", 6012.5, "Golden Gate, upper", "resistance", "if VIX < 18"),
    ]


def test_parse_csv_minimal_header_and_errors():
    fmt, rows, errors = cli.parse_levels_file("symbol,price,name\nSPY,595,A\nXYZ,1,B\nSPY,1,\n")
    assert fmt == "csv" and len(rows) == 1
    assert [(e["line"], "symbol" in e["error"] or "name" in e["error"]) for e in errors] == [(3, True), (4, True)]
    _, _, errors = cli.parse_levels_file("symbol,price,name,colour\nSPY,595,A,red\n")
    assert "unknown columns" in errors[0]["error"]


def test_text_file_detected_as_text():
    fmt, rows, errors = cli.parse_levels_file("SPY 595.20 PM high\n")
    assert fmt == "text" and rows[0]["name"] == "PM high" and not errors


# ---- HTTP: fake urlopen routed into the admin app ---------------------------
class _Resp:
    def __init__(self, status, body):
        self.status, self._body = status, body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture
def wired(monkeypatch):
    store = MemoryStore()
    app = create_admin_app(store, LevelConfig(), prior_close=lambda s: {"SPY": 595.0, "SPX": 6000.0}[s],
                           today=lambda: TODAY)
    tc = TestClient(app, base_url="http://127.0.0.1:8790")
    seen = []

    def fake_urlopen(req, timeout=None):
        u = urllib.parse.urlsplit(req.full_url)
        headers = dict(req.header_items())
        seen.append((req.get_method(), u.path, headers))
        r = tc.request(req.get_method(), u.path + ("?" + u.query if u.query else ""),
                       content=req.data, headers=headers)
        if r.status_code >= 400:
            raise urllib.error.HTTPError(req.full_url, r.status_code, "err", {}, io.BytesIO(r.content))
        return _Resp(r.status_code, r.content)

    monkeypatch.setattr(cli, "_urlopen", fake_urlopen)
    monkeypatch.delenv(cli.ENV_URL, raising=False)
    return store, seen


def test_cli_import_and_show(wired, tmp_path, capsys):
    store, seen = wired
    f = tmp_path / "levels.csv"
    f.write_text("symbol,price,name,kind\nSPY,595.2,PM high,support\nSPX,6012.5,Golden Gate,\n")

    assert cli.main(["import", str(f), "--dry-run"]) == 0
    assert store.list_levels(TODAY) == []
    assert "would import 2" in capsys.readouterr().out

    assert cli.main(["import", str(f)]) == 0
    assert len(store.list_levels(TODAY)) == 2
    method, path, headers = seen[-1]
    assert (method, path) == ("POST", "/api/levels/bulk")
    assert headers["Origin"] == "http://127.0.0.1:8790"  # satisfies the CSRF origin check

    capsys.readouterr()
    assert cli.main(["show", "--date", TODAY.isoformat()]) == 0
    out = capsys.readouterr().out
    assert "PM high" in out and "Golden Gate" in out and "2 entered" in out

    assert cli.main(["show", "--json"]) == 0
    assert len(json.loads(capsys.readouterr().out)["levels"]) == 2


def test_cli_import_server_refusal_inserts_nothing(wired, tmp_path, capsys):
    store, _ = wired
    f = tmp_path / "levels.txt"
    f.write_text("SPY 595.2 ok\nSPY 700 typo\n")  # 700 is outside ±5% of 595
    assert cli.main(["import", str(f)]) == 1
    assert store.list_levels(TODAY) == []
    assert "line 2" in capsys.readouterr().err


def test_cli_import_local_parse_error_sends_nothing(wired, tmp_path, capsys):
    store, seen = wired
    f = tmp_path / "levels.txt"
    f.write_text("SPY 595.2 ok\nbogus line\n")
    assert cli.main(["import", str(f)]) == 1
    assert seen == [] and store.list_levels(TODAY) == []
    assert "line 2" in capsys.readouterr().err


def test_cli_past_date_refused(wired, tmp_path, capsys):
    f = tmp_path / "levels.txt"
    f.write_text("SPY 595.2 ok\n")
    assert cli.main(["import", str(f), "--date", "2026-11-19"]) == 1
    assert "403" in capsys.readouterr().err


def test_cli_unreachable(monkeypatch, capsys):
    def boom(req, timeout=None):
        raise urllib.error.URLError("connection refused")
    monkeypatch.setattr(cli, "_urlopen", boom)
    assert cli.main(["--url", "http://127.0.0.1:1", "show"]) == 2
    assert "cannot reach" in capsys.readouterr().err


def test_base_url_from_env(monkeypatch):
    monkeypatch.setenv(cli.ENV_URL, "http://localhost:9000/")
    c = cli.AdminClient()
    assert c.base == "http://localhost:9000" and c.origin == "http://localhost:9000"

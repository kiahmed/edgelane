"""Level Engine admin listener (docs/level_engine.md §10, §17) — hermetic."""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest
from fastapi.testclient import TestClient

from level_engine.admin import create_admin_app
from level_engine.config import LevelConfig
from level_engine.store import MemoryStore

BASE = "http://127.0.0.1:8790"
ORIGIN = {"Origin": BASE}
TODAY = date(2026, 11, 20)
PAST = date(2026, 11, 19)
CLOSES = {"SPY": 595.0, "SPX": 6000.0}


@pytest.fixture
def store():
    return MemoryStore()


def make(store, closes=CLOSES, **kw):
    app = create_admin_app(store, LevelConfig(), prior_close=lambda s: closes.get(s),
                           today=lambda: TODAY, **kw)
    return TestClient(app, base_url=BASE, follow_redirects=False)


@pytest.fixture
def client(store):
    return make(store)


def post(client, url, data):
    return client.post(url, data=data, headers=ORIGIN)


# ---------------------------------------------------------------------------
def test_healthz_and_index(client):
    assert client.get("/healthz").json() == {"ok": True}
    r = client.get("/")
    assert r.status_code == 200
    assert "Level Engine Management" in r.text
    assert "No levels for this date" in r.text


def test_security_headers(client):
    r = client.get("/")
    csp = r.headers["content-security-policy"]
    assert "default-src 'none'" in csp and "frame-ancestors 'none'" in csp
    assert "form-action 'self'" in csp and "'unsafe-inline'" not in csp
    assert r.headers["x-frame-options"] == "DENY"
    assert r.headers["x-content-type-options"] == "nosniff"
    nonce = csp.split("'nonce-")[1].split("'")[0]
    assert f"nonce='{nonce}'" in r.text


@pytest.mark.parametrize("hdr", ["Cf-Ray", "Cf-Connecting-Ip"])
def test_tunnel_headers_404(client, hdr):
    assert client.get("/", headers={hdr: "x"}).status_code == 404
    assert client.get("/healthz", headers={hdr: "x"}).status_code == 404
    r = client.post("/api/levels", json={"symbol": "SPY", "price": 595, "name": "x"},
                    headers={**ORIGIN, hdr: "1.2.3.4"})
    assert r.status_code == 404


def test_unknown_host_refused(store):
    app = create_admin_app(store, LevelConfig(), today=lambda: TODAY)
    c = TestClient(app, base_url="http://evil.example:8790")
    assert c.get("/").status_code == 400
    assert c.get("/healthz").status_code == 200  # container probe


# ---- CSRF -----------------------------------------------------------------
def test_csrf_cross_origin_post_rejected(client, store):
    form = {"symbol": "SPY", "price": "595.2", "name": "PM high"}
    assert client.post("/levels", data=form, headers={"Origin": "http://evil.example"}).status_code == 403
    assert client.post("/levels", data=form, headers={"Origin": "http://127.0.0.1:9999"}).status_code == 403
    assert client.post("/levels", data=form, headers={"Origin": "null"}).status_code == 403
    assert client.post("/levels", data=form).status_code == 403            # no Origin/Referer
    assert client.delete("/api/levels/x").status_code == 403
    assert store.list_levels(TODAY) == []
    # Referer fallback (same origin) is accepted
    r = client.post("/levels", data=form, headers={"Referer": BASE + "/?date=2026-11-20"})
    assert r.status_code == 303
    assert len(store.list_levels(TODAY)) == 1


# ---- form writes ----------------------------------------------------------
def test_form_add_edit_delete(client, store):
    r = post(client, "/levels", {"date": TODAY.isoformat(), "symbol": "spy", "price": "595.20",
                                 "name": "PM high", "kind": "support", "condition_text": ""})
    assert r.status_code == 303 and r.headers["location"].startswith("/?msg=")
    [lv] = store.list_levels(TODAY)
    assert (lv["symbol"], lv["price"], lv["name"], lv["kind"], lv["source"]) == \
        ("SPY", 595.2, "PM high", "support", "entered")
    page = client.get("/").text
    assert "PM high" in page and "595.20" in page

    r = post(client, f"/levels/{lv['id']}/edit", {"symbol": "SPY", "price": "596", "name": "PM hi",
                                                  "kind": "", "condition_text": "vix<18"})
    assert r.status_code == 303
    lv2 = store.get_level(lv["id"])
    assert (lv2["price"], lv2["name"], lv2["kind"], lv2["condition_text"]) == (596.0, "PM hi", None, "vix<18")

    r = post(client, f"/levels/{lv['id']}/delete", {})
    assert r.status_code == 303
    assert store.list_levels(TODAY) == []
    assert [a["action"] for a in store.list_audit(TODAY)] == ["add", "edit", "delete"]


def test_form_validation_error_rerenders(client, store):
    r = post(client, "/levels", {"symbol": "QQQ", "price": "500", "name": "x"})
    assert r.status_code == 422 and "symbol must be SPY or SPX" in r.text
    r = post(client, "/levels", {"symbol": "SPY", "price": "", "name": "x"})
    assert r.status_code == 422 and "need a price" in r.text
    assert store.list_levels(TODAY) == []
    assert post(client, "/levels/nope/edit", {"name": "x"}).status_code == 404


def test_form_bulk_and_one_bad_line_inserts_nothing(client, store):
    good = "SPY 595.20 PM high\nSPX 6012.5 Golden Gate\n# comment\n\nSPY 594.1 ON low [pivot] | if gap"
    r = post(client, "/levels/bulk", {"date": TODAY.isoformat(), "text": good})
    assert r.status_code == 303
    rows = store.list_levels(TODAY)
    assert [(x["symbol"], x["price"], x["name"], x["kind"]) for x in rows] == [
        ("SPY", 595.2, "PM high", None), ("SPX", 6012.5, "Golden Gate", None),
        ("SPY", 594.1, "ON low", "pivot")]
    assert rows[2]["condition_text"] == "if gap"

    bad = "SPY 595.5 A\nSPY abc B\nSPX 6001 C [sideways]\nSPY 700 D"
    r = post(client, "/levels/bulk", {"text": bad})
    assert r.status_code == 422
    assert "line 2" in r.text and "line 3" in r.text and "line 4" in r.text
    assert "SPY abc B" in r.text  # textarea keeps the paste
    assert len(store.list_levels(TODAY)) == 3  # nothing from the bad batch


# ---- JSON API ---------------------------------------------------------------
def test_api_crud(client, store):
    r = client.post("/api/levels", json={"symbol": "SPY", "price": 595.2, "name": "PM high",
                                         "kind": "resistance", "source": "converted", "ratio": 9},
                    headers=ORIGIN)
    assert r.status_code == 201, r.text
    lv = r.json()
    assert lv["source"] == "entered" and lv["ratio"] is None  # engine-owned fields ignored
    assert lv["warning"] is None

    r = client.patch(f"/api/levels/{lv['id']}", json={"price": "596.10"}, headers=ORIGIN)
    assert r.status_code == 200 and r.json()["price"] == 596.1

    r = client.get("/api/levels", params={"date": TODAY.isoformat()})
    body = r.json()
    assert body["trade_date"] == "2026-11-20" and body["read_only"] is False
    assert [x["name"] for x in body["levels"]] == ["PM high"]

    assert client.delete(f"/api/levels/{lv['id']}", headers=ORIGIN).json()["ok"] is True
    assert client.delete(f"/api/levels/{lv['id']}", headers=ORIGIN).status_code == 404
    assert client.get("/api/levels").json()["levels"] == []


def test_api_bulk_rows_text_dry_run(client, store):
    rows = [{"line": 2, "symbol": "SPY", "price": 595.2, "name": "A"},
            {"line": 3, "symbol": "SPX", "price": 6010, "name": "B", "kind": "pivot"}]
    r = client.post("/api/levels/bulk", json={"rows": rows, "dry_run": True}, headers=ORIGIN)
    assert r.status_code == 200 and len(r.json()["levels"]) == 2
    assert store.list_levels(TODAY) == []

    r = client.post("/api/levels/bulk", json={"rows": rows}, headers=ORIGIN)
    assert r.status_code == 201 and len(store.list_levels(TODAY)) == 2

    r = client.post("/api/levels/bulk", json={"text": "SPY 595 X\nSPY 1 Y"}, headers=ORIGIN)
    assert r.status_code == 422
    assert r.json()["errors"][0]["line"] == 2
    assert len(store.list_levels(TODAY)) == 2

    r = client.post("/api/levels/bulk", json={"text": ""}, headers=ORIGIN)
    assert r.status_code == 422


# ---- §17 guard --------------------------------------------------------------
def test_price_guard(client, store):
    def add(sym, price):
        return client.post("/api/levels", json={"symbol": sym, "price": price, "name": "g"},
                           headers=ORIGIN)
    assert add("SPY", 595 * 1.05).status_code == 201
    assert add("SPY", 595 * 0.95).status_code == 201
    r = add("SPY", 595 * 1.051)
    assert r.status_code == 422 and "prior close" in r.json()["error"]
    assert add("SPX", 5000).status_code == 422
    # edit is guarded too
    lv = store.list_levels(TODAY)[0]
    r = client.patch(f"/api/levels/{lv['id']}", json={"price": 59.5}, headers=ORIGIN)
    assert r.status_code == 422


def test_no_prior_close_accepts_with_warning(store):
    c = make(store, closes={})
    r = c.post("/api/levels", json={"symbol": "SPY", "price": 123.0, "name": "far"}, headers=ORIGIN)
    assert r.status_code == 201 and "guard not applied" in r.json()["warning"]
    assert "unverified" in c.get("/").text
    assert c.get("/api/levels").json()["levels"][0]["warning"]


# ---- past dates -------------------------------------------------------------
def _seed_past(store):
    lv = store.add_level(PAST, {"symbol": "SPY", "price": 590.0, "name": "Old high"}, today=PAST)
    store.insert("legs", {"id": "L1", "trade_date": PAST, "opened_at": datetime(2026, 11, 19, 15, tzinfo=timezone.utc),
                          "call": "up", "target_level_id": lv["id"], "invalidation_level_id": None,
                          "entry_spot": 5950.0, "result": "win", "progress_pct": 100.0})
    store.upsert_day_summary({"trade_date": PAST, "close": 5990.25, "final_zone": "above Old high",
                              "levels_outcome": {lv["id"]: "held_support"}, "legs": {"win": 1},
                              "notes": "clean day"})
    return lv


def test_past_date_read_only_view(client, store):
    lv = _seed_past(store)
    r = client.get("/", params={"date": PAST.isoformat()})
    assert r.status_code == 200
    t = r.text
    assert "read-only" in t and "Old high" in t
    assert "held_support" in t and "5,990.25" in t and "clean day" in t
    assert "win" in t and "Audit trail (1)" in t
    assert "<form method=post" not in t and "<textarea" not in t
    assert client.get("/api/levels", params={"date": PAST.isoformat()}).json()["read_only"] is True

    # every write path refused
    assert post(client, "/levels", {"date": PAST.isoformat(), "symbol": "SPY", "price": "591",
                                    "name": "x"}).status_code == 403
    assert post(client, "/levels/bulk", {"date": PAST.isoformat(), "text": "SPY 591 x"}).status_code == 403
    assert post(client, f"/levels/{lv['id']}/edit", {"name": "y"}).status_code == 403
    assert post(client, f"/levels/{lv['id']}/delete", {}).status_code == 403
    assert client.patch(f"/api/levels/{lv['id']}", json={"name": "y"}, headers=ORIGIN).status_code == 403
    assert client.delete(f"/api/levels/{lv['id']}", headers=ORIGIN).status_code == 403
    assert client.post("/api/levels", json={"date": PAST.isoformat(), "symbol": "SPY", "price": 591,
                                            "name": "x"}, headers=ORIGIN).status_code == 403
    assert client.post("/api/levels/bulk", json={"date": PAST.isoformat(), "text": "SPY 591 x"},
                       headers=ORIGIN).status_code == 403
    assert store.get_level(lv["id"])["name"] == "Old high"
    assert len(store.list_levels(PAST)) == 1


def test_past_date_empty_renders(client):
    r = client.get("/", params={"date": "2026-01-02"})
    assert r.status_code == 200
    assert "No day summary yet" in r.text and "No legs recorded" in r.text
    assert client.get("/", params={"date": "garbage"}).status_code == 400


# ---- escaping / converted --------------------------------------------------
def test_html_escapes_script_name(client, store):
    store.add_level(TODAY, {"symbol": "SPY", "price": 595.0, "name": "<script>alert(1)</script>",
                            "condition_text": "\"'><img src=x>"}, today=TODAY)
    t = client.get("/").text
    assert "<script>alert(1)</script>" not in t
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in t
    assert "<img src=x>" not in t


def test_converted_shown_and_not_editable(client, store):
    lv = store.add_level(TODAY, {"symbol": "SPY", "price": 595.0, "name": "PM high"}, today=TODAY)
    [cv] = store.replace_converted(TODAY, [{"price": 6005.0, "name": "PM high", "ratio": 10.0924,
                                            "converted_from": lv["id"]}], today=TODAY)
    t = client.get("/").text
    assert "6,005.00" in t and "10.0924" in t
    body = client.get("/api/levels").json()
    assert body["levels"][0]["converted"]["price"] == 6005.0
    assert body["ratio"] == {"value": 10.0924, "locked": False}
    assert client.patch(f"/api/levels/{cv['id']}", json={"name": "x"}, headers=ORIGIN).status_code == 409


def test_converted_view_callable(store):
    lv = store.add_level(TODAY, {"symbol": "SPY", "price": 595.0, "name": "PM high"}, today=TODAY)
    view = lambda d: [{"id": "c1", "symbol": "SPX", "price": 6004.5, "name": "PM high",
                       "ratio": 10.0916, "converted_from": lv["id"], "source": "converted"}]
    c = make(store, converted_view=view)
    assert "6,004.50" in c.get("/").text

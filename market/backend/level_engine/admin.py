"""Level Engine Management — the admin listener (docs/level_engine.md §3.1, §10, §17).

Served on its own ASGI app: container 0.0.0.0:8790, published to the host as
127.0.0.1:8790 only — never proxied, never on the tunnel. No user auth; instead:

* **Tunnel refusal** — any request carrying `Cf-Ray` / `Cf-Connecting-Ip` gets 404.
* **Host allow-list** — `Host` must be 127.0.0.1 / localhost / ::1 (plus
  $LEVEL_ADMIN_ALLOWED_HOSTS, comma-separated) so a DNS-rebinding page can't
  talk to it under its own hostname. `/healthz` is exempt (container probes).
* **CSRF: same-origin check** — every non-GET request must carry an `Origin`
  (or, failing that, `Referer`) whose host:port equals `Host`; otherwise 403.
  Browsers always send `Origin` on form POSTs, so a page on another origin
  (another local port included) can't write. The CLI sends `Origin` = its base
  URL. (Chosen over a form token: works for the JSON API with no token plumbing.)
* **Headers** — strict CSP (`default-src 'none'`, nonce'd inline style/script,
  `form-action 'self'`, `frame-ancestors 'none'`), `X-Frame-Options: DENY`,
  nosniff, `Cache-Control: no-store`, same-origin COOP/CORP.

Pages / endpoints
  GET  /                      today's levels (editable); ?date=YYYY-MM-DD → that day
                              (read-only when before today: levels, outcomes, legs,
                              day summary, audit trail)
  POST /levels                add            (form; 303 back)
  POST /levels/{id}/edit      edit           (form; 303 back)
  POST /levels/{id}/delete    delete         (form; 303 back)
  POST /levels/bulk           bulk paste     (form; all-or-nothing; 303 back)
  GET  /api/levels?date=      JSON list
  POST /api/levels            JSON add       → 201
  PATCH /api/levels/{id}      JSON edit
  DELETE /api/levels/{id}     JSON delete
  POST /api/levels/bulk       JSON bulk {date?, rows:[…] | text:"…", dry_run?}
  GET  /healthz               {"ok": true}

Bulk grammar: see `levels_cli` (shared parser) — `SYMBOL PRICE NAME [kind] [| condition]`.
Writes for a trade_date before today are refused (403). Every write is audited
by the store.

Typo check (§17, soft — the hard ±5% guard was removed 2026-11-23): when the
reference price (`prior_close(symbol)` → feed.prior_close = latest trade) is
known and a NEW or EDITED level's price is more than ±TYPO_CHECK_PCT % from it,
the write is NOT applied until confirmed. Forms re-render with the notice and a
"Save anyway" button that re-posts the same values with `confirm=1`; the JSON
API answers 409 {error, needs_confirm: true, flagged:[{line?, symbol, price,
last, pct_off, message}]} — resend with `"confirm": true`. Bulk stays
all-or-nothing; `dry_run` returns the flags (200, `flagged`). Unknown reference
or TYPO_CHECK_PCT=0 → no check.
"""
from __future__ import annotations

import html
import json
import math
import os
import secrets
from datetime import date, datetime
from typing import Any, Callable, Iterable
from urllib.parse import parse_qs, urlencode, urlsplit

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from .clock import ET, today_et
from .config import LevelConfig
from .levels_cli import GRAMMAR, USER_FIELDS, parse_bulk_text, parse_price
from .store import KINDS, SYMBOLS, LevelValidationError, PastDateError, Store, validate_level_fields

DEFAULT_ALLOWED_HOSTS = ("127.0.0.1", "localhost", "::1")
ENV_ALLOWED_HOSTS = "LEVEL_ADMIN_ALLOWED_HOSTS"
MAX_BODY = 256 * 1024
_SAFE_METHODS = ("GET", "HEAD", "OPTIONS")


class _BadRequest(ValueError):
    pass


class _Conflict(RuntimeError):
    pass


class _NeedsConfirm(Exception):
    """Price(s) far from the last trade — not applied until resent with confirm."""

    def __init__(self, flagged: list[dict], fields: dict | None = None):
        msgs = "; ".join(f["message"] for f in flagged)
        super().__init__(f"{msgs} — not saved; confirm to save anyway")
        self.flagged = flagged
        self.fields = fields


def typo_message(symbol: str, price: float, last: float, pct_off: float) -> str:
    """'SPY 72.48 is 90% below the last trade 722.29 — typo?'"""
    side = "below" if pct_off < 0 else "above"
    return (f"{symbol} {_num(price)} is {abs(pct_off):g}% {side} the last trade "
            f"{_num(last)} — typo?")


def _truthy(v: Any) -> bool:
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on")
    return v is True or (isinstance(v, int) and not isinstance(v, bool) and v == 1)


class _BulkError(LevelValidationError):
    def __init__(self, errors: list[dict]):
        super().__init__(f"{len(errors)} bad line(s) - nothing imported")
        self.errors = errors


def _status_for(exc: Exception) -> int:
    if isinstance(exc, _NeedsConfirm):
        return 409
    if isinstance(exc, PastDateError):
        return 403
    if isinstance(exc, KeyError):
        return 404
    if isinstance(exc, _Conflict):
        return 409
    if isinstance(exc, LevelValidationError):
        return 422
    return 400


def _msg(exc: Exception) -> str:
    if isinstance(exc, KeyError):
        return "level not found"
    return str(exc)


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------
def e(v: Any) -> str:
    return html.escape("" if v is None else str(v), quote=True)


def _num(v: Any, nd: int = 2) -> str:
    if v is None or v == "":
        return "—"
    try:
        return f"{float(v):,.{nd}f}"
    except (TypeError, ValueError):
        return str(v)


def _ts(v: Any) -> str:
    if isinstance(v, datetime):
        if v.tzinfo is not None:
            v = v.astimezone(ET)
        return v.strftime("%Y-%m-%d %H:%M:%S ET")
    return "—" if v is None else str(v)


def _as_date(v: Any) -> date:
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    return date.fromisoformat(str(v))


def _hostname(host_header: str) -> str:
    try:
        return (urlsplit("//" + (host_header or "")).hostname or "").lower()
    except ValueError:
        return ""


def _same_origin(headers) -> bool:
    host = (headers.get("host") or "").lower()
    src = headers.get("origin")
    if not src or src == "null":
        src = headers.get("referer")
    if not src or not host:
        return False
    u = urlsplit(src)
    return u.scheme in ("http", "https") and u.netloc.lower() == host


def _user_fields(src: dict) -> dict:
    """Pick + coerce the operator-editable fields; '' → None. Engine-owned
    fields (source, ratio, converted_from, condition_rule) are never accepted."""
    out: dict[str, Any] = {}
    for k in USER_FIELDS:
        if k not in src:
            continue
        v = src[k]
        if isinstance(v, str):
            v = v.strip() or None
        if k == "price" and v is not None:
            if isinstance(v, bool):
                raise LevelValidationError("price must be a number")
            if isinstance(v, (int, float)):
                if not math.isfinite(v) or v <= 0:
                    raise LevelValidationError("price must be a positive number")
                v = float(v)
            else:
                try:
                    v = parse_price(str(v))
                except ValueError as ex:
                    raise LevelValidationError(str(ex)) from None
        if k == "condition_text" and v is not None:
            v = str(v)[:500]
        out[k] = v
    return out


def _outcomes(summary: dict | None) -> dict[str, str]:
    """levels_outcome (jsonb) → {level_id: outcome}. Tolerates dict-of-str,
    dict-of-dict and list-of-dict shapes (phase 2 decides the exact one)."""
    lo = (summary or {}).get("levels_outcome")
    if isinstance(lo, str):
        try:
            lo = json.loads(lo)
        except ValueError:
            return {}
    out: dict[str, str] = {}

    def _val(v):
        if isinstance(v, dict):
            return v.get("outcome") or v.get("state") or json.dumps(v, default=str)
        return v

    if isinstance(lo, dict):
        for k, v in lo.items():
            out[str(k)] = str(_val(v))
    elif isinstance(lo, list):
        for item in lo:
            if isinstance(item, dict) and item.get("level_id") is not None:
                out[str(item["level_id"])] = str(_val(item))
    return out


_CSS = """
:root{--bg:#fff;--fg:#1b1f24;--mut:#5b6470;--line:#d8dde3;--acc:#1f5fbf;--bad:#b42318;
--badbg:#fdecea;--ok:#146c2e;--okbg:#e7f5ec;--warn:#8a5a00;--warnbg:#fff4d6;--hd:#f4f6f8}
@media (prefers-color-scheme:dark){:root{--bg:#121417;--fg:#e6e8eb;--mut:#9aa3ad;--line:#2c3138;
--acc:#6aa1ff;--bad:#ff8a80;--badbg:#3a1d1b;--ok:#7ad69a;--okbg:#17301f;--warn:#ffcf66;
--warnbg:#3a2f12;--hd:#1a1d21}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);
font:14px/1.45 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
main{max-width:1180px;margin:0 auto;padding:16px}
h1{font-size:20px;margin:0 0 4px}h2{font-size:15px;margin:22px 0 8px}
.sub{color:var(--mut);margin:0 0 12px}
.bar{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin:8px 0 12px}
table{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums}
th,td{border-bottom:1px solid var(--line);padding:6px 8px;text-align:left;vertical-align:top}
th{background:var(--hd);font-weight:600;font-size:12px;color:var(--mut)}
td.n{text-align:right;white-space:nowrap}.wrap{overflow-x:auto}
input,select,textarea,button{font:inherit;color:inherit;background:var(--bg);
border:1px solid var(--line);border-radius:4px;padding:4px 6px}
button{cursor:pointer;background:var(--hd)}button.danger{color:var(--bad)}
textarea{width:100%;min-height:110px;font-family:ui-monospace,Menlo,Consolas,monospace}
form.inline{display:inline}.grid{display:flex;flex-wrap:wrap;gap:6px;align-items:end}
.grid label{display:flex;flex-direction:column;font-size:12px;color:var(--mut)}
.msg{padding:8px 10px;border-radius:4px;margin:8px 0}
.msg.err{background:var(--badbg);color:var(--bad)}.msg.ok{background:var(--okbg);color:var(--ok)}
.msg.warn{background:var(--warnbg);color:var(--warn)}.msg ul{margin:4px 0;padding-left:20px}
.badge{display:inline-block;font-size:11px;padding:0 6px;border-radius:9px;margin-left:4px}
.badge.warn{background:var(--warnbg);color:var(--warn)}.badge.ro{background:var(--hd);color:var(--mut)}
.mut{color:var(--mut)}details summary{cursor:pointer;color:var(--acc)}code{font-size:12px}
"""

_JS = """
document.querySelectorAll('form.del').forEach(function(f){
  f.addEventListener('submit',function(ev){if(!confirm('Delete this level?'))ev.preventDefault();});
});
"""


# ---------------------------------------------------------------------------
# app
# ---------------------------------------------------------------------------
def create_admin_app(store: Store, cfg: LevelConfig,
                     prior_close: Callable[[str], float | None] = lambda s: None,
                     today: Callable[[], date] = today_et,
                     converted_view: Callable[[date], list[dict]] | None = None,
                     *, allowed_hosts: Iterable[str] | None = None) -> FastAPI:
    app = FastAPI(title="Level Engine Management", docs_url=None, redoc_url=None,
                  openapi_url=None)
    if allowed_hosts is None:
        extra = [h.strip() for h in os.environ.get(ENV_ALLOWED_HOSTS, "").split(",") if h.strip()]
        allowed_hosts = (*DEFAULT_ALLOWED_HOSTS, *extra)
    allowed = {h.strip("[]").lower() for h in allowed_hosts}

    # ---- middleware: tunnel refusal, host allow-list, CSRF, headers ----
    def _secure(resp: Response, nonce: str = "") -> Response:
        src = f"'nonce-{nonce}'" if nonce else "'none'"
        resp.headers["Content-Security-Policy"] = (
            f"default-src 'none'; style-src {src}; script-src {src}; form-action 'self'; "
            "frame-ancestors 'none'; base-uri 'none'")
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Referrer-Policy"] = "same-origin"
        resp.headers["Cache-Control"] = "no-store"
        resp.headers["Cross-Origin-Opener-Policy"] = "same-origin"
        resp.headers["Cross-Origin-Resource-Policy"] = "same-origin"
        return resp

    @app.middleware("http")
    async def _guard(request: Request, call_next):
        h = request.headers
        if "cf-ray" in h or "cf-connecting-ip" in h:
            return _secure(Response("Not Found", status_code=404, media_type="text/plain"))
        if request.url.path != "/healthz" and _hostname(h.get("host", "")) not in allowed:
            return _secure(Response("unknown host", status_code=400, media_type="text/plain"))
        if request.method not in _SAFE_METHODS and not _same_origin(h):
            return _secure(JSONResponse({"error": "cross-origin write refused (Origin/Referer "
                                                  "must match Host)"}, status_code=403))
        nonce = secrets.token_urlsafe(16)
        request.state.nonce = nonce
        return _secure(await call_next(request), nonce)

    # ---- core operations (shared by forms + JSON) ----
    def _pc(symbol: str) -> float | None:
        try:
            v = prior_close(symbol)
            v = float(v) if v is not None else None
        except Exception:
            return None
        return v if v is not None and math.isfinite(v) and v > 0 else None

    def _writable(d: date) -> None:
        t = today()
        if d < t:
            raise PastDateError(f"levels for {d} are read-only (today is {t})")

    def _typo(symbol: str | None, price: float | None) -> dict | None:
        """{symbol, price, last, pct_off, message} when price is > ±TYPO_CHECK_PCT %
        from the latest trade; None when within the band, off, or unknown."""
        pct = float(cfg.typo_check_pct or 0)
        if pct <= 0 or price is None or not symbol:
            return None
        last = _pc(symbol)
        if last is None:
            return None
        off = round((float(price) - last) / last * 100.0, 1)
        if abs(off) <= pct:
            return None
        return {"symbol": symbol, "price": float(price), "last": last, "pct_off": off,
                "message": typo_message(symbol, float(price), last, off)}

    def _clean_new(fields: dict) -> tuple[dict, str | None]:
        f = validate_level_fields({**_user_fields(fields), "source": "entered"})
        if f["symbol"] == "SPY" and f.get("price") is None:
            raise LevelValidationError("SPY levels need a price")
        return f, None

    def op_add(d: date, fields: dict, confirm: bool = False) -> dict:
        _writable(d)
        f, warn = _clean_new(fields)
        flag = _typo(f["symbol"], f.get("price"))
        if flag and not confirm:
            raise _NeedsConfirm([flag], fields=f)
        row = store.add_level(d, f, today())
        return {**row, "warning": warn}

    def _get_editable(level_id: str) -> dict:
        cur = store.get_level(level_id)
        if cur is None:
            raise KeyError(level_id)
        _writable(_as_date(cur["trade_date"]))
        if cur.get("source") == "converted":
            raise _Conflict("converted levels are written by the engine; edit the SPY level instead")
        return cur

    def op_edit(level_id: str, fields: dict, confirm: bool = False) -> dict:
        cur = _get_editable(level_id)
        f = validate_level_fields(_user_fields(fields), partial=True)
        sym = f.get("symbol", cur["symbol"])
        price = f["price"] if "price" in f else cur.get("price")
        if sym == "SPY" and price is None:
            raise LevelValidationError("SPY levels need a price")
        cur_price = None if cur.get("price") is None else float(cur["price"])
        if (sym, price) != (cur["symbol"], cur_price):  # only a changed price/symbol is checked
            flag = _typo(sym, price)
            if flag and not confirm:
                raise _NeedsConfirm([flag], fields=f)
        row = store.update_level(level_id, f, today())
        return {**row, "warning": None}

    def op_delete(level_id: str) -> dict:
        cur = _get_editable(level_id)
        store.delete_level(level_id, today())
        return cur

    def op_bulk(d: date, rows: list[dict], pre_errors: list[dict] | None = None,
                dry_run: bool = False, confirm: bool = False) -> tuple[list[dict], list[dict]]:
        """→ (rows inserted — or validated on dry_run —, typo flags)."""
        _writable(d)
        errors = list(pre_errors or [])
        clean: list[dict] = []
        flagged: list[dict] = []
        for i, r in enumerate(rows, 1):
            line = r.get("line", i) if isinstance(r, dict) else i
            try:
                if not isinstance(r, dict):
                    raise LevelValidationError("row must be an object")
                f, _ = _clean_new(r)
                clean.append(f)
                flag = _typo(f["symbol"], f.get("price"))
                if flag:
                    flagged.append({"line": line, **flag})
            except LevelValidationError as ex:
                errors.append({"line": line, "error": str(ex)})
        if not rows and not errors:
            errors.append({"line": 0, "error": "nothing to import"})
        if errors:
            raise _BulkError(sorted(errors, key=lambda x: (x.get("line") or 0)))
        if dry_run:
            return clean, flagged
        if flagged and not confirm:
            raise _NeedsConfirm(flagged)
        # all-or-nothing: one transaction in PgStore, validate-then-insert in MemoryStore
        return store.add_levels(d, clean, today()), flagged

    def _converted(d: date) -> list[dict]:
        try:
            if converted_view is not None:
                return list(converted_view(d) or [])
            return [r for r in store.list_levels(d) if r.get("source") == "converted"]
        except Exception:
            return []

    def _ratio(d: date, converted: list[dict]) -> dict:
        try:
            locked = store.latest_locked_ratio(d)
        except Exception:
            locked = None
        if locked:
            return {"value": float(locked), "locked": True}
        for c in converted:
            if c.get("ratio"):
                return {"value": float(c["ratio"]), "locked": False}
        return {"value": None, "locked": False}

    def _day(d: date) -> dict:
        t = today()
        entered = store.list_levels(d, include_converted=False)
        converted = _converted(d)
        by_parent = {str(c["converted_from"]): c for c in converted if c.get("converted_from")}
        try:
            summary = store.get_day_summary(d)
        except Exception:
            summary = None
        outcomes = _outcomes(summary)
        editable = d >= t
        pcs = {s: _pc(s) for s in SYMBOLS} if editable else {}
        levels = []
        for r in entered:
            rid = str(r["id"])
            c = by_parent.get(rid)
            levels.append({**r, "converted": c, "warning": None,
                           "outcome": outcomes.get(rid)})
        parent_ids = {str(r["id"]) for r in entered}
        unmatched = [c for c in converted if str(c.get("converted_from")) not in parent_ids]
        return {"trade_date": d, "today": t, "read_only": not editable,
                "ratio": _ratio(d, converted), "last_price": pcs, "levels": levels,
                "converted": converted, "unmatched_converted": unmatched,
                "summary": summary, "outcomes": outcomes}

    # ---- request helpers ----
    def _parse_date(s: str | None) -> date:
        if not s:
            return today()
        try:
            return date.fromisoformat(s.strip())
        except ValueError:
            raise _BadRequest(f"bad date {s!r} (want YYYY-MM-DD)") from None

    async def _body(request: Request) -> bytes:
        raw = await request.body()
        if len(raw) > MAX_BODY:
            raise _BadRequest("request too large")
        return raw

    async def _form(request: Request) -> dict[str, str]:
        raw = (await _body(request)).decode("utf-8", "replace")
        return {k: v[0] for k, v in parse_qs(raw, keep_blank_values=True).items()}

    async def _json(request: Request) -> Any:
        raw = await _body(request)
        try:
            return json.loads(raw or b"{}")
        except ValueError:
            raise _BadRequest("body must be JSON") from None

    def _back(d: date, msg: str) -> RedirectResponse:
        q = {"msg": msg}
        if d != today():
            q = {"date": d.isoformat(), **q}
        return RedirectResponse("/?" + urlencode(q), status_code=303)

    def _jerr(exc: Exception) -> JSONResponse:
        body: dict[str, Any] = {"error": _msg(exc)}
        if isinstance(exc, _BulkError):
            body["errors"] = exc.errors
        if isinstance(exc, _NeedsConfirm):
            body["needs_confirm"] = True
            body["flagged"] = exc.flagged
        return JSONResponse(body, status_code=_status_for(exc))

    def _j(obj: Any, status: int = 200) -> JSONResponse:
        return JSONResponse(jsonable_encoder(obj), status_code=status)

    def _label(r: dict | None) -> str:
        if not r:
            return "—"
        return f"{r.get('symbol', '')} {_num(r.get('price'))} {r.get('name', '')}".strip()

    # ---- HTML ----
    def _render(request: Request, d: date, *, status: int = 200, msg: str | None = None,
                err: str | None = None, errors: list[dict] | None = None,
                bulk_text: str = "", confirm: dict | None = None,
                add_values: dict | None = None) -> HTMLResponse:
        nonce = getattr(request.state, "nonce", "")
        day = _day(d)
        ro = day["read_only"]
        t = day["today"]
        p: list[str] = []
        a = p.append
        a(f"<!doctype html><html lang=en><head><meta charset=utf-8>"
          f"<meta name=viewport content='width=device-width,initial-scale=1'>"
          f"<title>Level Engine Management</title><style nonce='{e(nonce)}'>{_CSS}</style>"
          f"</head><body><main>")
        a("<h1>Level Engine Management</h1>")
        state = ("<span class='badge ro'>read-only</span>" if ro
                 else ("today" if d == t else "<span class='badge warn'>future date</span>"))
        a(f"<p class=sub>{e(cfg.symbol)} · trade date <b>{e(d.isoformat())}</b> {state}"
          f" · today (ET) {e(t.isoformat())}</p>")
        a("<form method=get action='/' class=bar><label>Date "
          f"<input type=date name=date value='{e(d.isoformat())}'></label>"
          "<button type=submit>Go</button><a href='/'>Today</a></form>")
        if msg:
            a(f"<div class='msg ok'>{e(msg)}</div>")
        if err:
            a(f"<div class='msg err'>{e(err)}")
            if errors:
                a("<ul>" + "".join(f"<li>line {e(x.get('line'))}: {e(x.get('error'))}</li>"
                                   for x in errors) + "</ul>")
            a("</div>")
        if confirm:
            # typo check: nothing saved; offer a one-click re-post of the same values
            fl = confirm["flagged"]
            a("<div class='msg warn' id=typo><b>Not saved — "
              f"{'this price looks' if len(fl) == 1 else 'these prices look'} like a typo "
              f"(more than ±{e(f'{float(cfg.typo_check_pct):g}')}% from the last trade):</b><ul>")
            for x in fl:
                ln = f"line {e(x['line'])}: " if x.get("line") is not None else ""
                a(f"<li>{ln}{e(x['message'])}</li>")
            a(f"</ul><form method=post action='{e(confirm['action'])}' class=bar>")
            for k, v in confirm["fields"].items():
                a(f"<input type=hidden name='{e(k)}' value='{e(v)}'>")
            a("<input type=hidden name=confirm value=1><button type=submit>Save anyway</button>"
              "<span class=mut>or fix it below and submit again</span></form></div>")

        r = day["ratio"]
        rtxt = (f"{r['value']:.4f} ({'locked' if r['locked'] else 'provisional'})"
                if r["value"] else "not set yet")
        a(f"<p class=mut>SPX/SPY ratio: {e(rtxt)}")
        if not ro:
            pcs = day["last_price"]
            parts = [f"{s} {_num(pcs[s])}" if pcs.get(s) else f"{s} unknown" for s in SYMBOLS]
            a(f" · last price: {e(' · '.join(parts))}")
        a("</p>")

        # levels table
        a("<h2>Levels</h2><div class=wrap><table><thead><tr><th>Symbol</th><th>Price</th>"
          "<th>Name</th><th>Kind</th><th>Condition</th><th>Source</th><th>SPX (converted)</th>"
          "<th>Outcome</th><th>Created</th><th>Updated</th>")
        if not ro:
            a("<th>Actions</th>")
        a("</tr></thead><tbody>")
        if not day["levels"]:
            a(f"<tr><td colspan={11 if not ro else 10} class=mut>No levels for this date.</td></tr>")
        for lv in day["levels"]:
            rid = e(lv["id"])
            c = lv.get("converted")
            conv = (f"{_num(c.get('price'))} <span class=mut>× {e(_num(c.get('ratio'), 4))}</span>"
                    if c else "—")
            badge = (f"<span class='badge warn' title='{e(lv['warning'])}'>unverified</span>"
                     if lv.get("warning") else "")
            a(f"<tr><td>{e(lv['symbol'])}</td><td class=n>{e(_num(lv.get('price')))}{badge}</td>"
              f"<td>{e(lv.get('name'))}</td><td>{e(lv.get('kind') or '—')}</td>"
              f"<td>{e(lv.get('condition_text') or '')}</td><td>{e(lv.get('source'))}</td>"
              f"<td class=n>{conv}</td><td>{e(lv.get('outcome') or '—')}</td>"
              f"<td>{e(_ts(lv.get('created_at')))}</td><td>{e(_ts(lv.get('updated_at')))}</td>")
            if not ro:
                a("<td><details><summary>Edit</summary>"
                  f"<form method=post action='/levels/{rid}/edit' class=grid>"
                  + _fields_html(lv) +
                  "<button type=submit>Save</button></form></details>"
                  f"<form method=post action='/levels/{rid}/delete' class='inline del'>"
                  "<button type=submit class=danger>Delete</button></form></td>")
            a("</tr>")
        a("</tbody></table></div>")
        if day["unmatched_converted"]:
            a("<h2>Converted (no matching entered level)</h2><div class=wrap><table><thead><tr>"
              "<th>Name</th><th>SPX</th><th>Ratio</th><th>From</th></tr></thead><tbody>")
            for c in day["unmatched_converted"]:
                a(f"<tr><td>{e(c.get('name'))}</td><td class=n>{e(_num(c.get('price')))}</td>"
                  f"<td class=n>{e(_num(c.get('ratio'), 4))}</td>"
                  f"<td>{e(c.get('converted_from') or '—')}</td></tr>")
            a("</tbody></table></div>")

        if not ro:
            dv = e(d.isoformat())
            a("<h2>Add a level</h2><form method=post action='/levels' class=grid>"
              f"<input type=hidden name=date value='{dv}'>" + _fields_html(add_values or {"symbol": "SPY"}) +
              "<button type=submit>Add</button></form>")
            a("<h2>Bulk paste</h2>"
              f"<p class=mut>One per line: <code>{e(GRAMMAR)}</code>. Also "
              "<code>SPX 6012.5 Golden Gate</code>. Kinds: support · resistance · pivot · "
              "trigger. Lines starting with # are skipped. All-or-nothing: any bad line "
              "and nothing is inserted.</p>"
              f"<form method=post action='/levels/bulk'><input type=hidden name=date value='{dv}'>"
              f"<textarea name=text placeholder='SPY 595.20 PM high'>{e(bulk_text)}</textarea>"
              "<div class=bar><button type=submit>Import</button></div></form>")

        # day summary / legs (filled from phase 2; empty-safe)
        summ = day["summary"]
        a("<h2>Day summary</h2>")
        if summ:
            legs_c = summ.get("legs")
            if isinstance(legs_c, dict):
                legs_c = ", ".join(f"{k}: {v}" for k, v in legs_c.items())
            a(f"<p>Close {e(_num(summ.get('close')))} · final zone "
              f"{e(_fmt_json(summ.get('final_zone')))} · legs {e(legs_c or '—')}</p>")
            if summ.get("notes"):
                a(f"<p class=mut>{e(summ['notes'])}</p>")
        else:
            a("<p class=mut>No day summary yet.</p>")

        names = {str(x["id"]): _label(x) for x in day["levels"]}
        names.update({str(x["id"]): _label(x) for x in day["converted"] if x.get("id")})
        try:
            legs = store.list_rows("legs", d)
        except Exception:
            legs = []
        a("<h2>Legs</h2>")
        if legs:
            a("<div class=wrap><table><thead><tr><th>Opened</th><th>Call</th><th>Target</th>"
              "<th>Invalidation</th><th>Entry</th><th>Closed</th><th>Result</th><th>Progress %</th>"
              "<th>MAE pts</th><th>Min to target</th><th>Strategy P&amp;L</th></tr></thead><tbody>")
            for g in legs:
                a(f"<tr><td>{e(_ts(g.get('opened_at')))}</td><td>{e(g.get('call'))}</td>"
                  f"<td>{e(names.get(str(g.get('target_level_id')), g.get('target_level_id') or '—'))}</td>"
                  f"<td>{e(names.get(str(g.get('invalidation_level_id')), g.get('invalidation_level_id') or '—'))}</td>"
                  f"<td class=n>{e(_num(g.get('entry_spot')))}</td><td>{e(_ts(g.get('closed_at')))}</td>"
                  f"<td>{e(g.get('result') or 'open')}</td><td class=n>{e(_num(g.get('progress_pct'), 1))}</td>"
                  f"<td class=n>{e(_num(g.get('mae_pts')))}</td>"
                  f"<td class=n>{e(_num(g.get('time_to_target_min'), 1))}</td>"
                  f"<td class=n>{e(_num(g.get('strategy_pnl')))}</td></tr>")
            a("</tbody></table></div>")
        else:
            a("<p class=mut>No legs recorded.</p>")

        try:
            audit = store.list_audit(d)
        except Exception:
            audit = []
        a(f"<details{' open' if ro else ''}><summary>Audit trail ({len(audit)})</summary>")
        if audit:
            a("<div class=wrap><table><thead><tr><th>At</th><th>Action</th><th>Level</th>"
              "<th>Change</th></tr></thead><tbody>")
            for x in audit:
                lvl = x.get("after") or x.get("before")
                lab = _label(lvl) if isinstance(lvl, dict) and "symbol" in lvl else (x.get("level_id") or "—")
                a(f"<tr><td>{e(_ts(x.get('at')))}</td><td>{e(x.get('action'))}</td>"
                  f"<td>{e(lab)}</td><td>{e(_audit_change(x))}</td></tr>")
            a("</tbody></table></div>")
        else:
            a("<p class=mut>No audit entries.</p>")
        a("</details>")
        a(f"</main><script nonce='{e(nonce)}'>{_JS}</script></body></html>")
        return HTMLResponse("".join(p), status_code=status)

    # ---- routes: pages + forms ----
    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request, date: str | None = None, msg: str | None = None):
        try:
            d = _parse_date(date)
        except _BadRequest as ex:
            return _render(request, today(), status=400, err=str(ex))
        return _render(request, d, msg=msg)

    _REPOST_KEYS = ("date", *USER_FIELDS, "text")

    def _form_fail(request: Request, d: date, ex: Exception, bulk_text: str = "",
                   action: str = "", form: dict | None = None,
                   prefill_add: bool = False) -> HTMLResponse:
        if isinstance(ex, _NeedsConfirm):
            fields = {k: v for k, v in (form or {}).items() if k in _REPOST_KEYS}
            return _render(request, d, status=409, bulk_text=bulk_text,
                           confirm={"action": action, "fields": fields, "flagged": ex.flagged},
                           add_values=ex.fields if prefill_add else None)
        return _render(request, d, status=_status_for(ex), err=_msg(ex),
                       errors=getattr(ex, "errors", None), bulk_text=bulk_text)

    @app.post("/levels")
    async def form_add(request: Request):
        form = await _form(request)
        d = today()
        try:
            d = _parse_date(form.get("date"))
            row = op_add(d, form, confirm=_truthy(form.get("confirm")))
        except (PastDateError, LevelValidationError, _BadRequest, _NeedsConfirm) as ex:
            return _form_fail(request, d, ex, action="/levels", form=form, prefill_add=True)
        return _back(d, f"Added {_label(row)}")

    @app.post("/levels/bulk")
    async def form_bulk(request: Request):
        form = await _form(request)
        d, text = today(), form.get("text", "")
        try:
            d = _parse_date(form.get("date"))
            rows, perrs = parse_bulk_text(text)
            done, _ = op_bulk(d, rows, perrs, confirm=_truthy(form.get("confirm")))
        except (PastDateError, LevelValidationError, _BadRequest, _NeedsConfirm) as ex:
            return _form_fail(request, d, ex, bulk_text=text, action="/levels/bulk", form=form)
        return _back(d, f"Imported {len(done)} level(s)")

    def _level_date(level_id: str) -> date:
        cur = store.get_level(level_id)
        return _as_date(cur["trade_date"]) if cur else today()

    @app.post("/levels/{level_id}/edit")
    async def form_edit(request: Request, level_id: str):
        form = await _form(request)
        d = _level_date(level_id)
        try:
            row = op_edit(level_id, form, confirm=_truthy(form.get("confirm")))
        except (PastDateError, LevelValidationError, KeyError, _Conflict, _NeedsConfirm) as ex:
            return _form_fail(request, d, ex, action=f"/levels/{level_id}/edit", form=form)
        return _back(d, f"Saved {_label(row)}")

    @app.post("/levels/{level_id}/delete")
    async def form_delete(request: Request, level_id: str):
        await _form(request)
        d = _level_date(level_id)
        try:
            row = op_delete(level_id)
        except (PastDateError, KeyError, _Conflict) as ex:
            return _form_fail(request, d, ex)
        return _back(d, f"Deleted {_label(row)}")

    # ---- routes: JSON API ----
    @app.get("/api/levels")
    def api_list(date: str | None = None):
        try:
            day = _day(_parse_date(date))
        except _BadRequest as ex:
            return _jerr(ex)
        return _j({k: day[k] for k in ("trade_date", "today", "read_only", "ratio",
                                        "last_price", "levels", "converted")})

    @app.post("/api/levels")
    async def api_add(request: Request):
        try:
            body = await _json(request)
            if not isinstance(body, dict):
                raise _BadRequest("body must be a JSON object")
            row = op_add(_parse_date(body.get("date")), body, confirm=_truthy(body.get("confirm")))
        except (PastDateError, LevelValidationError, _BadRequest, _NeedsConfirm) as ex:
            return _jerr(ex)
        return _j(row, 201)

    @app.post("/api/levels/bulk")
    async def api_bulk(request: Request):
        try:
            body = await _json(request)
            if not isinstance(body, dict):
                raise _BadRequest("body must be a JSON object")
            d = _parse_date(body.get("date"))
            if "rows" in body:
                rows, perrs = body["rows"], []
                if not isinstance(rows, list):
                    raise _BadRequest("rows must be a list")
            else:
                rows, perrs = parse_bulk_text(str(body.get("text") or ""))
            dry = bool(body.get("dry_run"))
            out, flagged = op_bulk(d, rows, perrs, dry_run=dry, confirm=_truthy(body.get("confirm")))
        except (PastDateError, LevelValidationError, _BadRequest, _NeedsConfirm) as ex:
            return _jerr(ex)
        return _j({"trade_date": d, "dry_run": dry, "levels": out, "flagged": flagged},
                  200 if dry else 201)

    @app.patch("/api/levels/{level_id}")
    async def api_edit(request: Request, level_id: str):
        try:
            body = await _json(request)
            if not isinstance(body, dict):
                raise _BadRequest("body must be a JSON object")
            row = op_edit(level_id, body, confirm=_truthy(body.get("confirm")))
        except (PastDateError, LevelValidationError, KeyError, _Conflict, _BadRequest,
                _NeedsConfirm) as ex:
            return _jerr(ex)
        return _j(row)

    @app.delete("/api/levels/{level_id}")
    def api_delete(level_id: str):
        try:
            op_delete(level_id)
        except (PastDateError, KeyError, _Conflict) as ex:
            return _jerr(ex)
        return _j({"ok": True, "id": level_id})

    return app


def _fields_html(lv: dict) -> str:
    sym = lv.get("symbol") or "SPY"
    price = lv.get("price")
    ptxt = "" if price is None else f"{float(price):g}"
    sym_opts = "".join(f"<option{' selected' if s == sym else ''}>{s}</option>" for s in SYMBOLS)
    kind_opts = "".join(
        f"<option value='{e(k or '')}'{' selected' if k == lv.get('kind') else ''}>{e(k or '—')}</option>"
        for k in KINDS)
    return (f"<label>Symbol<select name=symbol>{sym_opts}</select></label>"
            f"<label>Price<input name=price inputmode=decimal size=9 value='{e(ptxt)}'></label>"
            f"<label>Name<input name=name required maxlength=120 value='{e(lv.get('name') or '')}'></label>"
            f"<label>Kind<select name=kind>{kind_opts}</select></label>"
            f"<label>Condition<input name=condition_text size=22 maxlength=500 "
            f"value='{e(lv.get('condition_text') or '')}'></label>")


def _fmt_json(v: Any) -> str:
    if v is None:
        return "—"
    if isinstance(v, (dict, list)):
        return json.dumps(v, default=str)
    return str(v)


def _audit_change(x: dict) -> str:
    b, a = x.get("before"), x.get("after")
    act = x.get("action")
    if act == "edit" and isinstance(b, dict) and isinstance(a, dict):
        keys = [k for k in ("symbol", "price", "name", "kind", "condition_text")
                if b.get(k) != a.get(k)]
        return "; ".join(f"{k}: {b.get(k)!s} → {a.get(k)!s}" for k in keys) or "no change"
    if act == "convert":
        removed = b.get("removed") if isinstance(b, dict) else None
        n = len(a.get("rows") or []) if isinstance(a, dict) else 0
        return f"removed {removed or 0}, wrote {n} converted"
    lvl = a if isinstance(a, dict) else b
    if isinstance(lvl, dict):
        return f"{lvl.get('symbol', '')} {lvl.get('price')} {lvl.get('name', '')}".strip()
    return ""

"""Levels CLI — backup / automation front door to the admin listener (§10).

    python -m level_engine.levels_cli show   [--date YYYY-MM-DD] [--json]
    python -m level_engine.levels_cli import FILE [--date YYYY-MM-DD] [--dry-run] [--force]

It talks to the admin JSON API over HTTP (stdlib urllib only), so it runs on the
host against the container and never needs DB credentials. Base URL:
`--url` → $LEVEL_ADMIN_URL → http://127.0.0.1:8790. Every write carries an
`Origin` header equal to the base URL, which is what the admin app's same-origin
CSRF check requires.

Import file formats (auto-detected):

* CSV — header row `symbol,price,name,kind,condition_text` (case-insensitive;
  `kind` and `condition_text` optional, `price` may be blank for SPX).
* Bulk text — the admin page's paste grammar, one level per line:

      SYMBOL PRICE NAME [ "[" KIND "]" ] [ "|" CONDITION ]

      SPY 595.20 PM high
      SPX 6012.5 Golden Gate
      SPY 595.2 PM high [support]
      SPY $594.10 Overnight low [pivot] | only if VIX < 18

  SYMBOL is SPY|SPX (any case); PRICE is a positive number (a leading `$` and
  thousands commas are allowed); NAME is the rest of the line up to an optional
  trailing `[kind]` (support|resistance|pivot|trigger); anything after the first
  `|` is the free-text condition. Blank lines and lines starting with `#` are
  skipped.

Parsing is all-or-nothing: any bad line is reported with its line number and
nothing is sent. The server re-validates and also inserts nothing unless every
row passes.

Typo check (§17): a price more than ±TYPO_CHECK_PCT % from the latest trade is
flagged by the server (409 needs_confirm). The CLI then prints the flagged lines
and exits 1 with nothing inserted; `--force` sends `"confirm": true` and imports
them anyway. `--dry-run` lists the flags too (exit 1 if any, unless --force).

This module is stdlib-only on purpose; the admin app imports its parsers so the
page and the CLI share one grammar.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import math
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from .store import KINDS, SYMBOLS

DEFAULT_URL = "http://127.0.0.1:8790"
ENV_URL = "LEVEL_ADMIN_URL"
USER_FIELDS = ("symbol", "price", "name", "kind", "condition_text")
_KIND_RE = re.compile(r"\s*\[\s*([A-Za-z]*)\s*\]\s*$")
GRAMMAR = "SYMBOL PRICE NAME [kind] [| condition]   e.g.  SPY 595.20 PM high [support]"


# ---------------------------------------------------------------------------
# parsing (shared with admin.py)
# ---------------------------------------------------------------------------
def parse_price(s: str) -> float:
    """'595.20' / '$6,012.5' → float. Raises ValueError with a readable message."""
    t = str(s).strip().lstrip("$").replace(",", "")
    try:
        p = float(t)
    except ValueError:
        raise ValueError(f"price {s!r} is not a number") from None
    if not math.isfinite(p) or p <= 0:
        raise ValueError(f"price {s!r} must be a positive number")
    return p


def _norm_kind(k: str | None) -> str | None:
    k = (k or "").strip().lower() or None
    if k not in KINDS:
        raise ValueError(f"kind {k!r} must be one of support|resistance|pivot|trigger")
    return k


def _norm_symbol(s: str | None) -> str:
    sym = (s or "").strip().upper()
    if sym not in SYMBOLS:
        raise ValueError(f"symbol {s!r} must be SPY or SPX")
    return sym


def parse_bulk_line(line: str) -> dict:
    """One bulk-paste line → level fields (no 'line' key). Raises ValueError."""
    left, _, cond = line.partition("|")
    left = left.strip()
    kind = None
    m = _KIND_RE.search(left)
    if m:
        kind = _norm_kind(m.group(1))
        left = left[:m.start()].rstrip()
    parts = left.split(None, 2)
    if len(parts) < 3:
        raise ValueError(f"expected: {GRAMMAR}")
    sym, price_s, name = parts
    return {"symbol": _norm_symbol(sym), "price": parse_price(price_s), "name": name.strip(),
            "kind": kind, "condition_text": cond.strip() or None}


def parse_bulk_text(text: str) -> tuple[list[dict], list[dict]]:
    """Bulk-paste text → (rows, errors). Each row carries its 1-based 'line';
    each error is {'line': n, 'error': msg}."""
    rows, errors = [], []
    for n, raw in enumerate((text or "").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        try:
            rows.append({"line": n, **parse_bulk_line(line)})
        except ValueError as e:
            errors.append({"line": n, "error": str(e)})
    return rows, errors


def looks_like_csv(text: str) -> bool:
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        cols = {c.strip().lower() for c in line.split(",")}
        return {"symbol", "price", "name"} <= cols
    return False


def parse_csv(text: str) -> tuple[list[dict], list[dict]]:
    """CSV with header symbol,price,name[,kind][,condition_text] → (rows, errors)."""
    reader = csv.reader(io.StringIO(text or ""))

    def _skip(cells: list[str]) -> bool:
        return not any(c.strip() for c in cells) or cells[0].lstrip().startswith("#")

    header = None
    for cells in reader:
        if not _skip(cells):
            header = [h.strip().lower() for h in cells]
            break
    if header is None:
        return [], [{"line": 1, "error": "empty file"}]
    hline = reader.line_num
    missing = {"symbol", "price", "name"} - set(header)
    if missing:
        return [], [{"line": hline, "error": f"CSV header missing: {', '.join(sorted(missing))}"}]
    unknown = set(header) - set(USER_FIELDS) - {""}
    if unknown:
        return [], [{"line": hline, "error": f"CSV header has unknown columns: {', '.join(sorted(unknown))}"}]
    rows, errors = [], []
    for cells in reader:
        n = reader.line_num
        if _skip(cells):
            continue
        rec = {h: (cells[i].strip() if i < len(cells) else "") for i, h in enumerate(header) if h}
        try:
            price_s = rec.get("price", "")
            sym = _norm_symbol(rec.get("symbol"))
            name = rec.get("name", "")
            if not name:
                raise ValueError("name is required")
            rows.append({"line": n, "symbol": sym,
                         "price": parse_price(price_s) if price_s else None,
                         "name": name, "kind": _norm_kind(rec.get("kind")),
                         "condition_text": rec.get("condition_text") or None})
        except ValueError as e:
            errors.append({"line": n, "error": str(e)})
    return rows, errors


def parse_levels_file(text: str) -> tuple[str, list[dict], list[dict]]:
    """→ (format, rows, errors) with format 'csv' or 'text'."""
    if looks_like_csv(text):
        return ("csv", *parse_csv(text))
    return ("text", *parse_bulk_text(text))


# ---------------------------------------------------------------------------
# HTTP client
# ---------------------------------------------------------------------------
class CliError(RuntimeError):
    pass


_urlopen = urllib.request.urlopen  # tests swap this


class AdminClient:
    def __init__(self, base: str | None = None, timeout: float = 15.0) -> None:
        self.base = (base or os.environ.get(ENV_URL) or DEFAULT_URL).rstrip("/")
        u = urllib.parse.urlsplit(self.base)
        if u.scheme not in ("http", "https") or not u.netloc:
            raise CliError(f"bad admin URL {self.base!r}")
        self.origin = f"{u.scheme}://{u.netloc}"
        self.timeout = timeout

    def request(self, method: str, path: str, body: Any = None,
                query: dict | None = None) -> tuple[int, Any]:
        url = self.base + path
        if query:
            url += "?" + urllib.parse.urlencode({k: v for k, v in query.items() if v is not None})
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Accept": "application/json", "Origin": self.origin}
        if data is not None:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with _urlopen(req, timeout=self.timeout) as resp:
                status, raw = resp.status, resp.read()
        except urllib.error.HTTPError as e:
            status, raw = e.code, e.read()
        except urllib.error.URLError as e:
            raise CliError(f"cannot reach the level-engine admin at {self.base}: {e.reason}") from e
        try:
            return status, json.loads(raw or b"null")
        except ValueError:
            return status, {"error": raw.decode("utf-8", "replace")[:300]}


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------
def _fmt(v: Any, nd: int = 2) -> str:
    if v is None or v == "":
        return "-"
    try:
        return f"{float(v):,.{nd}f}"
    except (TypeError, ValueError):
        return str(v)


def _print_errors(errors: list[dict], out) -> None:
    for e in errors:
        print(f"  line {e.get('line', '?')}: {e.get('error')}", file=out)


def cmd_show(client: AdminClient, date: str | None, as_json: bool, out=None) -> int:
    out = out or sys.stdout
    status, data = client.request("GET", "/api/levels", query={"date": date})
    if status != 200:
        print(f"error ({status}): {(data or {}).get('error', data)}", file=sys.stderr)
        return 1
    if as_json:
        print(json.dumps(data, indent=2, sort_keys=True), file=out)
        return 0
    levels = data.get("levels") or []
    ratio = data.get("ratio") or {}
    mode = "read-only" if data.get("read_only") else "editable"
    rtxt = (f"ratio {ratio['value']:.4f} ({'locked' if ratio.get('locked') else 'provisional'})"
            if ratio.get("value") else "ratio not set")
    print(f"Levels for {data.get('trade_date')} ({mode}) - {len(levels)} entered, "
          f"{len(data.get('converted') or [])} converted, {rtxt}", file=out)
    if not levels:
        print("  (none)", file=out)
        return 0
    print(f"  {'SYM':<4} {'PRICE':>10}  {'NAME':<28} {'KIND':<11} {'SPX(conv)':>10}  CONDITION", file=out)
    for lv in levels:
        conv = (lv.get("converted") or {}).get("price")
        warn = ""
        print(f"  {lv.get('symbol', ''):<4} {_fmt(lv.get('price')):>10}  "
              f"{str(lv.get('name', ''))[:28]:<28} {lv.get('kind') or '-':<11} "
              f"{_fmt(conv):>10}  {lv.get('condition_text') or ''}{warn}", file=out)
    return 0


def _print_flagged(flagged: list[dict], out) -> None:
    for f in flagged:
        where = f"line {f['line']}: " if f.get("line") is not None else ""
        msg = f.get("message") or (f"{f.get('symbol')} {_fmt(f.get('price'))} is "
                                   f"{f.get('pct_off')}% off the last trade {_fmt(f.get('last'))}")
        print(f"  {where}{msg}", file=out)


def cmd_import(client: AdminClient, path: str, date: str | None, dry_run: bool,
               out=None, force: bool = False) -> int:
    out = out or sys.stdout
    try:
        with open(path, encoding="utf-8-sig") as fh:
            text = fh.read()
    except OSError as e:
        print(f"cannot read {path}: {e}", file=sys.stderr)
        return 1
    fmt, rows, errors = parse_levels_file(text)
    if errors:
        print(f"{path}: {len(errors)} bad line(s) ({fmt} format) - nothing imported:", file=sys.stderr)
        _print_errors(errors, sys.stderr)
        return 1
    if not rows:
        print(f"{path}: no levels found", file=sys.stderr)
        return 1
    status, data = client.request("POST", "/api/levels/bulk",
                                  body={"date": date, "rows": rows, "dry_run": dry_run,
                                        "confirm": bool(force)})
    data = data or {}
    if status == 409 and data.get("needs_confirm"):
        flagged = data.get("flagged") or []
        print(f"{path}: {len(flagged)} price(s) look like typos (far from the last trade) "
              "- nothing imported; re-run with --force if they are right:", file=sys.stderr)
        _print_flagged(flagged, sys.stderr)
        return 1
    if status not in (200, 201):
        print(f"refused ({status}): {data.get('error', '')} - nothing imported", file=sys.stderr)
        _print_errors(data.get("errors") or [], sys.stderr)
        return 1
    verb = "would import" if dry_run else "imported"
    print(f"{verb} {len(data.get('levels') or [])} level(s) for {data.get('trade_date')} "
          f"from {path} ({fmt})", file=out)
    for lv in data.get("levels") or []:
        print(f"  {lv.get('symbol')} {_fmt(lv.get('price'))} {lv.get('name')}"
              f"{' [' + lv['kind'] + ']' if lv.get('kind') else ''}", file=out)
    flagged = data.get("flagged") or []
    if flagged:
        if force:
            print(f"forced past {len(flagged)} typo flag(s):", file=out)
            _print_flagged(flagged, out)
        else:
            print(f"{len(flagged)} price(s) look like typos - a real import needs --force:",
                  file=sys.stderr)
            _print_flagged(flagged, sys.stderr)
            return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="levels_cli", description="Level Engine levels (admin API client)")
    ap.add_argument("--url", help=f"admin base URL (default ${ENV_URL} or {DEFAULT_URL})")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("show", help="list a day's levels")
    s.add_argument("--date", help="YYYY-MM-DD (default: today ET)")
    s.add_argument("--json", action="store_true", help="raw JSON")
    i = sub.add_parser("import", help="import levels from a CSV or bulk-text file")
    i.add_argument("file")
    i.add_argument("--date", help="YYYY-MM-DD (default: today ET)")
    i.add_argument("--dry-run", action="store_true", help="validate on the server, insert nothing")
    i.add_argument("--force", action="store_true",
                   help="import prices flagged by the typo check (far from the last trade)")
    a = ap.parse_args(argv)
    try:
        client = AdminClient(a.url)
        if a.cmd == "show":
            return cmd_show(client, a.date, a.json)
        return cmd_import(client, a.file, a.date, a.dry_run, force=a.force)
    except CliError as e:
        print(str(e), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())

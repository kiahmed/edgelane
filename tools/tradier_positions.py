"""
Tradier position + order manager — uniform verb-flag CLI.

Flag scheme:
    SHORT flags `-X <row>` operate on EXISTING items in the table.
    LONG  flags `--verb`   open NEW positions (single-leg or multi-leg ticket).

CLI summary

    List
        tp                                       # list positions (default)
        tp -O                                    # list working orders

    Existing positions
        tp -S 3                                  # Sell (close) position #3 at MARKET
        tp -S 3 -L 1.20                          # close at LIMIT $1.20
        tp -S 3 -Q 2                             # close 2 contracts at MARKET
        tp -S 3 -L 1.20 -Q 2 --execute           # live close

        tp -A 3 -Q 2                             # Add 2 contracts to position #3 at MARKET
        tp -A 3 -Q 2 -L 1.30                     # add 2 at LIMIT $1.30
        tp -A 3 -Q 2 -L 1.30 --execute           # live add

    Open NEW single-leg  (date optional → defaults to TODAY; lenient parsing)
        tp --buy  QQQ 746c                                  # date = today, MARKET, qty 1, preview
        tp --buy  QQQ 746C 2026-06-4 -Q 2 -L 2.90           # ISO date with single-digit day works
        tp --buy  QQQ 746C 06-04 -L 2.90                    # MM-DD → current year auto-filled
        tp --sell SPY 540p 06/04 -L 1.50 --execute          # US slash style + lowercase 'p'
        tp --sell SPY 540P 2026-06-30 -L 1.50 --execute     # full ISO, live

    Open NEW multi-leg (EdgeLane copy-button ticket; '·' separator detected)
        tp --buy  "Buy Aggressive · NVDA · 2026-06-30 · 220/225C · LIMIT debit $1.80 GTC · 1 contract"
        tp --sell "Sell Balanced · NDX · 2026-05-19 · ..." --execute

    Existing working orders
        tp -O 2 -L 1.35                          # modify order #2 limit to $1.35
        tp -O 2 -M                               # flip order #2 to MARKET
        tp -O 2 -Q 3                             # change order #2 quantity to 3
        tp -O 2 -L 1.30 -Q 2 --execute           # live modify
        tp -C 2                                  # Cancel order #2 (preview)
        tp -C 2 --execute                        # live cancel

    Closed-position P&L
        tp -G                                    # today's realized P&L (live close orders)
        tp -G 2026-06-04                         # specific past date (settled /gainloss)
        tp -G --since 7                          # last 7 days (settled history + today's live)
        tp -G --settled                          # force Tradier /gainloss endpoint only
        tp -G -V                                 # verbose: show match/no-match diagnostics

      NOTE: Tradier's /orders endpoint only exposes the CURRENT trading day, so
      multi-day ranges merge two sources: prior days come from the settled
      /gainloss endpoint, today comes from live filled close orders (which alone
      can capture intraday partial closes).

Modifier flags (apply to whichever operation is active)
    -L <price>     limit price; omit for MARKET
    -Q <qty>       quantity (close: partial-close size; add/new: contract count)
    -M             modify: flip to MARKET (clears price)
    -D <duration>  day | gtc | pre | post   (default gtc)
    --execute, -x  REQUIRED to actually submit; otherwise PREVIEW-ONLY.

Sandbox vs production picked via DEVMODE → resolve_tradier_creds in utils.py.
"""
from __future__ import annotations
import sys
import re
import time
import json
import urllib.parse
import urllib.request
import urllib.error
from pathlib import Path
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta

# Tool lives under tools/; shared helpers (utils.py, tradier_execute_ticket.py)
# remain under tests/. Add both directories to sys.path so imports work.
_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_HERE.parent))
sys.path.insert(0, str(_HERE.parent / "tests"))
from utils import load_config, resolve_tradier_creds

G, R, Y, B, D, N, BOLD, DIM = "\033[32m", "\033[31m", "\033[33m", "\033[34m", "\033[2m", "\033[0m", "\033[1m", "\033[2m"


# ─── Tradier REST helpers ─────────────────────────────────────────────────

def tradier_get(path: str, params: dict, token: str, base: str, timeout: int = 30) -> dict:
    qs = urllib.parse.urlencode(params or {})
    url = f"{base.rstrip('/')}/v1/{path}?{qs}" if qs else f"{base.rstrip('/')}/v1/{path}"
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    }, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:500]
        raise RuntimeError(f"Tradier GET {path}: HTTP {e.code} — {body}")


def tradier_post(path: str, body: str, token: str, base: str, timeout: int = 30) -> dict:
    url = f"{base.rstrip('/')}/v1/{path}"
    req = urllib.request.Request(
        url, data=body.encode("utf-8"),
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body_text = e.read().decode("utf-8", "replace")[:500]
        raise RuntimeError(f"Tradier POST {path}: HTTP {e.code} — {body_text}")


def tradier_put(path: str, body: str, token: str, base: str, timeout: int = 30) -> dict:
    """PUT /v1/{path} — used to modify an existing order."""
    url = f"{base.rstrip('/')}/v1/{path}"
    req = urllib.request.Request(
        url, data=body.encode("utf-8"),
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        method="PUT",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body_text = e.read().decode("utf-8", "replace")[:500]
        raise RuntimeError(f"Tradier PUT {path}: HTTP {e.code} — {body_text}")


def tradier_delete(path: str, token: str, base: str, timeout: int = 30) -> dict:
    """DELETE /v1/{path} — used to cancel an order."""
    url = f"{base.rstrip('/')}/v1/{path}"
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        },
        method="DELETE",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body_text = e.read().decode("utf-8", "replace")[:500]
        raise RuntimeError(f"Tradier DELETE {path}: HTTP {e.code} — {body_text}")


def resolve_account_id(token: str, base: str) -> str:
    prof = tradier_get("user/profile", {}, token, base)
    accounts = (prof.get("profile") or {}).get("account") or []
    if isinstance(accounts, dict):
        accounts = [accounts]
    if not accounts:
        raise RuntimeError("No Tradier accounts on this profile.")
    usable = next((a for a in accounts if int(a.get("option_level", 0) or 0) >= 3), accounts[0])
    return usable["account_number"]


# ─── OCC option symbol decoder ────────────────────────────────────────────

OCC_RE = re.compile(r"^([A-Z][A-Z0-9.]*?)(\d{6})([CP])(\d{8})$")


def decode_occ(symbol: str) -> dict | None:
    """Decode an OCC option symbol into (root, expiration YYYY-MM-DD, side, strike).
    Returns None for non-option symbols (equities)."""
    m = OCC_RE.match(symbol.strip())
    if not m:
        return None
    root, yymmdd, cp, strike8 = m.groups()
    yy, mm, dd = yymmdd[:2], yymmdd[2:4], yymmdd[4:]
    year = 2000 + int(yy)
    expiration = f"{year:04d}-{mm}-{dd}"
    strike = int(strike8) / 1000.0
    return {
        "root": root,
        "expiration": expiration,
        "side": "call" if cp == "C" else "put",
        "strike": strike,
    }


def _num(v):
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ─── Position aggregation ─────────────────────────────────────────────────

@dataclass
class PositionRow:
    """One row in the displayed table — either a single contract / equity, or
    a multi-leg spread aggregated from its originating filled order."""
    row_num: int = 0
    kind: str = "single"        # 'single' | 'multileg'
    label: str = ""             # 'vertical' | 'iron_condor' | 'iron_butterfly' | 'butterfly' | 'option' | 'equity'
    underlying: str = ""        # 'NVDA' (or root for stocks)
    expiration: str | None = None
    acquired: str = ""          # YYYY-MM-DD HH:MM:SS local
    cost_basis: float = 0.0     # signed: + paid (debit), - collected (credit)
    bid: float | None = None    # current mid components
    ask: float | None = None
    pnl_dollar: float = 0.0
    pnl_pct: float = 0.0
    quantity: int = 1
    structure_text: str = ""    # "220/225P + 235/240C" etc
    # For ordering: list of (occ_symbol, side, strike, original_side, qty)
    legs: list = field(default_factory=list)


def fetch_positions(token: str, base: str, account_id: str) -> list[dict]:
    resp = tradier_get(f"accounts/{account_id}/positions", {}, token, base)
    # Tradier returns "positions": "null" (literal string) when account is empty.
    container = resp.get("positions")
    if not isinstance(container, dict):
        return []
    pos = container.get("position") or []
    if isinstance(pos, dict):
        pos = [pos]
    return pos


def fetch_recent_orders(token: str, base: str, account_id: str, limit_days: int = 90) -> list[dict]:
    """Tradier orders endpoint with explicit large limit + pagination.

    Without an explicit `limit`, Tradier defaults to 25 orders. If the account
    has done more than 25 orders since the open we're trying to match, the
    relevant open falls off page 1 and partial closes can't be paired.
    """
    all_orders: list[dict] = []
    page = 1
    page_limit = 1000   # Tradier max
    while True:
        resp = tradier_get(
            f"accounts/{account_id}/orders",
            {"includeTags": "true", "page": str(page), "limit": str(page_limit)},
            token, base,
        )
        container = resp.get("orders")
        if not isinstance(container, dict):
            break
        chunk = container.get("order") or []
        if isinstance(chunk, dict):
            chunk = [chunk]
        if not chunk:
            break
        all_orders.extend(chunk)
        if len(chunk) < page_limit:
            break  # last page
        page += 1
        if page > 10:  # safety cap — 10k orders
            break
    return all_orders


def fetch_quotes(token: str, base: str, symbols: list[str]) -> dict:
    if not symbols:
        return {}
    resp = tradier_get("markets/quotes", {"symbols": ",".join(symbols), "greeks": "false"}, token, base)
    container = resp.get("quotes")
    if not isinstance(container, dict):
        return {}
    qs = container.get("quote") or []
    if isinstance(qs, dict):
        qs = [qs]
    return {q["symbol"]: q for q in qs}


def fetch_gainloss(token: str, base: str, account_id: str,
                   start: str | None = None, end: str | None = None,
                   limit: int = 1000) -> list[dict]:
    """Tradier /accounts/{id}/gainloss → list of closed-position rows.

    start/end are YYYY-MM-DD strings. If both omitted, Tradier returns
    everything (we still filter locally if needed).
    Tradier returns `gainloss.closed_position` — a list of per-leg closures
    with fields: symbol, quantity, cost, proceeds, gain_loss, gain_loss_percent,
    open_date, close_date, term.
    """
    params: dict[str, str] = {"limit": str(limit), "sortBy": "closeDate", "sort": "desc"}
    if start: params["start"] = start
    if end:   params["end"]   = end
    resp = tradier_get(f"accounts/{account_id}/gainloss", params, token, base)
    container = resp.get("gainloss")
    if not isinstance(container, dict):
        return []
    rows = container.get("closed_position") or []
    if isinstance(rows, dict):
        rows = [rows]
    return rows


def build_history_opens(token: str, base: str, account_id: str,
                        start: str, end: str) -> dict[str, list[dict]]:
    """Fetch /accounts/{id}/history trade events in [start,end] →
    {option_symbol: [fill, ...]}.

    This is the ONLY source for a prior-day OPEN's price when its position is
    closed today: /orders is current-session-only, /positions is empty once
    fully closed, and /gainloss doesn't list the close until it settles (next
    day). /history keeps per-fill trades for prior days, each with a signed
    quantity (+ = bought/long, − = sold/short), price, and signed cash amount.

    Each fill: {date (YYYY-MM-DD), qty (signed float), price (float),
    amount (signed float, includes fees)}.
    """
    try:
        resp = tradier_get(
            f"accounts/{account_id}/history",
            {"limit": "5000", "page": "1", "type": "trade", "start": start, "end": end},
            token, base,
        )
    except Exception:
        return {}
    hist = resp.get("history")
    if not isinstance(hist, dict):
        return {}
    events = hist.get("event") or []
    if isinstance(events, dict):
        events = [events]
    out: dict[str, list[dict]] = {}
    for e in events:
        if (e.get("type") or "").lower() != "trade":
            continue
        t = e.get("trade") or {}
        sym = t.get("symbol")
        if not sym:
            continue
        out.setdefault(sym, []).append({
            "date": (e.get("date") or "")[:10],
            "qty": _num(t.get("quantity")) or 0.0,
            "price": _num(t.get("price")) or 0.0,
            "amount": _num(e.get("amount")) or 0.0,
        })
    return out


def _gainloss_group_by_close(rows: list[dict]) -> list[list[dict]]:
    """Group closure legs that share the same close_date (down to seconds).
    Multi-leg spread closes always share a timestamp; single-leg closes form
    one-element groups."""
    by_ts: dict[str, list[dict]] = {}
    order: list[str] = []
    for r in rows:
        ts = (r.get("close_date") or "").strip()
        if ts not in by_ts:
            by_ts[ts] = []
            order.append(ts)
        by_ts[ts].append(r)
    return [by_ts[ts] for ts in order]


def _format_gainloss_ticker(group: list[dict]) -> tuple[str, str]:
    """Return (ticker_label, expiration) for a group of closure legs."""
    if not group:
        return ("?", "")
    underlyings: set[str] = set()
    expirations: set[str] = set()
    strike_parts: list[tuple[float, str]] = []
    for r in group:
        sym = r.get("symbol") or ""
        dec = decode_occ(sym)
        if dec:
            underlyings.add(dec["root"])
            expirations.add(dec["expiration"])
            strike_parts.append((dec["strike"], dec["side"][0].upper()))
        else:
            underlyings.add(sym)
    underlying_text = "/".join(sorted(underlyings)) or "?"
    exp_text = " / ".join(sorted(expirations)) if expirations else ""
    if strike_parts:
        # Distinguish sides — if all same side, group together
        sides = {s for _, s in strike_parts}
        strike_parts.sort()
        if len(sides) == 1:
            side = next(iter(sides))
            strikes = "/".join(_fmt_strike(k) for k, _ in strike_parts)
            structure = f"{strikes}{side}"
        else:
            puts = sorted(k for k, s in strike_parts if s == "P")
            calls = sorted(k for k, s in strike_parts if s == "C")
            chunks = []
            if puts: chunks.append(f"{'/'.join(_fmt_strike(k) for k in puts)}P")
            if calls: chunks.append(f"{'/'.join(_fmt_strike(k) for k in calls)}C")
            structure = " + ".join(chunks)
        return (f"{underlying_text} {structure}", exp_text)
    return (underlying_text, exp_text)


def render_gainloss_table(rows: list[dict], date_label: str) -> None:
    if not rows:
        print(f"{Y}No closed positions for {date_label}.{N}")
        return
    groups = _gainloss_group_by_close(rows)

    cols = [
        ("#",           3,  "right"),
        ("Ticker",      36, "left"),
        ("Expiration",  12, "left"),
        ("Opened",      11, "left"),
        ("Closed",      16, "left"),
        ("Qty",         4,  "right"),
        ("Cost",        11, "right"),
        ("Proceeds",    11, "right"),
        ("P&L $",       11, "right"),
        ("P&L %",       8,  "right"),
    ]
    header = "  ".join(_pad(name, w, align) for name, w, align in cols)
    print(BOLD + header + N)
    print(DIM + "─" * len(header) + N)

    total_cost = 0.0
    total_proceeds = 0.0
    total_pnl = 0.0
    for i, group in enumerate(groups, 1):
        ticker, exp_text = _format_gainloss_ticker(group)
        # Use the FIRST leg's open/close timestamps for display
        first = group[0]
        opened = _format_date(first.get("open_date"))[:10]
        closed = _format_date(first.get("close_date"))[:16]
        # Total qty across legs (use abs of any leg — they should match within a spread)
        qty_total = abs(int(_num(first.get("quantity")) or 0))
        # Sum cost, proceeds, P&L across the group
        gcost = sum(_num(r.get("cost")) or 0 for r in group)
        gproceeds = sum(_num(r.get("proceeds")) or 0 for r in group)
        gpnl = sum(_num(r.get("gain_loss")) or 0 for r in group)
        # P&L % = P&L / |cost| (cost-basis denominator)
        gpct = (gpnl / abs(gcost) * 100) if gcost else 0.0

        total_cost += gcost
        total_proceeds += gproceeds
        total_pnl += gpnl

        color = G if gpnl >= 0 else R
        pnl_str = f"{color}{_sign_dollars(gpnl)}{N}"
        pct_str = f"{color}{gpct:+.1f}%{N}"
        row_str = "  ".join([
            _pad(str(i), cols[0][1], cols[0][2]),
            _pad(ticker, cols[1][1], cols[1][2]),
            _pad(exp_text, cols[2][1], cols[2][2]),
            _pad(opened, cols[3][1], cols[3][2]),
            _pad(closed, cols[4][1], cols[4][2]),
            _pad(str(qty_total), cols[5][1], cols[5][2]),
            _pad(_sign_dollars(gcost), cols[6][1], cols[6][2]),
            _pad(_sign_dollars(gproceeds), cols[7][1], cols[7][2]),
            _pad_ansi(pnl_str, cols[8][1], cols[8][2]),
            _pad_ansi(pct_str, cols[9][1], cols[9][2]),
        ])
        print(row_str)

    print(DIM + "─" * len(header) + N)
    total_color = G if total_pnl >= 0 else R
    total_pct = (total_pnl / abs(total_cost) * 100) if total_cost else 0.0
    summary_pnl = f"{total_color}{_sign_dollars(total_pnl)}{N}"
    summary_pct = f"{total_color}{total_pct:+.1f}%{N}"
    print(f"  {BOLD}Total ({len(groups)} closure{'s' if len(groups) != 1 else ''}):{N}  "
          f"cost {_sign_dollars(total_cost)}   proceeds {_sign_dollars(total_proceeds)}   "
          f"P&L {summary_pnl} ({summary_pct})")


def gainloss_to_intraday_rows(gl_rows: list[dict]) -> list[dict]:
    """Convert Tradier /gainloss closed_position rows into the row shape that
    render_intraday_realized_table expects, so settled multi-day history and
    live intraday closes can be merged into ONE table.

    Tradier stamps every settled close on a calendar day with the SAME
    midnight-UTC timestamp, so grouping on close_date alone would conflate
    unrelated spreads (even across underlyings) into one row. We therefore
    subdivide each close-timestamp group by (underlying root, open_date) so
    each originating spread becomes its own row.

    gainloss rows are always fully-closed/settled, so _is_partial is False.
    """
    def _root(sym):
        dec = decode_occ(sym or "")
        return dec["root"] if dec else (sym or "?")

    rows: list[dict] = []
    for tsgroup in _gainloss_group_by_close(gl_rows):
        if not tsgroup:
            continue
        # Split this close-timestamp bucket into per-spread subgroups so two
        # different positions closed the same day don't collapse together.
        subgroups: dict[tuple, list[dict]] = {}
        suborder: list[tuple] = []
        for r in tsgroup:
            key = (_root(r.get("symbol") or ""), (r.get("open_date") or "")[:10])
            if key not in subgroups:
                subgroups[key] = []
                suborder.append(key)
            subgroups[key].append(r)

        for key in suborder:
            group = subgroups[key]
            legs = [r.get("symbol") for r in group if r.get("symbol")]
            gcost = sum(_num(r.get("cost")) or 0 for r in group)
            gproceeds = sum(_num(r.get("proceeds")) or 0 for r in group)
            gpnl = sum(_num(r.get("gain_loss")) or 0 for r in group)
            first = group[0]
            qty = abs(int(_num(first.get("quantity")) or 0))
            underlying = "?"
            if legs:
                dec = decode_occ(legs[0])
                underlying = dec["root"] if dec else legs[0]
            rows.append({
                "_legs": sorted(set(legs)),
                "_underlying": underlying,
                "_close_order_id": None,
                "_close_order_ids": [],
                "_open_order_id": None,
                "_open_order_ids": [],
                "_n_close_orders": 1,
                "_is_partial": False,
                "_open_qty_total": qty,
                "_source": "gainloss",
                "quantity": qty,
                "cost": gcost,
                "proceeds": gproceeds,
                "gain_loss": gpnl,
                "gain_loss_percent": (gpnl / abs(gcost) * 100) if gcost else 0.0,
                # /gainloss timestamps are calendar dates at midnight UTC (no real
                # intraday time). Keep them date-only so _format_date doesn't
                # TZ-shift them to the previous evening.
                "open_date": (first.get("open_date") or "")[:10] or None,
                "close_date": (first.get("close_date") or "")[:10] or None,
            })
    return rows


def compute_intraday_realized(orders: list[dict], start_date: str, end_date: str,
                              positions: list[dict] | None = None,
                              history_opens: dict[str, list[dict]] | None = None,
                              history_days_back: int = 7,
                              verbose: bool = False) -> list[dict]:
    """Compute realized P&L for filled CLOSE orders in [start_date, end_date]
    by matching each one to its originating OPEN order via leg-symbol set.

    Critical for INTRADAY visibility — Tradier's /gainloss endpoint only
    reports fully-settled, fully-closed positions. Partial closes on still-
    open positions never show up there. This function instead reads the
    orders endpoint (which has filled trades immediately) and matches by
    leg symbols.

    Returns rows in the same shape render_intraday_realized_table expects.
    """
    opens_by_legkey: dict[frozenset, list[dict]] = {}   # leg_key → [open_order, ...] (FIFO order)
    close_candidates: list[tuple[dict, list, frozenset, str]] = []

    for o in orders:
        status = (o.get("status") or "").lower()
        if status not in ("filled", "partially_filled"):
            continue
        legs = o.get("leg") or o.get("legs") or []
        if isinstance(legs, dict):
            legs = [legs]
        leg_syms: list[str] = []
        for l in legs:
            sym = l.get("option_symbol") or l.get("symbol")
            if sym:
                leg_syms.append(sym)
        if not leg_syms:
            sym = o.get("option_symbol") or o.get("symbol")
            if sym:
                leg_syms = [sym]
        if not leg_syms:
            continue
        leg_key = frozenset(leg_syms)

        # Direction: any side containing 'open' or 'close'
        sides: list[str] = []
        for l in legs:
            s = str(l.get("side") or "").lower()
            if s:
                sides.append(s)
        if not sides:
            sides = [str(o.get("side") or "").lower()]
        has_close = any("close" in s for s in sides)
        has_open  = any("open"  in s for s in sides)

        date_str = (o.get("transaction_date") or o.get("create_date") or "")[:10]

        if has_close and date_str and start_date <= date_str <= end_date:
            close_candidates.append((o, leg_syms, leg_key, date_str))
        if has_open:
            opens_by_legkey.setdefault(leg_key, []).append(o)

    # Index current positions by leg symbol for partial-close fallback.
    pos_by_sym: dict[str, dict] = {}
    if positions:
        for p in positions:
            sym = p.get("symbol")
            if sym:
                pos_by_sym[sym] = p

    if verbose:
        n_opens = sum(len(v) for v in opens_by_legkey.values())
        print(f"  [verbose] fetched {len(orders)} orders, {len(close_candidates)} close candidates in range, {n_opens} opens across {len(opens_by_legkey)} unique leg keys")

    # ── Multi-leg-aware aggregation ────────────────────────────────────────
    # BUG FIX (v2): A spread opened as multileg ($X debit/credit, one order, 2 legs)
    # is often closed by N separate single-leg orders (Tradier's UI default).
    # Before this fix, each leg-close was matched independently to the same open
    # and each got attributed the FULL spread's open cost — double-counting and
    # inverting the sign. Now we group all close orders that match the same open
    # and sum their cash flows into ONE row per closeout.
    rows: list[dict] = []
    unmatched: list[tuple[dict, list]] = []
    fallback_rows: list[dict] = []

    # Helper: sign of an order's cash flow
    #   +1 means we RECEIVED money, -1 means we PAID
    #
    # CRITICAL: SIDE first, TYPE last.
    # Tradier sets `type=debit/credit` based on the SPREAD's INTRINSIC NATURE
    # (a bull call is a "debit spread" regardless of whether THIS particular
    # order opens or closes it). The cash flow direction of any given order
    # comes from `side`: "buy*" = paid, "sell*" = received. A CLOSE order on
    # a debit spread comes back from Tradier with type="debit" AND
    # side="sell_to_close" — type lies about cash, side tells the truth.
    # Pre-fix used type first, which inverted the sign on multileg closes.
    def _cash_sign(order: dict, default_side_sign: int) -> int:
        side = str(order.get("side") or "").lower()
        if not side:
            legs = order.get("leg") or order.get("legs") or []
            if isinstance(legs, dict): legs = [legs]
            for l in legs:
                s = str(l.get("side") or "").lower()
                if "sell" in s: return +1
                if "buy"  in s: return -1
            t = (order.get("type") or "").lower()
            if t == "credit": return +1
            if t == "debit":  return -1
            return default_side_sign
        if "sell" in side: return +1
        if "buy"  in side: return -1
        return default_side_sign

    # Helper: convert Tradier's order.quantity into actual SPREAD UNITS.
    # Tradier reports multileg order quantity as leg_count × spread_units
    # (so a 1-contract vertical = qty 2, a 1-contract iron condor = qty 4,
    # a 1-contract butterfly = qty 3). Single-leg orders are quantity =
    # contract count directly.
    def _spread_units(order: dict) -> int:
        qty = abs(int(_num(order.get("quantity")) or 0))
        if (order.get("class") or "").lower() != "multileg":
            return qty
        legs = order.get("leg") or order.get("legs") or []
        if isinstance(legs, dict): legs = [legs]
        n_legs = len(legs)
        if n_legs <= 1:
            return qty
        # avg_fill_price is per-spread; divide qty by leg count to get spreads
        return max(1, qty // n_legs)

    # ── FIFO matching: each close order consumes qty across multiple opens ──
    # Sort opens by create_date (FIFO) so earliest buys are consumed first.
    for key in opens_by_legkey:
        opens_by_legkey[key].sort(key=lambda o: o.get("create_date") or o.get("transaction_date") or "")

    # Track remaining qty per open order (FIFO consumption)
    open_remaining: dict[int, int] = {}  # open_order_id → remaining qty
    for open_list in opens_by_legkey.values():
        for o in open_list:
            oid = o.get("id")
            if oid is not None:
                open_remaining[oid] = _spread_units(o)

    def _find_opens(leg_key: frozenset) -> list[dict] | None:
        opens = opens_by_legkey.get(leg_key)
        if opens:
            return opens
        for k, v in opens_by_legkey.items():
            if leg_key.issubset(k) or k.issubset(leg_key):
                return v
        return None

    # ── /history fallback: recover a prior-day open's cost basis ────────────
    # When a position is closed TODAY but was opened on an EARLIER day, the open
    # order is gone from /orders (current-session only) and the position is gone
    # from /positions (fully closed), and /gainloss hasn't settled it yet. The
    # opening fills still live in /history, so we reconstruct the cost basis from
    # there: for each leg we take the qty-weighted average open price across all
    # opening-direction fills within the look-back window (so partial entries
    # average into one basis, per the "average price of the whole trade" rule).
    def _realized_from_history(close_order: dict, close_leg_syms: list[str]) -> dict | None:
        if not history_opens:
            return None
        try:
            cutoff = (datetime.strptime(end_date, "%Y-%m-%d")
                      - timedelta(days=history_days_back)).strftime("%Y-%m-%d")
        except ValueError:
            return None
        legs = close_order.get("leg") or close_order.get("legs") or []
        if isinstance(legs, dict):
            legs = [legs]
        if not legs:
            legs = [close_order]   # single-leg order: side/symbol on the order itself
        units = _spread_units(close_order)
        if units <= 0:
            return None
        close_avg = _num(close_order.get("avg_fill_price")) or 0.0

        total_open_cash = 0.0
        open_dates: list[str] = []
        any_partial = False
        n_option_legs = 0
        for l in legs:
            sym = l.get("option_symbol") or l.get("symbol")
            side = str(l.get("side") or close_order.get("side") or "").lower()
            if not sym or "close" not in side:
                return None
            fills = history_opens.get(sym)
            if not fills:
                return None
            # Closed by selling ⇒ opened by buying (qty>0); closed by buying ⇒
            # opened by selling (qty<0). Restrict to prior days in the window.
            want_buy = "sell" in side
            ofills = [f for f in fills
                      if cutoff <= f["date"] < end_date
                      and f["qty"] != 0 and ((f["qty"] > 0) == want_buy)]
            if not ofills:
                return None
            oqty = sum(abs(f["qty"]) for f in ofills)
            if oqty <= 0:
                return None
            avg_price = sum(f["price"] * abs(f["qty"]) for f in ofills) / oqty
            if oqty > units:
                any_partial = True
            mult = 100 if decode_occ(sym) else 1
            if mult == 100:
                n_option_legs += 1
            open_sign = -1 if want_buy else +1
            total_open_cash += open_sign * avg_price * units * mult
            open_dates += [f["date"] for f in ofills]

        mult = 100 if n_option_legs else 1
        cls = (close_order.get("class") or "").lower()
        if cls == "multileg":
            ct = (close_order.get("type") or "").lower()
            close_sign = +1 if ct == "credit" else (-1 if ct == "debit" else _cash_sign(close_order, +1))
        else:
            close_sign = _cash_sign(close_order, +1)
        total_close_cash = close_sign * close_avg * units * mult

        pnl = total_open_cash + total_close_cash
        cost = -total_open_cash
        proceeds = total_close_cash
        close_date = close_order.get("transaction_date") or close_order.get("create_date") or ""
        close_id = close_order.get("id")
        return {
            "_legs": sorted(set(close_leg_syms)),
            "_underlying": (close_order.get("symbol") or "?"),
            "_close_order_id": str(close_id) if close_id else None,
            "_close_order_ids": [str(close_id)] if close_id else [],
            "_open_order_id": None,
            "_open_order_ids": [],
            "_n_close_orders": 1,
            "_is_partial": any_partial,
            "_via_history": True,
            "_open_qty_total": units,
            "quantity": units,
            "cost": cost,
            "proceeds": proceeds,
            "gain_loss": pnl,
            "gain_loss_percent": (pnl / abs(cost) * 100) if cost else 0.0,
            "open_date": (min(open_dates) if open_dates else None),
            "close_date": close_date,
        }

    for close_order, close_leg_syms, leg_key, _ in close_candidates:
        open_list = _find_opens(leg_key)
        if not open_list:
            row = _realized_from_positions(close_order, close_leg_syms, pos_by_sym)
            if row:
                fallback_rows.append(row)
                if verbose:
                    print(f"  [verbose] matched {close_order.get('id')} via positions fallback (partial close)")
                continue
            row = _realized_from_history(close_order, close_leg_syms)
            if row:
                fallback_rows.append(row)
                if verbose:
                    print(f"  [verbose] matched {close_order.get('id')} via /history fallback (prior-day open, basis from {row.get('open_date')})")
                continue
            unmatched.append((close_order, close_leg_syms))
            if verbose:
                print(f"  [verbose] UNMATCHED close order {close_order.get('id')} — open not in /orders, /positions, or /history (last {history_days_back}d)")
            continue

        close_qty_remaining = _spread_units(close_order)
        close_avg = _num(close_order.get("avg_fill_price")) or 0.0
        close_sign = _cash_sign(close_order, +1)
        close_date = close_order.get("transaction_date") or close_order.get("create_date") or ""
        close_id = close_order.get("id")

        # Determine multiplier from any open in the group
        sample_open = open_list[0]
        sample_legkey: set = set()
        sample_legs = sample_open.get("leg") or sample_open.get("legs") or []
        if isinstance(sample_legs, dict): sample_legs = [sample_legs]
        for l in sample_legs:
            sym = l.get("option_symbol") or l.get("symbol")
            if sym: sample_legkey.add(sym)
        if not sample_legkey:
            sym = sample_open.get("option_symbol") or sample_open.get("symbol")
            if sym: sample_legkey = {sym}
        is_multileg = (sample_open.get("class") or "").lower() == "multileg"
        has_option = bool(sample_legkey) and any(decode_occ(s) for s in sample_legkey)
        mult = 100 if (is_multileg or has_option) else 1

        # FIFO: consume close qty across opens in chronological order
        total_open_cash = 0.0
        total_close_cash = 0.0
        total_matched_qty = 0
        matched_open_ids: list[int] = []

        for open_order in open_list:
            if close_qty_remaining <= 0:
                break
            oid = open_order.get("id")
            if oid is None:
                continue
            avail = open_remaining.get(oid, 0)
            if avail <= 0:
                continue

            take = min(avail, close_qty_remaining)
            open_avg = _num(open_order.get("avg_fill_price")) or 0.0
            open_sign = _cash_sign(open_order, -1)

            total_open_cash += open_sign * open_avg * take * mult
            total_close_cash += close_sign * close_avg * take * mult
            total_matched_qty += take
            close_qty_remaining -= take
            open_remaining[oid] -= take
            matched_open_ids.append(oid)

            if verbose:
                print(f"  [verbose] close {close_id}: consumed {take} from open {oid} (remaining {open_remaining[oid]})")

        if total_matched_qty == 0:
            unmatched.append((close_order, close_leg_syms))
            if verbose:
                print(f"  [verbose] UNMATCHED close order {close_id} — all opens for {leg_key} fully consumed")
            continue

        pnl = total_open_cash + total_close_cash
        cost = -total_open_cash
        proceeds = total_close_cash
        is_partial = close_qty_remaining > 0

        rows.append({
            "_legs": sorted(set(close_leg_syms)),
            "_underlying": (sample_open.get("symbol") or close_order.get("symbol") or "?"),
            "_close_order_id": str(close_id) if close_id else None,
            "_close_order_ids": [str(close_id)] if close_id else [],
            "_open_order_id": matched_open_ids[0] if matched_open_ids else None,
            "_open_order_ids": matched_open_ids,
            "_n_close_orders": 1,
            "_is_partial": is_partial,
            "_open_qty_total": total_matched_qty,
            "quantity": total_matched_qty,
            "cost": cost,
            "proceeds": proceeds,
            "gain_loss": pnl,
            "gain_loss_percent": (pnl / abs(cost) * 100) if cost else 0.0,
            "open_date": open_list[0].get("create_date") or open_list[0].get("transaction_date"),
            "close_date": close_date,
        })

    rows.extend(fallback_rows)

    if unmatched and not verbose:
        # Show a brief note even when not verbose so the user knows some closes were dropped
        ids = [str(o.get("id")) for o, _ in unmatched[:5]]
        print(f"  {Y}note: {len(unmatched)} close order(s) couldn't be matched to an open or current position (ids: {', '.join(ids)}). Try -V for details or --settled for the Tradier endpoint.{N}")
    # Newest close first
    rows.sort(key=lambda r: r.get("close_date") or "", reverse=True)
    return rows


def _realized_from_positions(close_order: dict, close_leg_syms: list[str],
                              pos_by_sym: dict[str, dict]) -> dict | None:
    """Compute realized P&L for a partial close using the current position's
    cost basis. Works when the position still exists (i.e. partial close —
    quantity reduced but not zero). Per-unit cost is invariant under partial
    close, so closed_cost = current_cost_basis × (close_qty / remaining_qty).
    Returns None if the position can't be matched leg-for-leg.
    """
    close_qty = abs(int(_num(close_order.get("quantity")) or 0))
    close_avg = _num(close_order.get("avg_fill_price")) or 0.0
    if close_qty == 0:
        return None

    # All close legs must still have a position; else we can't compute.
    matched_positions: list[tuple[str, dict]] = []
    for sym in close_leg_syms:
        p = pos_by_sym.get(sym)
        if not p:
            return None
        matched_positions.append((sym, p))
    if not matched_positions:
        return None

    # Sum per-leg cost_basis (signed by Tradier). Quantity per leg = remaining
    # quantity (since position rows are post-close). Per-unit cost = cost_basis / qty.
    # Each leg has the SAME per-unit-of-spread cost contribution; sum gives spread cost.
    is_multileg = (close_order.get("class") or "").lower() == "multileg"
    has_option = any(decode_occ(s) for s in close_leg_syms)
    mult = 100 if (is_multileg or has_option) else 1

    closed_cost_total = 0.0
    remaining_qty = None
    for sym, p in matched_positions:
        pos_qty = abs(_num(p.get("quantity")) or 0)
        pos_cost = _num(p.get("cost_basis")) or 0.0
        if pos_qty == 0:
            return None
        # Per-spread cost contribution from this leg.
        per_unit = pos_cost / pos_qty
        closed_cost_total += per_unit * close_qty
        if remaining_qty is None:
            remaining_qty = int(pos_qty)
        else:
            remaining_qty = min(remaining_qty, int(pos_qty))

    # Close cash flow per spread-unit
    close_type = (close_order.get("type") or "").lower()
    if close_type == "credit":
        close_per_unit_signed = close_avg
    elif close_type == "debit":
        close_per_unit_signed = -close_avg
    else:
        side = str(close_order.get("side") or "").lower()
        if "sell" in side:   close_per_unit_signed = close_avg
        elif "buy" in side:  close_per_unit_signed = -close_avg
        else:                 close_per_unit_signed = close_avg
    close_cash = close_per_unit_signed * close_qty * mult

    # closed_cost_total is signed cost basis (positive = we paid, negative = we received)
    # P&L = close_cash - closed_cost_total (cash flow out − cash flow in)
    # Actually: open_cash = -closed_cost_total (cash flow at entry, signed)
    open_cash = -closed_cost_total
    pnl = open_cash + close_cash
    cost = closed_cost_total
    proceeds = close_cash

    return {
        "_legs": list(close_leg_syms),
        "_underlying": close_order.get("symbol") or "?",
        "_close_order_id": close_order.get("id"),
        "_open_order_id": None,
        "_is_partial": True,   # always — if it weren't partial, position wouldn't exist
        "_open_qty_total": (remaining_qty or 0) + close_qty,
        "_source": "positions_fallback",
        "quantity": close_qty,
        "cost": cost,
        "proceeds": proceeds,
        "gain_loss": pnl,
        "gain_loss_percent": (pnl / abs(cost) * 100) if cost else 0.0,
        "open_date": None,
        "close_date": close_order.get("transaction_date") or close_order.get("create_date"),
    }


def render_intraday_realized_table(rows: list[dict], date_label: str) -> None:
    if not rows:
        print(f"{Y}No close orders found for {date_label}.{N}")
        return

    cols = [
        ("#",          3,  "right"),
        ("Ticker",     36, "left"),
        ("Expiration", 12, "left"),
        ("Opened",     11, "left"),
        ("Closed",     16, "left"),
        ("Qty",        7,  "right"),
        ("Cost",       11, "right"),
        ("Proceeds",   11, "right"),
        ("P&L $",      11, "right"),
        ("P&L %",      8,  "right"),
    ]
    header = "  ".join(_pad(name, w, align) for name, w, align in cols)
    print(BOLD + header + N)
    print(DIM + "─" * len(header) + N)

    total_cost = total_proceeds = total_pnl = 0.0
    for i, r in enumerate(rows, 1):
        # Ticker label from legs
        underlyings: set[str] = set()
        expirations: set[str] = set()
        strike_parts: list[tuple[float, str]] = []
        for sym in r.get("_legs", []):
            dec = decode_occ(sym)
            if dec:
                underlyings.add(dec["root"])
                expirations.add(dec["expiration"])
                strike_parts.append((dec["strike"], dec["side"][0].upper()))
        underlying_text = "/".join(sorted(underlyings)) or r.get("_underlying", "?")
        exp_text = " / ".join(sorted(expirations)) if expirations else ""
        if strike_parts:
            sides_set = {s for _, s in strike_parts}
            strike_parts.sort()
            if len(sides_set) == 1:
                side = next(iter(sides_set))
                strikes_txt = "/".join(_fmt_strike(k) for k, _ in strike_parts)
                structure = f"{strikes_txt}{side}"
            else:
                puts  = sorted(k for k, s in strike_parts if s == "P")
                calls = sorted(k for k, s in strike_parts if s == "C")
                chunks = []
                if puts:  chunks.append(f"{'/'.join(_fmt_strike(k) for k in puts)}P")
                if calls: chunks.append(f"{'/'.join(_fmt_strike(k) for k in calls)}C")
                structure = " + ".join(chunks)
            ticker = f"{underlying_text} {structure}"
        else:
            ticker = underlying_text

        opened = _format_date(r.get("open_date"))[:10]
        closed = _format_date(r.get("close_date"))[:16]
        qty_disp = f"{r['quantity']}/{r['_open_qty_total']}" if r.get("_is_partial") else str(r['quantity'])

        cost     = r.get("cost", 0) or 0
        proceeds = r.get("proceeds", 0) or 0
        pnl      = r.get("gain_loss", 0) or 0
        pct      = r.get("gain_loss_percent", 0) or 0

        total_cost     += cost
        total_proceeds += proceeds
        total_pnl      += pnl

        color = G if pnl >= 0 else R
        pnl_str = f"{color}{_sign_dollars(pnl)}{N}"
        pct_str = f"{color}{pct:+.1f}%{N}"
        row_str = "  ".join([
            _pad(str(i),                       cols[0][1], cols[0][2]),
            _pad(ticker,                       cols[1][1], cols[1][2]),
            _pad(exp_text,                     cols[2][1], cols[2][2]),
            _pad(opened,                       cols[3][1], cols[3][2]),
            _pad(closed,                       cols[4][1], cols[4][2]),
            _pad(qty_disp,                     cols[5][1], cols[5][2]),
            _pad(_sign_dollars(cost),          cols[6][1], cols[6][2]),
            _pad(_sign_dollars(proceeds),      cols[7][1], cols[7][2]),
            _pad_ansi(pnl_str,                 cols[8][1], cols[8][2]),
            _pad_ansi(pct_str,                 cols[9][1], cols[9][2]),
        ])
        print(row_str)

    print(DIM + "─" * len(header) + N)
    total_color = G if total_pnl >= 0 else R
    total_pct = (total_pnl / abs(total_cost) * 100) if total_cost else 0.0
    summary_pnl = f"{total_color}{_sign_dollars(total_pnl)}{N}"
    summary_pct = f"{total_color}{total_pct:+.1f}%{N}"
    n = len(rows)
    print(f"  {BOLD}Total ({n} closure{'s' if n != 1 else ''}):{N}  "
          f"cost {_sign_dollars(total_cost)}   proceeds {_sign_dollars(total_proceeds)}   "
          f"P&L {summary_pnl} ({summary_pct})")
    has_partial = any(r.get("_is_partial") for r in rows)
    note = "computed from filled close orders — includes partial closes"
    if has_partial:
        note += ". Qty column shows closed/total for partials."
    print(f"  {D}({note}){N}")


def aggregate_positions(positions: list[dict], orders: list[dict]) -> list[PositionRow]:
    """Group per-leg positions into multi-leg rows when they originated from
    the same filled multi-leg order. Singletons remain singletons."""
    # Index positions by symbol for quick lookup
    pos_by_sym: dict[str, dict] = {}
    for p in positions:
        sym = p.get("symbol")
        if sym:
            pos_by_sym[sym] = p
    used_symbols: set[str] = set()
    rows: list[PositionRow] = []

    # Pass 1 — match filled multi-leg orders to their surviving positions
    for o in orders:
        if (o.get("class") or "").lower() != "multileg":
            continue
        if (o.get("status") or "").lower() not in ("filled", "partially_filled"):
            continue
        legs = o.get("leg") or o.get("legs") or []
        if isinstance(legs, dict):
            legs = [legs]
        if len(legs) < 2:
            continue
        matched_positions = []
        for leg in legs:
            sym = leg.get("option_symbol") or leg.get("symbol")
            if not sym or sym not in pos_by_sym:
                matched_positions = []
                break
            matched_positions.append((leg, pos_by_sym[sym]))
        if not matched_positions:
            continue
        # All legs of this order still exist in positions — aggregate them
        row = _build_multileg_row(o, matched_positions)
        if row:
            rows.append(row)
            for _, p in matched_positions:
                used_symbols.add(p["symbol"])

    # Pass 2 — anything left (singleton options or equities) is its own row
    for p in positions:
        sym = p.get("symbol")
        if not sym or sym in used_symbols:
            continue
        rows.append(_build_single_row(p))

    # Sort by acquired desc so newest opens at the top
    rows.sort(key=lambda r: r.acquired, reverse=True)
    for i, r in enumerate(rows, 1):
        r.row_num = i
    return rows


def _build_multileg_row(order: dict, leg_pairs: list) -> PositionRow | None:
    """leg_pairs: list of (order_leg_dict, position_dict)"""
    legs_info = []
    underlying = order.get("symbol") or ""
    expiration = None
    for leg, pos in leg_pairs:
        sym = pos["symbol"]
        decoded = decode_occ(sym)
        if not decoded:
            continue
        if expiration is None:
            expiration = decoded["expiration"]
        side = decoded["side"]
        strike = decoded["strike"]
        qty = _num(pos.get("quantity")) or 0
        original_side = (leg.get("side") or "").lower()
        legs_info.append({
            "symbol": sym, "side": side, "strike": strike,
            "qty": qty, "original_side": original_side,
        })
    if not legs_info:
        return None
    # Attach per-leg cost_basis from Tradier. Tradier signs these for us:
    #   long position  → positive cost_basis (cash you paid)
    #   short position → negative cost_basis (cash you collected)
    # So summing them gives the correct net signed cost basis with no
    # order.type field guesswork.
    for li, (_, pos) in zip(legs_info, leg_pairs):
        li["cost_basis"] = _num(pos.get("cost_basis")) or 0
    # Classify shape
    n = len(legs_info)
    label = "spread"
    if n == 2 and len({l["side"] for l in legs_info}) == 1:
        label = "vertical"
    elif n == 3 and len({l["side"] for l in legs_info}) == 1:
        label = "butterfly"
    elif n == 4 and len({l["side"] for l in legs_info}) == 2:
        # 2 puts + 2 calls → IC; if mid strikes match → IB
        put_strikes = sorted(l["strike"] for l in legs_info if l["side"] == "put")
        call_strikes = sorted(l["strike"] for l in legs_info if l["side"] == "call")
        if len(put_strikes) == 2 and len(call_strikes) == 2:
            label = "iron_butterfly" if put_strikes[1] == call_strikes[0] else "iron_condor"
    # Build structure_text
    if label == "vertical":
        strikes = sorted(l["strike"] for l in legs_info)
        side_char = legs_info[0]["side"][0].upper()
        structure = f"{_fmt_strike(strikes[0])}/{_fmt_strike(strikes[1])}{side_char}"
    elif label in ("iron_condor", "iron_butterfly"):
        ps = sorted(l["strike"] for l in legs_info if l["side"] == "put")
        cs = sorted(l["strike"] for l in legs_info if l["side"] == "call")
        structure = (f"{_fmt_strike(ps[0])}/{_fmt_strike(ps[1])}P + "
                     f"{_fmt_strike(cs[0])}/{_fmt_strike(cs[1])}C")
    elif label == "butterfly":
        strikes = sorted(l["strike"] for l in legs_info)
        side_char = legs_info[0]["side"][0].upper()
        structure = (f"{_fmt_strike(strikes[0])}/{_fmt_strike(strikes[1])}/"
                     f"{_fmt_strike(strikes[2])}{side_char}")
    else:
        structure = " + ".join(
            f"{_fmt_strike(l['strike'])}{l['side'][0].upper()}" for l in legs_info
        )

    # Cost basis: sum Tradier's per-leg cost_basis. Tradier signs them already:
    #   long  →  +cost_basis (cash you paid)
    #   short →  −cost_basis (cash you collected)
    # Net is automatically negative for credit spreads, positive for debit.
    # We deliberately do NOT use order.avg_fill_price + order.type, because
    # Tradier often returns type="limit" on multi-leg fills (instead of
    # "credit"/"debit"), which made the previous fallback sign-flip wrong.
    qty_per_leg = abs(int(legs_info[0]["qty"]))
    cost_basis = sum(_num(p.get("cost_basis")) or 0 for _, p in leg_pairs)

    return PositionRow(
        kind="multileg",
        label=label,
        underlying=underlying,
        expiration=expiration,
        acquired=_format_date(order.get("create_date") or order.get("transaction_date")),
        cost_basis=cost_basis,
        quantity=qty_per_leg,
        structure_text=structure,
        legs=legs_info,
    )


def _build_single_row(pos: dict) -> PositionRow:
    sym = pos.get("symbol") or ""
    qty = _num(pos.get("quantity")) or 0
    cost = _num(pos.get("cost_basis")) or 0
    decoded = decode_occ(sym)
    if decoded:
        side_char = decoded["side"][0].upper()
        structure = f"{_fmt_strike(decoded['strike'])}{side_char}"
        return PositionRow(
            kind="single",
            label="option",
            underlying=decoded["root"],
            expiration=decoded["expiration"],
            acquired=_format_date(pos.get("date_acquired")),
            cost_basis=cost,
            quantity=abs(int(qty)),
            structure_text=structure,
            legs=[{
                "symbol": sym, "side": decoded["side"], "strike": decoded["strike"],
                "qty": qty, "original_side": "buy_to_open" if qty > 0 else "sell_to_open",
            }],
        )
    return PositionRow(
        kind="single", label="equity",
        underlying=sym, expiration=None,
        acquired=_format_date(pos.get("date_acquired")),
        cost_basis=cost, quantity=abs(int(qty)),
        structure_text=sym,
        legs=[{"symbol": sym, "side": "equity", "strike": 0, "qty": qty, "original_side": "buy"}],
    )


def _fmt_strike(s: float) -> str:
    if s == int(s):
        return str(int(s))
    return f"{s:.2f}".rstrip("0").rstrip(".")


def _normalize_date(s: str | None) -> str | None:
    """Normalize varied date input shapes to canonical YYYY-MM-DD.

    Accepts:
      2026-06-04 / 2026-06-4 / 2026-6-4     (ISO, zero-pad optional)
      06-04 / 6-4                            (MM-DD, fills current year)
      06/04/2026                             (US slash form)
      06/04 or 6/4                           (US slash, current year)
      20260604                               (8-digit YYYYMMDD)
    Returns None if unparseable.
    """
    if not s:
        return None
    s = s.strip().replace("/", "-")
    today = datetime.now()
    parts = s.split("-")
    try:
        if len(parts) == 3:
            if len(parts[0]) == 4:
                y, mo, d = parts
            elif len(parts[2]) == 4:
                mo, d, y = parts
            else:
                return None
            return f"{int(y):04d}-{int(mo):02d}-{int(d):02d}"
        if len(parts) == 2:
            mo, d = parts
            return f"{today.year:04d}-{int(mo):02d}-{int(d):02d}"
        if len(parts) == 1 and len(parts[0]) == 8 and parts[0].isdigit():
            y, mo, d = parts[0][:4], parts[0][4:6], parts[0][6:8]
            return f"{int(y):04d}-{int(mo):02d}-{int(d):02d}"
    except (ValueError, TypeError):
        return None
    return None


def _format_date(s):
    if not s:
        return "—"
    try:
        # Try with timezone
        if "T" in s:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
            return dt.astimezone().strftime("%Y-%m-%d %H:%M")
        return s
    except Exception:
        return str(s)[:19]


# ─── P&L computation ──────────────────────────────────────────────────────

def compute_pnl(row: PositionRow, quotes: dict) -> None:
    """Fill row.bid / row.ask / row.pnl_dollar / row.pnl_pct from current quotes.

    Tradier does NOT expose live unrealized P&L on positions (only cost_basis,
    quantity, symbol, date_acquired — see /accounts/{id}/positions docs). So
    we compute it the standard way:

        market_value = Σ ( qty_signed × mid × 100 )
        P&L          = market_value − cost_basis

    Both sides are SIGNED on the same basis:
      • long position: qty > 0, cost_basis > 0 (paid), mkt_value > 0
      • short position: qty < 0, cost_basis < 0 (collected), mkt_value < 0

    For a bull put for $6 credit currently mid $3.90:
       cost_basis = −$600   (Tradier reports short leg cost_basis negative)
       mkt_value  = −$390   (the open short obligation is worth $390 to close)
       P&L        = −390 − (−600) = +$210 ✓
    """
    if not row.legs:
        return
    market_value = 0.0      # signed: + asset, − liability
    mark_low = 0.0          # absolute spread mark — conservative side
    mark_high = 0.0         # absolute spread mark — aggressive side
    have_quotes = True
    for leg in row.legs:
        q = quotes.get(leg["symbol"])
        if not q:
            have_quotes = False
            continue
        b = _num(q.get("bid")) or 0
        a = _num(q.get("ask")) or 0
        mid = (b + a) / 2
        qty_signed = leg.get("qty") or 0     # + long, − short
        market_value += qty_signed * mid * 100
        # Spread mark — net price if you re-entered this same orientation.
        # Use mid for both sides initially; we'll split bid/ask below.
        if qty_signed > 0:   # long leg — pay ask to buy / receive bid to sell
            mark_low  += b   # bid side of spread (conservative for closing long)
            mark_high += a
        elif qty_signed < 0: # short leg — receive bid to sell / pay ask to buy
            mark_low  -= a   # closing the short = buying back at ask (worst)
            mark_high -= b   # closing the short = buying back at bid (best)
    if not have_quotes:
        # Missing one or more quotes — leave bid/ask None, P&L 0
        return
    # Display bid/ask as the absolute spread quote. For a credit spread the raw
    # values come out negative (you'd pay debit to close). The user thinks of
    # the spread as "$3.80 bid / $4.00 ask" regardless of which side they own.
    raw_bid, raw_ask = sorted([abs(mark_low), abs(mark_high)])
    row.bid = raw_bid
    row.ask = raw_ask
    row.pnl_dollar = market_value - row.cost_basis
    denom = abs(row.cost_basis) if row.cost_basis else 1
    row.pnl_pct = (row.pnl_dollar / denom) * 100 if denom else 0


# ─── Table rendering ──────────────────────────────────────────────────────

def render_table(rows: list[PositionRow]) -> None:
    if not rows:
        print(f"{Y}No open positions.{N}")
        return

    cols = [
        ("#",         3,  "right"),
        ("Ticker",    36, "left"),
        ("Acquired",  16, "left"),
        ("Entry",     8,  "right"),   # per-unit avg entry price (signed: −credit, +debit)
        ("Cost",      11, "right"),
        ("Bid",       7,  "right"),
        ("Ask",       7,  "right"),
        ("P&L $",     11, "right"),
        ("P&L %",     8,  "right"),
        ("Qty",       5,  "right"),
    ]
    header = "  ".join(_pad(name, w, align) for name, w, align in cols)
    print(BOLD + header + N)
    print(DIM + "─" * len(header) + N)
    for r in rows:
        ticker = f"{r.underlying} {r.structure_text}"
        if r.expiration and r.kind == "multileg":
            ticker = f"{r.underlying} {r.label[0:2].upper()} {r.structure_text} {r.expiration}"
        elif r.expiration:
            ticker = f"{r.underlying} {r.structure_text} {r.expiration}"
        # Per-unit entry price.
        #   Options (single + multi-leg): cost_basis / qty / 100
        #   Equity:                       cost_basis / qty
        # Sign mirrors cost_basis: − for credit collected, + for debit paid.
        is_equity = r.label == "equity"
        contract_mult = 1 if is_equity else 100
        divisor = (r.quantity or 1) * contract_mult
        entry_per_unit = (r.cost_basis / divisor) if divisor else 0.0
        if entry_per_unit >= 0:
            entry_str = f"+${entry_per_unit:.2f}"
        else:
            entry_str = f"−${abs(entry_per_unit):.2f}"
        cost_str = _sign_dollars(r.cost_basis)
        bid_str = f"${r.bid:.2f}" if r.bid is not None else "—"
        ask_str = f"${r.ask:.2f}" if r.ask is not None else "—"
        pnl_color = G if r.pnl_dollar >= 0 else R
        pnl_dollar_str = f"{pnl_color}{_sign_dollars(r.pnl_dollar)}{N}"
        pnl_pct_str = f"{pnl_color}{r.pnl_pct:+.1f}%{N}"
        row_str = "  ".join([
            _pad(str(r.row_num), cols[0][1], cols[0][2]),
            _pad(ticker, cols[1][1], cols[1][2]),
            _pad(r.acquired, cols[2][1], cols[2][2]),
            _pad(entry_str, cols[3][1], cols[3][2]),
            _pad(cost_str, cols[4][1], cols[4][2]),
            _pad(bid_str, cols[5][1], cols[5][2]),
            _pad(ask_str, cols[6][1], cols[6][2]),
            _pad_ansi(pnl_dollar_str, cols[7][1], cols[7][2]),
            _pad_ansi(pnl_pct_str, cols[8][1], cols[8][2]),
            _pad(str(r.quantity), cols[9][1], cols[9][2]),
        ])
        print(row_str)


def _pad(s, w, align):
    if len(s) > w:
        s = s[:w-1] + "…"
    if align == "right":
        return s.rjust(w)
    if align == "center":
        return s.center(w)
    return s.ljust(w)


def _pad_ansi(s, w, align):
    """Pad ANSI-colored string to visible width w."""
    visible = re.sub(r"\033\[[0-9;]*m", "", s)
    pad_n = max(0, w - len(visible))
    if align == "right":
        return " " * pad_n + s
    return s + " " * pad_n


def _sign_dollars(v):
    if v >= 0:
        return f"+${v:,.2f}"
    return f"−${abs(v):,.2f}"


# ─── Close (sell) order builder ───────────────────────────────────────────

def build_close_order_body(row: PositionRow, limit_price: float | None, qty: int,
                           preview: bool = True) -> tuple[str, str]:
    """Build the form-urlencoded body to close `qty` contracts of `row`.

    Returns (path, body). `path` is `accounts/{id}/orders` — caller fills in {id}.
    """
    # Determine direction of close per leg: flip the original
    params: list[tuple[str, str]] = []
    is_multileg = row.kind == "multileg" and len(row.legs) >= 2
    if is_multileg:
        params.append(("class", "multileg"))
    else:
        params.append(("class", "option" if row.label == "option" else "equity"))
    params.append(("symbol", row.underlying.upper()))
    # Order type:
    #   - Market (no limit price): use 'market'
    #   - Limit on a multi-leg: 'credit' / 'debit' / 'even' (signed by what you'd
    #     collect or pay on the close)
    #   - Limit on a single-leg (option or equity): plain 'limit' — Tradier
    #     rejects credit/debit on class=option with "Invalid parameter, type"
    if limit_price is None:
        params.append(("type", "market"))
    elif is_multileg:
        # Collected credit on entry (cost_basis < 0) → closing PAYS a debit
        # Paid debit on entry  (cost_basis > 0) → closing COLLECTS a credit
        if row.cost_basis < 0:
            params.append(("type", "debit"))
        else:
            params.append(("type", "credit"))
        params.append(("price", f"{limit_price:.2f}"))
    else:
        params.append(("type", "limit"))
        params.append(("price", f"{limit_price:.2f}"))
    params.append(("duration", "day"))
    tag = re.sub(r"[^a-zA-Z0-9]", "", f"edgelaneClose{row.underlying}")[:30]
    if tag:
        params.append(("tag", tag))
    if preview:
        params.append(("preview", "true"))
    if is_multileg:
        for i, leg in enumerate(row.legs):
            params.append((f"option_symbol[{i}]", leg["symbol"]))
            # Flip the side: buy_to_open → sell_to_close; sell_to_open → buy_to_close
            opened = (leg.get("original_side") or "").lower()
            if "buy" in opened:
                params.append((f"side[{i}]", "sell_to_close"))
            else:
                params.append((f"side[{i}]", "buy_to_close"))
            params.append((f"quantity[{i}]", str(qty)))
    else:
        leg = row.legs[0]
        if leg["side"] == "equity":
            params.append(("side", "sell"))
            params.append(("quantity", str(qty)))
        else:
            params.append(("option_symbol", leg["symbol"]))
            opened = (leg.get("original_side") or "").lower()
            params.append(("side", "sell_to_close" if "buy" in opened else "buy_to_close"))
            params.append(("quantity", str(qty)))
    return ("accounts/{account_id}/orders", urllib.parse.urlencode(params))


# ─── Add-to-existing-position order builder ───────────────────────────────

def build_add_order_body(row: PositionRow, limit_price, qty: int,
                         preview: bool = True) -> tuple[str, str]:
    """Build the form-urlencoded body to ADD `qty` contracts to position `row`.

    Unlike close, sides REPEAT the original open direction:
      original buy_to_open  → buy_to_open   (long leg, paying more debit)
      original sell_to_open → sell_to_open  (short leg, collecting more credit)

    Order type is matched to the existing position's net direction:
      cost_basis > 0 (debit position)   → type=debit  (paying to scale up)
      cost_basis < 0 (credit position)  → type=credit (collecting to scale up)
      cost_basis == 0                   → type=even
    For market orders, type=market.
    """
    params: list[tuple[str, str]] = []
    is_multileg = row.kind == "multileg" and len(row.legs) >= 2
    if is_multileg:
        params.append(("class", "multileg"))
    else:
        params.append(("class", "option" if row.label == "option" else "equity"))
    params.append(("symbol", row.underlying.upper()))
    # Order type — direction matches existing position.
    # Multi-leg: credit / debit / even. Single-leg: plain 'limit' (Tradier
    # rejects credit/debit on class=option with "Invalid parameter, type").
    if limit_price is None:
        params.append(("type", "market"))
    elif is_multileg:
        if row.cost_basis < 0:
            params.append(("type", "credit"))
        elif row.cost_basis > 0:
            params.append(("type", "debit"))
        else:
            params.append(("type", "even"))
        params.append(("price", f"{float(limit_price):.2f}"))
    else:
        params.append(("type", "limit"))
        params.append(("price", f"{float(limit_price):.2f}"))
    params.append(("duration", "day"))
    tag = re.sub(r"[^a-zA-Z0-9]", "", f"edgelaneAdd{row.underlying}")[:30]
    if tag:
        params.append(("tag", tag))
    if preview:
        params.append(("preview", "true"))
    if is_multileg:
        for i, leg in enumerate(row.legs):
            params.append((f"option_symbol[{i}]", leg["symbol"]))
            opened = (leg.get("original_side") or "").lower()
            # Same side as the original open — that's what makes it "add", not flip
            params.append((f"side[{i}]", "buy_to_open" if "buy" in opened else "sell_to_open"))
            params.append((f"quantity[{i}]", str(qty)))
    else:
        leg = row.legs[0]
        if leg["side"] == "equity":
            # For equities: positive existing qty → buy more shares; negative → sell more (short)
            existing_qty = float(leg.get("qty") or 0)
            params.append(("side", "buy" if existing_qty >= 0 else "sell_short"))
            params.append(("quantity", str(qty)))
        else:
            params.append(("option_symbol", leg["symbol"]))
            opened = (leg.get("original_side") or "").lower()
            params.append(("side", "buy_to_open" if "buy" in opened else "sell_to_open"))
            params.append(("quantity", str(qty)))
    return ("accounts/{account_id}/orders", urllib.parse.urlencode(params))


# ─── New single-leg position order builder ────────────────────────────────

def build_new_leg_order_body(verb: str, parent_symbol: str, occ_symbol_str: str,
                             qty: int, limit_price, duration: str,
                             preview: bool = True) -> tuple[str, str]:
    """Build the form-urlencoded body to OPEN a single-leg option position.

    verb:           'buy' or 'sell'
    parent_symbol:  underlying symbol (e.g., 'QQQ', 'NDXP' if PM-settled)
    occ_symbol_str: canonical OCC option symbol
    qty:            contract count
    limit_price:    float (LIMIT) or None (MARKET)
    duration:       'day' | 'gtc' | 'pre' | 'post'
    """
    params: list[tuple[str, str]] = []
    params.append(("class", "option"))
    params.append(("symbol", parent_symbol.upper()))
    if limit_price is None:
        params.append(("type", "market"))
    else:
        params.append(("type", "limit"))
        params.append(("price", f"{float(limit_price):.2f}"))
    params.append(("duration", duration))
    tag = re.sub(r"[^a-zA-Z0-9]", "", f"edgelane{verb.capitalize()}{parent_symbol}")[:30]
    if tag:
        params.append(("tag", tag))
    if preview:
        params.append(("preview", "true"))
    params.append(("option_symbol", occ_symbol_str))
    params.append(("side", "buy_to_open" if verb.lower() == "buy" else "sell_to_open"))
    params.append(("quantity", str(qty)))
    return ("accounts/{account_id}/orders", urllib.parse.urlencode(params))


# ─── Open-new-position pass-through to ticket executor ────────────────────

def open_via_ticket(token: str, base: str, account_id: str, ticket: str, execute: bool) -> None:
    """Delegate to tradier_execute_ticket.py's parse + submit logic by import."""
    try:
        from tradier_execute_ticket import (
            parse_ticket, build_tradier_body, lookup_canonical_symbols,
            resolve_account_id as _ra
        )
    except ImportError as e:
        sys.exit(f"{R}Cannot import tradier_execute_ticket.py — make sure it's in the same tests/ dir.{N}\n{e}")
    print(f"{BOLD}Open new position via ticket string{N}")
    print(f"  {D}{ticket}{N}\n")
    t = parse_ticket(ticket)
    print(f"  parsed: {t.verb} {t.symbol} {t.expiration} {t.structure_text} {t.order_type}"
          f"{f' ${t.limit_price:.2f}' if t.limit_price is not None else ''} {t.duration} ×{t.quantity}")
    # Canonical-symbol lookup so NDX → NDXP routing happens automatically
    resolved_root, leg_syms, missing = lookup_canonical_symbols(token, base, t.symbol, t.expiration, t.legs)
    if missing:
        sys.exit(f"{R}✗ Could not locate legs on any root variant: {missing}{N}")
    print(f"  root: {G}{resolved_root}{N}")
    for sym in leg_syms:
        print(f"    {D}{sym}{N}")
    body = build_tradier_body(t, preview=True, option_symbols=leg_syms, symbol_override=resolved_root)
    print(f"\n{B}preview{N}")
    resp = tradier_post(f"accounts/{account_id}/orders", body, token, base)
    order = resp.get("order") or resp
    print(f"  status: {order.get('status', '?')}")
    if not execute:
        print(f"\n{Y}Preview only.{N} Re-run with {BOLD}--execute{N} to submit live.")
        return
    live_body = build_tradier_body(t, preview=False, option_symbols=leg_syms, symbol_override=resolved_root)
    live = tradier_post(f"accounts/{account_id}/orders", live_body, token, base)
    lorder = live.get("order") or live
    oid = lorder.get("id")
    if not oid:
        sys.exit(f"{R}Submit returned no order id: {json.dumps(live)[:200]}{N}")
    print(f"\n{G}{BOLD}✓ Order {oid} submitted.{N}  status: {lorder.get('status', '?')}")


# ─── Working-orders fetch / render / modify / cancel ──────────────────────

WORKING_STATUSES = {"open", "pending", "partially_filled", "submitted",
                    "preview", "accepted", "queued"}


def working_orders(orders: list[dict]) -> list[dict]:
    """Filter to orders that can still be modified or cancelled."""
    out = []
    for o in orders:
        st = (o.get("status") or "").lower()
        if st in WORKING_STATUSES:
            out.append(o)
    # Newest first
    out.sort(key=lambda o: o.get("create_date") or "", reverse=True)
    return out


def _summarize_order_legs(o: dict) -> str:
    """One-line spread summary like 'SPX 7365/7385P × 1' for the table."""
    underlying = o.get("symbol") or ""
    cls = (o.get("class") or "").lower()
    if cls == "multileg":
        legs = o.get("leg") or o.get("legs") or []
        if isinstance(legs, dict):
            legs = [legs]
        parts = []
        for leg in legs:
            sym = leg.get("option_symbol") or leg.get("symbol") or ""
            dec = decode_occ(sym)
            if dec:
                side_letter = dec["side"][0].upper()
                parts.append((dec["side"], dec["strike"], side_letter))
        # Try to render nicely for common shapes
        if len({s for s, _, _ in parts}) == 1 and len(parts) in (2, 3):
            ks = sorted(k for _, k, _ in parts)
            letter = parts[0][2]
            return f"{underlying} {'/'.join(_fmt_strike(k) for k in ks)}{letter}"
        if len(parts) == 4:
            ps = sorted(k for s, k, _ in parts if s == "put")
            cs = sorted(k for s, k, _ in parts if s == "call")
            if len(ps) == 2 and len(cs) == 2:
                return (f"{underlying} {_fmt_strike(ps[0])}/{_fmt_strike(ps[1])}P + "
                        f"{_fmt_strike(cs[0])}/{_fmt_strike(cs[1])}C")
        return f"{underlying} " + " + ".join(f"{_fmt_strike(k)}{ltr}" for _, k, ltr in parts)
    # Single-leg option
    sym = o.get("option_symbol") or o.get("symbol") or underlying
    dec = decode_occ(sym)
    if dec:
        return f"{underlying} {_fmt_strike(dec['strike'])}{dec['side'][0].upper()}"
    return underlying or "?"


def render_orders_table(orders: list[dict]) -> None:
    if not orders:
        print(f"{Y}No working orders.{N}")
        return
    cols = [
        ("#",         3,  "right"),
        ("ID",        10, "right"),
        ("Ticker",    34, "left"),
        ("Type",      8,  "left"),
        ("Price",     9,  "right"),
        ("Qty",       5,  "right"),
        ("Status",    11, "left"),
        ("Submitted", 16, "left"),
    ]
    header = "  ".join(_pad(name, w, align) for name, w, align in cols)
    print(BOLD + header + N)
    print(DIM + "─" * len(header) + N)
    for i, o in enumerate(orders, 1):
        oid = str(o.get("id") or "?")
        ticker = _summarize_order_legs(o)
        otype = (o.get("type") or "").lower()
        price = o.get("price")
        if otype in ("market",) or price in (None, "", 0):
            price_str = "MKT"
        else:
            try:
                price_str = f"${float(price):.2f}"
            except (TypeError, ValueError):
                price_str = str(price)
        qty = o.get("quantity") or o.get("num_legs") or "—"
        status = (o.get("status") or "?").lower()
        submitted = _format_date(o.get("create_date") or o.get("transaction_date"))
        row = "  ".join([
            _pad(str(i), cols[0][1], cols[0][2]),
            _pad(oid, cols[1][1], cols[1][2]),
            _pad(ticker, cols[2][1], cols[2][2]),
            _pad(otype.upper(), cols[3][1], cols[3][2]),
            _pad(price_str, cols[4][1], cols[4][2]),
            _pad(str(int(float(qty))) if str(qty).replace('.','',1).isdigit() else str(qty),
                 cols[5][1], cols[5][2]),
            _pad(status, cols[6][1], cols[6][2]),
            _pad(submitted, cols[7][1], cols[7][2]),
        ])
        print(row)


def build_modify_order_body(order: dict, new_limit, new_qty, market_flag: bool) -> str:
    """Build form-urlencoded body for PUT /accounts/{id}/orders/{order_id}.

    Tradier modify supports: type, duration, price, stop. Quantity is also
    accepted in practice (sent here when -Q given) — Tradier will reject with
    a clear error if not supported for that order class.
    """
    params: list[tuple[str, str]] = []
    existing_type = (order.get("type") or "").lower()
    existing_price = order.get("price")
    existing_duration = (order.get("duration") or "day").lower()

    if market_flag:
        # Flip to market — drop price
        params.append(("type", "market"))
    elif new_limit is not None:
        # Determine the right limit-type token. For multi-leg orders the
        # legal types are credit/debit/even (NOT 'limit'). For single options
        # and equities, 'limit' is correct.
        cls = (order.get("class") or "").lower()
        if cls == "multileg":
            # Preserve credit/debit/even if Tradier already gave us one;
            # otherwise default to debit (safer — most user-set limits are
            # buy-side from EdgeLane). If wrong, Tradier rejects clearly.
            if existing_type in ("credit", "debit", "even"):
                params.append(("type", existing_type))
            else:
                params.append(("type", "debit"))
        else:
            params.append(("type", "limit"))
        params.append(("price", f"{new_limit:.2f}"))
    else:
        # Neither -M nor -L given → preserve existing type/price.
        if existing_type:
            params.append(("type", existing_type))
        if existing_price not in (None, "", 0):
            try:
                params.append(("price", f"{float(existing_price):.2f}"))
            except (TypeError, ValueError):
                pass

    params.append(("duration", existing_duration))

    if new_qty is not None:
        params.append(("quantity", str(new_qty)))

    return urllib.parse.urlencode(params)


# ─── CLI ──────────────────────────────────────────────────────────────────

def parse_args(argv):
    """Parse CLI args. See module docstring for the full flag scheme."""
    args = {
        "mode": "list",
        # rows / qty / price / duration / execute apply across operations
        "row": None,
        "limit": None,
        "qty": None,
        "execute": False,
        "duration": "gtc",
        "market": False,                  # -M (modify only)
        # --buy / --sell new-position params
        "new_verb": None,
        "new_symbol": None,
        "new_strike_side": None,
        "new_expiration": None,
        "ticket": None,                   # set when --buy/--sell got a multi-leg ticket
        # -G closed-P&L params
        "gainloss_date": None,            # YYYY-MM-DD, default today
        "gainloss_since_days": None,      # last N days
        "gainloss_settled": False,        # --settled → use /gainloss endpoint (full closes only)
        "gainloss_verbose": False,        # -V / --verbose → print matching diagnostics
    }

    def _consume_int(label, it):
        try:
            return int(next(it))
        except (StopIteration, ValueError):
            sys.exit(f"{R}{label} requires an integer{N}")

    def _consume_float(label, it):
        try:
            return float(next(it))
        except (StopIteration, ValueError):
            sys.exit(f"{R}{label} requires a number{N}")

    it = iter(argv)
    while True:
        a = next(it, None)
        if a is None:
            break
        if a in ("--execute", "-x"):
            args["execute"] = True

        # ── Operations on EXISTING items (short flags + row#) ──
        elif a in ("-S", "--close"):
            args["row"] = _consume_int(a, it)
            args["mode"] = "close"
        elif a in ("-A", "--add"):
            args["row"] = _consume_int(a, it)
            args["mode"] = "add"
        elif a in ("-O", "--orders"):
            # -O alone → list orders. -O <row> → modify that working order.
            args["mode"] = "orders"
            # peek next; consume if it's an integer
            peek = next(it, None)
            if peek is None:
                continue
            try:
                args["row"] = int(peek)
                args["mode"] = "modify"
            except ValueError:
                # Not a number — push back into stream by re-iterating
                # (cheap re-walk via list)
                remaining = [peek] + list(it)
                it = iter(remaining)
        elif a in ("-C", "--cancel"):
            args["row"] = _consume_int(a, it)
            args["mode"] = "cancel"
        elif a in ("-G", "--gainloss"):
            # Closed-position P&L. Optional positional date (YYYY-MM-DD), --since N,
            # --settled (use Tradier /gainloss endpoint — fully closed + settled only).
            # Default: compute from today's filled close orders, capturing partials.
            args["mode"] = "gainloss"
            peek = next(it, None)
            if peek is None:
                continue
            if peek == "--since":
                try:
                    args["gainloss_since_days"] = int(next(it))
                except (StopIteration, ValueError):
                    sys.exit(f"{R}--since requires an integer day count{N}")
            elif peek == "--settled":
                args["gainloss_settled"] = True
            elif peek in ("-V", "--verbose"):
                args["gainloss_verbose"] = True
            else:
                # Try to parse as a date (lenient: 2026-06-4, 06-04, 6/4, 20260604)
                normalized = _normalize_date(peek)
                if normalized:
                    args["gainloss_date"] = normalized
                else:
                    # Not a date — push back for the next iteration to handle
                    it = iter([peek] + list(it))
        elif a == "--settled":
            args["gainloss_settled"] = True
        elif a == "--since":
            # Standalone so flag order doesn't matter (e.g. -G --settled --since 7).
            try:
                args["gainloss_since_days"] = int(next(it))
            except (StopIteration, ValueError):
                sys.exit(f"{R}--since requires an integer day count{N}")
            if args["mode"] == "list":
                args["mode"] = "gainloss"
        elif a in ("-V", "--verbose"):
            args["gainloss_verbose"] = True

        # ── Open NEW positions (long flags) ──
        elif a in ("--buy", "-B", "--sell"):
            args["new_verb"] = "buy" if a in ("--buy", "-B") else "sell"
            first = next(it, None)
            if first is None:
                sys.exit(f"{R}{a} requires arguments — either a ticket string or SYMBOL STRIKE_SIDE [EXPIRATION]{N}")
            if "·" in first:
                # Multi-leg ticket — let open_via_ticket handle it (verb in ticket wins).
                args["ticket"] = first
                args["mode"] = "open"
            else:
                # Single-leg: SYMBOL  STRIKE_SIDE  [EXPIRATION]
                # EXPIRATION is OPTIONAL — defaults to today when omitted.
                # Date input is lenient: accepts 2026-06-4, 06-04, 06/04/2026, etc.
                try:
                    args["new_symbol"] = first.upper()
                    args["new_strike_side"] = next(it).upper()
                    args["mode"] = "new_leg"
                except StopIteration:
                    sys.exit(f"{R}{a} single-leg form needs at least SYMBOL STRIKE_SIDE (e.g., {a} QQQ 746C){N}")
                # Peek for optional expiration
                peek = next(it, None)
                if peek is None:
                    args["new_expiration"] = datetime.now().strftime("%Y-%m-%d")
                elif peek.startswith("-"):
                    # It's the next flag — push back and default to today
                    args["new_expiration"] = datetime.now().strftime("%Y-%m-%d")
                    it = iter([peek] + list(it))
                else:
                    normalized = _normalize_date(peek)
                    if not normalized:
                        sys.exit(f"{R}Bad expiration: {peek!r}. Try YYYY-MM-DD, MM-DD, MM/DD/YYYY, or 20260604.{N}")
                    args["new_expiration"] = normalized

        # ── Modifier flags (apply to whichever operation is active) ──
        elif a in ("-L", "--limit"):
            args["limit"] = _consume_float("-L", it)
        elif a.startswith("-L") and len(a) > 2:
            try: args["limit"] = float(a[2:])
            except ValueError: sys.exit(f"{R}-L value not numeric: {a[2:]}{N}")
        elif a in ("-Q", "--quantity"):
            args["qty"] = _consume_int("-Q", it)
        elif a.startswith("-Q") and len(a) > 2:
            try: args["qty"] = int(a[2:])
            except ValueError: sys.exit(f"{R}-Q value not integer: {a[2:]}{N}")
        elif a in ("-M", "--market"):
            args["market"] = True
        elif a in ("-D", "--duration"):
            try:
                v = next(it).lower()
                if v not in ("day", "gtc", "pre", "post"):
                    sys.exit(f"{R}-D must be day | gtc | pre | post (got {v!r}){N}")
                args["duration"] = v
            except StopIteration:
                sys.exit(f"{R}-D requires a duration value{N}")
        elif a in ("-h", "--help", "help"):
            print(__doc__)
            sys.exit(0)
        else:
            sys.exit(f"{R}Unknown argument: {a!r}. Run `tp -h` for usage.{N}")

    return args

def main():
    args = parse_args(sys.argv[1:])
    cfg = load_config()
    token, base, env = resolve_tradier_creds(cfg)
    if not token:
        sys.exit(f"{R}No Tradier {env} token configured.{N}")

    if args["mode"] == "open":
        account_id = resolve_account_id(token, base)
        open_via_ticket(token, base, account_id, args["ticket"], args["execute"])
        return

    if args["mode"] == "new_leg":
        # Open a single-leg option position. Validates inputs, runs canonical
        # OCC root lookup (NDX→NDXP, SPX→SPXW, etc.), previews, optionally submits.
        account_id = resolve_account_id(token, base)
        qty = args["qty"] if args["qty"] is not None and args["qty"] > 0 else 1
        m = re.match(r"^(\d+(?:\.\d+)?)([CP])$", args["new_strike_side"], re.IGNORECASE)
        if not m:
            sys.exit(f"{R}Bad strike+side: {args['new_strike_side']!r}. Expected like '746C' or '540P'.{N}")
        strike = float(m.group(1))
        side = "call" if m.group(2).upper() == "C" else "put"
        try:
            datetime.strptime(args["new_expiration"], "%Y-%m-%d")
        except ValueError:
            sys.exit(f"{R}Bad expiration: {args['new_expiration']!r}. Expected YYYY-MM-DD.{N}")

        # Canonical-root lookup so e.g. NDX→NDXP routes automatically.
        try:
            from tradier_execute_ticket import Leg, lookup_canonical_symbols
        except ImportError as e:
            sys.exit(f"{R}Cannot import tradier_execute_ticket.py: {e}{N}")
        action = ("buy_to_open" if args["new_verb"] == "buy" else "sell_to_open")
        synthetic_leg = Leg(strike=strike, side=side, quantity=qty, action=action)
        resolved_root, leg_syms, missing = lookup_canonical_symbols(
            token, base, args["new_symbol"], args["new_expiration"], [synthetic_leg]
        )
        if missing or not leg_syms:
            sys.exit(f"{R}✗ Could not locate {strike}{m.group(2)} {args['new_expiration']} on any root variant of {args['new_symbol']}: {missing}{N}")
        occ = leg_syms[0]

        # Tradier wants the PARENT symbol (NDX) for `symbol`, not the OCC root
        # (NDXP). This mirrors what the JSX side does post-v4.7.56.
        order_parent = args["new_symbol"].upper()
        mode_label = 'MARKET' if args['limit'] is None else f'LIMIT ${args["limit"]:.2f}'
        print(f"{BOLD}Open new single-leg position{N}")
        print(f"  {args['new_verb'].upper()} {qty} {args['new_symbol']} {args['new_strike_side']} {args['new_expiration']}")
        print(f"  resolved root: {G}{resolved_root}{N}   parent symbol on order: {order_parent}")
        print(f"  OCC:  {D}{occ}{N}")
        print(f"  order: {mode_label} · duration={args['duration']}")
        print()

        body = build_new_leg_order_body(args["new_verb"], order_parent, occ, qty,
                                        args["limit"], args["duration"], preview=True)[1]
        print(f"{B}[preview]{N}")
        try:
            resp = tradier_post(f"accounts/{account_id}/orders", body, token, base)
        except Exception as e:
            sys.exit(f"  {R}✗ {e}{N}")
        porder = resp.get("order") or resp
        if resp.get("errors"):
            err = resp["errors"].get("error")
            sys.exit(f"  {R}✗ preview rejected: {err if not isinstance(err, list) else '; '.join(err)}{N}")
        print(f"  status: {porder.get('status', '?')}")
        if porder.get("status") and str(porder["status"]).lower() not in ("ok", "filled", "open"):
            sys.exit(f"  {R}✗ preview rejected: {porder.get('reason_description') or json.dumps(porder)[:200]}{N}")

        if not args["execute"]:
            print(f"\n{Y}Preview only.{N} Re-run with {BOLD}--execute{N} to submit live.")
            return

        live_body = build_new_leg_order_body(args["new_verb"], order_parent, occ, qty,
                                             args["limit"], args["duration"], preview=False)[1]
        print(f"\n{B}[LIVE]{N}")
        live = tradier_post(f"accounts/{account_id}/orders", live_body, token, base)
        lorder = live.get("order") or live
        oid = lorder.get("id")
        if not oid:
            sys.exit(f"  {R}✗ accepted POST but no order id: {json.dumps(live)[:200]}{N}")
        print(f"  {G}✓ order {oid} submitted{N}  status: {lorder.get('status', '?')}")
        return

    # Orders / modify / cancel paths
    if args["mode"] in ("orders", "modify", "cancel"):
        account_id = resolve_account_id(token, base)
        print(f"{BOLD}Tradier orders{N}  {D}env: {env}  account: {account_id}{N}\n")
        all_orders = fetch_recent_orders(token, base, account_id)
        wo = working_orders(all_orders)

        if args["mode"] == "orders" and args["row"] is None:
            render_orders_table(wo)
            return

        if not wo:
            sys.exit(f"{R}No working orders.{N}")
        if args["row"] is None or args["row"] < 1 or args["row"] > len(wo):
            sys.exit(f"{R}Row #{args['row']} out of range — there are {len(wo)} working order(s).{N}")
        target = wo[args["row"] - 1]
        oid = target.get("id")
        if not oid:
            sys.exit(f"{R}Order has no id field — cannot modify/cancel.{N}")

        if args["mode"] == "modify":
            if args["limit"] is None and args["qty"] is None and not args["market"]:
                sys.exit(f"{R}Modify requires at least one of -L <price>, -Q <qty>, or -M (market).{N}")
            ticker = _summarize_order_legs(target)
            cur_type = (target.get("type") or "?").upper()
            cur_price = target.get("price")
            try:
                cur_price_str = f"${float(cur_price):.2f}" if cur_price not in (None, "", 0) else "—"
            except (TypeError, ValueError):
                cur_price_str = str(cur_price)
            print(f"{BOLD}Modify order #{args['row']}{N}  (Tradier id: {oid})")
            print(f"  {ticker}")
            print(f"  current: {cur_type} {cur_price_str}  qty {target.get('quantity', '?')}  "
                  f"duration {target.get('duration', '?')}  status {target.get('status', '?')}")
            change_parts = []
            if args["market"]:
                change_parts.append("→ MARKET")
            elif args["limit"] is not None:
                change_parts.append(f"→ LIMIT ${args['limit']:.2f}")
            if args["qty"] is not None:
                change_parts.append(f"→ qty {args['qty']}")
            print(f"  {Y}changes: {'  '.join(change_parts)}{N}")
            body = build_modify_order_body(target, args["limit"], args["qty"], args["market"])
            print(f"\n{B}body{N}: {body}")
            if not args["execute"]:
                print(f"\n{Y}Preview only.{N} Re-run with {BOLD}--execute{N} to actually modify.")
                return
            print(f"\n{B}[LIVE]{N}  PUT accounts/{account_id}/orders/{oid}")
            try:
                resp = tradier_put(f"accounts/{account_id}/orders/{oid}", body, token, base)
            except Exception as e:
                sys.exit(f"  {R}✗ {e}{N}")
            mo = resp.get("order") or resp
            print(f"  {G}✓ modify accepted{N}  status: {mo.get('status', '?')}")
            return

        # Cancel
        ticker = _summarize_order_legs(target)
        cur_type = (target.get("type") or "?").upper()
        cur_price = target.get("price")
        try:
            cur_price_str = f"${float(cur_price):.2f}" if cur_price not in (None, "", 0) else "—"
        except (TypeError, ValueError):
            cur_price_str = str(cur_price)
        print(f"{BOLD}Cancel order #{args['row']}{N}  (Tradier id: {oid})")
        print(f"  {ticker}")
        print(f"  current: {cur_type} {cur_price_str}  qty {target.get('quantity', '?')}  "
              f"status {target.get('status', '?')}")
        if not args["execute"]:
            print(f"\n{Y}Preview only.{N} Re-run with {BOLD}--execute{N} to actually cancel.")
            return
        print(f"\n{B}[LIVE]{N}  DELETE accounts/{account_id}/orders/{oid}")
        try:
            resp = tradier_delete(f"accounts/{account_id}/orders/{oid}", token, base)
        except Exception as e:
            sys.exit(f"  {R}✗ {e}{N}")
        co = resp.get("order") or resp
        print(f"  {G}✓ cancel accepted{N}  status: {co.get('status', '?')}")
        return

    # ── Closed-position P&L ──
    if args["mode"] == "gainloss":
        account_id = resolve_account_id(token, base)
        today_local = datetime.now().strftime("%Y-%m-%d")
        if args["gainloss_since_days"] is not None:
            d = args["gainloss_since_days"]
            start_dt = datetime.now() - timedelta(days=d)
            start_date = start_dt.strftime("%Y-%m-%d")
            end_date = today_local
            date_label = f"last {d} day{'s' if d != 1 else ''} ({start_date} → {end_date})"
        else:
            start_date = end_date = args["gainloss_date"] or today_local
            date_label = start_date if start_date != today_local else (start_date + " (today)")

        if args["gainloss_settled"]:
            # User explicitly wants Tradier's settled-only gainloss endpoint
            print(f"{BOLD}Tradier settled gain/loss{N}  {D}env: {env}  account: {account_id}  range: {date_label}{N}\n")
            rows = fetch_gainloss(token, base, account_id, start=start_date, end=end_date)
            render_gainloss_table(rows, date_label)
        else:
            # Default path. Tradier's /orders endpoint only exposes the CURRENT
            # trading day, so it alone can never satisfy a multi-day range. We
            # therefore merge two sources, partitioned strictly by date so they
            # never overlap:
            #   • days before today  → /gainloss (settled, fully-closed history)
            #   • today              → filled close orders (live, incl. partials)
            yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
            want_hist = start_date <= yesterday
            want_today = end_date >= today_local
            label_parts = []
            if want_hist:  label_parts.append("settled /gainloss")
            if want_today: label_parts.append("live close orders")
            src = " + ".join(label_parts) or "filled close orders"
            print(f"{BOLD}Realized P&L ({src}){N}  {D}env: {env}  account: {account_id}  range: {date_label}{N}\n")

            merged_rows: list[dict] = []
            if want_hist:
                hist_end = min(end_date, yesterday)
                gl_rows = fetch_gainloss(token, base, account_id, start=start_date, end=hist_end)
                # Tradier can return rows just outside the asked window — clamp by close_date.
                gl_rows = [r for r in gl_rows
                           if start_date <= (r.get("close_date") or "")[:10] <= hist_end]
                merged_rows.extend(gainloss_to_intraday_rows(gl_rows))
            if want_today:
                orders = fetch_recent_orders(token, base, account_id)
                # Positions are the fallback for partial closes whose open order
                # isn't in today's /orders window.
                positions = fetch_positions(token, base, account_id)
                # /history recovers the cost basis of a TODAY close whose OPEN
                # was on a prior day (gone from /orders, /positions, /gainloss).
                hist_lookback = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
                history_opens = build_history_opens(token, base, account_id,
                                                    start=hist_lookback, end=today_local)
                merged_rows.extend(compute_intraday_realized(
                    orders, today_local, today_local,
                    positions=positions,
                    history_opens=history_opens,
                    verbose=args["gainloss_verbose"],
                ))
            # Newest close first across both sources.
            merged_rows.sort(key=lambda r: r.get("close_date") or "", reverse=True)
            render_intraday_realized_table(merged_rows, date_label)
        return

    # List or close — both need positions
    account_id = resolve_account_id(token, base)
    print(f"{BOLD}Tradier positions{N}  {D}env: {env}  account: {account_id}{N}\n")
    positions = fetch_positions(token, base, account_id)
    orders = fetch_recent_orders(token, base, account_id)
    rows = aggregate_positions(positions, orders)

    # Quote everything for P&L
    all_symbols = set()
    for r in rows:
        for leg in r.legs:
            all_symbols.add(leg["symbol"])
    quotes = fetch_quotes(token, base, sorted(all_symbols))
    for r in rows:
        compute_pnl(r, quotes)

    if args["mode"] == "list":
        render_table(rows)
        return

    # ── Add to existing position ──
    if args["mode"] == "add":
        if not rows:
            sys.exit(f"{R}No open positions to add to.{N}")
        if args["row"] is None or args["row"] < 1 or args["row"] > len(rows):
            sys.exit(f"{R}Row #{args['row']} out of range — there are {len(rows)} position(s).{N}")
        if args["qty"] is None or args["qty"] < 1:
            sys.exit(f"{R}-A requires -Q <qty> (number of contracts to add, ≥ 1).{N}")
        target = rows[args["row"] - 1]
        qty_to_add = args["qty"]
        mode_label = 'MARKET' if args['limit'] is None else f'LIMIT ${args["limit"]:.2f}'
        plural = 's' if qty_to_add > 1 else ''
        bid_str = f"${target.bid:.2f}" if target.bid is not None else "—"
        ask_str = f"${target.ask:.2f}" if target.ask is not None else "—"

        print(f"{BOLD}Add to position — row {args['row']}{N}")
        print(f"  {target.underlying} {target.structure_text} {target.expiration or ''}")
        print(f"  current qty: {target.quantity}  cost: {_sign_dollars(target.cost_basis)}  bid: {bid_str}  ask: {ask_str}")
        print()
        direction_word = 'CREDIT' if target.cost_basis < 0 else ('DEBIT' if target.cost_basis > 0 else 'EVEN')
        print(f"  adding {qty_to_add} contract{plural} as {direction_word} at {mode_label}")
        for leg in target.legs:
            opened = (leg.get("original_side") or "").lower()
            side = "buy_to_open" if "buy" in opened else "sell_to_open"
            print(f"    {leg['symbol']}  side={side}  qty={qty_to_add}")
        print()

        _path, preview_body = build_add_order_body(target, args["limit"], qty_to_add, preview=True)
        print(f"{B}[preview]{N}")
        try:
            resp = tradier_post(f"accounts/{account_id}/orders", preview_body, token, base)
        except Exception as e:
            sys.exit(f"  {R}✗ {e}{N}")
        porder = resp.get("order") or resp
        if resp.get("errors"):
            err = resp["errors"].get("error")
            sys.exit(f"  {R}✗ preview rejected: {err if not isinstance(err, list) else '; '.join(err)}{N}")
        print(f"  status: {porder.get('status', '?')}")
        if porder.get("status") and str(porder["status"]).lower() not in ("ok", "filled", "open"):
            sys.exit(f"  {R}✗ preview rejected: {porder.get('reason_description') or json.dumps(porder)[:200]}{N}")

        if not args["execute"]:
            print(f"\n{Y}Preview only.{N} Re-run with {BOLD}--execute{N} to submit live.")
            return

        _path, live_body = build_add_order_body(target, args["limit"], qty_to_add, preview=False)
        print(f"\n{B}[LIVE]{N}")
        live = tradier_post(f"accounts/{account_id}/orders", live_body, token, base)
        lorder = live.get("order") or live
        oid = lorder.get("id")
        if not oid:
            sys.exit(f"  {R}✗ accepted POST but no order id: {json.dumps(live)[:200]}{N}")
        print(f"  {G}✓ add order {oid} submitted{N}  status: {lorder.get('status', '?')}")
        return

    # Close position
    if not rows:
        sys.exit(f"{R}No open positions to close.{N}")
    if args["row"] is None or args["row"] < 1 or args["row"] > len(rows):
        sys.exit(f"{R}Row #{args['row']} out of range — there are {len(rows)} position(s).{N}")
    target = rows[args["row"] - 1]
    qty_to_close = args["qty"] if args["qty"] is not None else target.quantity
    if qty_to_close < 1:
        sys.exit(f"{R}Quantity must be ≥ 1.{N}")
    if qty_to_close > target.quantity:
        sys.exit(f"{R}Quantity {qty_to_close} > position size {target.quantity}.{N}")

    print(f"{BOLD}Close target — row {args['row']}{N}")
    print(f"  {target.underlying} {target.structure_text} {target.expiration or ''}")
    print(f"  acquired: {target.acquired}  cost: {_sign_dollars(target.cost_basis)}  qty: {target.quantity}")
    bid_str = f"${target.bid:.2f}" if target.bid is not None else "—"
    ask_str = f"${target.ask:.2f}" if target.ask is not None else "—"
    print(f"  current bid: {bid_str}  ask: {ask_str}  P&L: {_sign_dollars(target.pnl_dollar)} ({target.pnl_pct:+.1f}%)")
    print()
    mode_str = 'MARKET' if args['limit'] is None else f'LIMIT ${args["limit"]:.2f}'
    plural = 's' if qty_to_close > 1 else ''
    print(f"  closing {qty_to_close}/{target.quantity} contract{plural} at {mode_str}")
    print()

    _path, preview_body = build_close_order_body(target, args["limit"], qty_to_close, preview=True)
    print(f"{B}[preview]{N}")
    try:
        resp = tradier_post(f"accounts/{account_id}/orders", preview_body, token, base)
    except Exception as e:
        sys.exit(f"  {R}✗ {e}{N}")
    porder = resp.get("order") or resp
    print(f"  status: {porder.get('status', '?')}")
    if porder.get("status") and str(porder["status"]).lower() not in ("ok", "filled", "open"):
        sys.exit(f"  {R}✗ preview rejected: {porder.get('reason_description') or json.dumps(porder)[:200]}{N}")

    if not args["execute"]:
        print(f"\n{Y}Preview only.{N} Re-run with {BOLD}--execute{N} to submit live.")
        return

    _path, live_body = build_close_order_body(target, args["limit"], qty_to_close, preview=False)
    print(f"\n{B}[LIVE]{N}")
    live = tradier_post(f"accounts/{account_id}/orders", live_body, token, base)
    lorder = live.get("order") or live
    oid = lorder.get("id")
    if not oid:
        sys.exit(f"  {R}✗ accepted POST but no order id: {json.dumps(live)[:200]}{N}")
    print(f"  {G}✓ order {oid} submitted{N}  status: {lorder.get('status', '?')}")


if __name__ == "__main__":
    main()

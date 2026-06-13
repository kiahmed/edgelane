"""Dump raw Tradier orders for a date so we can see exactly what shape comes
back for closes — figure out why the multi-leg P&L aggregation is mis-signing
the close cash.

Usage:
    python3 tools/tp_dump_orders.py                   # today's orders
    python3 tools/tp_dump_orders.py 2026-06-05        # specific date
    python3 tools/tp_dump_orders.py --since 1         # yesterday + today
    python3 tools/tp_dump_orders.py --ticker SPXW     # filter by underlying

Output is JSON with one entry per filled order containing: id, class, type,
side, status, transaction_date, quantity, avg_fill_price, option_symbol /
symbol, and the full leg[] array if present. Sensitive fields stripped.
"""
from __future__ import annotations
import sys, json
from datetime import date, timedelta
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_HERE.parent / "tests"))
from utils import load_config, resolve_tradier_creds  # type: ignore
from tradier_positions import tradier_get, resolve_account_id  # type: ignore


SAFE_KEYS = {
    "id", "class", "type", "side", "status", "duration",
    "transaction_date", "create_date",
    "symbol", "option_symbol",
    "quantity", "remaining_quantity", "exec_quantity",
    "avg_fill_price", "price", "stop", "last_fill_price", "last_fill_quantity",
    "num_legs", "strategy", "tag",
}


def _clean(d):
    """Recursively keep only SAFE_KEYS so tokens/account ids never appear."""
    if isinstance(d, dict):
        out = {k: _clean(v) for k, v in d.items() if k in SAFE_KEYS or k in ("leg", "legs")}
        if "leg" in d and isinstance(d["leg"], list):
            out["leg"] = [_clean(l) for l in d["leg"]]
        if "legs" in d and isinstance(d["legs"], list):
            out["legs"] = [_clean(l) for l in d["legs"]]
        return out
    if isinstance(d, list):
        return [_clean(x) for x in d]
    return d


def main():
    args = sys.argv[1:]
    target_date = None
    since_days = 0
    ticker_filter = None
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--since":
            i += 1
            since_days = int(args[i])
        elif a == "--ticker":
            i += 1
            ticker_filter = args[i].upper()
        elif not target_date:
            target_date = a
        i += 1
    if target_date is None and since_days == 0:
        target_date = date.today().isoformat()

    cfg = load_config()
    token, base, env = resolve_tradier_creds(cfg)
    if not token:
        print(f"ERROR: no Tradier token for env={env}", file=sys.stderr)
        sys.exit(1)
    aid = resolve_account_id(token, base)
    print(f"# env={env} account={aid[:4]}...{aid[-4:]}", file=sys.stderr)

    # Tradier orders endpoint — paginate through page 1 (usually enough)
    raw = tradier_get(f"accounts/{aid}/orders", {"includeTags": "true"}, token, base)
    container = (raw.get("orders") or {})
    orders = container.get("order") or []
    if isinstance(orders, dict):
        orders = [orders]

    if since_days > 0:
        end = date.today()
        start = end - timedelta(days=since_days)
        start_s, end_s = start.isoformat(), end.isoformat()
    else:
        start_s = end_s = target_date

    matched = []
    for o in orders:
        if (o.get("status") or "").lower() not in ("filled", "partially_filled"):
            continue
        d = (o.get("transaction_date") or o.get("create_date") or "")[:10]
        if not (start_s <= d <= end_s):
            continue
        if ticker_filter:
            # Match against TOP-LEVEL symbol AND option_symbol AND any leg's
            # option_symbol — Tradier puts SPX at top level but the leg OCC
            # symbols start with SPXW (weekly root), so a user typing 'SPXW'
            # or 'SPX' should both match.
            tf = ticker_filter.upper()
            haystacks = [(o.get("symbol") or "").upper(),
                         (o.get("option_symbol") or "").upper()]
            legs = o.get("leg") or o.get("legs") or []
            if isinstance(legs, dict): legs = [legs]
            for l in legs:
                haystacks.append((l.get("option_symbol") or l.get("symbol") or "").upper())
            if not any(tf in h for h in haystacks if h):
                continue
        matched.append(_clean(o))

    out = {
        "date_range": [start_s, end_s],
        "ticker_filter": ticker_filter,
        "n_filled_orders": len(matched),
        "orders": matched,
    }
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()

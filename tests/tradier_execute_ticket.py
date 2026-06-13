"""
Tradier ticket-string executor.

Takes a single-line trade ticket in EdgeLane's copy-button format and submits
it to Tradier as a multi-leg order. Defaults to PREVIEW-ONLY (no live submit)
so it's safe to dry-run.

Ticket format (matches _buildTradeTicket in spread_optimizer_v4_7_html.jsx):

    {verb} {label} . {SYMBOL} . {YYYY-MM-DD} . {legs} . {mode} . {N} contract[s]

Where:
    verb   = "Sell" (credit) | "Buy" (debit)
    legs   = vertical:        "LOW/HIGH[P|C]"
             iron condor/fly: "LOWP/HIGHP + LOWC/HIGHC"
             butterfly:       "LOW/MID/HIGH[P|C]"  (mid doubled)
    mode   = "MARKET"
           | "LIMIT credit $X.XX GTC|DAY"
           | "LIMIT debit  $X.XX GTC|DAY"

Examples:
    Sell Balanced . NDX . 2026-05-19 . 28070/28400P + 29060/29390C . LIMIT credit $134.08 GTC . 1 contract
    Buy  Aggressive . SPY . 2026-06-20 . 540/545C . MARKET . 1 contract
    Buy  Balanced . QQQ . 2026-06-20 . 500/505/510C . LIMIT debit $2.10 GTC . 1 contract

Usage:
    python tests/tradier_execute_ticket.py "Sell Balanced . NDX . 2026-05-19 . ..."
        # preview only -- validates the payload against Tradier, prints the result

    python tests/tradier_execute_ticket.py --execute "Sell Balanced . ..."
        # preview AND submit live. Requires explicit flag.

    python tests/tradier_execute_ticket.py --dump "Sell Balanced . ..."
        # just print the parsed structure + computed payload; no network call

Reads Tradier creds via the standard DEVMODE -> SANDBOX/PROD token selector.

v4.7.32: parsing, OCC building, and Tradier order-body construction live in
`market/backend/app/order_builder.py` so the FastAPI /orders route and this
CLI share one source of truth. This file is now just the network glue.
"""
from __future__ import annotations
import sys
import time
import json
import urllib.parse
import urllib.request
import urllib.error
from pathlib import Path
from datetime import datetime

_HERE = Path(__file__).resolve()
sys.path.insert(0, str(_HERE.parent))                                      # tests/
sys.path.insert(0, str(_HERE.parent.parent))                               # repo root
sys.path.insert(0, str(_HERE.parent.parent / "market" / "backend"))        # market/backend (for app.*)
from utils import load_config, resolve_tradier_creds  # noqa: E402

from app.order_builder import (  # noqa: E402
    Leg,
    ParsedTicket,
    parse_ticket,
    occ_symbol,
    resolve_leg_symbols,
    build_tradier_order_payload,
    build_tradier_order_body,
    INDEX_ROOT_VARIANTS,
)

# ANSI
G, R, Y, B, D, N, BOLD = "\033[32m", "\033[31m", "\033[33m", "\033[34m", "\033[2m", "\033[0m", "\033[1m"


# --- helpers (CLI-specific wrappers around the shared builders) -------------

def build_tradier_body(ticket: ParsedTicket, preview: bool = True,
                       option_symbols: list | None = None,
                       symbol_override: str | None = None) -> str:
    """Back-compat shim that mirrors the legacy signature. Builds Tradier
    form body from a ParsedTicket; uses the shared builder under the hood."""
    legs = []
    for i, leg in enumerate(ticket.legs):
        sym = (option_symbols[i] if (option_symbols and i < len(option_symbols))
               else occ_symbol(ticket.symbol, ticket.expiration, leg.side, leg.strike))
        legs.append(Leg(
            side=leg.side,
            strike=leg.strike,
            action=leg.action,
            quantity=leg.quantity,
            occ_symbol=sym,
        ))
    tag = f"edgelane{ticket.label or 'ticket'}"
    return build_tradier_order_body(
        account_id="cli",  # placeholder, not used in the body
        symbol=(symbol_override or ticket.symbol),
        legs=legs,
        order_type=ticket.order_type,
        limit_price=ticket.limit_price,
        duration=ticket.duration,
        preview=preview,
        tag=tag,
    )


# --- chain-lookup (avoid OCC root guesswork) -------------------------------

def lookup_canonical_symbols(token: str, base: str, underlying: str, expiration: str,
                             legs: list, timeout: int = 20) -> tuple[str, list, list]:
    """For each leg find Tradier's canonical option symbol via the chain
    endpoint, trying common index-root suffixes (NDX -> NDXP, SPX -> SPXW,
    etc). Returns (resolved_root, leg_symbols, missing)."""
    legs_needed = [(float(l.strike), l.side.lower()) for l in legs]
    for suffix in INDEX_ROOT_VARIANTS:
        root = underlying.upper() + suffix
        try:
            exp_resp = tradier_get("markets/options/expirations",
                                   {"symbol": root, "includeAllRoots": "true"},
                                   token, base, timeout=timeout)
        except Exception:
            continue
        dates = (exp_resp.get("expirations") or {}).get("date") or []
        if isinstance(dates, str):
            dates = [dates]
        if expiration not in dates:
            continue
        try:
            chain_resp = tradier_get("markets/options/chains",
                                     {"symbol": root, "expiration": expiration,
                                      "greeks": "false"},
                                     token, base, timeout=timeout)
        except Exception:
            continue
        options = (chain_resp.get("options") or {}).get("option") or []
        if not isinstance(options, list):
            options = [options]
        try:
            tmp_legs = [Leg(side=s, strike=k, action="buy_to_open") for (k, s) in legs_needed]
            resolved_legs = resolve_leg_symbols(tmp_legs, options, expiration=expiration)
            return root, [l.occ_symbol for l in resolved_legs], []
        except ValueError:
            continue
    return underlying.upper(), [], legs_needed


# --- Tradier REST glue -----------------------------------------------------

def tradier_get(path: str, params: dict, token: str, base: str, timeout: int = 30) -> dict:
    qs = urllib.parse.urlencode(params or {})
    url = f"{base.rstrip('/')}/v1/{path}?{qs}" if qs else f"{base.rstrip('/')}/v1/{path}"
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}", "Accept": "application/json",
    }, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:500]
        raise RuntimeError(f"Tradier GET {path}: HTTP {e.code} -- {body}")


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
        raise RuntimeError(f"Tradier POST {path}: HTTP {e.code} -- {body_text}")


def resolve_account_id(token: str, base: str) -> str:
    """Pick the first account with option_level >= 3."""
    prof = tradier_get("user/profile", {}, token, base)
    accounts = (prof.get("profile") or {}).get("account") or []
    if isinstance(accounts, dict):
        accounts = [accounts]
    if not accounts:
        raise RuntimeError("No accounts on this Tradier profile.")
    usable = next((a for a in accounts if int(a.get("option_level", 0) or 0) >= 3), accounts[0])
    return usable["account_number"]


# --- pretty output ---------------------------------------------------------

def pp_ticket(t: ParsedTicket) -> None:
    print(f"{BOLD}Parsed ticket{N}")
    print(f"  verb        : {t.verb}")
    print(f"  label       : {t.label}")
    print(f"  symbol      : {t.symbol}")
    print(f"  expiration  : {t.expiration}")
    print(f"  order_type  : {t.order_type}")
    if t.limit_price is not None:
        print(f"  limit_price : ${t.limit_price:.2f}")
    print(f"  duration    : {t.duration}")
    print(f"  quantity    : {t.quantity}")
    if t.warning:
        print(f"  {Y}warning     : {t.warning}{N}")
    print(f"  {BOLD}legs ({len(t.legs)}):{N}")
    for i, l in enumerate(t.legs):
        action_color = G if l.action == "buy_to_open" else R
        print(f"    [{i}] {action_color}{l.action:<14}{N} {l.side.upper():<4} ${l.strike:>8.2f}  x{l.quantity}")


def pp_body(body: str) -> None:
    print(f"{BOLD}Form-urlencoded payload{N}")
    parsed = urllib.parse.parse_qsl(body, keep_blank_values=True)
    for k, v in parsed:
        print(f"  {D}{k:<25}{N} = {v}")


# --- main ------------------------------------------------------------------

def main():
    argv = sys.argv[1:]
    execute = False
    dump_only = False
    if "--execute" in argv:
        execute = True
        argv.remove("--execute")
    if "--dump" in argv:
        dump_only = True
        argv.remove("--dump")
    if not argv:
        print(__doc__)
        sys.exit(2)
    ticket_str = " ".join(argv)

    print(f"{BOLD}Ticket string{N}")
    print(f"  {D}{ticket_str}{N}\n")
    try:
        t = parse_ticket(ticket_str)
    except ValueError as e:
        sys.exit(f"{R}x Parse error: {e}{N}")
    pp_ticket(t)

    body = build_tradier_body(t, preview=True)
    print()
    pp_body(body)

    if dump_only:
        print(f"\n{Y}--dump set; skipping network call.{N}")
        return

    cfg = load_config()
    token, base, env = resolve_tradier_creds(cfg)
    if not token:
        sys.exit(f"{R}No Tradier {env} token configured (DEVMODE={cfg.get('DEVMODE','true')}); "
                 f"set TRADIER_{env.upper()}_TOKEN.{N}")
    print(f"\n{BOLD}Tradier env{N}: {env} ({base})")

    print(f"\n{B}[1/4] resolve account_id{N}")
    t0 = time.time()
    try:
        account_id = resolve_account_id(token, base)
        print(f"  {G}OK {(time.time()-t0):.2f}s -- account {account_id}{N}")
    except Exception as e:
        sys.exit(f"  {R}x {e}{N}")

    print(f"\n{B}[2/4] resolve canonical option symbols via chain{N}")
    t0 = time.time()
    try:
        resolved_root, leg_syms, missing = lookup_canonical_symbols(
            token, base, t.symbol, t.expiration, t.legs)
    except Exception as e:
        sys.exit(f"  {R}x {e}{N}")
    if missing:
        sys.exit(f"  {R}x {(time.time()-t0):.2f}s -- couldn't locate these legs on any root variant "
                 f"of {t.symbol}: {missing}.{N}")
    print(f"  {G}OK {(time.time()-t0):.2f}s -- root: {resolved_root}{N}")
    for sym in leg_syms:
        print(f"    {D}{sym}{N}")

    body = build_tradier_body(t, preview=True, option_symbols=leg_syms,
                              symbol_override=resolved_root)

    print(f"\n{B}[3/4] preview order{N}")
    t0 = time.time()
    try:
        resp = tradier_post(f"accounts/{account_id}/orders", body, token, base)
    except Exception as e:
        sys.exit(f"  {R}x {e}{N}")
    elapsed = time.time() - t0
    order = resp.get("order") or resp
    status = (order.get("status") or "").lower()
    if order.get("result") is False or status == "rejected":
        reason = order.get("reason_description") or order.get("status") or json.dumps(order)[:200]
        sys.exit(f"  {R}x {elapsed:.2f}s -- preview rejected: {reason}{N}")
    print(f"  {G}OK {elapsed:.2f}s -- preview OK . status={status or 'ok'}{N}")
    if "cost" in order:        print(f"    {D}cost={order.get('cost')}{N}")
    if "margin_change" in order: print(f"    {D}margin_change={order.get('margin_change')}{N}")
    if "commission" in order:  print(f"    {D}commission={order.get('commission')}{N}")
    if "fees" in order:        print(f"    {D}fees={order.get('fees')}{N}")

    if not execute:
        print(f"\n{Y}{BOLD}Preview only.{N} Re-run with {BOLD}--execute{N} to submit live.")
        return

    print(f"\n{B}[4/4] LIVE submit{N}")
    live_body = build_tradier_body(t, preview=False, option_symbols=leg_syms,
                                   symbol_override=resolved_root)
    t0 = time.time()
    try:
        live = tradier_post(f"accounts/{account_id}/orders", live_body, token, base)
    except Exception as e:
        sys.exit(f"  {R}x {e}{N}")
    elapsed = time.time() - t0
    lorder = live.get("order") or live
    oid = lorder.get("id")
    if not oid:
        sys.exit(f"  {R}x accepted POST but no order id in response: {json.dumps(live)[:200]}{N}")
    print(f"  {G}OK {elapsed:.2f}s -- order id {oid} . status {lorder.get('status','?')}{N}")
    if lorder.get("partner_id"):
        print(f"    {D}partner_id: {lorder.get('partner_id')}{N}")
    print(f"\n{G}{BOLD}Done.{N} Track {oid} in your Tradier terminal.")


if __name__ == "__main__":
    main()

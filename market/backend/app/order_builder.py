"""Multi-leg order builder for Tradier. Pure functions: take a candidate
(or ticket-string) + chain + account id -> return a Tradier order payload dict.

Source of truth: refactored out of tests/tradier_execute_ticket.py so the new
/orders route and the legacy CLI both call the same builders. The CLI's
parser (`parse_ticket`) and OCC-resolver helpers are re-exported here so the
CLI can import from one place.

All builders are PURE -- they do not hit the network. The network glue lives
in the CLI (urllib) or the FastAPI route (httpx via TradierClient).
"""
from __future__ import annotations

import re
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime


# --- data classes ----------------------------------------------------------

@dataclass
class Leg:
    """A single option leg of a multi-leg order.

    `action` is the Tradier `side[i]` value: 'buy_to_open' | 'sell_to_open'.
    `occ_symbol` is filled in once the chain has been resolved.
    """
    side: str                       # 'call' | 'put'
    strike: float
    action: str                     # 'buy_to_open' | 'sell_to_open'
    quantity: int = 1
    occ_symbol: str | None = None


@dataclass
class ParsedTicket:
    """Result of parsing the EdgeLane copy-button ticket string."""
    verb: str                       # 'Sell' or 'Buy'
    label: str                      # 'Balanced' / 'Conservative' / 'Aggressive' / ...
    symbol: str
    expiration: str                 # YYYY-MM-DD
    legs: list[Leg] = field(default_factory=list)
    order_type: str = ""            # 'market' | 'credit' | 'debit'
    limit_price: float | None = None
    duration: str = "day"           # 'day' | 'gtc'
    quantity: int = 1
    structure_text: str = ""        # raw legs string for echo/debugging
    warning: str | None = None


# --- ticket parser (verbatim port of tests/tradier_execute_ticket.py) ------

def parse_ticket(text: str) -> ParsedTicket:
    """Parse the EdgeLane copy-button ticket string into a ParsedTicket.
    Raises ValueError with a descriptive message on bad input."""
    raw = text.strip()
    warning = None
    m = re.search(r"\s*\[(WARN[^\]]*)\]\s*$", raw)
    if m:
        warning = m.group(1).strip()
        raw = raw[:m.start()].strip()

    parts = [p.strip() for p in raw.split("·") if p.strip()]

    # Tolerate "GTC" / "DAY" as a separate slot.
    if len(parts) == 7:
        if re.match(r"^(GTC|DAY|PRE|POST)$", parts[5], re.IGNORECASE):
            parts = parts[:4] + [parts[4] + " " + parts[5]] + parts[6:]
    # Normalize "Qty N" / "Qty: N" -> "N contract(s)" in the last slot.
    if len(parts) >= 6:
        qm = re.match(r"^Qty\s*:?\s*(\d+)\s*(contracts?)?\s*$", parts[-1], re.IGNORECASE)
        if qm:
            n = int(qm.group(1))
            parts[-1] = f"{n} contract" if n == 1 else f"{n} contracts"

    if len(parts) != 6:
        raise ValueError(f"Ticket has {len(parts)} parts (expected 6): {parts}")
    head, symbol, expiration, legs_str, mode_str, qty_part = parts

    head_tokens = head.split(None, 1)
    if not head_tokens:
        raise ValueError(f"Empty head: {head!r}")
    verb = head_tokens[0]
    if verb.lower() not in ("buy", "sell"):
        raise ValueError(f"Verb must be 'Buy' or 'Sell', got: {verb!r}")
    label = head_tokens[1] if len(head_tokens) > 1 else ""

    symbol = symbol.upper()
    if not re.match(r"^[A-Z0-9.]+$", symbol):
        raise ValueError(f"Bad symbol: {symbol!r}")

    try:
        datetime.strptime(expiration, "%Y-%m-%d")
    except ValueError:
        raise ValueError(f"Expiration must be YYYY-MM-DD, got: {expiration!r}")

    mode_lc = mode_str.strip()
    order_type = ""
    limit_price = None
    duration = "day"
    if mode_lc.upper() == "MARKET":
        order_type = "market"
    else:
        m = re.match(
            r"^LIMIT\s+(credit|debit)\s+\$?([0-9]*\.?[0-9]+)\s+(GTC|DAY)\s*$",
            mode_lc, re.IGNORECASE,
        )
        if not m:
            hint = ""
            if "$" not in mode_lc and re.search(r"\s\.?\d*\s+(GTC|DAY)", mode_lc, re.IGNORECASE):
                hint = ("\n  Hint: looks like bash stripped the leading dollar sign. "
                        "Wrap the ticket in SINGLE quotes ('...') instead of double quotes, "
                        "or escape as \\$6.20.")
            raise ValueError(f"Cannot parse mode: {mode_str!r}. "
                             f"Expected 'MARKET' or 'LIMIT credit|debit $X.XX GTC|DAY'.{hint}")
        order_type = m.group(1).lower()
        limit_price = float(m.group(2))
        duration = m.group(3).lower()

    qm = re.match(r"^(\d+)\s+contracts?$", qty_part.strip())
    if not qm:
        raise ValueError(f"Cannot parse quantity: {qty_part!r}. "
                         f"Expected 'N contract' or 'N contracts'.")
    quantity = int(qm.group(1))

    legs = _parse_legs(legs_str, verb, quantity)

    return ParsedTicket(
        verb=verb,
        label=label,
        symbol=symbol,
        expiration=expiration,
        legs=legs,
        order_type=order_type,
        limit_price=limit_price,
        duration=duration,
        quantity=quantity,
        structure_text=legs_str,
        warning=warning,
    )


def _normalize_legs_str(legs_str: str, verb: str) -> str:
    s = legs_str
    s = re.sub(r"\s+fly\s*$", "", s, flags=re.IGNORECASE)
    s = re.sub(r"\s*/\s*", "/", s)
    s = re.sub(r"\s*\+\s*", " + ", s)
    s = re.sub(r"(\d)\s+([pcPC])\s*$", r"\1\2", s)
    if re.search(r"\b(Short|Long)\b", s, re.IGNORECASE):
        out_segments = []
        for seg in s.split(" + "):
            pieces = seg.split("/")
            triples = []
            for piece in pieces:
                # Side char (P/C) is OPTIONAL per strike: a vertical/fly shares
                # one side, so users often write it on only one leg, e.g.
                # "Long 30130 / Short 30030P". We backfill the missing side below.
                pm = re.match(r"^\s*(short|long)?\s*(\d+(?:\.\d+)?)\s*([pcPC])?\s*$",
                              piece, re.IGNORECASE)
                if not pm:
                    return legs_str
                kw = (pm.group(1) or "").lower()
                k = float(pm.group(2))
                side = pm.group(3).upper() if pm.group(3) else None
                triples.append((kw, k, side))
            known_sides = {t[2] for t in triples if t[2]}
            if len(known_sides) != 1:
                # No side char anywhere, or conflicting sides → can't normalize.
                return legs_str
            side = next(iter(known_sides))
            strikes_sorted = sorted({t[1] for t in triples})
            strike_part = "/".join(str(int(k) if k == int(k) else k) for k in strikes_sorted)
            out_segments.append(f"{strike_part}{side}")
        return " + ".join(out_segments)
    return s


def _parse_legs(legs_str: str, verb: str, qty: int) -> list[Leg]:
    legs_str = _normalize_legs_str(legs_str, verb)
    is_credit = verb.lower() == "sell"
    segments = [s.strip() for s in legs_str.split("+")]
    legs: list[Leg] = []
    for seg in segments:
        m = re.match(r"^([\d./]+)\s*([PCpc])\s*$", seg)
        if not m:
            raise ValueError(f"Cannot parse leg segment: {seg!r}")
        strikes_part = m.group(1)
        side_char = m.group(2).upper()
        side = "put" if side_char == "P" else "call"
        strikes = [float(x) for x in strikes_part.split("/")]
        if len(strikes) == 2:
            lo, hi = sorted(strikes)
            legs.extend(_two_leg_vertical(side, lo, hi, is_credit, qty))
        elif len(strikes) == 3:
            lo, mid, high = sorted(strikes)
            legs.extend(_three_leg_butterfly(side, lo, mid, high, is_credit, qty))
        else:
            raise ValueError(f"Leg segment must have 2 or 3 strikes, got {len(strikes)}: {seg!r}")
    return legs


def _two_leg_vertical(side: str, lo: float, hi: float,
                      is_credit: bool, qty: int) -> list[Leg]:
    if is_credit:
        if side == "put":
            return [Leg("put",  lo, "buy_to_open",  qty),
                    Leg("put",  hi, "sell_to_open", qty)]
        else:
            return [Leg("call", lo, "sell_to_open", qty),
                    Leg("call", hi, "buy_to_open",  qty)]
    else:
        if side == "call":
            return [Leg("call", lo, "buy_to_open",  qty),
                    Leg("call", hi, "sell_to_open", qty)]
        else:
            return [Leg("put",  lo, "sell_to_open", qty),
                    Leg("put",  hi, "buy_to_open",  qty)]


def _three_leg_butterfly(side: str, lo: float, mid: float, hi: float,
                         is_credit: bool, qty: int) -> list[Leg]:
    if not is_credit:
        return [Leg(side, lo,  "buy_to_open",  qty),
                Leg(side, mid, "sell_to_open", qty * 2),
                Leg(side, hi,  "buy_to_open",  qty)]
    else:
        return [Leg(side, lo,  "sell_to_open", qty),
                Leg(side, mid, "buy_to_open",  qty * 2),
                Leg(side, hi,  "sell_to_open", qty)]


# --- OCC symbol fallback (when chain lookup is not available) --------------

def occ_symbol(symbol: str, expiration: str, side: str, strike: float) -> str:
    """OSI 21-char option symbol: {ROOT}{YYMMDD}{C|P}{strike*1000 zero-padded to 8}."""
    d = datetime.strptime(expiration, "%Y-%m-%d")
    yy = f"{d.year % 100:02d}"
    mm = f"{d.month:02d}"
    dd = f"{d.day:02d}"
    cp = "C" if side.lower().startswith("c") else "P"
    strike_int = round(strike * 1000)
    return f"{symbol.upper()}{yy}{mm}{dd}{cp}{strike_int:08d}"


# --- candidate -> Leg list -------------------------------------------------

def _action_from_long_short(ls: int) -> str:
    """Map candidate.legs[i].long_short -> Tradier side.

    +1 -> buy_to_open
    -1 -> sell_to_open
    -2 -> sell_to_open (fly middle, quantity will be doubled by caller)
    """
    return "buy_to_open" if int(ls) > 0 else "sell_to_open"


def _abs_qty_multiplier(ls: int) -> int:
    """Magnitude of the long_short field -- fly middle is -2 meaning 2x qty."""
    return max(1, abs(int(ls)))


def build_legs_from_candidate(candidate: dict, base_quantity: int = 1) -> list[Leg]:
    """Translate an engine candidate (from strategy_engine.generate_candidates)
    into Leg objects.

    The engine emits a `legs` list with {strike, side, long_short, symbol?, ...}.
    long_short in {+1, -1, -2}:
      +1 -> buy_to_open
      -1 -> sell_to_open
      -2 -> sell_to_open with 2x qty (fly middle)

    Pre-resolved OCC symbols on the candidate (when the chain ingest carries
    them through) get attached to the Leg.occ_symbol field so the caller can
    skip a chain lookup. base_quantity is the per-spread contract count the
    user requested (1 by default -- wires from the dialog spinner).
    """
    out: list[Leg] = []
    for leg in (candidate.get("legs") or []):
        try:
            strike = float(leg.get("strike"))
        except (TypeError, ValueError):
            raise ValueError(f"Leg has no numeric strike: {leg!r}")
        side = (leg.get("side") or "").lower()
        if side not in ("call", "put"):
            raise ValueError(f"Leg side must be 'call' or 'put', got: {leg.get('side')!r}")
        ls = int(leg.get("long_short") or 0)
        if ls == 0:
            raise ValueError(f"Leg long_short missing/zero: {leg!r}")
        qty = base_quantity * _abs_qty_multiplier(ls)
        out.append(Leg(
            side=side,
            strike=strike,
            action=_action_from_long_short(ls),
            quantity=qty,
            occ_symbol=leg.get("symbol"),
        ))
    if not out:
        raise ValueError(
            f"Candidate has no legs: {candidate.get('strategy')}/{candidate.get('label')}"
        )
    return out


# --- chain symbol resolution -----------------------------------------------

INDEX_ROOT_VARIANTS = ('', 'P', 'W', 'PM', 'X')


def resolve_leg_symbols(legs: list[Leg],
                        chain_contracts: list[dict],
                        expiration: str | None = None) -> list[Leg]:
    """Look up each leg's OCC symbol from an already-fetched chain (the same
    list of dicts returned by TradierClient.options_chain()).

    Returns a new list of Legs with `occ_symbol` populated. Matches by
    (strike, option_type) -- the chain row's 'symbol' string IS the Tradier
    canonical OCC identifier, so no construction is needed.

    Raises ValueError if any leg can't be located.
    """
    index: dict[tuple[float, str], str] = {}
    for opt in chain_contracts or []:
        try:
            k = float(opt.get("strike"))
        except (TypeError, ValueError):
            continue
        side = (opt.get("option_type") or opt.get("side") or "").lower()
        if side not in ("call", "put"):
            continue
        sym = opt.get("symbol")
        if not sym:
            continue
        if expiration is not None:
            row_exp = opt.get("expiration_date") or opt.get("expiration")
            if row_exp and str(row_exp) != str(expiration):
                continue
        index[(round(k, 2), side)] = sym

    resolved: list[Leg] = []
    missing: list[tuple[float, str]] = []
    for leg in legs:
        key = (round(leg.strike, 2), leg.side.lower())
        sym = leg.occ_symbol or index.get(key)
        if not sym:
            missing.append(key)
            resolved.append(leg)
            continue
        resolved.append(Leg(
            side=leg.side,
            strike=leg.strike,
            action=leg.action,
            quantity=leg.quantity,
            occ_symbol=sym,
        ))
    if missing:
        raise ValueError(
            f"Could not resolve OCC symbols for {len(missing)} leg(s): {missing}. "
            f"Expiration={expiration!r}; chain rows={len(chain_contracts or [])}."
        )
    return resolved


# --- Tradier order body ----------------------------------------------------

def build_tradier_order_payload(
    *,
    account_id: str,
    symbol: str,
    legs: list[Leg],
    order_type: str,
    limit_price: float | None,
    duration: str = "day",
    preview: bool = True,
    tag: str | None = None,
) -> dict:
    """Build the form-encoded params for Tradier POST /v1/accounts/{id}/orders
    as a dict (suitable for httpx data=...).

    `account_id` is informational here -- the caller wires it into the URL path.
    Every leg must have `occ_symbol` populated (call resolve_leg_symbols first).
    """
    if not account_id:
        raise ValueError("account_id is required")
    if not legs:
        raise ValueError("at least one leg required")
    order_type = (order_type or "").lower()
    if order_type not in ("market", "credit", "debit"):
        raise ValueError(f"order_type must be 'market'|'credit'|'debit', got {order_type!r}")
    if order_type != "market" and limit_price is None:
        raise ValueError("limit_price required for non-market orders")
    duration = (duration or "day").lower()
    if duration not in ("day", "gtc", "pre", "post"):
        raise ValueError(f"duration must be 'day'|'gtc'|'pre'|'post', got {duration!r}")

    params: dict[str, str] = {
        "class": "multileg",
        "symbol": symbol.upper(),
        "type": order_type,
        "duration": duration,
    }
    if order_type != "market":
        params["price"] = f"{float(limit_price):.2f}"
    if tag:
        clean = re.sub(r"[^a-zA-Z0-9]", "", tag)[:30]
        if clean:
            params["tag"] = clean
    if preview:
        params["preview"] = "true"

    for i, leg in enumerate(legs):
        sym = leg.occ_symbol
        if not sym:
            raise ValueError(
                f"Leg {i} has no occ_symbol -- call resolve_leg_symbols first."
            )
        params[f"option_symbol[{i}]"] = sym
        params[f"side[{i}]"] = leg.action
        params[f"quantity[{i}]"] = str(int(leg.quantity))
    return params


def build_tradier_order_body(*args, **kwargs) -> str:
    """Same as build_tradier_order_payload() but returns the urlencoded string
    (used by the legacy urllib-based CLI)."""
    return urllib.parse.urlencode(build_tradier_order_payload(*args, **kwargs))


# --- reverse: candidate -> ticket text (matches JSX copy-button format) ----

def candidate_to_ticket_text(candidate: dict, symbol: str, expiration: str,
                              quantity: int = 1) -> str:
    """Build the copy-button ticket string from a candidate, matching the
    format parse_ticket() understands."""
    label = candidate.get("label") or "Balanced"
    cand_type = (candidate.get("type") or "credit").lower()
    verb = "Sell" if cand_type == "credit" else "Buy"
    structure = candidate.get("structure_text") or ""
    net = float(candidate.get("net_premium") or 0.0)
    if net > 0:
        mode = f"LIMIT {cand_type} ${net:.2f} DAY"
    else:
        mode = "MARKET"
    qty_part = f"{quantity} contract" if quantity == 1 else f"{quantity} contracts"
    sep = "·"
    return (f"{verb} {label} {sep} {symbol.upper()} {sep} {expiration} {sep} {structure} "
            f"{sep} {mode} {sep} {qty_part}")

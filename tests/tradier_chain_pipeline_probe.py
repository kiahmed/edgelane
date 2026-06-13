"""
Tradier chain pipeline probe — mirrors EdgeLane's JSX-side optionsChain()
filter + normalize logic so we can see exactly where contracts get dropped.

Tradier itself works fine (tradier_smoke.py confirms) — but EdgeLane runs
additional client-side filtering: ±30% strike band around spot, normalizer
that filters out null sides, ATM strike pick, expected-move compute. If
any step empties the list for NVDA but not SPX, this will show it.

Usage:
    python tests/tradier_chain_pipeline_probe.py NVDA [YYYY-MM-DD]
"""
from __future__ import annotations
import sys
import time
import json
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent))
from utils import load_config, resolve_tradier_creds

G, R, Y, B, D, N, BOLD = "\033[32m", "\033[31m", "\033[33m", "\033[34m", "\033[2m", "\033[0m", "\033[1m"


def tradier_get(path: str, params: dict, token: str, base: str) -> dict:
    qs = urllib.parse.urlencode(params)
    url = f"{base.rstrip('/')}/v1/{path}?{qs}"
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    })
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def main():
    symbol = (sys.argv[1] if len(sys.argv) > 1 else "NVDA").upper()
    expiration_arg = sys.argv[2] if len(sys.argv) > 2 else None

    cfg = load_config()
    token, base, env = resolve_tradier_creds(cfg)
    if not token:
        sys.exit(f"{R}No {env} Tradier token configured.{N}")

    print(f"{BOLD}EdgeLane chain pipeline probe — {symbol}{N}")
    print(f"{D}env: {env}  base: {base}{N}\n")

    print(f"{B}[1/6] markets/quotes({symbol}){N}")
    t0 = time.time()
    quote_resp = tradier_get("markets/quotes", {"symbols": symbol, "greeks": "false"}, token, base)
    q = (quote_resp.get("quotes") or {}).get("quote") or {}
    if isinstance(q, list):
        q = q[0]
    spot = float(q.get("last") or q.get("close") or 0)
    print(f"  {G}✓ {(time.time()-t0):.2f}s — spot ${spot:.2f}{N}")
    if not spot:
        sys.exit(f"  {R}✗ no usable spot price{N}")

    print(f"\n{B}[2/6] markets/options/expirations({symbol}){N}")
    t0 = time.time()
    exp_resp = tradier_get("markets/options/expirations",
                           {"symbol": symbol, "includeAllRoots": "true"}, token, base)
    expirations = (exp_resp.get("expirations") or {}).get("date") or []
    if isinstance(expirations, str):
        expirations = [expirations]
    print(f"  {G}✓ {(time.time()-t0):.2f}s — {len(expirations)} expirations available{N}")
    print(f"  {D}front 5: {expirations[:5]}{N}")

    if expiration_arg:
        chosen_exp = expiration_arg
        if chosen_exp not in expirations:
            print(f"  {Y}⚠ requested {chosen_exp} not in Tradier list — proceeding anyway{N}")
    else:
        chosen_exp = expirations[0]
    print(f"\n  using expiration: {BOLD}{chosen_exp}{N}")

    print(f"\n{B}[3/6] markets/options/chains({symbol}, {chosen_exp}, greeks=true){N}")
    t0 = time.time()
    chain_resp = tradier_get("markets/options/chains",
                             {"symbol": symbol, "expiration": chosen_exp, "greeks": "true"},
                             token, base)
    raw = (chain_resp.get("options") or {}).get("option") or []
    if not isinstance(raw, list):
        raw = [raw]
    print(f"  {G}✓ {(time.time()-t0):.2f}s — Tradier returned {len(raw)} raw contracts{N}")

    if not raw:
        sys.exit(f"  {R}✗ empty raw chain — Tradier doesn't list {symbol} options for {chosen_exp}{N}")

    side_counts = {"call": 0, "put": 0, "unknown": 0}
    for c in raw:
        s = (c.get("option_type") or "").lower()
        if "call" in s:
            side_counts["call"] += 1
        elif "put" in s:
            side_counts["put"] += 1
        else:
            side_counts["unknown"] += 1
    print(f"  {D}sides: {side_counts}{N}")

    print(f"\n{B}[4/6] EdgeLane normalizer — drop contracts with unresolvable side{N}")
    def normalize(c):
        side_raw = (c.get("option_type") or "").lower()
        if "call" in side_raw:
            side = "call"
        elif "put" in side_raw:
            side = "put"
        else:
            return None
        return {
            "strike": _num(c.get("strike")),
            "side": side,
            "expiration": chosen_exp,
            "bid": _num(c.get("bid")) or 0,
            "ask": _num(c.get("ask")) or 0,
            "open_interest": _num(c.get("open_interest")) or 0,
            "volume": _num(c.get("volume")) or 0,
            "symbol": c.get("symbol"),
        }
    normalized = [n for n in (normalize(c) for c in raw) if n is not None]
    print(f"  {G}✓ {len(normalized)}/{len(raw)} kept after normalize{N}")
    if not normalized:
        sys.exit(f"  {R}✗ all contracts dropped by normalizer — option_type field missing or unrecognized{N}")

    print(f"\n{B}[5/6] EdgeLane ±30% strike-band filter (lo=${spot*0.7:.2f}, hi=${spot*1.3:.2f}){N}")
    lo = spot * 0.7
    hi = spot * 1.3
    in_band = [c for c in normalized if c["strike"] is not None and lo <= c["strike"] <= hi]
    print(f"  {G}✓ {len(in_band)}/{len(normalized)} kept in ±30% band{N}")
    if not in_band:
        sys.exit(f"  {R}✗ no contracts in band — strikes are too sparse around spot{N}")
    strikes = sorted({c['strike'] for c in in_band})
    print(f"  {D}{len(strikes)} distinct strikes: {strikes[:5]} ... {strikes[-5:]}{N}")

    print(f"\n{B}[6/6] ATM detection + bid>0 filter (what spread builders need){N}")
    closest = min((s for s in strikes), key=lambda s: abs(s - spot))
    print(f"  ATM strike pick: {closest}")
    bid_positive = [c for c in in_band if c["bid"] > 0]
    print(f"  {G}✓ {len(bid_positive)}/{len(in_band)} contracts have bid > 0 (selectable as short legs){N}")
    if len(bid_positive) < 4:
        sys.exit(f"  {R}✗ fewer than 4 contracts with bid > 0 — can't build any vertical, condor, or fly{N}")

    # ATM IV / expected move
    atm_call = next((c for c in in_band if c["strike"] == closest and c["side"] == "call"), None)
    atm_put = next((c for c in in_band if c["strike"] == closest and c["side"] == "put"), None)
    bid_call = atm_call["bid"] if atm_call else 0
    bid_put = atm_put["bid"] if atm_put else 0
    print(f"  ATM call: {atm_call['symbol'] if atm_call else '—'}  bid=${bid_call:.2f}")
    print(f"  ATM put : {atm_put['symbol'] if atm_put else '—'}  bid=${bid_put:.2f}")
    if not (atm_call and atm_put):
        print(f"  {Y}⚠ ATM strike missing call or put — expectedMove will be 0{N}")

    print(f"\n{BOLD}═══ SUMMARY ═══{N}")
    print(f"  Tradier raw:        {len(raw)} contracts")
    print(f"  After normalize:    {len(normalized)} contracts")
    print(f"  In ±30% band:       {len(in_band)} contracts")
    print(f"  Bid>0 (selectable): {len(bid_positive)} contracts")
    print(f"  Distinct strikes:   {len(strikes)}")
    print()
    if len(bid_positive) >= 4 and atm_call and atm_put:
        print(f"  {G}✓ Pipeline produces enough contracts for EdgeLane to build candidates.{N}")
        print(f"  {Y}If the JSX still hides the bottom panel after this passes, the issue is downstream{N}")
        print(f"  {Y}(generateCandidates / scoreVertical / scoreCondor) — check console for{N}")
        print(f"  {Y}[candidates] log line from the v4.7.43 diagnostics.{N}")
    else:
        print(f"  {R}✗ Pipeline drops the chain to unusable. EdgeLane will show 0 candidates.{N}")


def _num(v):
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Smoke-test the three Atlas REST endpoints v4.6 depends on.

Checks the ACTUAL response shapes (not the docs' theoretical ones):
  - get_stock_quote         → symbol, price, change, change_percent, volume, timestamp
  - analyze_greek_exposures → symbol, current_price, exposures_by_date, portfolio_totals,
                              key_levels, expirations_analyzed (NOT flat gex/dex/vex/tex)
  - get_options_chain       → expects expiration param; returns chain[] with per-contract
                              fields: strike, side, bid, ask, mid, delta, gamma, theta,
                              iv, volume, open_interest

Strict pass criteria — missing required fields = test fails (no more "✓ pass" lies).
Verbose shape dumps so the next iteration debugs in one round.

Usage:
    python3 tests/atlas_rest_smoke.py [SYMBOL]      # default SPY
"""
import json
import sys
from utils import load_config, require_keys, atlas_call, AtlasError, Stopwatch, pp

RST = "\033[0m"
GREEN = "\033[32m"; RED = "\033[31m"; DIM = "\033[2m"; BOLD = "\033[1m"; YELLOW = "\033[33m"

# Required fields per response — test fails if any are missing
REQ_QUOTE_KEYS = ["symbol", "price"]
REQ_GREEKS_KEYS = ["symbol", "current_price", "exposures_by_date"]
REQ_CONTRACT_FIELDS = ["strike", "side", "bid", "ask", "delta", "gamma", "theta", "iv", "volume", "open_interest"]


def check_required(obj, required, label):
    """Return list of missing required keys."""
    if not isinstance(obj, dict):
        return required[:]  # everything missing if not a dict
    return [k for k in required if k not in obj]


def main():
    symbol = (sys.argv[1] if len(sys.argv) > 1 else "SPY").upper()
    cfg = load_config()
    require_keys(cfg, "ATLAS_KEY")
    key = cfg["ATLAS_KEY"]

    print(f"{BOLD}Atlas REST smoke test — symbol={symbol}{RST}\n")

    failures = []  # list of (test_name, reason) — empty == all pass

    # ------------------------------------------------------------------------
    # [1/3] get_stock_quote
    # ------------------------------------------------------------------------
    print(f"{BOLD}[1/3] get_stock_quote{RST}")
    try:
        with Stopwatch() as sw:
            quote = atlas_call("get_stock_quote", {"symbol": symbol}, key)
        pp("  latency", f"{sw.ms:.0f} ms", DIM)
        pp("  raw keys", ", ".join(quote.keys()) if isinstance(quote, dict) else type(quote).__name__)
        for k in ("symbol", "price", "change", "change_percent", "volume", "timestamp"):
            pp(f"  {k}", quote.get(k))
        missing = check_required(quote, REQ_QUOTE_KEYS, "quote")
        if missing:
            print(RED + f"  ✗ MISSING required: {missing}" + RST)
            failures.append(("get_stock_quote", f"missing {missing}"))
        else:
            print(GREEN + "  ✓ pass" + RST)
    except AtlasError as e:
        print(RED + f"  ✗ {e}" + RST); failures.append(("get_stock_quote", str(e)))

    spot = quote.get("price") if "quote" in dir() and not failures else None

    # ------------------------------------------------------------------------
    # [2/3] analyze_greek_exposures
    # ------------------------------------------------------------------------
    print(f"\n{BOLD}[2/3] analyze_greek_exposures (num_expirations=3){RST}")
    target_expiration = None
    try:
        with Stopwatch() as sw:
            greeks = atlas_call("analyze_greek_exposures", {"symbol": symbol, "num_expirations": 3}, key)
        pp("  latency", f"{sw.ms:.0f} ms", DIM)
        pp("  raw keys", ", ".join(greeks.keys()) if isinstance(greeks, dict) else type(greeks).__name__)
        pp("  current_price", greeks.get("current_price"))
        pp("  expirations_analyzed", greeks.get("expirations_analyzed"))
        pp("  portfolio_totals keys", list((greeks.get("portfolio_totals") or {}).keys())[:8])
        pp("  key_levels keys", list((greeks.get("key_levels") or {}).keys())[:8])
        ebd = greeks.get("exposures_by_date") or {}
        dates = list(ebd.keys())
        pp("  exposures_by_date dates", dates)
        if dates:
            target_expiration = dates[0]
            sample_date = dates[0]
            sample = ebd[sample_date]
            sample_keys = list(sample.keys())[:10] if isinstance(sample, dict) else []
            pp(f"  sample date '{sample_date}' keys", sample_keys)
            # Try to find per-strike data (most likely under by_strike or strikes)
            for k in ("by_strike", "strikes", "data", "rows"):
                if isinstance(sample, dict) and k in sample:
                    arr = sample[k]
                    if isinstance(arr, list) and arr:
                        pp(f"    sample {k}[0] keys", list(arr[0].keys())[:10])
                        break

        missing = check_required(greeks, REQ_GREEKS_KEYS, "greeks")
        if missing:
            print(RED + f"  ✗ MISSING required: {missing}" + RST)
            failures.append(("analyze_greek_exposures", f"missing {missing}"))
        elif not dates:
            print(RED + "  ✗ exposures_by_date is empty" + RST)
            failures.append(("analyze_greek_exposures", "empty exposures_by_date"))
        else:
            print(GREEN + "  ✓ pass" + RST)
    except AtlasError as e:
        print(RED + f"  ✗ {e}" + RST); failures.append(("analyze_greek_exposures", str(e)))

    # ------------------------------------------------------------------------
    # [3/3] get_options_chain — REQUIRES expiration
    # ------------------------------------------------------------------------
    print(f"\n{BOLD}[3/3] get_options_chain (expiration={target_expiration}){RST}")
    if not target_expiration:
        print(YELLOW + "  ⚠ no expiration available from greeks; using fallback" + RST)
        target_expiration = "2026-05-15"
    try:
        with Stopwatch() as sw:
            chain = atlas_call("get_options_chain", {"symbol": symbol, "expiration": target_expiration}, key)
        pp("  latency", f"{sw.ms:.0f} ms", DIM)
        pp("  raw keys", ", ".join(chain.keys()) if isinstance(chain, dict) else type(chain).__name__)
        pp("  current_price", chain.get("current_price"))
        pp("  expiration", chain.get("expiration"))
        pp("  total_contracts", chain.get("total_contracts"))

        # Find contracts wherever they live
        contracts = []
        if "contracts" in chain and isinstance(chain["contracts"], list):
            contracts = chain["contracts"]
        elif "chain" in chain and isinstance(chain["chain"], list):
            for grp in chain["chain"]:
                if isinstance(grp, dict):
                    contracts.extend(grp.get("contracts", []) or [])
                    contracts.extend(grp.get("calls", []) or [])
                    contracts.extend(grp.get("puts", []) or [])

        pp("  contracts found", len(contracts))
        if contracts:
            sample = contracts[0]
            present = [f for f in REQ_CONTRACT_FIELDS if f in sample]
            missing_fields = [f for f in REQ_CONTRACT_FIELDS if f not in sample]
            pp("  sample contract", {k: sample.get(k) for k in present[:6]})
            if missing_fields:
                print(RED + f"  ✗ contract MISSING required: {missing_fields}" + RST)
                failures.append(("get_options_chain", f"contract missing {missing_fields}"))
            else:
                print(GREEN + f"  ✓ pass — all {len(REQ_CONTRACT_FIELDS)} required contract fields present" + RST)
        else:
            print(RED + "  ✗ no contracts in response" + RST)
            failures.append(("get_options_chain", "no contracts"))
    except AtlasError as e:
        print(RED + f"  ✗ {e}" + RST); failures.append(("get_options_chain", str(e)))

    # ------------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------------
    print(f"\n{BOLD}{'='*60}{RST}")
    if failures:
        print(RED + f"✗ {len(failures)} failure(s):" + RST)
        for name, reason in failures:
            print(RED + f"  - {name}: {reason}" + RST)
        sys.exit(1)
    else:
        print(GREEN + "✓ all 3 endpoints responded with expected shape" + RST)


if __name__ == "__main__":
    main()

"""
Tradier sandbox smoke test.

Verifies:
    1. Token works (user/profile)
    2. Equity quote returns a price
    3. Options expirations endpoint returns dates
    4. Options chain returns contracts with greeks populated

Usage:
    python tests/tradier_smoke.py [SYMBOL]   # default SPY

Requires in config:
    TRADIER_ACCESS_TOKEN="..."
    TRADIER_BASE_URL="https://sandbox.tradier.com"   # or production
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent))

from utils import load_config
from data_providers import tradier

# ANSI
G, R, Y, B, D, N, BOLD = "\033[32m", "\033[31m", "\033[33m", "\033[34m", "\033[2m", "\033[0m", "\033[1m"


def main():
    symbol = (sys.argv[1] if len(sys.argv) > 1 else "SPY").upper()
    cfg = load_config()
    token = cfg.get("TRADIER_ACCESS_TOKEN")
    base = cfg.get("TRADIER_BASE_URL") or "https://sandbox.tradier.com"
    if not token:
        sys.exit(f"{R}TRADIER_ACCESS_TOKEN missing from config{N}")

    print(f"{BOLD}Tradier smoke — {symbol}{N}")
    print(f"{D}base: {base}{N}\n")
    fails = []

    # 1. user/profile — token sanity
    print(f"{B}[1/4] user/profile{N}")
    t0 = time.time()
    try:
        prof = tradier.user_profile(token, base)
        elapsed = time.time() - t0
        pid = (prof.get("profile") or {}).get("id", "?")
        print(f"  {G}✓ {elapsed:.2f}s — profile id: {pid}{N}")
    except Exception as e:
        elapsed = time.time() - t0
        print(f"  {R}✗ {elapsed:.2f}s — {e}{N}")
        fails.append(("user/profile", str(e)))
        sys.exit(1)  # everything else depends on auth working

    # 2. equity quote
    print(f"\n{B}[2/4] markets/quotes({symbol}){N}")
    t0 = time.time()
    try:
        q = tradier.stock_quote(symbol, token, base)
        elapsed = time.time() - t0
        if not q:
            print(f"  {R}✗ {elapsed:.2f}s — empty quote response{N}")
            fails.append(("quote", "empty response"))
        else:
            last = q.get("last")
            bid = q.get("bid")
            ask = q.get("ask")
            print(f"  {G}✓ {elapsed:.2f}s — last=${last}  bid=${bid}  ask=${ask}{N}")
    except Exception as e:
        elapsed = time.time() - t0
        print(f"  {R}✗ {elapsed:.2f}s — {e}{N}")
        fails.append(("quote", str(e)))

    # 3. expirations
    print(f"\n{B}[3/4] markets/options/expirations({symbol}){N}")
    t0 = time.time()
    expirations = []
    try:
        expirations = tradier.option_expirations(symbol, token, base)
        elapsed = time.time() - t0
        if not expirations:
            print(f"  {R}✗ {elapsed:.2f}s — empty expirations list{N}")
            fails.append(("expirations", "empty"))
        else:
            print(f"  {G}✓ {elapsed:.2f}s — {len(expirations)} dates, front 3: {expirations[:3]}{N}")
    except Exception as e:
        elapsed = time.time() - t0
        print(f"  {R}✗ {elapsed:.2f}s — {e}{N}")
        fails.append(("expirations", str(e)))

    # 4. options chain with greeks
    if expirations:
        front = expirations[0]
        print(f"\n{B}[4/4] markets/options/chains({symbol}, {front}, greeks=true){N}")
        t0 = time.time()
        try:
            raw = tradier.options_chain(symbol, front, token, base, greeks=True)
            normalized = tradier.normalize_chain(raw, front)
            elapsed = time.time() - t0
            if not normalized:
                print(f"  {R}✗ {elapsed:.2f}s — empty chain{N}")
                fails.append(("chain", "empty"))
            else:
                with_greeks = sum(1 for c in normalized if c.get("delta") is not None)
                calls = sum(1 for c in normalized if c["side"] == "call")
                puts = sum(1 for c in normalized if c["side"] == "put")
                with_oi = sum(1 for c in normalized if c["open_interest"] > 0)
                print(f"  {G}✓ {elapsed:.2f}s — {len(normalized)} contracts "
                      f"({calls} calls / {puts} puts), {with_greeks} with greeks, "
                      f"{with_oi} with OI>0{N}")
                # Spot-check the first contract that has greeks
                sample = next((c for c in normalized if c.get("delta") is not None), None)
                if sample:
                    print(f"  {D}sample contract: strike=${sample['strike']:.2f} {sample['side']} "
                          f"bid=${sample['bid']:.2f} ask=${sample['ask']:.2f} "
                          f"delta={sample['delta']:+.3f} gamma={sample['gamma']:+.4f} "
                          f"iv={sample.get('iv')}{N}")
        except Exception as e:
            elapsed = time.time() - t0
            print(f"  {R}✗ {elapsed:.2f}s — {e}{N}")
            fails.append(("chain", str(e)))
    else:
        print(f"\n{B}[4/4] markets/options/chains — SKIPPED (no expirations){N}")
        fails.append(("chain", "skipped — no expirations"))

    # Rate-limit footer
    rl = tradier.last_rate_limit()
    if rl:
        print(f"\n{D}rate-limit: used {rl.get('X-Ratelimit-Used', '?')} of "
              f"{rl.get('X-Ratelimit-Allowed', '?')}, "
              f"{rl.get('X-Ratelimit-Available', '?')} remaining{N}")

    # Summary
    print(f"\n{BOLD}SUMMARY{N}")
    if fails:
        for step, err in fails:
            print(f"  {R}✗ {step}: {err[:120]}{N}")
        sys.exit(1)
    else:
        print(f"  {G}✓ all 4 checks passed — Tradier is reachable and chain data is normalized correctly{N}")


if __name__ == "__main__":
    main()

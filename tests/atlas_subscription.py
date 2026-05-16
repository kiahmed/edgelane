#!/usr/bin/env python3
"""Check Atlas subscription status — plan, calls used, calls remaining.

Free tier: 10/month. Paid tier: 200/month at $30. Each detectBias = 2 calls,
each fetchChain = 1 call. Run this before a session to see how much you can
afford, and after to see the delta.

Usage:
    python tests/atlas_subscription.py
"""
import json
import sys
from utils import load_config, require_keys, atlas_call, AtlasError, Stopwatch

RST = "\033[0m"
GREEN = "\033[32m"; RED = "\033[31m"; DIM = "\033[2m"; BOLD = "\033[1m"; YELLOW = "\033[33m"


def _walk_for(d, keys):
    """Defensive walk: try multiple plausible key names at multiple depths."""
    if not isinstance(d, dict): return None
    # try direct
    for k in keys:
        if k in d and d[k] is not None: return d[k]
    # try one level down
    for v in d.values():
        if isinstance(v, dict):
            for k in keys:
                if k in v and v[k] is not None: return v[k]
    return None


def _num(v):
    if v is None: return None
    if isinstance(v, (int, float)): return float(v)
    try: return float(str(v).replace(',', ''))
    except (ValueError, TypeError): return None


def main():
    cfg = load_config()
    require_keys(cfg, "ATLAS_KEY")
    key = cfg["ATLAS_KEY"]

    print(f"{BOLD}Atlas subscription status{RST}")
    print(f"{DIM}  POST /api/v1/tools/get_subscription_status{RST}\n")

    try:
        with Stopwatch() as sw:
            resp = atlas_call("get_subscription_status", {}, key)
    except AtlasError as e:
        print(RED + f"✗ {e}" + RST)
        sys.exit(1)

    # Pretty-print structured fields. Atlas docs say { plan, status, usage, limits }
    # — we walk defensively because the actual nested shape isn't documented.
    plan      = _walk_for(resp, ["plan", "tier", "subscription"])
    status    = _walk_for(resp, ["status", "state", "subscription_status"])
    used      = _num(_walk_for(resp, ["calls_used", "calls_this_period", "used", "consumed", "count"]))
    limit     = _num(_walk_for(resp, ["calls_limit", "limit", "calls_per_period", "max_calls", "total"]))
    period    = _walk_for(resp, ["period", "billing_period", "cycle"])
    resets_at = _walk_for(resp, ["reset_at", "renewal_date", "next_reset", "period_end"])

    print(f"  {BOLD}Plan:{RST}     {plan or '?'}")
    print(f"  {BOLD}Status:{RST}   {status or '?'}")
    if used is not None and limit is not None and limit > 0:
        pct = used / limit * 100
        color = GREEN if pct < 75 else YELLOW if pct < 95 else RED
        bar_width = 30
        filled = int(round(min(1.0, used / limit) * bar_width))
        bar = color + "█" * filled + DIM + "·" * (bar_width - filled) + RST
        print(f"  {BOLD}Usage:{RST}    {color}{int(used)}/{int(limit)}{RST}  ({color}{pct:.1f}%{RST})")
        print(f"            {bar}")
        remaining = int(max(0, limit - used))
        bias_calls_left  = remaining // 2
        chain_calls_left = remaining
        print(f"  {DIM}Remaining: {remaining} calls = ~{bias_calls_left} more bias detections, "
              f"~{chain_calls_left} chain refreshes (1 bias = 2 calls, 1 chain = 1 call){RST}")
    else:
        print(f"  {BOLD}Usage:{RST}    {used}/{limit}  {YELLOW}(could not parse — see raw below){RST}")

    if period:    print(f"  {BOLD}Period:{RST}   {period}")
    if resets_at: print(f"  {BOLD}Resets:{RST}   {resets_at}")

    print(f"\n  {DIM}fetched in {sw.ms:.0f} ms{RST}")
    print(f"\n{DIM}Raw response (for shape debugging):{RST}")
    print(json.dumps(resp, indent=2)[:1200])


if __name__ == "__main__":
    main()

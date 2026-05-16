"""
Probe Atlas for chunked greek-exposure feasibility.

Tests three things:
  1. Baseline: time analyze_greek_exposures(symbol, num_expirations=3)
  2. Chunked:  fetch expirations, fan out 3× greek_exposure_single_expiration in parallel
  3. Shape compatibility: confirm single-expiration response shape matches one
     entry of the baseline's exposures_by_date dict

Usage:
  python tests/atlas_chunked_probe.py [SYMBOL]    # default SYMBOL=MU

Cost:
  4-5 Atlas quota calls per run (1 baseline + 1 expirations list + 3 chunked).
"""
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from utils import load_config, atlas_call, AtlasError

SYMBOL = sys.argv[1].upper() if len(sys.argv) > 1 else "MU"

# ANSI
G = "\033[32m"
R = "\033[31m"
Y = "\033[33m"
B = "\033[34m"
D = "\033[2m"
N = "\033[0m"
BOLD = "\033[1m"


def _shape(d, depth=0, max_depth=2):
    """Pretty-print the top-level shape of a dict for inspection."""
    if depth >= max_depth:
        return "..."
    if isinstance(d, dict):
        return "{" + ", ".join(
            f"{k}: {_shape(v, depth + 1, max_depth)}" for k, v in list(d.items())[:6]
        ) + ("}" if len(d) <= 6 else f", ...+{len(d) - 6} more}}")
    if isinstance(d, list):
        if not d:
            return "[]"
        return f"[{_shape(d[0], depth + 1, max_depth)}, ...×{len(d)}]"
    if isinstance(d, str):
        return f'"{d[:30]}{"…" if len(d) > 30 else ""}"'
    return type(d).__name__


def _time_call(label, fn):
    """Run fn(), time it, return (result, elapsed_seconds, error_str_or_None)."""
    start = time.time()
    try:
        result = fn()
        return result, time.time() - start, None
    except Exception as e:
        return None, time.time() - start, str(e)[:200]


def main():
    cfg = load_config()
    key = cfg.get("ATLAS_KEY")
    if not key:
        sys.exit(f"{R}ATLAS_KEY missing from config{N}")

    print(f"{BOLD}Atlas chunked-probe — symbol: {SYMBOL}{N}")
    print(f"{D}gateway: atlasmcp.finmanagerai.com{N}")
    print()

    # ─── Step 1: expirations list (cheap) ──────────────────────────
    print(f"{B}[1/3] Option-Expiration-Dates({SYMBOL}){N}")
    exp_result, exp_t, exp_err = _time_call(
        "expirations",
        lambda: atlas_call("Option-Expiration-Dates", {"symbol": SYMBOL, "filter": "next_10"}, key, timeout=30),
    )
    if exp_err:
        sys.exit(f"  {R}✗ failed in {exp_t:.1f}s — {exp_err}{N}")
    # Try common keys for the expirations list
    expirations = (
        exp_result.get("expirations")
        or exp_result.get("dates")
        or exp_result.get("expiration_dates")
        or []
    )
    if not expirations and isinstance(exp_result, list):
        expirations = exp_result
    expirations = [str(e) for e in expirations[:3]]
    print(f"  {G}✓ {exp_t:.1f}s — front 3: {expirations}{N}")
    print(f"  {D}response shape: {_shape(exp_result)}{N}")
    print()

    if len(expirations) < 1:
        sys.exit(f"{R}no expirations returned — can't continue{N}")

    # ─── Step 2: baseline analyze_greek_exposures(num_expirations=3) ──
    print(f"{B}[2/3] analyze_greek_exposures({SYMBOL}, num_expirations=3) — BASELINE{N}")
    baseline_result, baseline_t, baseline_err = _time_call(
        "baseline",
        lambda: atlas_call(
            "analyze_greek_exposures",
            {"symbol": SYMBOL, "num_expirations": 3},
            key,
            timeout=120,  # generous so we capture actual server time
        ),
    )
    if baseline_err:
        print(f"  {R}✗ {baseline_t:.1f}s — {baseline_err}{N}")
        baseline_keys = None
    else:
        print(f"  {G}✓ {baseline_t:.1f}s{N}")
        print(f"  {D}top-level keys: {list(baseline_result.keys())}{N}")
        baseline_keys = set(baseline_result.keys())
        # Inspect exposures_by_date
        ebd = baseline_result.get("exposures_by_date", {})
        if ebd:
            first_key = next(iter(ebd))
            first_entry = ebd[first_key]
            print(f"  {D}exposures_by_date[{first_key}] keys: "
                  f"{list(first_entry.keys()) if isinstance(first_entry, dict) else type(first_entry).__name__}{N}")
    print()

    # ─── Step 3: chunked — 3× greek_exposure_single_expiration in parallel ───
    print(f"{B}[3/3] Greek-Exposure-Single-Expiration × {len(expirations)} parallel — CHUNKED{N}")
    chunked_start = time.time()
    chunked_results = {}
    chunked_errors = {}

    def _one(exp):
        return atlas_call(
            "Greek-Exposure-Single-Expiration",
            {"symbol": SYMBOL, "expiration": exp},
            key,
            timeout=60,
        )

    with ThreadPoolExecutor(max_workers=len(expirations)) as ex:
        futures = {ex.submit(_one, exp): exp for exp in expirations}
        for fut in as_completed(futures):
            exp = futures[fut]
            try:
                t0 = time.time()  # individual timing isn't precise here; just measure wall-clock
                chunked_results[exp] = fut.result()
                print(f"  {G}✓ {exp} returned{N}")
            except Exception as e:
                chunked_errors[exp] = str(e)[:200]
                print(f"  {R}✗ {exp} — {chunked_errors[exp]}{N}")

    chunked_t = time.time() - chunked_start
    print(f"  {BOLD}wall-clock total: {chunked_t:.1f}s ({len(chunked_results)} ok, {len(chunked_errors)} failed){N}")

    if chunked_results:
        sample_exp = next(iter(chunked_results))
        sample = chunked_results[sample_exp]
        print(f"  {D}top-level keys of single-expiration response: "
              f"{list(sample.keys()) if isinstance(sample, dict) else type(sample).__name__}{N}")
    print()

    # ─── Summary ──────────────────────────────────────────────────
    print(f"{BOLD}SUMMARY{N}")
    print(f"  baseline (all 3 in one call): {baseline_t:.1f}s {'✓' if not baseline_err else '✗'}")
    print(f"  chunked  (3 parallel):        {chunked_t:.1f}s ({len(chunked_results)}/{len(expirations)} ok)")
    if baseline_t and chunked_t and not baseline_err and chunked_results:
        ratio = baseline_t / chunked_t
        verdict = f"{G}chunked is {ratio:.1f}× faster" if ratio > 1.2 else f"{Y}roughly equivalent (ratio {ratio:.2f})"
        print(f"  {verdict}{N}")
    elif baseline_err and chunked_results:
        print(f"  {G}baseline FAILED, chunked WORKED — chunking is mandatory for this symbol{N}")
    elif chunked_errors and not baseline_err:
        print(f"  {Y}chunked failed (some/all), baseline ok — keep single call for this symbol{N}")
    print()

    # Shape compatibility check
    if baseline_result and chunked_results:
        ebd = baseline_result.get("exposures_by_date", {})
        sample_exp = next(iter(chunked_results))
        if sample_exp in ebd:
            baseline_entry_keys = set(ebd[sample_exp].keys()) if isinstance(ebd[sample_exp], dict) else set()
            chunked_keys = set(chunked_results[sample_exp].keys()) if isinstance(chunked_results[sample_exp], dict) else set()
            common = baseline_entry_keys & chunked_keys
            only_in_baseline = baseline_entry_keys - chunked_keys
            only_in_chunked = chunked_keys - baseline_entry_keys
            print(f"  shape compat (exp {sample_exp}):")
            print(f"    common fields:        {sorted(common)}")
            if only_in_baseline:
                print(f"    only in baseline:     {sorted(only_in_baseline)}")
            if only_in_chunked:
                print(f"    only in chunked:      {sorted(only_in_chunked)}")
            print(f"    {G if not only_in_baseline else Y}{'compatible' if not only_in_baseline else 'partial — chunked may miss some fields'}{N}")
        else:
            print(f"  {Y}can't compare shapes: {sample_exp} not present in baseline's exposures_by_date{N}")


if __name__ == "__main__":
    main()

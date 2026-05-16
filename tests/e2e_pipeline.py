#!/usr/bin/env python3
"""End-to-end pipeline timing — what the v4.6 user actually experiences.

Simulates the full user flow:
  1. Atlas REST: Stock-Quote + Analyze-Greek-Exposures (parallel)  → bias data
  2. Gemini Flash: bias synthesis with structured-output schema
  3. Atlas REST: get_options_chain (single call)                   → chain data
  4. Local: filter to ±30% strike band, find ATM, compute EM/IV    → ready

Reports wall-clock latency for each stage and totals.

Usage:
    python3 tests/e2e_pipeline.py [SYMBOL] [EXPIRATION]
"""
import json
import sys
import threading
from utils import (
    load_config, require_keys, atlas_call, gemini_call,
    AtlasError, GeminiError, Stopwatch, pp, BIAS_SCHEMA, build_bias_prompt,
)

RST = "\033[0m"
GREEN = "\033[32m"; RED = "\033[31m"; DIM = "\033[2m"; BOLD = "\033[1m"; YELLOW = "\033[33m"


def parallel(*calls):
    """Run callables concurrently, return list of (result, error) tuples in order."""
    out = [None] * len(calls)
    def runner(i, fn):
        try: out[i] = (fn(), None)
        except Exception as e: out[i] = (None, e)
    ts = [threading.Thread(target=runner, args=(i, fn)) for i, fn in enumerate(calls)]
    for t in ts: t.start()
    for t in ts: t.join()
    return out


def filter_chain(chain_resp, target_exp, spot, band_pct=30):
    """Replicate v4.6's _flattenChain + strike-band filter in pure Python."""
    out = []
    target = str(target_exp) if target_exp else None
    def walk(node, side_hint=None):
        if node is None: return
        if isinstance(node, list):
            for n in node: walk(n, side_hint)
            return
        if not isinstance(node, dict): return
        exp = node.get("expiration") or node.get("expiry") or node.get("exp_date")
        if target and exp and str(exp) != target: return
        for c in node.get("calls", []) or []: out.append({**c, "side": "call"})
        for c in node.get("puts",  []) or []: out.append({**c, "side": "put"})
        for c in node.get("contracts", []) or []: out.append(c)
        if "strike" in node and ("side" in node or "type" in node or "option_type" in node):
            out.append(node)
        for k, v in node.items():
            if k in ("calls", "puts", "contracts"): continue
            if isinstance(v, (list, dict)): walk(v, side_hint)

    walk(chain_resp.get("chain") or chain_resp.get("contracts") or chain_resp)
    if not out:  # fallback: don't filter by expiration
        walk(chain_resp.get("chain") or chain_resp.get("contracts") or chain_resp)

    lo, hi = spot * (1 - band_pct/100), spot * (1 + band_pct/100)
    return [c for c in out if c.get("strike") is not None and lo <= c["strike"] <= hi]


def main():
    symbol = (sys.argv[1] if len(sys.argv) > 1 else "SPY").upper()
    expiration = sys.argv[2] if len(sys.argv) > 2 else None
    cfg = load_config()
    require_keys(cfg, "ATLAS_KEY", "GEMINI_API_KEY")
    atlas_key = cfg["ATLAS_KEY"]
    gemini_key = cfg["GEMINI_API_KEY"]
    model = cfg.get("GEMINI_MODEL", "gemini-2.5-flash")

    print(f"{BOLD}EdgeLane v4.6 end-to-end pipeline simulation{RST}")
    print(f"  symbol = {symbol}, model = {model}\n")

    total_t0 = __import__("time").perf_counter()

    # ============================================================================
    # STAGE 1: Atlas REST (parallel) — quote + greeks
    # ============================================================================
    print(f"{BOLD}[stage 1] Atlas REST: quote + greeks (parallel){RST}")
    with Stopwatch() as sw1:
        results = parallel(
            lambda: atlas_call("get_stock_quote", {"symbol": symbol}, atlas_key),
            lambda: atlas_call("analyze_greek_exposures", {"symbol": symbol, "num_expirations": 5}, atlas_key),
        )
    quote, q_err = results[0]
    greeks, g_err = results[1]
    if q_err: print(RED + f"  ✗ quote: {q_err}" + RST); sys.exit(1)
    if g_err: print(RED + f"  ✗ greeks: {g_err}" + RST); sys.exit(1)
    spot = quote.get("price")
    pp("  spot", spot)
    pp("  greeks keys", ", ".join(greeks.keys()) if isinstance(greeks, dict) else "?")
    pp("  stage 1 latency", f"{sw1.ms:.0f} ms", DIM)

    if not expiration:
        exps = greeks.get("expirations") if isinstance(greeks, dict) else None
        if exps:
            expiration = exps[0] if isinstance(exps[0], str) else exps[0].get("expiration")
    if not expiration:
        expiration = "2026-05-15"
    pp("  using expiration", expiration)

    # ============================================================================
    # STAGE 2: Gemini bias synthesis
    # ============================================================================
    print(f"\n{BOLD}[stage 2] Gemini {model}: bias synthesis{RST}")
    prompt = build_bias_prompt(symbol, expiration, spot, greeks)
    pp("  prompt size", f"{len(prompt):,} chars", DIM)
    with Stopwatch() as sw2:
        try:
            bias, meta = gemini_call(prompt, gemini_key, model=model, response_schema=BIAS_SCHEMA, temperature=0.1)
        except GeminiError as e:
            print(RED + f"  ✗ {e}" + RST); sys.exit(1)
    pp("  bias_label", bias.get("bias_label"), GREEN)
    pp("  directional_score", bias.get("directional_score"))
    pp("  gex_wall_strike", bias.get("gex_wall_strike"))
    pp("  rec strategies", bias.get("recommended_strategies"))
    pp("  stage 2 latency", f"{sw2.ms:.0f} ms", DIM)

    # ============================================================================
    # STAGE 3: Atlas REST — chain
    # ============================================================================
    print(f"\n{BOLD}[stage 3] Atlas REST: get_options_chain{RST}")
    with Stopwatch() as sw3:
        try:
            chain_resp = atlas_call("get_options_chain", {"symbol": symbol, "expiration": expiration}, atlas_key)
        except AtlasError as e:
            print(RED + f"  ✗ {e}" + RST); sys.exit(1)
    pp("  top-level keys", ", ".join(chain_resp.keys()) if isinstance(chain_resp, dict) else "?")
    pp("  stage 3 latency", f"{sw3.ms:.0f} ms", DIM)

    # ============================================================================
    # STAGE 4: client-side filter (matches v4.6 _flattenChain)
    # ============================================================================
    print(f"\n{BOLD}[stage 4] local: filter to ±30% strike band{RST}")
    with Stopwatch() as sw4:
        contracts = filter_chain(chain_resp, expiration, spot, band_pct=30)
    pp("  contracts after filter", len(contracts))
    if contracts:
        sample = contracts[0]
        pp("  sample contract", {k: sample.get(k) for k in ("strike", "side", "bid", "ask", "delta", "iv")})
    pp("  stage 4 latency", f"{sw4.ms:.0f} ms", DIM)

    # ============================================================================
    # SUMMARY
    # ============================================================================
    import time
    total_ms = (time.perf_counter() - total_t0) * 1000
    print(f"\n{BOLD}{'='*60}{RST}")
    print(f"{BOLD}TOTAL pipeline latency: {GREEN}{total_ms:.0f} ms{RST}")
    print(f"  stage 1 (atlas parallel): {sw1.ms:>7.0f} ms")
    print(f"  stage 2 (gemini bias):    {sw2.ms:>7.0f} ms")
    print(f"  stage 3 (atlas chain):    {sw3.ms:>7.0f} ms")
    print(f"  stage 4 (local filter):   {sw4.ms:>7.0f} ms")
    print(f"\n{DIM}Compare to v4.5 (LLM-routed): typically 8-15s for the same flow.{RST}")


if __name__ == "__main__":
    main()

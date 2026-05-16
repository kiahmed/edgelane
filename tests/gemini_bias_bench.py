#!/usr/bin/env python3
"""Benchmark Gemini bias synthesis — Flash vs Pro on real Atlas data.

Runs the EXACT prompt the v4.6 JSX uses through both models and prints:
  - latency for each
  - token usage from each
  - the bias_label / score / wall they each produced
  - a side-by-side diff of the structured fields

Useful for deciding whether Pro's slower/pricier reasoning is worth it on
real options data, or if Flash is already accurate enough.

Usage:
    python3 tests/gemini_bias_bench.py [SYMBOL] [EXPIRATION]
    python3 tests/gemini_bias_bench.py SPY 2026-05-15
"""
import json
import sys
from utils import (
    load_config, require_keys, atlas_call, gemini_call,
    AtlasError, GeminiError, Stopwatch, pp, BIAS_SCHEMA, build_bias_prompt,
)

RST = "\033[0m"
GREEN = "\033[32m"; RED = "\033[31m"; DIM = "\033[2m"; BOLD = "\033[1m"; YELLOW = "\033[33m"
MODELS = ["gemini-2.5-flash", "gemini-2.5-pro"]


def fetch_atlas(symbol, atlas_key):
    """Pull spot + greek exposures (parallel-ish; serial here for simplicity)."""
    with Stopwatch() as sw_q:
        quote = atlas_call("get_stock_quote", {"symbol": symbol}, atlas_key)
    with Stopwatch() as sw_g:
        greeks = atlas_call("analyze_greek_exposures", {"symbol": symbol, "num_expirations": 5}, atlas_key)
    return quote, greeks, sw_q.ms + sw_g.ms


def run_model(model, prompt, gemini_key):
    with Stopwatch() as sw:
        try:
            result, meta = gemini_call(prompt, gemini_key, model=model, response_schema=BIAS_SCHEMA, temperature=0.1)
            return {"ok": True, "result": result, "meta": meta, "latency_ms": sw.ms}
        except GeminiError as e:
            return {"ok": False, "error": str(e), "latency_ms": sw.ms}


def main():
    symbol = (sys.argv[1] if len(sys.argv) > 1 else "SPY").upper()
    expiration = sys.argv[2] if len(sys.argv) > 2 else None
    cfg = load_config()
    require_keys(cfg, "ATLAS_KEY", "GEMINI_API_KEY")
    atlas_key, gemini_key = cfg["ATLAS_KEY"], cfg["GEMINI_API_KEY"]

    print(f"{BOLD}Gemini bias synthesis benchmark — symbol={symbol}\n")

    # ---- 1. Pull Atlas data --------------------------------------------
    print(f"{BOLD}[1] Atlas data pull{RST}")
    try:
        quote, greeks, atlas_latency = fetch_atlas(symbol, atlas_key)
    except AtlasError as e:
        print(RED + f"  ✗ {e}" + RST); sys.exit(1)
    spot = quote.get("price")
    pp("  spot", spot)
    pp("  atlas latency (sum)", f"{atlas_latency:.0f} ms", DIM)

    # If no expiration provided, pick first one from greeks response
    if not expiration:
        exps = greeks.get("expirations") if isinstance(greeks, dict) else None
        if exps and isinstance(exps, list):
            expiration = exps[0] if isinstance(exps[0], str) else exps[0].get("expiration")
    if not expiration:
        expiration = "2026-05-15"
        print(YELLOW + f"  ⚠ no expiration in greeks response, using fallback {expiration}" + RST)
    pp("  using expiration", expiration)

    prompt = build_bias_prompt(symbol, expiration, spot, greeks)
    pp("  prompt size", f"{len(prompt):,} chars / ~{len(prompt)//4:,} tokens", DIM)

    # ---- 2. Run both models --------------------------------------------
    runs = {}
    for model in MODELS:
        print(f"\n{BOLD}[2] {model}\033[0m")
        run = run_model(model, prompt, gemini_key)
        runs[model] = run
        if not run["ok"]:
            print(RED + f"  ✗ {run['error']}" + RST); continue
        m = run["result"]
        pp("  latency", f"{run['latency_ms']:.0f} ms", DIM)
        usage = run["meta"].get("usage", {})
        pp("  usage", f"in={usage.get('promptTokenCount','?')} out={usage.get('candidatesTokenCount','?')} total={usage.get('totalTokenCount','?')}", DIM)
        pp("  bias_label", m.get("bias_label"), GREEN)
        pp("  directional_score", m.get("directional_score"))
        pp("  confidence", m.get("confidence"))
        pp("  gex_wall_strike", m.get("gex_wall_strike"))
        pp("  gex_wall_strength", m.get("gex_wall_strength"))
        pp("  rec strategies", m.get("recommended_strategies"))
        pp("  summary", m.get("summary", "")[:160] + ("..." if len(m.get("summary","")) > 160 else ""))

    # ---- 3. Side-by-side comparison ------------------------------------
    if all(runs[m]["ok"] for m in MODELS):
        flash_r, pro_r = runs["gemini-2.5-flash"]["result"], runs["gemini-2.5-pro"]["result"]
        print(f"\n{BOLD}[3] Side-by-side comparison\033[0m")
        keys = ["bias_label", "directional_score", "confidence", "gex_wall_strike", "gex_wall_strength"]
        print(f"  {'field':<22} {'flash':<22} {'pro':<22}  match?")
        print(f"  {'-'*22} {'-'*22} {'-'*22}  ------")
        agreed = 0
        for k in keys:
            f, p = flash_r.get(k), pro_r.get(k)
            same = "✓" if f == p else ("≈" if str(f).lower() == str(p).lower() else "✗")
            if same == "✓": agreed += 1
            color = GREEN if same == "✓" else (YELLOW if same == "≈" else RED)
            print(f"  {k:<22} {str(f):<22} {str(p):<22}  {color}{same}\033[0m")

        latency_diff = runs["gemini-2.5-pro"]["latency_ms"] - runs["gemini-2.5-flash"]["latency_ms"]
        print(f"\n  agreement: {agreed}/{len(keys)} structured fields")
        print(f"  pro is {latency_diff:+.0f} ms slower than flash ({runs['gemini-2.5-pro']['latency_ms']/runs['gemini-2.5-flash']['latency_ms']:.1f}x)")

    print(f"\n{BOLD}{'='*60}\033[0m")
    if any(not runs[m]["ok"] for m in MODELS):
        sys.exit(1)


if __name__ == "__main__":
    main()

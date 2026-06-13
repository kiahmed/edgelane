"""
Tradier vs Atlas validation probe — the migration gate.

Pulls the SAME options chain via both providers for the same symbol +
expiration in the same minute. Computes dealer GEX locally from Tradier's
chain. Diffs the local result against Atlas's analyze_greek_exposures output.

ACCEPTANCE CRITERIA for migration to Tradier on a given symbol:
    1. call_wall strike: local must match Atlas within 1 strike
    2. put_wall  strike: same tolerance
    3. portfolio_totals.net_gex magnitude: within ±15%

If all three pass on SPY, NVDA, and MU → Tradier hard-switch is safe.
If a symbol class fails → document the gap, keep Atlas as fallback for that
class only, retest after tuning conventions in data_providers/gex_local.py.

Usage:
    python tests/tradier_vs_atlas_probe.py [SYMBOL]    # default: cycles SPY/NVDA/MU

Cost: 1 expirations call + 1 chain call + 1 analyze_greek_exposures call per
symbol per provider = ~6 quota calls per symbol per run.

Requires:
    DATA_PROVIDER=tradier or atlas (irrelevant — probe hits both directly)
    ATLAS_KEY=...
    TRADIER_ACCESS_TOKEN=...
    TRADIER_BASE_URL=https://sandbox.tradier.com  (or production)
"""
import sys
import time
import urllib.request
import urllib.error
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent))
from utils import load_config, atlas_call, resolve_tradier_creds
from data_providers.gex_local import compute_dealer_exposures


SYMBOLS_DEFAULT = ["SPY", "NVDA", "MU"]

# ANSI
G, R, Y, B, D, N, BOLD = "\033[32m", "\033[31m", "\033[33m", "\033[34m", "\033[2m", "\033[0m", "\033[1m"


# ─── Tradier client (inline, single-file probe — not yet promoted to data_providers/tradier.py) ──

def tradier_get(path: str, params: dict, token: str, base: str, timeout: int = 30) -> dict:
    """GET https://<base>/v1/<path> with Bearer auth. Returns parsed JSON dict."""
    qs = "&".join(f"{k}={v}" for k, v in params.items())
    url = f"{base.rstrip('/')}/v1/{path}?{qs}"
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        },
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def tradier_normalize_chain(tradier_chain_resp: dict, expiration: str) -> list:
    """Convert Tradier's options/chains response into the normalized contract
    shape that compute_dealer_exposures expects."""
    out = []
    options = (tradier_chain_resp.get("options") or {}).get("option") or []
    for c in options:
        greeks = c.get("greeks") or {}
        side_raw = (c.get("option_type") or "").lower()
        side = "call" if "call" in side_raw else ("put" if "put" in side_raw else None)
        if not side:
            continue
        out.append({
            "strike": _num(c.get("strike")),
            "side": side,
            "expiration": expiration,
            "bid": _num(c.get("bid")),
            "ask": _num(c.get("ask")),
            "last": _num(c.get("last")),
            "mid": (_num(c.get("bid", 0)) + _num(c.get("ask", 0))) / 2
                   if c.get("bid") is not None and c.get("ask") is not None else None,
            "delta": _num(greeks.get("delta")),
            "gamma": _num(greeks.get("gamma")),
            "theta": _num(greeks.get("theta")),
            "vega":  _num(greeks.get("vega")),
            "iv":    _num(greeks.get("mid_iv")),     # decimal in Tradier
            "open_interest": _num(c.get("open_interest")) or 0,
            "volume":        _num(c.get("volume")) or 0,
        })
    return out


def _num(v):
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ─── Atlas wall extraction ────────────────────────────────────────────────

def atlas_extract_walls(atlas_resp: dict) -> tuple:
    """Pull (call_wall_strike, put_wall_strike, net_gex) from Atlas's
    analyze_greek_exposures response. Defensive about field naming."""
    kl = atlas_resp.get("key_levels") or {}
    cw = kl.get("call_wall") or {}
    pw = kl.get("put_wall") or {}
    pt = atlas_resp.get("portfolio_totals") or {}
    return (
        _num(cw.get("strike") or cw.get("price")),
        _num(pw.get("strike") or pw.get("price")),
        _num(pt.get("net_gex") or pt.get("gex") or pt.get("total")),
    )


# ─── Side-by-side probe ───────────────────────────────────────────────────

def probe_symbol(symbol: str, cfg: dict) -> dict:
    atlas_key = cfg["ATLAS_KEY"]
    # v4.7.31a: use the shared resolver — config moved from a single
    # TRADIER_ACCESS_TOKEN to DEVMODE-keyed TRADIER_SANDBOX_TOKEN /
    # TRADIER_PROD_TOKEN. Falls back to TRADIER_ACCESS_TOKEN for the old layout.
    tradier_token, tradier_base, env_label = resolve_tradier_creds(cfg)
    if not tradier_token:
        return {"error": f"No Tradier {env_label} token in config "
                         f"(DEVMODE={cfg.get('DEVMODE', 'true')}); "
                         f"set TRADIER_{env_label.upper()}_TOKEN"}

    print(f"\n{BOLD}─── {symbol} ───{N}")

    # 1. Get expiration list from Tradier (cheaper + more honest baseline)
    print(f"  {B}[1/4] Tradier: expirations({symbol}){N}")
    t0 = time.time()
    try:
        exp_resp = tradier_get(
            "markets/options/expirations",
            {"symbol": symbol, "includeAllRoots": "true"},
            tradier_token, tradier_base,
        )
    except Exception as e:
        return {"symbol": symbol, "error": f"Tradier expirations: {e}"}
    exp_list = (exp_resp.get("expirations") or {}).get("date") or []
    if isinstance(exp_list, str):
        exp_list = [exp_list]
    if not exp_list:
        return {"symbol": symbol, "error": "Tradier returned no expirations"}
    front_exp = exp_list[0]
    print(f"      {G}✓ {(time.time() - t0):.1f}s — front expiration: {front_exp} (of {len(exp_list)}){N}")

    # 2. Tradier chain with greeks
    print(f"  {B}[2/4] Tradier: chain({symbol}, {front_exp}, greeks=true){N}")
    t0 = time.time()
    try:
        chain_resp = tradier_get(
            "markets/options/chains",
            {"symbol": symbol, "expiration": front_exp, "greeks": "true"},
            tradier_token, tradier_base,
        )
    except Exception as e:
        return {"symbol": symbol, "error": f"Tradier chain: {e}"}
    contracts = tradier_normalize_chain(chain_resp, front_exp)
    print(f"      {G}✓ {(time.time() - t0):.1f}s — {len(contracts)} contracts normalized{N}")
    if not contracts:
        return {"symbol": symbol, "error": "Tradier chain returned 0 normalized contracts"}

    # Pull spot from Tradier in same window (avoid using Atlas's stale spot)
    t0 = time.time()
    try:
        q_resp = tradier_get("markets/quotes", {"symbols": symbol}, tradier_token, tradier_base)
    except Exception as e:
        return {"symbol": symbol, "error": f"Tradier quote: {e}"}
    quote = (q_resp.get("quotes") or {}).get("quote") or {}
    spot = _num(quote.get("last") or quote.get("close"))
    if not spot:
        return {"symbol": symbol, "error": "Tradier quote had no last/close"}
    print(f"  {B}[2.5/4] Tradier spot: ${spot:.2f}  {D}({(time.time() - t0):.1f}s){N}")

    # 3. Compute local GEX from Tradier chain
    print(f"  {B}[3/4] Local: compute_dealer_exposures({len(contracts)} contracts, spot={spot}){N}")
    t0 = time.time()
    local_gex = compute_dealer_exposures(contracts, spot)
    local_cw = (local_gex.get("key_levels") or {}).get("call_wall") or {}
    local_pw = (local_gex.get("key_levels") or {}).get("put_wall") or {}
    local_net_gex = (local_gex.get("portfolio_totals") or {}).get("net_gex") or 0.0
    print(f"      {G}✓ {(time.time() - t0)*1000:.0f}ms — call_wall={local_cw.get('strike')} "
          f"put_wall={local_pw.get('strike')} net_gex={local_net_gex:+.2e}{N}")

    # 4. Atlas analyze_greek_exposures for the SAME chain (for diff)
    print(f"  {B}[4/4] Atlas: analyze_greek_exposures({symbol}, num_expirations=1){N}")
    t0 = time.time()
    try:
        atlas_resp = atlas_call(
            "Analyze-Greek-Exposures",
            {"symbol": symbol, "num_expirations": 1},
            atlas_key, timeout=90,
        )
    except Exception as e:
        return {
            "symbol": symbol,
            "tradier_ok": True,
            "atlas_err": str(e)[:200],
            "local_cw": local_cw.get("strike"), "local_pw": local_pw.get("strike"),
            "local_net_gex": local_net_gex,
        }
    atlas_cw, atlas_pw, atlas_net_gex = atlas_extract_walls(atlas_resp)
    print(f"      {G}✓ {(time.time() - t0):.1f}s — call_wall={atlas_cw} "
          f"put_wall={atlas_pw} net_gex={atlas_net_gex:+.2e}{N}" if atlas_net_gex
          else f"      {Y}✓ {(time.time() - t0):.1f}s — Atlas response shape unfamiliar, see raw output{N}")

    # 5. Compare
    cw_diff = abs((local_cw.get("strike") or 0) - (atlas_cw or 0))
    pw_diff = abs((local_pw.get("strike") or 0) - (atlas_pw or 0))
    if atlas_net_gex and local_net_gex:
        net_gex_ratio = abs(local_net_gex - atlas_net_gex) / abs(atlas_net_gex)
    else:
        net_gex_ratio = None

    return {
        "symbol": symbol,
        "spot": spot,
        "expiration": front_exp,
        "contracts_count": len(contracts),
        "local_call_wall": local_cw.get("strike"),
        "atlas_call_wall": atlas_cw,
        "call_wall_diff_strikes": cw_diff,
        "local_put_wall": local_pw.get("strike"),
        "atlas_put_wall": atlas_pw,
        "put_wall_diff_strikes": pw_diff,
        "local_net_gex": local_net_gex,
        "atlas_net_gex": atlas_net_gex,
        "net_gex_diff_pct": net_gex_ratio * 100 if net_gex_ratio is not None else None,
    }


def grade(result: dict) -> tuple:
    """Return (passed: bool, reasons: list[str])."""
    if "error" in result:
        return False, [result["error"]]
    if "atlas_err" in result:
        return False, [f"atlas-side failure: {result['atlas_err']}"]
    reasons = []
    cw_ok = result["call_wall_diff_strikes"] <= 1
    pw_ok = result["put_wall_diff_strikes"] <= 1
    ng_ok = result["net_gex_diff_pct"] is not None and result["net_gex_diff_pct"] <= 15
    if not cw_ok:
        reasons.append(f"call_wall off by {result['call_wall_diff_strikes']} strikes (Atlas {result['atlas_call_wall']} vs local {result['local_call_wall']})")
    if not pw_ok:
        reasons.append(f"put_wall off by {result['put_wall_diff_strikes']} strikes (Atlas {result['atlas_put_wall']} vs local {result['local_put_wall']})")
    if not ng_ok:
        ng_pct = result["net_gex_diff_pct"]
        reasons.append(f"net_gex off by {ng_pct:.1f}% (Atlas {result['atlas_net_gex']:+.2e} vs local {result['local_net_gex']:+.2e})")
    return (cw_ok and pw_ok and ng_ok), reasons


def main():
    symbols = sys.argv[1:] if len(sys.argv) > 1 else SYMBOLS_DEFAULT
    cfg = load_config()
    _, base_for_banner, env_label = resolve_tradier_creds(cfg)
    print(f"{BOLD}Tradier vs Atlas validation probe{N}")
    print(f"{D}symbols: {', '.join(symbols)}{N}")
    print(f"{D}tradier env: {env_label} ({base_for_banner}){N}")

    results = []
    for sym in symbols:
        results.append(probe_symbol(sym, cfg))

    # Summary table
    print(f"\n{BOLD}═══ SUMMARY ═══{N}")
    header = f"  {'symbol':<8} {'spot':>8} {'cw Δ':>6} {'pw Δ':>6} {'net_gex Δ':>10}   verdict"
    print(header)
    print("  " + "─" * (len(header) - 2))
    all_passed = True
    for r in results:
        passed, reasons = grade(r)
        if not passed:
            all_passed = False
        sym = r.get("symbol", "?")
        if "error" in r:
            print(f"  {sym:<8}  {R}{'-':>8} {'-':>6} {'-':>6} {'-':>10}   FAIL — {r['error'][:50]}{N}")
            continue
        spot = r.get("spot", 0)
        cw = r.get("call_wall_diff_strikes", "-")
        pw = r.get("put_wall_diff_strikes", "-")
        ng = f"{r.get('net_gex_diff_pct', 0):.1f}%" if r.get('net_gex_diff_pct') is not None else "-"
        verdict = f"{G}PASS" if passed else f"{R}FAIL"
        print(f"  {sym:<8} ${spot:>7.2f} {cw:>6} {pw:>6} {ng:>10}   {verdict}{N}")
        for reason in reasons:
            print(f"            {D}- {reason}{N}")

    print()
    if all_passed:
        print(f"{G}{BOLD}✓ All symbols passed. Tradier migration cleared on this symbol set.{N}")
        sys.exit(0)
    else:
        print(f"{R}{BOLD}✗ One or more symbols failed. See reasons above; tune data_providers/gex_local.py sign conventions or wall-selection rules, then re-run.{N}")
        sys.exit(1)


if __name__ == "__main__":
    main()

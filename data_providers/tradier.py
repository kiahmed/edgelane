"""
Tradier REST client.

Wraps the Tradier brokerage API's market-data endpoints:
    GET /v1/markets/quotes
    GET /v1/markets/options/chains
    GET /v1/markets/options/expirations
    GET /v1/markets/options/strikes
    GET /v1/user/profile

Auth: Bearer token (sandbox or production). Base URL is configurable via
TRADIER_BASE_URL — defaults to sandbox so dev work doesn't accidentally hit
production.

Rate limits: production 120/min markets, sandbox 60/min. The response headers
X-Ratelimit-{Allowed, Used, Available, Expiry} are surfaced via last_rate_limit().

Docs: https://docs.tradier.com/reference
"""
import json
import time
import urllib.request
import urllib.error
import urllib.parse
from typing import Optional


SANDBOX_BASE = "https://sandbox.tradier.com"
PRODUCTION_BASE = "https://api.tradier.com"


class TradierError(RuntimeError):
    pass


# ─── Module-level rate-limit cache (last response's headers) ────────────────
_last_rate_limit: dict = {}


def last_rate_limit() -> dict:
    """Return the last seen X-Ratelimit-* headers as a dict.
    Empty until a request has been made."""
    return dict(_last_rate_limit)


# ─── Core REST helper ──────────────────────────────────────────────────────

def _request(path: str, params: dict, token: str, base: str,
             timeout: int = 30, max_retries: int = 2) -> dict:
    """GET {base}/v1/{path}?{params}. Bearer auth. Returns parsed JSON.
    Updates _last_rate_limit from response headers.

    Retries on 429 / 5xx / network errors with simple linear backoff.
    """
    qs = urllib.parse.urlencode(params) if params else ""
    url = f"{base.rstrip('/')}/v1/{path.lstrip('/')}"
    if qs:
        url = f"{url}?{qs}"

    last_err = None
    for attempt in range(max_retries + 1):
        req = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
            },
            method="GET",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                # Cache rate-limit headers regardless of status
                for h in ("X-Ratelimit-Allowed", "X-Ratelimit-Used",
                          "X-Ratelimit-Available", "X-Ratelimit-Expiry"):
                    v = resp.headers.get(h)
                    if v is not None:
                        _last_rate_limit[h] = v
                body = resp.read().decode("utf-8")
                return json.loads(body) if body else {}
        except urllib.error.HTTPError as e:
            body_text = ""
            try:
                body_text = e.read().decode("utf-8", "replace")[:300]
            except Exception:
                pass
            last_err = TradierError(f"Tradier {path}: HTTP {e.code} — {body_text}")
            if e.code in (429, 500, 502, 503, 504) and attempt < max_retries:
                time.sleep(1.0 * (attempt + 1))
                continue
            raise last_err
        except urllib.error.URLError as e:
            last_err = TradierError(f"Tradier {path}: network — {e.reason}")
            if attempt < max_retries:
                time.sleep(1.0 * (attempt + 1))
                continue
            raise last_err
        except TimeoutError as e:
            last_err = TradierError(f"Tradier {path}: read timed out after {timeout}s")
            if attempt < max_retries:
                time.sleep(1.0 * (attempt + 1))
                continue
            raise last_err
    raise last_err if last_err else TradierError(f"Tradier {path}: unknown failure")


# ─── Endpoint methods ──────────────────────────────────────────────────────

def user_profile(token: str, base: str = SANDBOX_BASE) -> dict:
    """GET /v1/user/profile — cheap call to verify the token works."""
    return _request("user/profile", {}, token, base)


def quotes(symbols, token: str, base: str = SANDBOX_BASE,
           greeks: bool = False) -> dict:
    """GET /v1/markets/quotes. symbols can be a string ('SPY') or list."""
    if isinstance(symbols, (list, tuple)):
        symbols = ",".join(symbols)
    params = {"symbols": symbols, "greeks": "true" if greeks else "false"}
    return _request("markets/quotes", params, token, base)


def stock_quote(symbol: str, token: str, base: str = SANDBOX_BASE) -> dict:
    """Convenience wrapper that pulls a single equity quote.
    Returns the inner quote object {symbol, last, bid, ask, ...} or None."""
    resp = quotes(symbol, token, base, greeks=False)
    q = (resp.get("quotes") or {}).get("quote")
    if isinstance(q, list):
        q = q[0] if q else None
    return q


def option_expirations(symbol: str, token: str, base: str = SANDBOX_BASE,
                       include_all_roots: bool = True,
                       strikes: bool = False) -> list:
    """GET /v1/markets/options/expirations. Returns the list of YYYY-MM-DD
    date strings. If strikes=True, returns list of {date, contract_size,
    expiration_type, strikes: [...]} dicts."""
    params = {
        "symbol": symbol,
        "includeAllRoots": "true" if include_all_roots else "false",
    }
    if strikes:
        params["strikes"] = "true"
    resp = _request("markets/options/expirations", params, token, base)
    exp = resp.get("expirations") or {}
    if strikes:
        items = exp.get("expiration") or []
        return items if isinstance(items, list) else [items]
    dates = exp.get("date") or []
    if isinstance(dates, str):
        dates = [dates]
    return dates


def options_chain(symbol: str, expiration: str, token: str,
                  base: str = SANDBOX_BASE, greeks: bool = True) -> list:
    """GET /v1/markets/options/chains. Returns raw contracts list (not yet
    normalized to EdgeLane's shape — use normalize_chain() for that).

    `expiration` is YYYY-MM-DD."""
    params = {
        "symbol": symbol,
        "expiration": expiration,
        "greeks": "true" if greeks else "false",
    }
    resp = _request("markets/options/chains", params, token, base)
    options = (resp.get("options") or {}).get("option") or []
    return options if isinstance(options, list) else [options]


# ─── Chain normalization → EdgeLane shape ───────────────────────────────────

def normalize_chain(tradier_options: list, expiration: str) -> list:
    """Convert Tradier's per-contract format into EdgeLane's normalized shape:

        {strike, side, expiration, bid, ask, last, mid, delta, gamma, theta,
         vega, iv, open_interest, volume}

    - side is 'call' or 'put'
    - mid is computed as (bid + ask) / 2 when both present, else None
    - iv is decimal (Tradier returns decimal already; Atlas returns percent)
    - open_interest and volume default to 0 (not None) — matches existing
      EdgeLane convention from _normalizeContract in the JSX
    """
    out = []
    for c in tradier_options or []:
        side_raw = (c.get("option_type") or "").lower()
        side = "call" if "call" in side_raw else ("put" if "put" in side_raw else None)
        if not side:
            continue

        greeks = c.get("greeks") or {}
        bid = _num(c.get("bid"))
        ask = _num(c.get("ask"))
        mid = ((bid + ask) / 2) if (bid is not None and ask is not None) else None

        out.append({
            "strike": _num(c.get("strike")),
            "side": side,
            "expiration": expiration,
            "bid": bid if bid is not None else 0.0,
            "ask": ask if ask is not None else 0.0,
            "last": _num(c.get("last")),
            "mid": mid if mid is not None else 0.0,
            "delta": _num(greeks.get("delta")),
            "gamma": _num(greeks.get("gamma")),
            "theta": _num(greeks.get("theta")),
            "vega":  _num(greeks.get("vega")),
            "iv":    _num(greeks.get("mid_iv")),
            "open_interest": _num(c.get("open_interest")) or 0,
            "volume":        _num(c.get("volume")) or 0,
        })
    return out


def _num(v):
    if v is None or v == "":
        return None
    try:
        f = float(v)
        return f if f == f else None
    except (TypeError, ValueError):
        return None

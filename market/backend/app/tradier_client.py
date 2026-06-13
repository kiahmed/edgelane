"""Async Tradier REST client for EdgeLane MARKET.

Mirrors the operator-level Tradier calls the existing JSX makes:
    - GET /v1/markets/quotes
    - GET /v1/markets/options/expirations
    - GET /v1/markets/options/chains
    - GET /v1/user/profile        (cheap auth probe used by /diag)

Aligned with data_providers/tradier.py (the battle-tested production sync
client) so the response shapes and error semantics are identical.

Hardening notes (2026-06-04):
  * 401 -> TradierAuthError (no retry — token is wrong, retrying just burns
    the rate-limit quota)
  * 429 -> TradierRateLimitError(retry_after_sec) — retry_after parsed from
    X-Ratelimit-Expiry when present, else the Retry-After header
  * 5xx -> single retry with 2s backoff, then raise as RuntimeError
  * 30s read timeout -> descriptive error (TradierTimeoutError)
  * options_chain() ALWAYS sends greeks=true — the math layer requires
    delta/gamma/theta/vega on every contract
  * Rate-limit headers captured into a module-level RateLimitState dict
    after every successful call; /status surfaces this for the operator
"""
from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx


# --- Exceptions ------------------------------------------------------------

class TradierError(RuntimeError):
    """Base class for all Tradier client errors."""


class TradierAuthError(TradierError):
    """401 — invalid or expired token. Do NOT retry."""


class TradierRateLimitError(TradierError):
    """429 — quota exhausted. retry_after_sec is best-effort from headers."""

    def __init__(self, message: str, retry_after_sec: float = 0.0):
        super().__init__(message)
        self.retry_after_sec = retry_after_sec


class TradierTimeoutError(TradierError):
    """Network timeout (read or connect)."""


# --- Module-level rate-limit state -----------------------------------------

_RATE_LIMIT_STATE: dict[str, Any] = {
    # Populated after every successful call. Keys mirror Tradier's headers:
    #   x-ratelimit-allowed, x-ratelimit-used, x-ratelimit-available,
    #   x-ratelimit-expiry. Plus 'observed_at' (epoch sec) for staleness.
}


def get_rate_limit_state() -> dict[str, Any]:
    """Snapshot of the last captured Tradier rate-limit headers.
    Returns {} if no successful call has happened yet."""
    return dict(_RATE_LIMIT_STATE)


def _record_rate_limit(headers) -> None:
    """Pull X-Ratelimit-* headers from a response and stash them."""
    captured = {}
    for k, v in headers.items():
        if k.lower().startswith("x-ratelimit"):
            captured[k.lower()] = v
    if captured:
        captured["observed_at"] = time.time()
        _RATE_LIMIT_STATE.clear()
        _RATE_LIMIT_STATE.update(captured)


def _retry_after_from_headers(headers) -> float:
    """Best-effort retry-after seconds from a 429 response."""
    ra = headers.get("Retry-After") or headers.get("retry-after")
    if ra:
        try:
            return max(0.0, float(ra))
        except (TypeError, ValueError):
            pass
    expiry = headers.get("X-Ratelimit-Expiry") or headers.get("x-ratelimit-expiry")
    if expiry:
        try:
            # Tradier sends epoch seconds. Convert to "seconds until then".
            return max(0.0, float(expiry) - time.time())
        except (TypeError, ValueError):
            pass
    return 0.0


# --- Client ----------------------------------------------------------------

class TradierClient:
    def __init__(self, base_url: str, token: str, timeout: float = 30.0):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=self.timeout,
                headers={"Accept": "application/json"},
            )
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    @property
    def rate_limit(self) -> dict[str, Any]:
        """Last seen Tradier rate-limit headers (snapshot of the module state)."""
        return get_rate_limit_state()

    async def _get(self, path: str, params: dict[str, Any] | None = None,
                   _retried: bool = False) -> dict:
        client = await self._get_client()
        url = f"{self.base_url}/v1/{path.lstrip('/')}"
        headers = {"Authorization": f"Bearer {self.token}"}
        try:
            resp = await client.get(url, params=params or {}, headers=headers)
        except httpx.TimeoutException as e:
            raise TradierTimeoutError(
                f"Tradier GET {path}: timed out after {self.timeout}s ({e!s})"
            ) from e
        except httpx.RequestError as e:
            # Connection refused / DNS / SSL — treat like a transient and
            # let one retry happen.
            if not _retried:
                await asyncio.sleep(2.0)
                return await self._get(path, params, _retried=True)
            raise TradierError(f"Tradier GET {path}: network — {e!s}") from e

        # Always try to capture rate-limit headers, even on error responses.
        _record_rate_limit(resp.headers)

        if resp.status_code == 401:
            raise TradierAuthError(
                f"Tradier GET {path}: invalid token (HTTP 401)"
            )
        if resp.status_code == 429:
            retry_after = _retry_after_from_headers(resp.headers)
            raise TradierRateLimitError(
                f"Tradier GET {path}: rate-limited (HTTP 429)",
                retry_after_sec=retry_after,
            )
        if 500 <= resp.status_code < 600:
            if not _retried:
                await asyncio.sleep(2.0)
                return await self._get(path, params, _retried=True)
            body = resp.text[:500]
            raise TradierError(
                f"Tradier GET {path}: HTTP {resp.status_code} (after retry) — {body}"
            )
        if resp.status_code >= 400:
            body = resp.text[:500]
            raise TradierError(
                f"Tradier GET {path}: HTTP {resp.status_code} — {body}"
            )
        return resp.json()

    # --- Public methods (match production tradier.py shape) ---

    async def user_profile(self) -> dict:
        """Cheap auth probe — used by /diag/tradier."""
        return await self._get("user/profile")

    async def resolve_account_id(self) -> str:
        """Hit /v1/user/profile and return the first usable account_number.

        Mirrors the logic in `tools/tradier_positions.py:resolve_account_id` —
        prefers margin/cash brokerage accounts over IRAs/joint when there are
        multiple. Returns empty string if no usable account is found (caller
        decides whether that's fatal).
        """
        d = await self.user_profile()
        profile = (d or {}).get("profile") or {}
        accs = profile.get("account")
        if isinstance(accs, dict):
            accs = [accs]
        accs = accs or []
        if not accs:
            return ""
        # Prefer first non-archived, non-closed account. Tradier API doesn't
        # always include a "status" field — assume usable when absent.
        for a in accs:
            status = (a.get("status") or "active").lower()
            if status in ("active", ""):
                num = a.get("account_number")
                if num:
                    return str(num)
        # Fallback: take the first one regardless.
        return str(accs[0].get("account_number") or "")

    async def stock_quote(self, symbol: str) -> dict:
        """Return the inner quote dict {symbol, last, bid, ask, ...}.
        For multi-symbol requests Tradier returns a list under quote;
        we unwrap to the first element for the single-symbol case."""
        d = await self._get("markets/quotes",
                            {"symbols": symbol, "greeks": "false"})
        q = (d.get("quotes") or {}).get("quote") or {}
        if isinstance(q, list):
            q = q[0] if q else {}
        return q

    async def option_expirations(self, symbol: str) -> list[str]:
        """List of YYYY-MM-DD expiration dates. Handles:
          * dict.expirations.date as str (single exp) or list
          * dict.expirations as None (no listed options)
        """
        d = await self._get(
            "markets/options/expirations",
            {"symbol": symbol, "includeAllRoots": "true"},
        )
        expirations = d.get("expirations") or {}
        # Tradier sometimes returns expirations: null when a symbol has no
        # listed options — _get returns {}, .get("expirations") -> None -> {}
        dates = expirations.get("date") or []
        if isinstance(dates, str):
            dates = [dates]
        return [str(x) for x in dates]

    async def options_chain(self, symbol: str, expiration: str,
                            greeks: bool = True) -> list[dict]:
        """Raw options list for one expiration. Default greeks=true — the
        math layer requires delta/gamma/theta/vega on every contract."""
        d = await self._get(
            "markets/options/chains",
            {
                "symbol": symbol,
                "expiration": expiration,
                "greeks": "true" if greeks else "false",
            },
        )
        options = (d.get("options") or {}).get("option") or []
        if isinstance(options, dict):
            options = [options]
        return options

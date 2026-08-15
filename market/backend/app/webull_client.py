"""Per-user Webull OpenAPI client — async wrapper over webull-openapi-python-sdk.

Webull auth is an app_key + app_secret + region (not a bearer token), used via
Webull's official *signed* SDK. Each EdgeLane user supplies their OWN Webull
credentials (stored encrypted in broker_configs); we never ship app keys.

The SDK is synchronous (requests-based); we run its calls in a threadpool so the
async FastAPI event loop isn't blocked. The SDK is imported lazily so the Tradier
path keeps working even if the package isn't installed — a missing SDK surfaces
as a clear WebullError only when a Webull order/test is actually attempted.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import tempfile
from typing import Any, Optional

log = logging.getLogger("edgelane.market.webull")

# Webull US UAT (paper/testing) endpoint. Production uses the SDK's built-in
# per-region default endpoint (no add_endpoint call needed).
_UAT_ENDPOINT = "us-openapi-alb.uat.webullbroker.com"

# Cap on TradeClient construction. The SDK performs a server token exchange on
# build; for an app NOT yet authorized for trading on the account, that sits in a
# `check_token` PENDING loop for up to ~300s. We bound it so a pending /
# unauthorized connection fails fast (with WEBULL_AUTH_HINT) instead of hanging
# a request for ~5 minutes.
_INIT_TIMEOUT_SEC = 20

# Cap on every individual SDK call (account list, preview, place). The SDK is
# synchronous and is driven from a worker thread, and its own socket handling
# offers no guarantee of returning — an unbounded call here means an order
# request that never completes, so the browser hangs AND uvicorn never logs the
# request (its access line is only written once a response exists). Bound it so
# a wedged SDK surfaces as a clean, reportable error instead.
#
# Note the thread itself cannot be killed: on timeout it keeps running detached,
# exactly as _ensure_trade already documents. We stop *waiting* on it.
_CALL_TIMEOUT_SEC = 25

# Short, user-facing hint for the most common Webull production gotcha.
WEBULL_AUTH_HINT = (
    "Webull production requires the account owner to authorize the app for "
    "trading before its token activates."
)


class WebullError(Exception):
    """Any Webull SDK / API failure, surfaced to the route as a clean message."""


class WebullClient:
    def __init__(self, app_key: str, app_secret: str, region: str = "us", env: str = "production"):
        self.app_key = app_key
        self.app_secret = app_secret
        self.region = (region or "us").lower()
        self.env = (env or "production").lower()
        self._trade = None

    def _build_trade_client(self):
        """Synchronous SDK construction (BLOCKS on the token exchange/poll)."""
        try:
            from webull.core.client import ApiClient
            from webull.trade.trade_client import TradeClient
        except Exception as e:  # pragma: no cover - import-time env issue
            raise WebullError(
                "Webull SDK not installed — `pip install webull-openapi-python-sdk` "
                f"(import error: {e})"
            )
        # Construction performs a server token exchange, so it can raise the SDK's
        # ServerException (bad creds, 429, etc.). Wrap → clean WebullError.
        try:
            api = ApiClient(self.app_key, self.app_secret, self.region)
            if self.env == "uat":
                api.add_endpoint(self.region, _UAT_ENDPOINT)
            # Isolate the SDK's on-disk token cache PER credential. The default is
            # a single shared `conf/token.txt`, which in a multi-user backend would
            # cross-contaminate trade tokens between users. Namespace by a hash of
            # the app_key (no PII on disk).
            if hasattr(api, "set_token_dir"):
                digest = hashlib.sha256(self.app_key.encode()).hexdigest()[:16]
                api.set_token_dir(os.path.join(tempfile.gettempdir(), "edgelane_webull", digest))
            return TradeClient(api)
        except WebullError:
            raise
        except Exception as e:
            raise WebullError(f"Webull client init failed: {e}")

    async def _ensure_trade(self):
        """Build (once) the TradeClient in a thread, bounded by _INIT_TIMEOUT_SEC.

        On timeout the underlying SDK poll thread keeps running detached, but the
        request returns immediately with a clear, actionable error.
        """
        if self._trade is not None:
            return self._trade
        try:
            self._trade = await asyncio.wait_for(
                asyncio.to_thread(self._build_trade_client), timeout=_INIT_TIMEOUT_SEC
            )
        except asyncio.TimeoutError:
            raise WebullError(
                f"Webull connection timed out after {_INIT_TIMEOUT_SEC}s — "
                f"token not active yet. {WEBULL_AUTH_HINT}"
            )
        return self._trade

    @staticmethod
    def _result(res) -> tuple[int, Any]:
        code = getattr(res, "status_code", 0)
        try:
            body = res.json()
        except Exception:
            body = getattr(res, "text", None)
        return code, body

    async def _call(self, fn, *args, **kwargs):
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(fn, *args, **kwargs), timeout=_CALL_TIMEOUT_SEC
            )
        except asyncio.TimeoutError:
            raise WebullError(
                f"Webull did not respond within {_CALL_TIMEOUT_SEC}s "
                f"({getattr(fn, '__name__', 'call')}). Nothing was placed — try again."
            )
        except WebullError:
            raise
        except Exception as e:
            raise WebullError(f"Webull SDK call failed: {e}")

    async def list_accounts(self) -> list[dict]:
        tc = await self._ensure_trade()
        res = await self._call(tc.account_v2.get_account_list)
        code, body = self._result(res)
        if code != 200:
            raise WebullError(f"get_account_list HTTP {code}: {str(body)[:200]}")
        return body or []

    async def first_account_id(self) -> Optional[str]:
        accts = await self.list_accounts()
        return accts[0].get("account_id") if accts else None

    async def preview_option(self, account_id: str, orders: list[dict]) -> dict:
        tc = await self._ensure_trade()
        res = await self._call(tc.order_v2.preview_option, account_id, orders)
        code, body = self._result(res)
        if code != 200:
            raise WebullError(f"preview_option HTTP {code}: {str(body)[:300]}")
        return body

    async def place_option(self, account_id: str, orders: list[dict]) -> dict:
        tc = await self._ensure_trade()
        res = await self._call(tc.order_v2.place_option, account_id, orders)
        code, body = self._result(res)
        if code != 200:
            raise WebullError(f"place_option HTTP {code}: {str(body)[:300]}")
        return body

    async def close(self):
        # The sync SDK holds no async resources; just drop the reference.
        self._trade = None

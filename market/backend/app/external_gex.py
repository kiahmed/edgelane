"""External GEX data received via webhook from the private GEX provider extension.

The extension POSTs the latest wall/profile data to /webhook/edgelane_provider_gex
as it updates. We keep the latest payload per symbol in memory; the poller checks
freshness before using.
"""
from __future__ import annotations

import time
from threading import Lock


class ExternalGexState:
    def __init__(self) -> None:
        self._lock = Lock()
        self._by_symbol: dict[str, dict] = {}   # symbol -> {payload, received_at, ...}
        self._history: list[dict] = []          # last 50 payloads for /webhook/debug

    def set(self, symbol: str, payload: dict) -> None:
        with self._lock:
            entry = {
                "symbol": symbol.upper(),
                "payload": payload,
                "received_at": time.time(),
                "received_at_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
            self._by_symbol[symbol.upper()] = entry
            self._history.append(entry)
            if len(self._history) > 50:
                self._history.pop(0)

    def get_if_fresh(self, symbol: str, max_age_sec: float) -> dict | None:
        with self._lock:
            entry = self._by_symbol.get(symbol.upper())
            if not entry:
                return None
            age = time.time() - entry["received_at"]
            if age > max_age_sec:
                return None
            return dict(entry)  # snapshot

    def latest_per_symbol(self) -> dict:
        with self._lock:
            return {k: dict(v) for k, v in self._by_symbol.items()}

    def recent(self, limit: int = 20) -> list[dict]:
        with self._lock:
            return list(self._history[-limit:])


state = ExternalGexState()

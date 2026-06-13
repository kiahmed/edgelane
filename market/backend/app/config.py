"""Config loader for EdgeLane MARKET backend.

Reads `edgelane_market.config` (KEY=VALUE shell-style format, same convention
as the existing edge_lane_config.config). Parses inline `#` comments outside
quoted values. Exposed as an immutable Pydantic settings object.
"""
from __future__ import annotations
import os
import re
from pathlib import Path
from typing import Any
from pydantic import BaseModel, Field


# Default config path — repo root, two levels up from this file
_DEFAULT_CONFIG = Path(__file__).resolve().parents[3] / "edgelane_market.config"


def _strip_inline_comment(value: str) -> str:
    """Strip inline `#` comments from a config value, respecting quoted strings."""
    out: list[str] = []
    in_quote: str | None = None
    for ch in value:
        if in_quote:
            if ch == in_quote:
                in_quote = None
            out.append(ch)
        else:
            if ch in ('"', "'"):
                in_quote = ch
                out.append(ch)
            elif ch == "#":
                break
            else:
                out.append(ch)
    return "".join(out).strip()


def _parse_config_file(path: Path) -> dict[str, str]:
    """Parse KEY=VALUE config file. Tolerant of comments and blank lines.
    Strips surrounding quotes from values."""
    if not path.is_file():
        raise FileNotFoundError(f"Config not found: {path}")
    out: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        k, _, v = line.partition("=")
        k = k.strip()
        v = _strip_inline_comment(v.strip())
        if (v.startswith('"') and v.endswith('"')) or (v.startswith("'") and v.endswith("'")):
            v = v[1:-1]
        out[k] = v
    return out


class Settings(BaseModel):
    """Immutable runtime settings derived from the config file."""

    model_config = {"frozen": True}

    # Tradier
    tradier_token: str = Field(default="")
    tradier_token_sandbox: str = Field(default="")
    tradier_env: str = Field(default="production")
    # Account IDs: parallel to tokens. Leave blank to let the backend auto-
    # resolve from /v1/user/profile on lifespan startup (recommended).
    tradier_account_id: str = Field(default="")             # production account
    tradier_account_id_sandbox: str = Field(default="")     # sandbox/paper account
    devmode: bool = Field(default=False)

    # Polling
    symbols: list[str] = Field(default_factory=lambda: ["SPX"])
    poll_interval_sec: int = Field(default=20)
    market_hours_tz: str = Field(default="America/New_York")
    market_open: str = Field(default="09:30")
    market_close: str = Field(default="16:00")

    # Strategy
    width_preferences: list[str] = Field(default_factory=lambda: ["balanced", "generous"])
    target_deltas: list[float] = Field(default_factory=lambda: [0.20, 0.30])
    composite_pick_top: int = Field(default=3)

    # Self-eval
    eval_window_min: int = Field(default=3)
    eval_rolling_window: int = Field(default=20)
    pill_green_pct: float = Field(default=60.0)
    pill_red_pct: float = Field(default=40.0)
    neutral_band_pct: float = Field(default=0.05)
    regime_alert_consec_losses: int = Field(default=3)
    regime_clear_consec_wins: int = Field(default=2)

    # Storage
    db_path: str = Field(default="~/.edgelane/market.duckdb")

    # HTTP
    http_host: str = Field(default="127.0.0.1")
    http_port: int = Field(default=8788)
    cors_allow_origins: list[str] = Field(default_factory=lambda: [
        "http://localhost:5173",
        "http://127.0.0.1:5500",
        "null",
        # The private GEX provider's page origin (where the extension direct-POSTs
        # from) is added via the local config file, not hardcoded here.
    ])

    # WebSocket
    ws_heartbeat_sec: int = Field(default=30)

    # Dev override: keep polling even when market is closed (mock-mode dev)
    force_poll_when_closed: bool = Field(default=False)

    # When the market is closed, still poll on a slow cadence to keep the UI
    # rendering off the last-available chain + private GEX provider overlay — but WITHOUT
    # persisting bias_decisions or running the self-eval (frozen prices would
    # only manufacture meaningless "neutral" outcomes). Display-only.
    display_when_closed: bool = Field(default=True)
    closed_display_interval_sec: int = Field(default=60)

    # External GEX override (private GEX provider extension webhook)
    use_external_gex: bool = Field(default=True)         # prefer extension data over Tradier-OI walls when fresh
    external_gex_timeout_sec: int = Field(default=30)    # how stale before falling back
    extension_policy_version: int = Field(default=1)     # bump to force extension hot-reload
    # The provider page the extension attaches to. Real value set in the local
    # config file; left blank in the public default.
    provider_target_url: str = Field(default="")

    # --- Derived helpers ---
    @property
    def symbols_list(self) -> list[str]:
        return list(self.symbols)

    @property
    def width_preferences_list(self) -> list[str]:
        return list(self.width_preferences)

    @property
    def active_tradier_token(self) -> str:
        if self.devmode or self.tradier_env == "sandbox":
            return self.tradier_token_sandbox or self.tradier_token
        return self.tradier_token

    @property
    def active_account_id(self) -> str:
        """Return the account_id matching the active env. Empty string when
        not configured — main.py lifespan will then auto-resolve via
        /v1/user/profile and cache the result on app.state.tradier_account_id."""
        if self.devmode or self.tradier_env == "sandbox":
            return self.tradier_account_id_sandbox or self.tradier_account_id
        return self.tradier_account_id

    @property
    def tradier_base_url(self) -> str:
        if self.devmode or self.tradier_env == "sandbox":
            return "https://sandbox.tradier.com"
        return "https://api.tradier.com"

    @property
    def db_path_expanded(self) -> Path:
        """Resolve DB_PATH and inject an env suffix so sandbox/prod/mock
        data never co-mingle.

        Example: DB_PATH=~/.edgelane/market.duckdb yields
          - market-prod.duckdb     (live production)
          - market-sandbox.duckdb  (DEVMODE=true or TRADIER_ENV=sandbox)
          - market-mock.duckdb     (no token)
          - market-live_forced.duckdb (FORCE_POLL_WHEN_CLOSED override)

        Flipping DEVMODE in the config automatically routes to a fresh DB,
        so the rolling accuracy pill measures only outcomes from the env
        you're actually polling. No manual pre-seeding required.
        """
        base = Path(os.path.expanduser(self.db_path))
        env_tag = {
            'mock':         'mock',
            'live':         'prod',
            'live_dev':     'sandbox',
            'live_forced':  'prod',  # forced off-hours on prod is still prod data
        }.get(self.tradier_mode, 'unknown')
        if base.suffix:
            return base.with_name(f"{base.stem}-{env_tag}{base.suffix}")
        return base.with_name(f"{base.name}-{env_tag}")

    @property
    def tradier_mode(self) -> str:
        if not self.active_tradier_token:
            return "mock"
        if self.force_poll_when_closed:
            return "live_forced"
        if self.devmode:
            return "live_dev"
        return "live"

    @property
    def should_poll_when_market_closed(self) -> bool:
        if self.force_poll_when_closed:
            return True
        if self.devmode:
            return True
        if not self.active_tradier_token:
            return True
        return False


def _coerce(raw: dict[str, str]) -> dict[str, Any]:
    out: dict[str, Any] = {}

    if "TRADIER_TOKEN" in raw:               out["tradier_token"] = raw["TRADIER_TOKEN"]
    if "TRADIER_TOKEN_SANDBOX" in raw:       out["tradier_token_sandbox"] = raw["TRADIER_TOKEN_SANDBOX"]
    if "TRADIER_ENV" in raw:                 out["tradier_env"] = raw["TRADIER_ENV"].lower().strip()
    if "TRADIER_ACCOUNT_ID" in raw:          out["tradier_account_id"] = raw["TRADIER_ACCOUNT_ID"].strip()
    if "TRADIER_ACCOUNT_ID_SANDBOX" in raw:  out["tradier_account_id_sandbox"] = raw["TRADIER_ACCOUNT_ID_SANDBOX"].strip()
    if "DEVMODE" in raw:                     out["devmode"] = raw["DEVMODE"].strip().lower() in ("true", "1", "yes", "on")

    if "SYMBOLS" in raw:
        out["symbols"] = [s.strip().upper() for s in raw["SYMBOLS"].split(",") if s.strip()]
    if "POLL_INTERVAL_SEC" in raw:           out["poll_interval_sec"] = int(raw["POLL_INTERVAL_SEC"])
    if "MARKET_HOURS_TZ" in raw:             out["market_hours_tz"] = raw["MARKET_HOURS_TZ"]
    if "MARKET_OPEN" in raw:                 out["market_open"] = raw["MARKET_OPEN"]
    if "MARKET_CLOSE" in raw:                out["market_close"] = raw["MARKET_CLOSE"]

    if "WIDTH_PREFERENCES" in raw:
        out["width_preferences"] = [s.strip().lower() for s in raw["WIDTH_PREFERENCES"].split(",") if s.strip()]
    if "TARGET_DELTAS" in raw:
        out["target_deltas"] = [float(s.strip()) for s in raw["TARGET_DELTAS"].split(",") if s.strip()]
    if "COMPOSITE_PICK_TOP" in raw:          out["composite_pick_top"] = int(raw["COMPOSITE_PICK_TOP"])

    if "EVAL_WINDOW_MIN" in raw:             out["eval_window_min"] = int(raw["EVAL_WINDOW_MIN"])
    if "EVAL_ROLLING_WINDOW" in raw:         out["eval_rolling_window"] = int(raw["EVAL_ROLLING_WINDOW"])
    if "PILL_GREEN_PCT" in raw:              out["pill_green_pct"] = float(raw["PILL_GREEN_PCT"])
    if "PILL_RED_PCT" in raw:                out["pill_red_pct"] = float(raw["PILL_RED_PCT"])
    if "NEUTRAL_BAND_PCT" in raw:            out["neutral_band_pct"] = float(raw["NEUTRAL_BAND_PCT"])
    if "REGIME_ALERT_CONSEC_LOSSES" in raw:
        out["regime_alert_consec_losses"] = int(raw["REGIME_ALERT_CONSEC_LOSSES"])
    if "REGIME_CLEAR_CONSEC_WINS" in raw:
        out["regime_clear_consec_wins"] = int(raw["REGIME_CLEAR_CONSEC_WINS"])

    if "DB_PATH" in raw:                     out["db_path"] = raw["DB_PATH"]
    if "HTTP_HOST" in raw:                   out["http_host"] = raw["HTTP_HOST"]
    if "HTTP_PORT" in raw:                   out["http_port"] = int(raw["HTTP_PORT"])
    if "CORS_ALLOW_ORIGINS" in raw:
        out["cors_allow_origins"] = [s.strip() for s in raw["CORS_ALLOW_ORIGINS"].split(",") if s.strip()]
    if "WS_HEARTBEAT_SEC" in raw:            out["ws_heartbeat_sec"] = int(raw["WS_HEARTBEAT_SEC"])

    if "FORCE_POLL_WHEN_CLOSED" in raw:
        out["force_poll_when_closed"] = raw["FORCE_POLL_WHEN_CLOSED"].strip().lower() in ("true", "1", "yes", "on")
    if "DISPLAY_WHEN_CLOSED" in raw:
        out["display_when_closed"] = raw["DISPLAY_WHEN_CLOSED"].strip().lower() in ("true", "1", "yes", "on")
    if "CLOSED_DISPLAY_INTERVAL_SEC" in raw:
        out["closed_display_interval_sec"] = int(raw["CLOSED_DISPLAY_INTERVAL_SEC"])

    if "USE_EXTERNAL_GEX" in raw:
        out["use_external_gex"] = raw["USE_EXTERNAL_GEX"].strip().lower() in ("true", "1", "yes", "on")
    if "EXTERNAL_GEX_TIMEOUT_SEC" in raw:
        out["external_gex_timeout_sec"] = int(raw["EXTERNAL_GEX_TIMEOUT_SEC"])
    if "EXTENSION_POLICY_VERSION" in raw:
        out["extension_policy_version"] = int(raw["EXTENSION_POLICY_VERSION"])

    return out


def load_settings(path: Path | str | None = None) -> Settings:
    if path is None:
        env_path = os.environ.get("EDGELANE_MARKET_CONFIG")
        path = Path(env_path) if env_path else _DEFAULT_CONFIG
    p = Path(path)
    if not p.is_file():
        return Settings()
    raw = _parse_config_file(p)
    return Settings(**_coerce(raw))


_cached: Settings | None = None


def get_settings() -> Settings:
    global _cached
    if _cached is None:
        _cached = load_settings()
    return _cached

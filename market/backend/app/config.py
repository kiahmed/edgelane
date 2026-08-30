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


# Default config path — repo root (app/ -> backend/ -> market/ -> repo).
# In the container the source lives at /srv/app, which is shallower than the
# repo layout, so parents[3] doesn't exist; the container sets
# EDGELANE_MARKET_CONFIG (read in load_settings) and this default is unused.
# Guard the index so import never crashes regardless of where the file sits.
_PARENTS = Path(__file__).resolve().parents
_DEFAULT_CONFIG = (_PARENTS[3] if len(_PARENTS) > 3 else _PARENTS[-1]) / "edgelane_market.config"


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
    # Minimum graded outcomes before the bias-trust state leaves "calibrating"
    # and a win-rate is published (see docs/spread_outcome_eval.md).
    eval_min_graded: int = Field(default=10)
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
    # Regex fallback so the deployed Vercel frontend works across renamed projects
    # and rotating preview URLs (edgelane-hazel, edgelane-matrix, *-git-* previews)
    # without re-listing each one. Matched in addition to cors_allow_origins.
    # The facades.trade alternation covers the parent portal's product
    # subdomains, which serve the same Vercel deployments under a custom domain.
    # Torque is absent on purpose: it has no public subdomain yet — add it here
    # at the same time as its DNS record.
    # Override/disable via CORS_ALLOW_ORIGIN_REGEX in the config (blank = off).
    cors_allow_origin_regex: str | None = Field(
        default=r"^https://(edgelane[a-z0-9-]*\.vercel\.app|(matrix|simmer)\.facades\.trade)$")

    # WebSocket
    ws_heartbeat_sec: int = Field(default=30)

    # --- Auth (Supabase) + abuse protection ---
    # Master switch. When False (default — local dev / parity tests), all auth
    # dependencies pass through anonymously and rate limiting is off, so the
    # existing local flow is unchanged. Set AUTH_ENABLED=true in production.
    auth_enabled: bool = Field(default=False)
    # Supabase project (frontend handles signup/login; backend only verifies the
    # JWT the frontend attaches). service_key is server-only — bypasses RLS.
    supabase_url: str = Field(default="")
    supabase_jwt_secret: str = Field(default="")     # legacy HS256 projects only
    supabase_service_key: str = Field(default="")    # server-only; never shipped
    # Cloudflare Turnstile — verifies a real browser before minting an anon
    # teaser session token. Empty + auth_enabled=False → teaser open in dev.
    turnstile_secret: str = Field(default="")
    # Signs the short-lived anonymous teaser session tokens (spot/bias/walls).
    anon_session_secret: str = Field(default="")
    anon_session_ttl_sec: int = Field(default=1800)  # 30 min
    # Server-side secret for curl/testing — bypasses Turnstile + JWT. NEVER
    # shipped to a browser (kept in the gitignored config only).
    admin_api_token: str = Field(default="")
    # Per-session (authed user / anon session / IP) request budget.
    rate_limit_per_min: int = Field(default=120)
    # Tighter bound on /auth/* (login/signup/resend) — password brute-force, not
    # normal API traffic. Per client IP.
    rate_limit_auth_per_min: int = Field(default=10)
    rate_limit_window_sec: int = Field(default=60)

    # --- Support / contact form (Brevo email) ---
    # POST /contact stores the ticket (+ private-bucket attachment) then emails
    # support. support_email is the destination; from_email must be on a sending
    # domain authenticated in Brevo (facades.trade). If the transport or
    # support_email is blank the ticket is still saved — the email is just
    # logged, not sent (non-fatal).
    support_email: str = Field(default="")
    contact_from_email: str = Field(default="Facades <noreply@facades.trade>")
    contact_attachment_max_bytes: int = Field(default=5 * 1024 * 1024)   # 5 MB
    contact_bucket: str = Field(default="contact-attachments")
    # Transport: auto (SMTP if SMTP_HOST set, else Brevo), or force smtp|brevo.
    email_provider: str = Field(default="auto")
    # SMTP — any relay. For Brevo's, use smtp-relay.brevo.com:587 with an SMTP
    # key (xsmtpsib-…), which is NOT the v3 API key below. Leave SMTP_HOST blank
    # to send over Brevo's HTTP API instead.
    smtp_host: str = Field(default="")
    smtp_port: int = Field(default=587)
    smtp_user: str = Field(default="")
    smtp_password: str = Field(default="")
    smtp_starttls: bool = Field(default=True)   # 587 STARTTLS (set false only with SSL)
    smtp_use_ssl: bool = Field(default=False)   # implicit TLS on 465
    # Brevo v3 HTTP API key (xkeysib-…) — the same key and sending domain as
    # facades-portal, so both products send as the one Facades sender.
    brevo_api_key: str = Field(default="")
    # Product-specific sender for Simmer readiness-alert emails (must be on the
    # authenticated sending domain — solutionjet.net for the Workspace relay).
    simmer_alert_from_email: str = Field(
        default="Simmer <noreply-edgelane-simmer@solutionjet.net>")
    # Base URL of the Simmer UI, for the "open in Simmer" link in alert emails.
    simmer_app_url: str = Field(default="https://edgelane-simmer.vercel.app")

    # Dev override: keep polling even when market is closed (mock-mode dev)
    force_poll_when_closed: bool = Field(default=False)

    # When the market is closed, still poll on a slow cadence to keep the UI
    # rendering off the last-available chain + private GEX provider overlay — but WITHOUT
    # persisting bias_decisions or running the self-eval (frozen prices would
    # only manufacture meaningless "neutral" outcomes). Display-only.
    display_when_closed: bool = Field(default=True)
    closed_display_interval_sec: int = Field(default=60)

    # --- Simmer sweep data provider ---
    # Which market-data source the Simmer 5-minute watcher sweep uses for
    # quote/expirations/chain/daily-bars. "tradier" (default) keeps today's
    # behavior; "yahoo" moves the sweep off Tradier's 120 req/min budget
    # (Matrix + Torque keep it) onto Yahoo's public endpoints — unofficial,
    # personal-use posture; see app/simmer_data_provider.py. Needs no key.
    simmer_data_provider: str = Field(default="tradier")

    # --- Simmer news + sentiment (Phase 3a — see app/simmer_news.py) ---
    # Alpaca Market Data news (free with a paper account at alpaca.markets).
    # Both keys blank → the news refresh is a clean no-op with a data_quality
    # note (nothing breaks; sentiment fields stay NULL).
    alpaca_key_id: str = Field(default="")
    alpaca_secret_key: str = Field(default="")
    # Gemini headline scoring (aistudio.google.com). No key → headlines are
    # still ingested/deduped, just never scored (sentiment stays NULL, noted).
    gemini_api_key: str = Field(default="")
    # Scores are NOT reproducible across models — the model id is recorded on
    # every scored row. flash-lite is the cheapest with thinking off by default.
    gemini_model: str = Field(default="gemini-2.5-flash-lite")
    # News source: alpaca (default; needs the keys above), rss (keyless wire
    # firehose, regex ticker match, weaker coverage), off.
    simmer_news_provider: str = Field(default="alpaca")
    # Future-earnings-DATE source (the SEC feed only confirms PAST earnings).
    # yahoo (reuses the Simmer data provider's crumb session, no key), nasdaq
    # (free but undocumented/grey-ToS and often blocked), off (default —
    # earnings mode stays dormant). Powers the card's earnings toggle + the
    # catalyst window.
    simmer_earnings_provider: str = Field(default="off")

    # External GEX override (private GEX provider extension webhook)
    use_external_gex: bool = Field(default=True)         # prefer extension data over Tradier-OI walls when fresh
    external_gex_timeout_sec: int = Field(default=30)    # how stale before falling back
    # GEX weighting for the Tradier fallback walls (no external feed):
    #   "oi"     -> gamma × open_interest  (resting positioning; default)
    #   "volume" -> gamma × volume         (today's traded gamma; closer to the
    #               flow-based provider levels for 0DTE)
    gex_source: str = Field(default="oi")
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
    if "EVAL_MIN_GRADED" in raw:             out["eval_min_graded"] = int(raw["EVAL_MIN_GRADED"])
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
    if "CORS_ALLOW_ORIGIN_REGEX" in raw:
        # Blank value explicitly disables the regex fallback.
        out["cors_allow_origin_regex"] = raw["CORS_ALLOW_ORIGIN_REGEX"].strip() or None
    if "WS_HEARTBEAT_SEC" in raw:            out["ws_heartbeat_sec"] = int(raw["WS_HEARTBEAT_SEC"])

    if "AUTH_ENABLED" in raw:
        out["auth_enabled"] = raw["AUTH_ENABLED"].strip().lower() in ("true", "1", "yes", "on")
    if "SUPABASE_URL" in raw:                out["supabase_url"] = raw["SUPABASE_URL"].strip()
    if "SUPABASE_JWT_SECRET" in raw:         out["supabase_jwt_secret"] = raw["SUPABASE_JWT_SECRET"].strip()
    if "SUPABASE_SERVICE_KEY" in raw:        out["supabase_service_key"] = raw["SUPABASE_SERVICE_KEY"].strip()
    if "TURNSTILE_SECRET" in raw:            out["turnstile_secret"] = raw["TURNSTILE_SECRET"].strip()
    if "ANON_SESSION_SECRET" in raw:         out["anon_session_secret"] = raw["ANON_SESSION_SECRET"].strip()
    if "ANON_SESSION_TTL_SEC" in raw:        out["anon_session_ttl_sec"] = int(raw["ANON_SESSION_TTL_SEC"])
    if "ADMIN_API_TOKEN" in raw:             out["admin_api_token"] = raw["ADMIN_API_TOKEN"].strip()
    if "RATE_LIMIT_PER_MIN" in raw:          out["rate_limit_per_min"] = int(raw["RATE_LIMIT_PER_MIN"])
    if "RATE_LIMIT_AUTH_PER_MIN" in raw:     out["rate_limit_auth_per_min"] = int(raw["RATE_LIMIT_AUTH_PER_MIN"])
    if "RATE_LIMIT_WINDOW_SEC" in raw:       out["rate_limit_window_sec"] = int(raw["RATE_LIMIT_WINDOW_SEC"])

    if "SUPPORT_EMAIL" in raw:               out["support_email"] = raw["SUPPORT_EMAIL"].strip()
    if "CONTACT_FROM_EMAIL" in raw:          out["contact_from_email"] = raw["CONTACT_FROM_EMAIL"].strip()
    if "CONTACT_ATTACHMENT_MAX_BYTES" in raw:
        out["contact_attachment_max_bytes"] = int(raw["CONTACT_ATTACHMENT_MAX_BYTES"])
    if "CONTACT_BUCKET" in raw:              out["contact_bucket"] = raw["CONTACT_BUCKET"].strip()
    if "EMAIL_PROVIDER" in raw:              out["email_provider"] = raw["EMAIL_PROVIDER"].strip().lower()
    if "SMTP_HOST" in raw:                   out["smtp_host"] = raw["SMTP_HOST"].strip()
    if "SMTP_PORT" in raw:                   out["smtp_port"] = int(raw["SMTP_PORT"])
    if "SMTP_USER" in raw:                   out["smtp_user"] = raw["SMTP_USER"].strip()
    if "SMTP_PASSWORD" in raw:               out["smtp_password"] = raw["SMTP_PASSWORD"]
    if "SMTP_STARTTLS" in raw:
        out["smtp_starttls"] = raw["SMTP_STARTTLS"].strip().lower() in ("true", "1", "yes", "on")
    if "SMTP_USE_SSL" in raw:
        out["smtp_use_ssl"] = raw["SMTP_USE_SSL"].strip().lower() in ("true", "1", "yes", "on")
    if "BREVO_API_KEY" in raw:               out["brevo_api_key"] = raw["BREVO_API_KEY"].strip()
    if "SIMMER_ALERT_FROM_EMAIL" in raw:     out["simmer_alert_from_email"] = raw["SIMMER_ALERT_FROM_EMAIL"].strip()
    if "SIMMER_APP_URL" in raw:              out["simmer_app_url"] = raw["SIMMER_APP_URL"].strip().rstrip("/")

    if "FORCE_POLL_WHEN_CLOSED" in raw:
        out["force_poll_when_closed"] = raw["FORCE_POLL_WHEN_CLOSED"].strip().lower() in ("true", "1", "yes", "on")
    if "DISPLAY_WHEN_CLOSED" in raw:
        out["display_when_closed"] = raw["DISPLAY_WHEN_CLOSED"].strip().lower() in ("true", "1", "yes", "on")
    if "CLOSED_DISPLAY_INTERVAL_SEC" in raw:
        out["closed_display_interval_sec"] = int(raw["CLOSED_DISPLAY_INTERVAL_SEC"])

    if "SIMMER_DATA_PROVIDER" in raw:
        v = raw["SIMMER_DATA_PROVIDER"].strip().lower()
        out["simmer_data_provider"] = v if v in ("tradier", "yahoo") else "tradier"

    if "ALPACA_KEY_ID" in raw:               out["alpaca_key_id"] = raw["ALPACA_KEY_ID"].strip()
    if "ALPACA_SECRET_KEY" in raw:           out["alpaca_secret_key"] = raw["ALPACA_SECRET_KEY"].strip()
    if "GEMINI_API_KEY" in raw:              out["gemini_api_key"] = raw["GEMINI_API_KEY"].strip()
    if "GEMINI_MODEL" in raw:
        v = raw["GEMINI_MODEL"].strip()
        if v:
            out["gemini_model"] = v
    if "SIMMER_NEWS_PROVIDER" in raw:
        v = raw["SIMMER_NEWS_PROVIDER"].strip().lower()
        out["simmer_news_provider"] = v if v in ("alpaca", "rss", "off") else "alpaca"
    if "SIMMER_EARNINGS_PROVIDER" in raw:
        v = raw["SIMMER_EARNINGS_PROVIDER"].strip().lower()
        out["simmer_earnings_provider"] = v if v in ("off", "yahoo", "nasdaq") else "off"

    if "USE_EXTERNAL_GEX" in raw:
        out["use_external_gex"] = raw["USE_EXTERNAL_GEX"].strip().lower() in ("true", "1", "yes", "on")
    if "EXTERNAL_GEX_TIMEOUT_SEC" in raw:
        out["external_gex_timeout_sec"] = int(raw["EXTERNAL_GEX_TIMEOUT_SEC"])
    if "EXTENSION_POLICY_VERSION" in raw:
        out["extension_policy_version"] = int(raw["EXTENSION_POLICY_VERSION"])
    if "GEX_SOURCE" in raw:
        v = raw["GEX_SOURCE"].strip().lower()
        out["gex_source"] = "volume" if v == "volume" else "oi"

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

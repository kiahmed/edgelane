"""Shared utilities for EdgeLane test scripts.

Loads keys from ../edgelane_market.config (KEY=value format) and provides a tiny
stopwatch + pretty-printer.

Was ../edge_lane_config.config until 2026-08; that file belonged to the legacy
single-file frontend, which is gone. The market config already carries the same
Tradier credentials, so there is now one config instead of two.
"""
import json
import os
import time
from pathlib import Path

# ---- config loader -----------------------------------------------------------

def load_config(path: str | Path | None = None) -> dict:
    """Read edgelane_market.config from the project root and return as dict."""
    if path is None:
        # tests/ live in EdgeLane/tests, so config is one level up
        path = Path(__file__).resolve().parent.parent / "edgelane_market.config"
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(
            f"config not found: {path}\n"
            f"  copy edgelane_market.config.example → edgelane_market.config and fill in real keys."
        )
    cfg = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        v = v.strip()
        # Strip inline comments — bash sourcing tolerates these, our parser
        # didn't, so DEVMODE=true # sandbox became literally "true # sandbox"
        # and DEVMODE checks fell through to prod. v4.7.31b: handle them.
        # Only strip # comments OUTSIDE of quoted values (so a # inside
        # "abc#def" is preserved).
        if v.startswith('"') or v.startswith("'"):
            quote = v[0]
            end = v.find(quote, 1)
            if end >= 0:
                v = v[1:end]   # take only what's inside the quotes
            else:
                v = v[1:].strip()
        else:
            # Unquoted — anything from a ' #' or '\t#' is a comment.
            hash_idx = -1
            for i, ch in enumerate(v):
                if ch == "#" and (i == 0 or v[i-1] in (" ", "\t")):
                    hash_idx = i
                    break
            if hash_idx >= 0:
                v = v[:hash_idx].rstrip()
        cfg[k.strip()] = v
    return cfg


def require_keys(cfg: dict, *keys: str) -> None:
    missing = [k for k in keys if not cfg.get(k)]
    if missing:
        raise SystemExit(f"missing required keys in config: {', '.join(missing)}")


def resolve_tradier_creds(cfg: dict) -> tuple[str, str, str]:
    """Pick Tradier (token, base_url, env_label) based on cfg[DEVMODE].
    Returns ('', '', 'unset') if no token configured for the active env.

    v4.7.31b: more permissive DEVMODE matching. Defense-in-depth in case
    load_config didn't fully sanitize an inline-comment or stray quotes.

    DEVMODE truthy values  -> sandbox  : true, 1, yes, y, on, t  (default)
    DEVMODE falsy values   -> prod     : false, 0, no, n, off, f, ""
    Anything else -> sandbox (safe default; production requires explicit opt-in).
    """
    raw = str(cfg.get("DEVMODE", "true"))
    # Drop quotes and any trailing comment fragment, then lowercase + first token only.
    cleaned = raw.strip().strip('"').strip("'").split("#", 1)[0].strip().split()[0:1]
    devmode = (cleaned[0] if cleaned else "").lower()

    SANDBOX_VALUES = {"true", "1", "yes", "y", "on", "t"}
    PROD_VALUES    = {"false", "0", "no", "n", "off", "f"}

    if devmode in PROD_VALUES:
        token = cfg.get("TRADIER_TOKEN") or cfg.get("TRADIER_PROD_TOKEN") or ""
        return token, "https://api.tradier.com", "production"
    # Default-sandbox: explicit truthy values, empty, typos, anything that
    # isn't a recognized prod indicator. Silent — sandbox is the safe default.
    token = (cfg.get("TRADIER_TOKEN_SANDBOX") or cfg.get("TRADIER_SANDBOX_TOKEN")
             or cfg.get("TRADIER_ACCESS_TOKEN") or "")
    return token, "https://sandbox.tradier.com", "sandbox"




# ---- timing ------------------------------------------------------------------

class Stopwatch:
    """Works both inside and outside the `with` block. `sw.ms` returns live
    elapsed time during the block, frozen elapsed time after the block exits.
    """
    def __enter__(self):
        self.t0 = time.perf_counter()
        self.elapsed = None
        return self
    def __exit__(self, *_):
        self.elapsed = time.perf_counter() - self.t0
    @property
    def ms(self):
        if self.elapsed is None:
            return (time.perf_counter() - self.t0) * 1000
        return self.elapsed * 1000


# ---- pretty-print ------------------------------------------------------------

def pp(label: str, value, color: str = "", width: int = 28):
    """Print 'label: value' aligned, with optional ANSI color on label."""
    reset = "\033[0m" if color else ""
    print(f"{color}{label:<{width}}{reset} {value}")

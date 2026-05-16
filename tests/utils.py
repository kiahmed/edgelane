"""Shared utilities for EdgeLane test scripts.

Loads keys from ../edge_lane_config.config (bash-sourceable KEY=value format),
provides thin Atlas REST + Gemini API helpers, and a tiny stopwatch.
"""
import json
import os
import time
import sys
import random
import urllib.request
import urllib.error
from pathlib import Path

ATLAS_BASE = "https://atlasmcp.finmanagerai.com/api/v1/tools"
GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta/models"

# ---- config loader -----------------------------------------------------------

def load_config(path: str | Path | None = None) -> dict:
    """Read edge_lane_config.config from the project root and return as dict."""
    if path is None:
        # tests/ live in EdgeLane/tests, so config is one level up
        path = Path(__file__).resolve().parent.parent / "edge_lane_config.config"
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(
            f"config not found: {path}\n"
            f"  copy edge_lane_config.config.example → edge_lane_config.config and fill in real keys."
        )
    cfg = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        cfg[k.strip()] = v.strip().strip('"').strip("'")
    return cfg


def require_keys(cfg: dict, *keys: str) -> None:
    missing = [k for k in keys if not cfg.get(k)]
    if missing:
        raise SystemExit(f"missing required keys in config: {', '.join(missing)}")




# ---- retry policy ------------------------------------------------------------

_RETRY_HTTP_CODES = (429, 500, 502, 503, 504)


def _is_retryable(err_msg: str) -> bool:
    """True for transient errors worth retrying (rate-limit, 5xx, network, timeout)."""
    msg = err_msg.lower()
    if "network error" in msg or "timed out" in msg:
        return True
    return any(f"HTTP {code}" in err_msg for code in _RETRY_HTTP_CODES)


def _backoff_with_jitter(attempt: int, base: float = 1.0, cap: float = 8.0) -> float:
    """Exponential backoff with ±20% jitter. attempt is 0-indexed."""
    raw = min(cap, base * (2 ** attempt))
    jitter = raw * 0.2 * (2 * random.random() - 1)
    return max(0.3, raw + jitter)


# ---- Atlas REST --------------------------------------------------------------

class AtlasError(RuntimeError):
    pass


def atlas_call(tool: str, body: dict | None, atlas_key: str, timeout: int = 75,
               max_retries: int = 3) -> dict:
    """POST to /api/v1/tools/{tool}. Atlas wraps quota errors in 200 + {error,message}.
    Retries on 429 / 5xx / network / mid-read timeout with exponential backoff + jitter.

    Default timeout bumped from 30s → 75s because analyze_greek_exposures on
    heavy-chain symbols (MU, AMD, SMCI) routinely takes 40-60s server-side.
    """
    last_err = None
    for attempt in range(max_retries + 1):
        req = urllib.request.Request(
            f"{ATLAS_BASE}/{tool}",
            data=json.dumps(body or {}).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {atlas_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            if isinstance(data, dict) and data.get("error"):
                # Atlas's rate_limit envelope is non-retryable (quota exhausted, not transient)
                raise AtlasError(f"Atlas {tool}: {data['error']}: {data.get('message', '')}")
            return data
        except urllib.error.HTTPError as e:
            body_text = e.read().decode("utf-8", "replace")[:300]
            last_err = AtlasError(f"Atlas {tool}: HTTP {e.code} — {body_text}")
        except urllib.error.URLError as e:
            last_err = AtlasError(f"Atlas {tool}: network error — {e.reason}")
        except TimeoutError as e:
            # Mid-read socket timeout. Raw TimeoutError escapes URLError on
            # Python 3.10+ when the timeout fires AFTER connect — must catch
            # explicitly or the retry loop is bypassed.
            last_err = AtlasError(f"Atlas {tool}: read timed out after {timeout}s (server-side compute too slow)")
        except AtlasError:
            raise  # quota envelope — don't retry

        if attempt < max_retries and _is_retryable(str(last_err)):
            delay = _backoff_with_jitter(attempt)
            sys.stderr.write(f"  \033[33m⚠ Atlas {tool} attempt {attempt+1}/{max_retries+1} failed: "
                             f"{str(last_err)[:120]}\n    retrying in {delay:.1f}s...\033[0m\n")
            time.sleep(delay)
            continue
        raise last_err


# ---- Gemini ------------------------------------------------------------------

class GeminiError(RuntimeError):
    pass


def gemini_call(
    prompt: str,
    api_key: str,
    model: str = "gemini-2.5-flash",
    response_schema: dict | None = None,
    temperature: float = 0.1,
    timeout: int = 60,
    max_retries: int = 3,
) -> tuple[dict | str, dict]:
    """Call Gemini's generateContent endpoint with optional JSON schema mode.
    Retries on 429 / 5xx / network with exponential backoff + jitter — important
    for gemini-2.5-pro which 503s under load.
    Returns (parsed_response, raw_meta)."""
    gen_config: dict = {"temperature": temperature}
    if response_schema is not None:
        gen_config["responseMimeType"] = "application/json"
        gen_config["responseSchema"] = response_schema

    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": gen_config,
    }
    last_err = None
    for attempt in range(max_retries + 1):
        req = urllib.request.Request(
            f"{GEMINI_BASE}/{model}:generateContent",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "x-goog-api-key": api_key},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            parts = data.get("candidates", [{}])[0].get("content", {}).get("parts", [])
            text = "".join(p.get("text", "") for p in parts)
            if not text:
                raise GeminiError(f"Gemini {model}: empty response. raw: {json.dumps(data)[:400]}")
            meta = {
                "usage": data.get("usageMetadata", {}),
                "finish_reason": data.get("candidates", [{}])[0].get("finishReason"),
                "raw_text_len": len(text),
                "attempts": attempt + 1,
            }
            if response_schema is not None:
                try:
                    return json.loads(text), meta
                except json.JSONDecodeError as e:
                    raise GeminiError(f"Gemini {model}: JSON parse failed — {e}. text: {text[:300]}") from None
            return text, meta
        except urllib.error.HTTPError as e:
            body_text = e.read().decode("utf-8", "replace")[:400]
            last_err = GeminiError(f"Gemini {model}: HTTP {e.code} — {body_text}")
        except urllib.error.URLError as e:
            last_err = GeminiError(f"Gemini {model}: network error — {e.reason}")
        except GeminiError:
            raise  # empty/parse errors are not transient

        if attempt < max_retries and _is_retryable(str(last_err)):
            delay = _backoff_with_jitter(attempt)
            sys.stderr.write(f"  \033[33m⚠ Gemini {model} attempt {attempt+1}/{max_retries+1} failed: "
                             f"{str(last_err)[:120]}\n    retrying in {delay:.1f}s...\033[0m\n")
            time.sleep(delay)
            continue
        raise last_err


# ---- bias schema (shared between v4.6 JSX and tests) -------------------------

BIAS_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "spot": {"type": "NUMBER"},
        "directional_score": {"type": "NUMBER"},
        "bias_label": {
            "type": "STRING",
            "enum": ["bullish", "mild_bullish", "neutral", "mild_bearish", "bearish"],
        },
        "confidence": {"type": "STRING", "enum": ["high", "medium", "low"]},
        "gex_wall_strike": {"type": "NUMBER", "nullable": True},
        "gex_wall_strength": {"type": "STRING", "enum": ["high", "medium", "low"]},
        "signals": {
            "type": "OBJECT",
            "properties": {
                "dex_skew": {"type": "STRING"},
                "gex_wall": {"type": "STRING"},
                "gamma_regime": {"type": "STRING"},
                "spot_vs_wall": {"type": "STRING"},
            },
        },
        "summary": {"type": "STRING"},
        "recommended_strategies": {"type": "ARRAY", "items": {"type": "STRING"}},
    },
    "required": [
        "directional_score", "bias_label", "confidence", "signals",
        "summary", "recommended_strategies",
    ],
}


def build_bias_prompt(symbol: str, expiration: str, spot: float, greeks: dict) -> str:
    """Build the bias-synthesis prompt. Trims `greeks` to target expiration to
    match what v4.6 detectBias does — keeps the prompt small and focused.
    """
    # Atlas returns greeks under exposures_by_date keyed by date string.
    # Trim to the chosen date (target if present, else closest).
    ebd = (greeks or {}).get("exposures_by_date") or {}
    dates = list(ebd.keys())
    chosen = expiration if expiration in ebd else (
        min(dates, key=lambda d: abs((__import__("datetime").date.fromisoformat(d) - __import__("datetime").date.fromisoformat(expiration)).days))
        if dates and expiration else (dates[0] if dates else None)
    )
    trimmed = {
        "symbol": (greeks or {}).get("symbol"),
        "current_price": (greeks or {}).get("current_price"),
        "key_levels": (greeks or {}).get("key_levels"),
        "portfolio_totals": (greeks or {}).get("portfolio_totals"),
        "chosen_expiration": chosen,
        "exposures_for_chosen": ebd.get(chosen) if chosen else None,
    }
    return f"""Analyze dealer hedging structure for {symbol} expiring {expiration} and output directional bias.

DATA (live from Atlas — already trimmed to your target expiration):
- Spot: {spot}
- Atlas key_levels (pre-computed walls + flip points): {json.dumps(trimmed["key_levels"])}
- Portfolio totals (sum across all expirations): {json.dumps(trimmed["portfolio_totals"])}
- Per-strike exposures for {chosen}:
{json.dumps(trimmed["exposures_for_chosen"], indent=2)}

INSTRUCTIONS:
1) The data above is already scoped to the relevant expiration ({chosen}); use it as-is.
2) Compute four signals:
   - DEX_SKEW: net delta-exposure direction (sum of dex; positive = dealers long delta)
   - GEX_WALL: largest |positive GEX| strike (the dominant gamma wall)
   - GAMMA_REGIME: positive (dampening) vs negative (amplifying) net gamma
   - SPOT_POSITION: where current spot sits relative to the dominant wall
3) Classify wall strength relative to other walls:
   - "high" if >2x the next-largest wall
   - "medium" if 1.2-2x the next-largest
   - "low" otherwise
4) Combine into directional_score on [-100, 100]:
   - >= 60: bullish
   - 20..60: mild_bullish
   - -20..20: neutral
   - -60..-20: mild_bearish
   - <= -60: bearish

OUTPUT JSON ONLY matching the provided schema. gex_wall_strike MUST be the numeric strike price (e.g. 450), not a description; null only if no clear wall exists. recommended_strategies values must be from: bull_put, bear_call, iron_condor, iron_butterfly, bull_call, bear_put, call_butterfly, put_butterfly. summary is one paragraph of plain English. signals.* are one-line descriptions each."""


# ---- timing ------------------------------------------------------------------

class Stopwatch:
    """Works both inside and outside the `with` block. `sw.ms` returns live
    elapsed time during the block, frozen elapsed time after the block exits.
    Fixes AttributeError: 'Stopwatch' object has no attribute 'elapsed' that
    bit gemini_bias_bench.py when ms was read mid-block.
    """
    def __enter__(self):
        self.t0 = time.perf_counter()
        self.elapsed = None
        return self
    def __exit__(self, *_):
        self.elapsed = time.perf_counter() - self.t0
    @property
    def ms(self):
        # If we're still inside the with block, compute live elapsed
        if self.elapsed is None:
            return (time.perf_counter() - self.t0) * 1000
        return self.elapsed * 1000


# ---- pretty-print ------------------------------------------------------------

def pp(label: str, value, color: str = "", width: int = 28):
    """Print 'label: value' aligned, with optional ANSI color on label."""
    reset = "\033[0m" if color else ""
    print(f"{color}{label:<{width}}{reset} {value}")

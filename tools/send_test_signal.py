#!/usr/bin/env python3
"""Fire a signed test event at POST /webhook/news_signal (entry or cancel).

Reusable operator tool — not a pytest test — for verifying the deployed
webhook end to end: auth, idempotency, market-hours gate, fan-out. Signs the
request exactly like facades-news-reactor does (see require_news_signal_auth
in market/backend/app/routes/torque.py): HMAC-SHA256 of
"{timestamp}.{raw body}" with NEWS_REACTOR_WEBHOOK_SECRET, sent as
X-Signal-Signature/X-Signal-Timestamp.

THIS CAN PLACE A REAL ORDER on whichever account is currently entitled to
`news-reactor` in Supabase, through that account's OWN broker connection
(sandbox or production, whatever it's configured with) — the flag/secret
gate what CAN process a signal, not what it trades with. Nothing here checks
that for you; know which account is qualified and what it's connected to
before pointing this at a live, deployed backend during market hours.

Config: reads NEWS_REACTOR_WEBHOOK_SECRET from edgelane_market.config (repo
root) by default, or --secret / $NEWS_REACTOR_WEBHOOK_SECRET. Never prints
the secret itself.

Usage:
  # Entry signal (bullish/bearish) against a local dev backend:
  python3 tools/send_test_signal.py --base-url http://127.0.0.1:8789 \\
      --symbol NDX --direction bullish

  # Same, against the deployed backend:
  python3 tools/send_test_signal.py --base-url https://edge.facades.trade \\
      --symbol NDX --direction bullish

  # Cancel a prior signal (needs the source_event_id it printed):
  python3 tools/send_test_signal.py --base-url https://edge.facades.trade \\
      --cancel <source_event_id>
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ROOT / "edgelane_market.config"
# Cloudflare 1010-blocks urllib's default UA (same workaround as db_push.py).
_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def _load_secret(config_path: Path) -> str:
    if not config_path.is_file():
        return ""
    for line in config_path.read_text().splitlines():
        line = line.strip()
        if line.startswith("NEWS_REACTOR_WEBHOOK_SECRET="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def _sign(secret: str, timestamp: str, body: bytes) -> str:
    return hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256).hexdigest()


def _post(base_url: str, secret: str, payload: dict) -> None:
    body = json.dumps(payload).encode()
    ts = str(int(time.time()))
    sig = _sign(secret, ts, body)
    url = base_url.rstrip("/") + "/webhook/news_signal"
    req = urllib.request.Request(
        url, method="POST", data=body,
        headers={"Content-Type": "application/json", "User-Agent": _UA,
                 "Accept": "application/json",
                 "X-Signal-Timestamp": ts, "X-Signal-Signature": sig})
    print(f"POST {url}")
    print(f"  payload: {json.dumps(payload, indent=2)}")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            print(f"  -> HTTP {r.status}: {r.read().decode()}")
    except urllib.error.HTTPError as e:
        print(f"  -> HTTP {e.code}: {e.read().decode()}", file=sys.stderr)
        raise SystemExit(1)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base-url", required=True, help="e.g. http://127.0.0.1:8789 or https://edge.facades.trade")
    ap.add_argument("--symbol", default="NDX")
    ap.add_argument("--direction", choices=["bullish", "bearish"], default="bullish")
    ap.add_argument("--confidence", type=float, default=0.8)
    ap.add_argument("--source-event-id", default=None, help="defaults to a fresh uuid")
    ap.add_argument("--cancel", metavar="SOURCE_EVENT_ID", default=None,
                    help="send a cancel for a prior signal's source_event_id instead of a fresh entry")
    ap.add_argument("--secret", default=None, help="overrides NEWS_REACTOR_WEBHOOK_SECRET from config")
    ap.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    args = ap.parse_args()

    secret = args.secret or _load_secret(args.config)
    if not secret:
        print(f"ERROR: no NEWS_REACTOR_WEBHOOK_SECRET found in {args.config} (or pass --secret)",
              file=sys.stderr)
        return 1

    if args.cancel:
        payload = {
            "source_event_id": args.source_event_id or f"test-cancel-{uuid.uuid4()}",
            "headline": "test cancel", "symbol": args.symbol.upper(),
            "state": "cancel", "cancels_event_id": args.cancel,
        }
    else:
        payload = {
            "source_event_id": args.source_event_id or f"test-{uuid.uuid4()}",
            "headline": f"[TEST] manual send_test_signal.py event", "category": "test",
            "symbol": args.symbol.upper(), "direction": args.direction,
            "sentiment": args.direction, "confidence": args.confidence,
            "rationale": "manual operator test — tools/send_test_signal.py",
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
    _post(args.base_url, secret, payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

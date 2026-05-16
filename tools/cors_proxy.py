#!/usr/bin/env python3
"""Tiny dev-only CORS proxy for EdgeLane.

Forwards browser → Atlas + Gemini requests, adding CORS headers so the page
served from file:// or localhost can actually reach them. Stdlib only — no
pip install needed.

Usage:
    python3 tools/cors_proxy.py                    # listens on :8787
    python3 tools/cors_proxy.py --port 9000        # custom port
    python3 tools/cors_proxy.py --bind 127.0.0.1   # localhost-only

Routes:
    /atlas/<tool>   →  https://atlasmcp.finmanagerai.com/api/v1/tools/<tool>
    /gemini/<rest>  →  https://generativelanguage.googleapis.com/v1beta/<rest>

The browser sends its `Authorization` and `x-goog-api-key` headers normally;
this proxy forwards them as-is. Keys never live on the proxy.

DO NOT EXPOSE THIS BEYOND LOCALHOST. It's a dev convenience, not a service.
"""
import argparse
import json
import sys
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ATLAS_UPSTREAM  = "https://atlasmcp.finmanagerai.com/api/v1/tools/"
GEMINI_UPSTREAM = "https://generativelanguage.googleapis.com/v1beta/"

CORS_HEADERS = {
    "Access-Control-Allow-Origin":  "*",
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Allow-Headers": "Authorization, Content-Type, x-goog-api-key",
    "Access-Control-Max-Age":       "86400",
}

# Headers we forward upstream (case-insensitive). Anything else is dropped.
FORWARD_HEADERS = {"authorization", "content-type", "x-goog-api-key", "accept"}


class ProxyHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        sys.stderr.write(f"[proxy] {self.address_string()} {fmt % args}\n")

    def _send_cors(self, status=204):
        self.send_response(status)
        for k, v in CORS_HEADERS.items():
            self.send_header(k, v)
        self.end_headers()

    def do_OPTIONS(self):
        self._send_cors(204)

    def do_POST(self):
        # Route
        path = self.path.lstrip("/")
        if path.startswith("atlas/"):
            upstream = ATLAS_UPSTREAM + path[len("atlas/"):]
        elif path.startswith("gemini/"):
            upstream = GEMINI_UPSTREAM + path[len("gemini/"):]
        else:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"unknown route. expected /atlas/<tool> or /gemini/<rest>\n")
            return

        # Read body
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length) if length else b""

        # Forward selected headers
        headers = {}
        for h in self.headers:
            if h.lower() in FORWARD_HEADERS:
                headers[h] = self.headers[h]

        req = urllib.request.Request(upstream, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                resp_body = resp.read()
                self.send_response(resp.status)
                for k, v in CORS_HEADERS.items():
                    self.send_header(k, v)
                # Also forward the upstream content-type
                ct = resp.headers.get("Content-Type", "application/json")
                self.send_header("Content-Type", ct)
                self.end_headers()
                self.wfile.write(resp_body)
        except urllib.error.HTTPError as e:
            self.send_response(e.code)
            for k, v in CORS_HEADERS.items():
                self.send_header(k, v)
            self.send_header("Content-Type", e.headers.get("Content-Type", "application/json"))
            self.end_headers()
            try: self.wfile.write(e.read())
            except Exception: self.wfile.write(json.dumps({"error": str(e)}).encode())
        except urllib.error.URLError as e:
            self.send_response(502)
            for k, v in CORS_HEADERS.items():
                self.send_header(k, v)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": "upstream_unreachable", "reason": str(e.reason)}).encode())


def main():
    ap = argparse.ArgumentParser(description="EdgeLane dev CORS proxy")
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--bind", default="127.0.0.1")
    args = ap.parse_args()

    srv = ThreadingHTTPServer((args.bind, args.port), ProxyHandler)
    print(f"EdgeLane CORS proxy listening on http://{args.bind}:{args.port}")
    print(f"  /atlas/<tool>   → {ATLAS_UPSTREAM}<tool>")
    print(f"  /gemini/<rest>  → {GEMINI_UPSTREAM}<rest>")
    print(f"\nKeep this terminal open. Ctrl+C to stop.")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down...")
        srv.shutdown()


if __name__ == "__main__":
    main()

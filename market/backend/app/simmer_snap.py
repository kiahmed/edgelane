"""Server-rendered Simmer card as standalone HTML — the SNAPSHOT surface.

The `simmer-snap` service (headless Chromium, soljet-postiz) can't screenshot the
SPA at simmer.facades.trade: that page is behind user login and a headless browser
has no session. So the poster screenshots THIS instead — a self-contained,
inline-CSS card served by the backend at GET /simmer/snap/{SYM}, gated by the
read-only machine bearer (SIMMER_API_TOKEN). No user session, secret stays
server-side. Data is the same cached envelope the read-only API already exposes.

Pure formatting; no I/O. `render_snap_card(env, ready, watch)` returns a full
HTML document with a `[data-snap="card"]` wrapper the crop targets."""
from __future__ import annotations

from typing import Any

from .simmer_email import _esc, _money, _pct, _strike, _strikes_line, _structure_name, _num

_BG = "#07090d"


def _decision_style(decision: str, score: float | None,
                    ready: float, watch: float) -> tuple[str, str]:
    """(label, accent-color) for the badge."""
    d = str(decision or "")
    if d == "ready":
        return "READY TO SELL", "#34d399"      # emerald
    if d == "watch":
        return "WATCHING", "#fbbf24"           # amber
    if d == "vetoed":
        return "NO TRADE", "#fb7185"           # rose
    return d.upper() or "—", "#94a3b8"


def render_snap_card(env: dict, ready: float = 70.0, watch: float = 50.0) -> str:
    symbol = _esc(str(env.get("symbol") or "?").upper())
    expiration = _esc(str(env.get("expiration") or ""))
    score = _num(env.get("score"))
    decision = str(env.get("decision") or "")
    is_ready = decision == "ready"
    score_s = "—" if (score is None or decision == "vetoed") else f"{score:.0f}"
    label, accent = _decision_style(decision, score, ready, watch)
    structure = _esc(_structure_name(env.get("structure"))) if env.get("structure") else ""
    regime = env.get("regime") or {}
    regime_state = _esc(str(regime.get("state") or "")) if isinstance(regime, dict) else ""

    veto = env.get("veto_reasons") or []
    # Body: the trade block when ready, else the refusal line.
    if is_ready and env.get("structure"):
        rows = "".join([
            _kv("Structure", f"{structure} &nbsp; {_strikes_line(env)}"),
            _kv("Credit", f"{_money(env.get('credit_fill'))} achievable"),
            _kv("Max loss", _money(env.get("max_loss"))),
            _kv("POP", _pct(env.get("pop_breakeven"))),
            _kv("EV / share", _money(env.get("expected_value"), 3)),
        ])
        body = f'<table style="border-collapse:collapse;width:100%;">{rows}</table>'
    else:
        why = (f"{len(veto)} gate{'s' if len(veto) != 1 else ''} vetoed"
               if veto else "below the ready band")
        body = (f'<div style="color:#94a3b8;font-size:15px;line-height:1.5;">'
                f'No sellable spread — <span style="color:#cbd5e1;">{_esc(why)}</span>.'
                f' The engine is refusing, not selling.</div>')

    reg_line = (f'<span style="color:#64748b;">regime: {regime_state.replace("_", " ")}</span>'
                if regime_state else "")

    return f"""<!doctype html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=640, initial-scale=1">
<title>{symbol} — Simmer readiness</title>
<style>
 html,body{{margin:0;background:{_BG};}}
 *{{box-sizing:border-box;font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;}}
</style></head>
<body>
 <div data-snap="card" style="width:600px;margin:0 auto;background:#1e293b;
      border:1px solid #334155;border-radius:14px;overflow:hidden;">
  <div style="padding:18px 22px;border-bottom:1px solid #334155;display:flex;
       align-items:center;justify-content:space-between;">
   <div>
     <div style="color:#f1f5f9;font-size:26px;font-weight:800;">{symbol}
       <span style="color:#cbd5e1;font-size:15px;font-weight:600;">&nbsp;exp {expiration}</span>
     </div>
     <div style="color:{accent};font-size:12px;font-weight:700;letter-spacing:.08em;margin-top:3px;">{_esc(label)}</div>
   </div>
   <div style="text-align:right;">
     <div style="color:{accent};font-size:34px;font-weight:800;font-family:ui-monospace,monospace;">{score_s}</div>
     <div style="color:#64748b;font-size:11px;">/ 100</div>
   </div>
  </div>
  <div style="padding:18px 22px;">
   {body}
   <div style="margin-top:16px;padding-top:12px;border-top:1px solid #334155;
        display:flex;justify-content:space-between;font-size:11px;color:#64748b;">
     <span>Facades Simmer · simmer.facades.trade</span>
     {reg_line}
   </div>
  </div>
 </div>
</body></html>"""


def _kv(label: str, value: str) -> str:
    return (f'<tr>'
            f'<td style="padding:5px 14px 5px 0;color:#94a3b8;font-size:14px;white-space:nowrap;">{label}</td>'
            f'<td style="padding:5px 0;color:#e2e8f0;font-size:14px;font-family:'
            f'ui-monospace,SFMono-Regular,Menlo,monospace;">{value}</td></tr>')

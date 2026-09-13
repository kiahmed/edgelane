"""Server-rendered Matrix cards as standalone HTML — the SNAPSHOT surface.

Launched with the lesson Simmer's first snap implementation learned the hard way
(docs/matrix_events_update.md §5): screenshotting the live SPA silently captured
nothing but the sign-in dialog, because matrix.facades.trade sits behind a user
session a headless browser does not have. So the poster screenshots THIS instead
— self-contained, inline-CSS, bearer-gated (`MATRIX_API_TOKEN`), no SPA, no user
session. Data is the cached poller snapshot the read-only API already exposes.

Pure formatting; no I/O. Each view returns a full HTML document whose crop target
is `[data-snap="<view>"]`, matching `simmer_snap.py`'s `[data-snap="card"]`.

Views (§5): engine_pick | strategy_grid | bias_chip | walls_chip | win_eval_grid
"""
from __future__ import annotations

from html import escape
from typing import Any

_BG = "#07090d"
_CARD = "#1e293b"
_EDGE = "#334155"
_FG = "#f1f5f9"
_DIM = "#94a3b8"
_MUTE = "#64748b"

VIEWS = ("engine_pick", "strategy_grid", "bias_chip", "walls_chip", "win_eval_grid")

# Matches the UI's own vocabulary so a chip reads like the product, not a report.
_STRATEGY_NAMES = {
    "bull_put": "Bull Put", "bear_call": "Bear Call", "iron_condor": "Iron Condor",
    "iron_butterfly": "Iron Butterfly", "bull_call": "Bull Call", "bear_put": "Bear Put",
    "call_butterfly": "Call Fly", "put_butterfly": "Put Fly",
}
_HEALTH_COLOR = {"healthy": "#34d399", "thin": "#fbbf24", "directional": "#fbbf24",
                 "broken": "#fb7185", "capital_trap": "#fb7185"}
_TREND = {"green": "#34d399", "yellow": "#fbbf24", "red": "#fb7185", "unknown": _DIM}


def _esc(v: Any) -> str:
    return escape(str(v if v is not None else ""), quote=True)


def _num(v: Any, dp: int = 2) -> str:
    try:
        return f"{float(v):.{dp}f}"
    except (TypeError, ValueError):
        return "—"


def _money(v: Any, dp: int = 2) -> str:
    try:
        return f"${float(v):.{dp}f}"
    except (TypeError, ValueError):
        return "—"


def _doc(title: str, view: str, inner: str, width: int = 600) -> str:
    return f"""<!doctype html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width={width + 40}, initial-scale=1">
<title>{_esc(title)}</title>
<style>
 html,body{{margin:0;background:{_BG};}}
 *{{box-sizing:border-box;font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;}}
 .mono{{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;}}
</style></head>
<body>
 <div data-snap="{_esc(view)}" style="width:{width}px;margin:0 auto;background:{_CARD};
      border:1px solid {_EDGE};border-radius:14px;overflow:hidden;">
{inner}
 </div>
</body></html>"""


def _header(symbol: str, expiration: str, right_top: str = "", right_sub: str = "",
            accent: str = _DIM, eyebrow: str = "") -> str:
    eb = (f'<div style="color:{accent};font-size:12px;font-weight:700;'
          f'letter-spacing:.08em;margin-top:3px;">{_esc(eyebrow)}</div>') if eyebrow else ""
    right = ""
    if right_top:
        right = (f'<div style="text-align:right;">'
                 f'<div class="mono" style="color:{accent};font-size:34px;font-weight:800;">{right_top}</div>'
                 f'<div style="color:{_MUTE};font-size:11px;">{_esc(right_sub)}</div></div>')
    return f"""  <div style="padding:18px 22px;border-bottom:1px solid {_EDGE};display:flex;
       align-items:center;justify-content:space-between;">
   <div>
     <div style="color:{_FG};font-size:26px;font-weight:800;">{_esc(symbol)}
       <span style="color:#cbd5e1;font-size:15px;font-weight:600;">&nbsp;exp {_esc(expiration)}</span>
     </div>{eb}
   </div>
   {right}
  </div>"""


def _footer(extra: str = "") -> str:
    return (f'   <div style="margin-top:16px;padding-top:12px;border-top:1px solid {_EDGE};'
            f'display:flex;justify-content:space-between;font-size:11px;color:{_MUTE};">'
            f'<span>Facades Matrix · matrix.facades.trade</span><span>{extra}</span></div>')


def _kv(label: str, value: str) -> str:
    return (f'<tr><td style="padding:5px 14px 5px 0;color:{_DIM};font-size:14px;'
            f'white-space:nowrap;">{_esc(label)}</td>'
            f'<td class="mono" style="padding:5px 0;color:#e2e8f0;font-size:14px;">{value}</td></tr>')


# ── Views ───────────────────────────────────────────────────────────────────

def _render_engine_pick(snap: dict) -> str:
    pick = snap.get("engine_pick") or {}
    sym = str(snap.get("symbol") or "?").upper()
    exp = str(snap.get("expiration") or "")
    if not pick:
        body = (f'<div style="color:{_DIM};font-size:15px;line-height:1.5;">'
                f'No engine pick right now — nothing cleared the bar. '
                f'The engine is refusing, not reaching.</div>')
        return _doc(f"{sym} — Matrix engine pick", "engine_pick",
                    _header(sym, exp, eyebrow="NO PICK") +
                    f'  <div style="padding:18px 22px;">{body}{_footer()}</div>')

    verdict = pick.get("composite_verdict") or {}
    vlabel = verdict.get("label") if isinstance(verdict, dict) else ""
    score = pick.get("composite_score")
    accent = _HEALTH_COLOR.get(str(pick.get("health") or ""), "#34d399")
    name = _STRATEGY_NAMES.get(str(pick.get("strategy") or ""), pick.get("name") or "")
    rows = "".join([
        _kv("Structure", _esc(pick.get("structure_text"))),
        _kv("Net premium", _money(pick.get("net_premium"))),
        _kv("Max profit", _money(pick.get("max_profit"))),
        _kv("Max loss", _money(pick.get("max_loss"))),
        _kv("POP", f"{_num(pick.get('pop_pct'), 1)}%"),
        _kv("EV (adj)", _money(pick.get("ev_adjusted"), 3)),
    ])
    sub = (f'<div style="color:{_DIM};font-size:13px;margin-bottom:12px;">'
           f'{_esc(name)} · {_esc(pick.get("label"))} · '
           f'<span style="color:{accent};">{_esc(vlabel)}</span></div>')
    inner = (_header(sym, exp, right_top=_num(score, 1), right_sub="/ 100",
                     accent=accent, eyebrow="ENGINE PICK") +
             f'  <div style="padding:18px 22px;">{sub}'
             f'<table style="border-collapse:collapse;width:100%;">{rows}</table>'
             f'{_footer(_esc(str(pick.get("health") or "")))}</div>')
    return _doc(f"{sym} — Matrix engine pick", "engine_pick", inner)


def _render_strategy_grid(snap: dict) -> str:
    sym = str(snap.get("symbol") or "?").upper()
    exp = str(snap.get("expiration") or "")
    strategies = snap.get("strategies") or {}
    cells = []
    for key in sorted(strategies):
        best = (strategies.get(key) or {}).get("best") or {}
        name = _STRATEGY_NAMES.get(key, key.replace("_", " ").title())
        if not best:
            cells.append(
                f'<div style="border:1px solid {_EDGE};border-radius:10px;padding:10px 12px;">'
                f'<div style="color:{_DIM};font-size:12px;font-weight:700;">{_esc(name)}</div>'
                f'<div style="color:{_MUTE};font-size:12px;margin-top:6px;">no candidate</div></div>')
            continue
        health = str(best.get("health") or "")
        color = _HEALTH_COLOR.get(health, _DIM)
        cells.append(
            f'<div style="border:1px solid {_EDGE};border-radius:10px;padding:10px 12px;">'
            f'<div style="display:flex;justify-content:space-between;align-items:baseline;">'
            f'<span style="color:#e2e8f0;font-size:12px;font-weight:700;">{_esc(name)}</span>'
            f'<span class="mono" style="color:{color};font-size:15px;font-weight:800;">'
            f'{_num(best.get("composite_score"), 1)}</span></div>'
            f'<div class="mono" style="color:{_DIM};font-size:11px;margin-top:5px;">'
            f'{_esc(best.get("structure_text"))}</div>'
            f'<div style="color:{color};font-size:10px;font-weight:700;letter-spacing:.06em;'
            f'margin-top:5px;text-transform:uppercase;">{_esc(health)}</div></div>')
    grid = ('<div style="display:grid;grid-template-columns:1fr 1fr;gap:10px;">'
            + "".join(cells) + "</div>")
    inner = (_header(sym, exp, eyebrow="STRATEGY GRID") +
             f'  <div style="padding:18px 22px;">{grid}{_footer(f"{len(cells)} structures")}</div>')
    return _doc(f"{sym} — Matrix strategy grid", "strategy_grid", inner, width=640)


def _render_bias_chip(snap: dict, trust: dict | None) -> str:
    sym = str(snap.get("symbol") or "?").upper()
    exp = str(snap.get("expiration") or "")
    bias = snap.get("bias") or {}
    trust = trust or {}
    label = str(bias.get("bias_label") or "—").replace("_", " ")
    score = bias.get("directional_score")
    conf = str(bias.get("confidence") or "")
    try:
        accent = "#34d399" if float(score) >= 60 else ("#fb7185" if float(score) <= -60 else "#fbbf24")
    except (TypeError, ValueError):
        accent = _DIM
    rows = "".join([
        _kv("Bias", f'<span style="color:{accent};">{_esc(label.upper())}</span>'),
        _kv("Confidence", _esc(conf)),
        _kv("Net GEX", _num(bias.get("net_gex"), 0)),
        _kv("Trust", _esc(trust.get("display_text") or "—")),
    ])
    hint = trust.get("hint_text") if trust.get("show_hint") else ""
    hint_html = (f'<div style="margin-top:12px;color:{_DIM};font-size:13px;line-height:1.5;">'
                 f'{_esc(hint)}</div>') if hint else ""
    inner = (_header(sym, exp, right_top=_num(score, 0), right_sub="bias score",
                     accent=accent, eyebrow="BIAS") +
             f'  <div style="padding:18px 22px;">'
             f'<table style="border-collapse:collapse;width:100%;">{rows}</table>'
             f'{hint_html}{_footer()}</div>')
    return _doc(f"{sym} — Matrix bias", "bias_chip", inner)


def _render_walls_chip(snap: dict) -> str:
    sym = str(snap.get("symbol") or "?").upper()
    exp = str(snap.get("expiration") or "")
    bias = snap.get("bias") or {}
    spot = snap.get("spot")
    rows = "".join([
        _kv("Spot", _num(spot, 2)),
        _kv("Call wall", f'{_num(bias.get("call_wall_strike"), 0)} '
                         f'<span style="color:{_MUTE};">{_esc(bias.get("call_wall_strength") or "")}</span>'),
        _kv("Put wall", f'{_num(bias.get("put_wall_strike"), 0)} '
                        f'<span style="color:{_MUTE};">{_esc(bias.get("put_wall_strength") or "")}</span>'),
        _kv("VEX wall", _num(bias.get("vex_wall_strike"), 0)),
        _kv("TEX wall", _num(bias.get("tex_wall_strike"), 0)),
        _kv("Expected move", _num(snap.get("expected_move"), 2)),
    ])
    inner = (_header(sym, exp, eyebrow="KEY LEVELS", accent="#38bdf8") +
             f'  <div style="padding:18px 22px;">'
             f'<table style="border-collapse:collapse;width:100%;">{rows}</table>{_footer()}</div>')
    return _doc(f"{sym} — Matrix key levels", "walls_chip", inner)


def _render_win_eval_grid(snap: dict, trust: dict | None, stats: dict | None) -> str:
    sym = str(snap.get("symbol") or "?").upper()
    exp = str(snap.get("expiration") or "")
    trust, stats = trust or {}, stats or {}
    tier = str(trust.get("tier") or stats.get("tier") or "unknown")
    accent = _TREND.get(tier, _DIM)
    wr = trust.get("win_rate")
    wr_s = "—" if wr is None else f"{float(wr):.0f}%"
    n = int(stats.get("n") or trust.get("graded") or 0)
    w = int(stats.get("wins") or 0)
    l = int(stats.get("losses") or 0)
    nu = int(stats.get("neutrals") or 0)
    # W/L/N spelled out: a bare percentage hides that neutrals sit in the
    # denominator, which is exactly the honesty the self-eval exists for.
    rows = "".join([
        _kv("Record", f'<span style="color:#34d399;">{w}W</span> · '
                      f'<span style="color:#fb7185;">{l}L</span> · '
                      f'<span style="color:{_DIM};">{nu}N</span>'),
        _kv("Graded picks", str(n)),
        _kv("State", _esc(str(trust.get("state") or "").replace("_", " "))),
    ])
    note = trust.get("display_text") or ""
    inner = (_header(sym, exp, right_top=wr_s, right_sub="win rate",
                     accent=accent, eyebrow="SELF-EVALUATION") +
             f'  <div style="padding:18px 22px;">'
             f'<table style="border-collapse:collapse;width:100%;">{rows}</table>'
             f'<div style="margin-top:12px;color:{_DIM};font-size:13px;">{_esc(note)}</div>'
             f'{_footer("one result per pick")}</div>')
    return _doc(f"{sym} — Matrix self-evaluation", "win_eval_grid", inner)


def render_snap_card(snap: dict, view: str = "engine_pick", *,
                     trust: dict | None = None, stats: dict | None = None) -> str:
    """Render one crop view. `trust`/`stats` come from the accuracy surface and
    are only needed by `bias_chip` and `win_eval_grid`."""
    if view == "strategy_grid":
        return _render_strategy_grid(snap)
    if view == "bias_chip":
        return _render_bias_chip(snap, trust)
    if view == "walls_chip":
        return _render_walls_chip(snap)
    if view == "win_eval_grid":
        return _render_win_eval_grid(snap, trust, stats)
    return _render_engine_pick(snap)

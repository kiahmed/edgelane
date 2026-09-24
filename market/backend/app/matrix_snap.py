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


# ── Keeping the two worlds apart ────────────────────────────────────────────
#
# Matrix has two independent opinions and the cards must never blur them:
#
#   ENGINE  — the picked strategy, its composite (0–100), and the graded record
#             of the engine's picks. Cards: engine_pick, bias_chip (a pick card
#             for the bias posts), strategy_grid, win_eval_grid.
#   MARKET  — the bias engine's direction read (signed −100…+100 from dealer
#             walls), its confidence, GEX, and the walls. Card: walls_chip only
#             (the session_open "market read").
#
# The composite never uses the market read. Mixing them on one card puts a
# number or a word in front of the reader that they cannot trace back to the
# thing the post is about.

def _engine_state(trust: dict) -> str:
    """The engine's own state for its record — paused | calibrating | active.

    The accuracy route also has `low_conf`, but that comes from the BIAS
    engine's confidence, not from how the picks have graded, so on an engine
    card it is simply "active"."""
    st = str((trust or {}).get("state") or "")
    return st if st in ("paused", "calibrating") else ("active" if st else "")


def _record_line(trust: dict, stats: dict | None = None) -> str:
    """The engine's graded record in words — built from engine facts only.

    Not `trust["display_text"]`: that string appends "— lower-conviction read"
    when the BIAS confidence is low, which is market verbiage on an engine card."""
    trust, stats = trust or {}, stats or {}
    st = _engine_state(trust)
    n = int(stats.get("n") or trust.get("graded") or 0)
    if st == "paused":
        return "Paused — recovering from a losing streak"
    if st == "calibrating":
        return f"Calibrating — {n} graded so far"
    wr = trust.get("win_rate")
    if wr is None:
        return "—"
    return f"{float(wr):.0f}% win rate ({n} graded picks)"


def _gex(v: Any) -> str:
    """Net GEX in readable units — a raw 31635942118938 means nothing to a reader."""
    try:
        x = float(v)
    except (TypeError, ValueError):
        return "—"
    sign = "−" if x < 0 else "+"
    x = abs(x)
    for div, unit in ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "K")):
        if x >= div:
            return f"{sign}{x / div:.1f}{unit}"
    return f"{sign}{x:.0f}"


def _bias_strength(score: Any) -> tuple[str, str, str]:
    """(headline, sub-label, direction) for the market-read gauge.

    The directional score is signed (−100 max bearish … +100 max bullish). A raw
    red "−90" reads like a failing grade, so the headline is the STRENGTH and the
    direction goes underneath: "90" / "bearish · of 100"."""
    try:
        x = float(score)
    except (TypeError, ValueError):
        return "—", "direction", ""
    direction = "bearish" if x < 0 else ("bullish" if x > 0 else "neutral")
    return f"{abs(x):.0f}", f"{direction} · of 100", direction


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
    """Card for the bias_aligned / bias_diverged posts.

    These posts are about the PICK ("the bias now agrees with the engine's
    pick"), so every number on the card is the picked strategy's own — the
    composite as the headline and the same stats the strategy-grid card shows
    (structure, Net / Max P / Max L / POP / EV, health, liquidity, verdict) —
    plus the engine's graded record on its picks.

    Market-direction data (the bias engine's wall-based score, its label and
    confidence, GEX, the walls themselves) is deliberately absent: the
    composite never uses it, and a reader can't connect it to anything on the
    card. That belongs only on a post that is ABOUT direction — the walls card.
    """
    sym = str(snap.get("symbol") or "?").upper()
    exp = str(snap.get("expiration") or "")
    pick = snap.get("engine_pick") or {}
    trust = trust or {}
    if not pick:
        return _render_engine_pick(snap)              # "no pick" card, same crop semantics
    health = str(pick.get("health") or "")
    accent = _HEALTH_COLOR.get(health, "#34d399")
    name = _STRATEGY_NAMES.get(str(pick.get("strategy") or ""), pick.get("name") or "—")
    verdict = pick.get("composite_verdict") or {}
    vlabel = verdict.get("label") if isinstance(verdict, dict) else ""
    pills = " · ".join(x.upper() for x in (health, f"liq {pick.get('liquidity')}"
                                          if pick.get("liquidity") else "", vlabel or "") if x)
    rows = "".join([
        _kv("Structure", _esc(pick.get("structure_text"))),
        _kv("Net", _money(pick.get("net_premium"))),
        _kv("Max P / Max L", f"{_money(pick.get('max_profit'))} / {_money(pick.get('max_loss'))}"),
        _kv("POP", f"{_num(pick.get('pop_pct'), 1)}%"),
        _kv("EV / Adj EV", f"{_money(pick.get('ev'))} / {_money(pick.get('ev_adjusted'))}"),
        _kv("Engine record", _esc(_record_line(trust))),
    ])
    sub = (f'<div style="color:{_DIM};font-size:13px;margin-bottom:12px;">'
           f'{_esc(name)} · {_esc(pick.get("label") or "")} · '
           f'<span style="color:{accent};">{_esc(pills)}</span></div>')
    inner = (_header(sym, exp, right_top=_num(pick.get("composite_score"), 1),
                     right_sub="composite", accent=accent, eyebrow="ENGINE PICK") +
             f'  <div style="padding:18px 22px;">{sub}'
             f'<table style="border-collapse:collapse;width:100%;">{rows}</table>'
             f'{_footer()}</div>')
    return _doc(f"{sym} — Matrix engine pick", "bias_chip", inner)


def _render_walls_chip(snap: dict) -> str:
    """The MARKET READ — the session_open card, and the only card that carries
    market-direction data: the bias engine's direction and strength, its
    confidence, net GEX, and the walls. Nothing about the engine's pick."""
    sym = str(snap.get("symbol") or "?").upper()
    exp = str(snap.get("expiration") or "")
    bias = snap.get("bias") or {}
    headline, sub, direction = _bias_strength(bias.get("directional_score"))
    accent = {"bearish": "#fb7185", "bullish": "#34d399"}.get(direction, "#fbbf24")
    label = str(bias.get("bias_label") or "—").replace("_", " ")

    def _wall(k: str, strength_key: str | None = None) -> str:
        v = _num(bias.get(k), 0)
        st = bias.get(strength_key) if strength_key else None
        return v + (f' <span style="color:{_MUTE};">{_esc(st)}</span>' if st else "")

    rows = "".join([
        _kv("Direction", f'<span style="color:{accent};">{_esc(label.upper())}</span>'),
        _kv("Confidence", _esc(str(bias.get("confidence") or "—"))),
        _kv("Spot", _num(snap.get("spot"), 2)),
        _kv("Call wall", _wall("call_wall_strike", "call_wall_strength")),
        _kv("Put wall", _wall("put_wall_strike", "put_wall_strength")),
        _kv("VEX / TEX wall", f'{_num(bias.get("vex_wall_strike"), 0)} / '
                              f'{_num(bias.get("tex_wall_strike"), 0)}'),
        _kv("Expected move", f'±{_num(snap.get("expected_move"), 2)}'),
        _kv("Net GEX", _gex(bias.get("net_gex"))),
    ])
    inner = (_header(sym, exp, right_top=headline, right_sub=sub,
                     accent=accent, eyebrow="MARKET READ") +
             f'  <div style="padding:18px 22px;">'
             f'<table style="border-collapse:collapse;width:100%;">{rows}</table>'
             f'{_footer("dealer positioning")}</div>')
    return _doc(f"{sym} — Matrix market read", "walls_chip", inner)


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
        _kv("State", _esc(_engine_state(trust))),
    ])
    note = _record_line(trust, stats)
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

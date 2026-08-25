"""Readiness-alert email body — mirrors the ready ticker card (strikes, credit,
POP, EV, alpha, management targets), so the email is self-contained: a seller can
act without opening the app. Pure formatting; no I/O. `render_readiness_email`
returns (subject, html)."""
from __future__ import annotations

import html
from typing import Any, Optional


def _esc(x: Any) -> str:
    """HTML-escape any dynamic string before interpolation. Cheap insurance so a
    future field carrying user/LLM text (rationale, veto reasons) can never break
    out of the markup — the safety invariant lives here, not at a distance."""
    return html.escape("" if x is None else str(x), quote=True)


_STRUCTURE_NAMES = {
    "bull_put": "Bull Put Spread",
    "bear_call": "Bear Call Spread",
    "iron_condor": "Iron Condor",
}


def _num(x: Any) -> Optional[float]:
    try:
        return float(x) if x is not None else None
    except (TypeError, ValueError):
        return None


def _money(x: Any, dp: int = 2) -> str:
    v = _num(x)
    return "—" if v is None else f"${v:,.{dp}f}"


def _pct(x: Any, dp: int = 0) -> str:
    v = _num(x)
    return "—" if v is None else f"{v * 100:.{dp}f}%"


def _strike(x: Any) -> str:
    v = _num(x)
    if v is None:
        return "—"
    return f"{v:.0f}" if abs(v - round(v)) < 1e-6 else f"{v:g}"


def _structure_name(s: Optional[str]) -> str:
    return _STRUCTURE_NAMES.get(str(s or ""), str(s or "Spread").replace("_", " ").title())


def _strikes_line(env: dict) -> str:
    """The '5400 / 5350 · 50 wide' (vertical) or 'P … · C …' (condor) line."""
    st = env.get("strikes") or {}
    if "put" in st and "call" in st:                       # iron condor
        p, c = st.get("put") or {}, st.get("call") or {}
        return (f"P {_strike(p.get('short'))}/{_strike(p.get('long'))} · "
                f"C {_strike(c.get('short'))}/{_strike(c.get('long'))}")
    short, long, width = st.get("short"), st.get("long"), st.get("width")
    if short is None:                                      # legs nested under a side
        cand = env.get("candidate") or {}
        short, long, width = cand.get("k_short"), cand.get("k_long"), cand.get("width")
    w = f" · {_strike(width)} wide" if width is not None else ""
    return f"{_strike(short)} / {_strike(long)}{w}"


def _row(label: str, value: str) -> str:
    return (f'<tr>'
            f'<td style="padding:4px 12px 4px 0;color:#94a3b8;font-size:13px;">{label}</td>'
            f'<td style="padding:4px 0;color:#e2e8f0;font-size:13px;font-family:'
            f'ui-monospace,SFMono-Regular,Menlo,monospace;">{value}</td>'
            f'</tr>')


def render_readiness_email(env: dict, app_url: str = "") -> tuple[str, str]:
    symbol = str(env.get("symbol") or "?").upper()
    expiration = str(env.get("expiration") or "")
    score = _num(env.get("score"))
    score_s = "—" if score is None else f"{score:.0f}"
    structure = _structure_name(env.get("structure"))

    cand = env.get("candidate") or {}
    pop_fc = cand.get("pop_breakeven_forecast")
    mgmt = env.get("management") or {}
    regime = (env.get("regime") or {}).get("state")
    earn = env.get("earnings") or None

    # subject is a plain-text header (not HTML) — no escaping there. Everything
    # interpolated into the HTML body below goes through _esc().
    subject = f"Simmer · {symbol} ready to sell — {structure} ({score_s}/100)"
    sym_h, exp_h, struct_h = _esc(symbol), _esc(expiration), _esc(structure)

    pop_cell = _pct(env.get("pop_breakeven"))
    if _num(pop_fc) is not None:
        pop_cell += f' <span style="color:#64748b;">/ {_pct(pop_fc)} fc</span>'

    alpha = _num(env.get("alpha"))
    alpha_cell = "—" if alpha is None else f"{alpha * 100:.2f}%"

    rows = "".join([
        _row("Expiry", f'<strong>{exp_h or "—"}</strong>'),
        _row("Structure", f"{struct_h} &nbsp; {_strikes_line(env)}"),
        _row("Credit", f'{_money(env.get("credit_fill"))} achievable '
                       f'<span style="color:#64748b;">· {_money(env.get("credit_mid"))} '
                       f'advertised</span>'),
        _row("Max loss", _money(env.get("max_loss"))),
        _row("POP", pop_cell),
        _row("EV / share", _money(env.get("expected_value"), 3)),
        _row("Alpha (EV/risk)", alpha_cell),
    ])

    manage_line = (
        f'Take profit at {_pct(_num(mgmt.get("profit_target_pct", 0)) / 100.0)} of max · '
        f'manage at {int(_num(mgmt.get("manage_dte")) or 0)} DTE · '
        f'stop at {_num(mgmt.get("stop_credit_multiple")) or 0:g}× credit'
    )

    earn_html = ""
    if earn and earn.get("in_window"):
        direction = _esc(earn.get("direction") or "neutral")
        conf = _pct(earn.get("confidence"))
        cbp = earn.get("close_before_print")
        earn_html = (
            f'<div style="margin-top:14px;padding:10px 12px;background:#422006;'
            f'border-radius:8px;color:#fdba74;font-size:12px;line-height:1.5;">'
            f'⚠ <strong>Earnings in the tenor.</strong> Analyzer read: {direction}, '
            f'confidence {conf}. '
            + ("This is a <strong>sell-the-run-up, CLOSE BEFORE THE PRINT</strong> "
               "play — never hold through the announcement." if cbp else
               "Held back — do not sell through the print.")
            + '</div>'
        )

    cta = ""
    if app_url:
        cta = (
            f'<div style="margin-top:20px;">'
            f'<a href="{_esc(app_url)}" style="display:inline-block;background:#059669;'
            f'color:#ffffff;text-decoration:none;padding:10px 18px;border-radius:8px;'
            f'font-size:13px;font-weight:600;">Open in Simmer →</a></div>'
        )

    regime_line = (f'<span style="color:#64748b;">regime: {_esc(str(regime).replace("_", " "))}</span>'
                   if regime else "")

    html = f"""\
<div style="background:#0f172a;padding:24px;font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;">
  <div style="max-width:520px;margin:0 auto;background:#1e293b;border:1px solid #334155;border-radius:12px;overflow:hidden;">
    <div style="padding:18px 20px;border-bottom:1px solid #334155;display:flex;align-items:center;justify-content:space-between;">
      <div>
        <div style="color:#f1f5f9;font-size:20px;font-weight:700;">{sym_h}
          <span style="color:#cbd5e1;font-size:14px;font-weight:600;">&nbsp;exp {exp_h}</span>
        </div>
        <div style="color:#34d399;font-size:12px;font-weight:600;letter-spacing:.05em;margin-top:2px;">READY TO SELL</div>
      </div>
      <div style="text-align:right;">
        <div style="color:#34d399;font-size:26px;font-weight:800;font-family:ui-monospace,monospace;">{score_s}</div>
        <div style="color:#64748b;font-size:11px;">/ 100</div>
      </div>
    </div>
    <div style="padding:18px 20px;">
      <table style="border-collapse:collapse;width:100%;">{rows}</table>
      <div style="margin-top:14px;padding-top:12px;border-top:1px solid #334155;color:#94a3b8;font-size:12px;line-height:1.5;">
        <strong style="color:#cbd5e1;">Manage:</strong> {manage_line}
      </div>
      {earn_html}
      <div style="margin-top:14px;color:#64748b;font-size:11px;">{regime_line}</div>
      {cta}
    </div>
  </div>
  <div style="max-width:520px;margin:14px auto 0;color:#475569;font-size:11px;line-height:1.5;">
    You're receiving this because readiness-alert email is on in your Simmer settings.
    Simmer decides WHEN a name is conditioned to sell premium; it is not trade advice —
    size and manage your own risk.
  </div>
</div>"""
    return subject, html

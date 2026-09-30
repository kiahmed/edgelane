"""Matrix daily summary email — the day's health and performance at a glance.

After the close (``matrix_daily_summary_at``, default 4:15 PM ET, once the last
picks have been graded) the engine builds one short report from its OWN
self-evaluation data — the Matrix tables ``bias_decisions`` + ``outcomes`` —
and emails it to every Matrix user who switched "Daily summary email" on.

What it says, per symbol:

  * right / wrong / flat across the day's picks, and "right when it called it"
    (wins ÷ decided picks — flats aren't a call either way);
  * how many times the engine PAUSED itself (a losing streak) and recovered;
  * a two-way table — strategy × win / loss / flat — so the reader sees at a
    glance which structures carried the day;
  * whether the engine watched the whole session (longest polling gap).

A pick is counted the way the win rate counts it: one result per pick (a run of
polls holding the same legs, graded by its last grade), flickers dropped.

Recipients are Matrix users only — ``profiles.tools_enabled`` contains
``market`` AND ``user_settings.notifications.email_daily_summary`` is true.
Opt-in; the default is off.

Sent at most once per ET session: the outcome is recorded in
``matrix_daily_email_log`` (DuckDB) before anything else can re-trigger it.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, time, timedelta, timezone
from html import escape
from typing import Any
from zoneinfo import ZoneInfo

from . import emailer, supabase_admin

log = logging.getLogger("edgelane.matrix.daily_summary")

_ET = ZoneInfo("America/New_York")
MATRIX_TOOL = "market"
OPT_IN_KEY = "email_daily_summary"

_NAMES = {
    "bull_put": "Bull Put", "bear_call": "Bear Call", "iron_condor": "Iron Condor",
    "iron_butterfly": "Iron Butterfly", "bull_call": "Bull Call", "bear_put": "Bear Put",
    "call_butterfly": "Call Fly", "put_butterfly": "Put Fly",
}


def _name(strategy: str) -> str:
    return _NAMES.get(strategy, str(strategy or "unknown").replace("_", " ").title())


def _session_bounds(session_date: date) -> tuple[datetime, datetime]:
    start = datetime.combine(session_date, time(0, 0), tzinfo=_ET)
    return start.astimezone(timezone.utc), (start + timedelta(days=1)).astimezone(timezone.utc)


def _replay_pauses(results: list[str], alert_after: int, clear_after: int) -> dict:
    """Run the day's picks through the SAME pause/recover state machine the
    engine uses (evaluator._update_regime / rehydrate_regime): N losing picks in
    a row pause it, M winning picks in a row lift the pause, flats move
    neither counter."""
    losses = wins = pauses = recoveries = worst_streak = 0
    paused = False
    for r in results:
        if r == "loss":
            losses += 1
            wins = 0
            worst_streak = max(worst_streak, losses)
            if losses >= alert_after and not paused:
                paused = True
                pauses += 1
        elif r == "win":
            wins += 1
            losses = 0
            if paused and wins >= clear_after:
                paused = False
                recoveries += 1
    return {"pauses": pauses, "recoveries": recoveries,
            "longest_losing_streak": worst_streak, "ended_paused": paused}


def build_report(db: Any, settings: Any, session_date: date) -> dict:
    """The day's numbers for every Matrix symbol. Pure read of the Matrix
    tables; sync (DuckDB) — call it off the event loop."""
    start, end = _session_bounds(session_date)
    dwell = int(getattr(settings, "pick_min_dwell_polls", 1) or 1)
    alert_after = int(getattr(settings, "regime_alert_consec_losses", 3))
    clear_after = int(getattr(settings, "regime_clear_consec_wins", 2))

    symbols = []
    for sym in list(getattr(settings, "symbols", []) or []):
        sym = str(sym).upper()
        picks = db.fetch_session_picks(sym, start, end, dwell)
        table: dict[str, dict[str, int]] = {}
        for p in picks:
            row = table.setdefault(_name(p["strategy"]), {"win": 0, "loss": 0, "neutral": 0})
            if p["result"] in row:
                row[p["result"]] += 1
        w = sum(r["win"] for r in table.values())
        l = sum(r["loss"] for r in table.values())
        n = sum(r["neutral"] for r in table.values())
        symbols.append({
            "symbol": sym,
            "picks": len(picks),
            "wins": w, "losses": l, "neutrals": n,
            "right_pct": (round(100.0 * w / (w + l)) if (w + l) else None),
            "table": dict(sorted(table.items(), key=lambda kv: -sum(kv[1].values()))),
            **_replay_pauses([p["result"] for p in picks], alert_after, clear_after),
            "coverage": db.fetch_session_coverage(sym, start, end),
        })
    return {"date": session_date.isoformat(), "symbols": symbols,
            "any_picks": any(s["picks"] for s in symbols)}


# ── email ───────────────────────────────────────────────────────────────────

def _headline(s: dict) -> str:
    if not s["picks"]:
        return f"{s['symbol']} — no picks today."
    right = f" · {s['right_pct']}% right when it called it" if s["right_pct"] is not None else ""
    pause = (f"Paused {s['pauses']}×" + (f", recovered {s['recoveries']}×" if s["pauses"] else "")
             if s["pauses"] else "Never paused")
    return (f"{s['symbol']} — right {s['wins']} · wrong {s['losses']} · flat {s['neutrals']} "
            f"({s['picks']} picks){right}. {pause}.")


def _health(s: dict) -> str:
    c = s.get("coverage") or {}
    if not c.get("polls"):
        return "Engine wasn't polling this symbol today."
    # DuckDB hands back NAIVE LOCAL time (it stores an aware datetime converted
    # to the process's zone), so a naive value is read as local — not stamped
    # as UTC. In the container (TZ=UTC) the two coincide.
    first, last = c["first_ts"].astimezone(_ET), c["last_ts"].astimezone(_ET)
    gap = c.get("max_gap_min") or 0
    flag = f" ⚠ a {gap:.0f}-min gap in coverage" if gap >= 10 else f", longest gap {gap:.0f} min"
    return f"Watched {first:%-I:%M}–{last:%-I:%M %p} ET{flag}."


def render_email(report: dict) -> tuple[str, str]:
    """(subject, html). Short on purpose: a headline per symbol, one small
    strategy × result table, one health line."""
    d = datetime.fromisoformat(report["date"])
    score = " · ".join(f"{s['symbol']} {s['wins']}–{s['losses']}" for s in report["symbols"] if s["picks"])
    subject = f"Matrix daily · {d:%b %-d}" + (f": {score} (right–wrong)" if score else "")

    cell = "padding:4px 10px;border-bottom:1px solid #e5e7eb;text-align:right;"
    blocks = []
    for s in report["symbols"]:
        rows = "".join(
            f"<tr><td style='padding:4px 10px;border-bottom:1px solid #e5e7eb;'>{escape(k)}</td>"
            f"<td style='{cell}color:#047857;'>{v['win']}</td>"
            f"<td style='{cell}color:#b91c1c;'>{v['loss']}</td>"
            f"<td style='{cell}color:#6b7280;'>{v['neutral']}</td></tr>"
            for k, v in s["table"].items())
        table = (f"<table style='border-collapse:collapse;font-size:13px;margin:6px 0 4px;'>"
                 f"<tr style='color:#6b7280;font-size:11px;text-transform:uppercase;'>"
                 f"<th style='padding:4px 10px;text-align:left;'>Strategy</th>"
                 f"<th style='padding:4px 10px;text-align:right;'>Right</th>"
                 f"<th style='padding:4px 10px;text-align:right;'>Wrong</th>"
                 f"<th style='padding:4px 10px;text-align:right;'>Flat</th></tr>{rows}</table>"
                 ) if s["table"] else ""
        blocks.append(
            f"<div style='margin:0 0 18px;'>"
            f"<div style='font-size:15px;font-weight:600;color:#111827;'>{escape(_headline(s))}</div>"
            f"{table}"
            f"<div style='font-size:12px;color:#6b7280;'>{escape(_health(s))}</div></div>")

    html = f"""<div style="font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;max-width:560px;color:#111827;">
  <div style="font-size:12px;letter-spacing:.08em;color:#059669;font-weight:700;">MATRIX · DAILY SUMMARY</div>
  <div style="font-size:13px;color:#6b7280;margin:2px 0 16px;">{d:%A, %B %-d, %Y}</div>
  {''.join(blocks)}
  <div style="font-size:11px;color:#9ca3af;border-top:1px solid #e5e7eb;padding-top:10px;">
    One result per pick, graded by how it ended. "Right when it called it" leaves flat picks out.
    A snapshot of engine performance, not advice.<br>
    You're getting this because <b>Daily summary email</b> is on in your Matrix settings — switch it off there anytime.
  </div>
</div>"""
    return subject, html


# ── recipients: Matrix users who opted in ───────────────────────────────────

async def recipients() -> list[str] | None:
    """Emails of Matrix users (tool ``market``) with the daily summary switched
    on. None = couldn't be determined (Supabase unreachable) — the caller
    retries later rather than treating it as "nobody"."""
    ids = await supabase_admin.get_users_with_tool(MATRIX_TOOL)
    if not ids:
        return []
    rows = await supabase_admin.select_many(
        "user_settings", "user_id,notifications",
        filters={"user_id": f"in.({','.join(ids)})"})
    if rows is None:
        return None
    opted = [r["user_id"] for r in rows
             if isinstance(r.get("notifications"), dict)
             and r["notifications"].get(OPT_IN_KEY) is True]
    emails: list[str] = []
    for uid in opted:
        e = await supabase_admin.get_user_email(uid)
        if e and e not in emails:
            emails.append(e)
    return emails


# ── the trigger ─────────────────────────────────────────────────────────────

_state: dict = {"inflight": False, "handled": None, "task": None}   # handled = ET date done


def _send_time(settings: Any) -> time:
    try:
        hh, mm = str(getattr(settings, "matrix_daily_summary_at", "16:15")).split(":")
        return time(int(hh), int(mm))
    except (TypeError, ValueError):
        return time(16, 15)


def due(settings: Any, now: datetime | None = None) -> date | None:
    """The ET session date to report on, if it's time and not done; else None."""
    # A dev instance (DEVMODE — sandbox Tradier, sandbox DB) never sends: the
    # recipient list is the REAL Supabase user base (not DEVMODE-split) and the
    # once-per-day record lives in the per-instance DuckDB, so a `make run-dev`
    # left running past 4:15 would email real users a second summary built from
    # sandbox data.
    if getattr(settings, "devmode", False):
        return None
    now = (now or datetime.now(timezone.utc)).astimezone(_ET)
    if now.weekday() >= 5 or now.time() < _send_time(settings):
        return None
    if _state["handled"] == now.date():
        return None
    return now.date()


async def send_for(db: Any, settings: Any, session_date: date) -> str:
    """Build, send, and record the summary for one session. Returns a short
    status for logs/tests. Never raises."""
    try:
        if await asyncio.to_thread(db.matrix_daily_email_handled, session_date):
            _state["handled"] = session_date
            return "already handled"
        report = await asyncio.to_thread(build_report, db, settings, session_date)
        if not report["any_picks"]:
            # A holiday or an outage: nothing graded, nothing to say.
            await asyncio.to_thread(db.log_matrix_daily_email, session_date, "skipped", 0,
                                    "no graded picks")
            _state["handled"] = session_date
            log.info("[matrix-daily] %s skipped — no graded picks", session_date)
            return "skipped"
        rcpts = await recipients()
        if rcpts is None:
            log.warning("[matrix-daily] %s recipients unknown (Supabase) — will retry", session_date)
            return "retry"
        subject, html = render_email(report)
        # Mark it handled BEFORE the first email goes out. From here on, nothing
        # — a send raising halfway, the record write failing — may make tick()
        # try again: that would re-send to everyone every ~30s for the rest of
        # the day. A partial or unrecorded send is logged, never retried.
        _state["handled"] = session_date
        sent = 0
        for addr in rcpts:
            try:
                if await emailer.send_email(addr, subject, html,
                                            from_email=getattr(settings, "matrix_from_email", None)):
                    sent += 1
            except Exception:
                log.exception("[matrix-daily] send to one recipient failed")
        try:
            await asyncio.to_thread(db.log_matrix_daily_email, session_date, "sent", sent,
                                    f"{sent}/{len(rcpts)} delivered")
        except Exception:
            # Still handled in memory for this process. Only a restart before
            # midnight could send again — once, not in a loop.
            log.exception("[matrix-daily] %s sent but NOT recorded", session_date)
        log.info("[matrix-daily] %s sent to %d/%d", session_date, sent, len(rcpts))
        return f"sent {sent}/{len(rcpts)}"
    except Exception:
        log.exception("[matrix-daily] summary for %s failed", session_date)
        return "error"


def tick(db: Any, settings: Any) -> None:
    """Called every evaluator sweep (~30s). Cheap: a clock check, and at most
    one background task at a time — never blocks the sweep."""
    sd = due(settings)
    if sd is None or _state["inflight"]:
        return

    async def _run() -> None:
        _state["inflight"] = True
        try:
            await send_for(db, settings, sd)
        finally:
            _state["inflight"] = False

    # Hold a reference: asyncio only weakly references tasks.
    _state["task"] = asyncio.create_task(_run())

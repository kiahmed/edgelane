"""Continuous polling loop for EdgeLane MARKET.

Started by main.py lifespan as an `asyncio.Task`. For each configured symbol
every `POLL_INTERVAL_SEC` seconds:

  1. Check market hours (America/New_York, 09:30-16:00 weekdays). If closed,
     sleep (5 min) and re-check — don't burn API quota off-hours.
  2. Fetch spot quote + nearest expiration + full options chain (with greeks).
  3. Normalize raw Tradier contracts -> internal shape used by the math layer.
  4. Run `engine.compute_engine_output(...)`.
  5. Persist per-strike GEX snapshot rows + a bias_decision (if a pick exists).
  6. Cache the engine output in module-level PollerState keyed by symbol;
     /snapshot endpoint reads from here.

Errors from a single symbol's poll are logged and stored on PollerState.last_error
but do NOT crash the loop — Tradier 429s, transient HTTP failures, and parsing
errors are all swallowed per-symbol with a retry on the next interval.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import date, datetime, time, timezone
from typing import Any
from zoneinfo import ZoneInfo

log = logging.getLogger("edgelane.market.poller")


class PollerState:
    """In-memory snapshot cache + polling status. Single instance per process."""

    def __init__(self) -> None:
        self.latest_by_symbol: dict[str, dict] = {}
        self.last_poll_at: dict[str, str] = {}  # ISO UTC per symbol
        self.last_error: dict[str, str] = {}
        self.is_polling: bool = False
        self.market_open: bool = False
        self.market_reason: str = "startup"
        # True only while the self-eval should run (market open). Display-only
        # off-hours polling leaves this False so the UI can show "eval paused".
        self.evaluation_active: bool = False
        self.last_loop_at: str | None = None
        # Tradier-specific health surface used by /status
        self.last_tradier_success_at: str | None = None
        self.last_tradier_error: str | None = None
        self.last_tradier_error_at: str | None = None


# Module-level singleton — routes import this directly.
state = PollerState()


def _is_market_open(tz_name: str = "America/New_York", now: datetime | None = None) -> tuple[bool, str]:
    tz = ZoneInfo(tz_name)
    n = now or datetime.now(tz)
    if n.weekday() >= 5:
        return False, f"weekend ({n.strftime('%A')})"
    t = n.time()
    if t < time(9, 30):
        mins = (9 * 60 + 30) - (t.hour * 60 + t.minute)
        return False, f"pre_market (opens in {mins}m ET)"
    if t >= time(16, 0):
        return False, "after_hours"
    return True, "open"


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _normalize_tradier_contract(o: dict) -> dict:
    """Tradier raw option shape -> internal contract shape consumed by the
    dealer-exposure aggregator and strategy engine."""
    g = o.get("greeks") or {}

    def f(v: Any, default: float = 0.0) -> float:
        if v is None or v == "":
            return default
        try:
            return float(v)
        except (TypeError, ValueError):
            return default

    def i(v: Any, default: int = 0) -> int:
        if v is None or v == "":
            return default
        try:
            return int(v)
        except (TypeError, ValueError):
            try:
                return int(float(v))
            except (TypeError, ValueError):
                return default

    return {
        "symbol": o.get("symbol"),
        "strike": f(o.get("strike")),
        "side": "call" if (o.get("option_type") or "").lower() == "call" else "put",
        "expiration": o.get("expiration_date"),
        "delta": f(g.get("delta")),
        "gamma": f(g.get("gamma")),
        "theta": f(g.get("theta")),
        "vega":  f(g.get("vega")),
        "iv":    f(g.get("mid_iv") or g.get("smv_vol")),
        "open_interest": i(o.get("open_interest")),
        "volume":        i(o.get("volume")),
        "bid":  f(o.get("bid")),
        "ask":  f(o.get("ask")),
        "last": f(o.get("last")),
    }


def _contract_mid(c: dict) -> float | None:
    """Mid of a contract from bid/ask (averaged) or an explicit mid; None if
    neither is usable. Mirrors strategy_engine._get_mid semantics."""
    m = c.get("mid")
    if m not in (None, ""):
        try:
            return float(m)
        except (TypeError, ValueError):
            pass
    try:
        bid = float(c.get("bid") or 0.0)
        ask = float(c.get("ask") or 0.0)
    except (TypeError, ValueError):
        return None
    if bid <= 0 and ask <= 0:
        return None
    return (bid + ask) / 2.0


def _quote_map(contracts: list[dict]) -> dict[str, dict]:
    """Map OCC option symbol → {bid, ask, mid} for evaluator re-pricing."""
    out: dict[str, dict] = {}
    for c in contracts:
        sym = c.get("symbol")
        if not sym:
            continue
        try:
            bid = float(c.get("bid") or 0.0)
            ask = float(c.get("ask") or 0.0)
        except (TypeError, ValueError):
            bid = ask = 0.0
        out[sym] = {"bid": bid, "ask": ask, "mid": _contract_mid(c)}
    return out


def _compute_dte_fraction(exp_iso: str) -> float:
    """DTE in days. Same-day expiration -> 0.5 (intraday gate).
    Future expirations -> integer days + 0.5 (mid-day execution assumption)."""
    try:
        exp_date = date.fromisoformat(exp_iso)
    except (TypeError, ValueError):
        return 1.0
    today = date.today()
    diff = (exp_date - today).days
    if diff <= 0:
        return 0.5
    return float(diff) + 0.5


async def poll_symbol(symbol: str, tradier_client, db, settings, persist: bool = True) -> dict:
    """Single poll cycle for one symbol. Returns engine output dict on success.
    Raises on hard failure (caller logs + stores in PollerState.last_error).

    `persist=False` (display-only, market-closed) skips writing gex_snapshots /
    bias_decisions so the self-eval has nothing frozen to score."""
    from .engine import compute_engine_output

    # 1. spot quote
    quote = await tradier_client.stock_quote(symbol)
    spot = 0.0
    for key in ("last", "close", "prevclose"):
        v = quote.get(key) if quote else None
        if v not in (None, ""):
            try:
                spot = float(v)
                if spot > 0:
                    break
            except (TypeError, ValueError):
                continue
    if not spot:
        raise RuntimeError(f"no spot price for {symbol}")

    # 2. expirations (returns list[str] from TradierClient).
    # INDEX SPEC: always 0DTE — prefer today's expiration if Tradier offers it
    # (SPX/NDX/RUT all have daily expirations Mon-Fri). Fall back to the nearest
    # future expiration only when today isn't listed (weekend, holiday, or a
    # cash-settled index without a same-day expiry). The legacy multi-day
    # barrier model is deliberately NOT used for indices — they always route
    # through the EdgelaneProvider asymmetric scorer (dte<1.5 gate in bias_engine).
    exps = await tradier_client.option_expirations(symbol)
    if not exps:
        raise RuntimeError(f"no expirations for {symbol}")
    today_iso = date.today().isoformat()
    exps_sorted = sorted(str(e) for e in exps)
    if today_iso in exps_sorted:
        chosen_exp = today_iso
    else:
        # Pick nearest future expiration (will be 1DTE on most weekends/holidays).
        future = [e for e in exps_sorted if e >= today_iso]
        chosen_exp = future[0] if future else exps_sorted[0]

    # 3. chain (returns list[dict] of raw Tradier options)
    raw = await tradier_client.options_chain(symbol, chosen_exp)
    if not raw:
        raise RuntimeError(f"empty chain for {symbol}@{chosen_exp}")
    contracts = [_normalize_tradier_contract(o) for o in raw if o.get("strike") is not None]

    # 4. dte
    dte = _compute_dte_fraction(chosen_exp)

    # 5. engine — resolve this symbol's debit strike profile (DB-backed, hot:
    # edits via /strike-profiles take effect next poll cycle).
    from .strike_profiles import resolve_profile
    strike_profile = resolve_profile(db, symbol)
    output = compute_engine_output(symbol, spot, contracts, chosen_exp, dte,
                                   strike_profile=strike_profile)

    # Attach a per-OCC-symbol quote map so the evaluator can re-price the saved
    # pick legs against THIS (later) snapshot without re-fetching the chain.
    # Engine output itself is left math-pure (parity); this is a poller add-on.
    output["_quote_by_symbol"] = _quote_map(contracts)

    # 6. persist — DuckDB ops are sync; run in default executor so we don't
    # block the asyncio loop on disk I/O. Skipped for display-only polls.
    output["market_open"] = state.market_open
    output["evaluation_active"] = persist
    if persist:
        try:
            await asyncio.to_thread(_persist_snapshot, db, symbol, chosen_exp, spot, output)
        except Exception as e:
            log.warning("persist failed for %s: %s", symbol, e)

    return output


def _persist_snapshot(db, symbol: str, expiration: str, spot: float, output: dict) -> None:
    """Write per-strike gex_snapshots rows + a bias_decisions row. Best-effort."""
    bias = output.get("bias") or {}
    now = datetime.now(timezone.utc)

    # Per-strike GEX rows — pull from dealer_exposures via re-compute is wasteful;
    # we stored exposures inside the engine output's bias indirectly. Easiest:
    # snapshot just the wall strikes (one row per wall) so /accuracy can trend.
    rows: list[dict] = []
    for label, strike_key, gex_key in (
        ("put_wall", "put_wall_strike", "put_wall_net_gex"),
        ("call_wall", "call_wall_strike", "call_wall_net_gex"),
        ("gex_wall", "gex_wall_strike", None),
    ):
        strike = bias.get(strike_key)
        if strike is None:
            continue
        rows.append({
            "ts": now,
            "symbol": symbol,
            "expiration": expiration,
            "spot": spot,
            "strike": float(strike),
            "call_gex": None,
            "put_gex": None,
            "net_gex": (bias.get(gex_key) if gex_key else bias.get("net_gex")),
            "call_oi": None,
            "put_oi": None,
        })
    for r in rows:
        try:
            db.insert_gex_snapshot(r)
        except Exception as e:
            log.warning("gex_snapshot insert failed: %s", e)

    # Engine-pick capture (spread-outcome eval). Null when no pick → not graded.
    pick = output.get("engine_pick") or {}
    pick_legs = pick.get("legs")
    pick_legs_json = json.dumps(pick_legs) if pick_legs else None

    # Bias decision row
    try:
        decision_id = db.insert_bias_decision({
            "ts": now,
            "symbol": symbol,
            "expiration": expiration,
            "spot_at_decision": spot,
            "score": bias.get("directional_score"),
            "label": bias.get("bias_label"),
            "confidence": bias.get("confidence"),
            "put_wall_strike": bias.get("put_wall_strike"),
            "put_wall_strength": bias.get("put_wall_strength"),
            "put_wall_net_gex": bias.get("put_wall_net_gex"),
            "call_wall_strike": bias.get("call_wall_strike"),
            "call_wall_strength": bias.get("call_wall_strength"),
            "call_wall_net_gex": bias.get("call_wall_net_gex"),
            "recommended_strategies": ",".join(bias.get("recommended_strategies") or []),
            "pick_legs": pick_legs_json,
            "pick_entry_mid": pick.get("net_premium") if pick_legs_json else None,
            "pick_spread_type": pick.get("spread_type") if pick_legs_json else None,
            "pick_strategy": pick.get("strategy") if pick_legs_json else None,
        })
        # attach decision_id back into the engine output so the UI can reference it
        if output.get("engine_pick"):
            output["engine_pick"]["decision_id"] = decision_id
    except Exception as e:
        log.warning("bias_decision insert failed: %s", e)

    # full +/-5% EdgelaneProvider book (external profile only) — additive, own table
    profile = bias.get("_profile_by_strike")
    if profile:
        for r in profile:
            try:
                strike = r.get("strike")
                if strike is None:
                    continue
                db.insert_gex_profile_snapshot({
                    "ts": now, "symbol": symbol, "expiration": expiration,
                    "spot": spot, "strike": float(strike),
                    "net_gex": r.get("net_gex"),
                })
            except Exception as e:
                log.warning("gex_profile insert failed: %s", e)

    # displayed target / sentiment + vol_expectation tag, if present
    ve = bias.get("_vol_expectation") or {}
    if bias.get("_displayed_target") is not None or bias.get("_displayed_sentiment") \
       or ve.get("swing") is not None:
        try:
            db.insert_edgelane_provider_display({
                "ts": now, "symbol": symbol, "spot": spot,
                "magnet_strike": bias.get("gex_wall_strike"),
                "magnet_score": bias.get("directional_score"),
                "displayed_target": bias.get("_displayed_target"),
                "displayed_sentiment": bias.get("_displayed_sentiment"),
                "vol_swing": ve.get("swing"),
                "vol_longterm": ve.get("longterm"),
            })
        except Exception as e:
            log.warning("edgelane_provider_display insert failed: %s", e)


_MODE_DESCRIPTIONS = {
    "live":        "live (market hours only)",
    "live_dev":    "live (dev: poll anytime)",
    "live_forced": "live (FORCE override)",
    "mock":        "mock (synthetic data)",
}


async def poll_loop(tradier_client, db, settings) -> None:
    """Main loop. Sleeps when market closed (unless mode permits off-hours
    polling); polls each symbol on cadence when open. Cancellation-safe —
    clears is_polling on exit.

    The decision "should we poll right now if the market is closed?" is
    resolved upstream into `settings.should_poll_when_closed` (a bool); the
    poller no longer second-guesses it.
    """
    state.is_polling = True
    tz_name = getattr(settings, "market_hours_tz", "America/New_York")
    interval = max(5, int(getattr(settings, "poll_interval_sec", 20)))
    symbols: list[str] = list(getattr(settings, "symbols", []) or [])
    # Prefer the new name, fall back to the legacy name for older settings views.
    poll_when_closed = bool(getattr(settings, "should_poll_when_market_closed",
                                    getattr(settings, "force_poll_when_closed", False)))
    display_when_closed = bool(getattr(settings, "display_when_closed", True))
    closed_interval = max(30, int(getattr(settings, "closed_display_interval_sec", 60)))
    mode = getattr(settings, "tradier_mode", "unknown")
    mode_label = _MODE_DESCRIPTIONS.get(mode, mode)
    log.info("poll loop started — mode=%s symbols=%s interval=%ss tz=%s closed_policy=%s",
             mode_label, symbols, interval,
             tz_name, "poll-anytime" if poll_when_closed else "skip-off-hours")

    try:
        while True:
            try:
                state.last_loop_at = _utc_iso()
                open_, reason = _is_market_open(tz_name)
                state.market_open = open_
                state.market_reason = reason

                if not open_:
                    if poll_when_closed:
                        # Mock/dev/forced: keep polling + persisting regardless
                        # of clock (eval runs against synthetic/forced data).
                        state.evaluation_active = True
                        await _poll_all(symbols, tradier_client, db, settings, persist=True)
                        await asyncio.sleep(interval)
                    elif display_when_closed:
                        # Production-live + after-hours: keep the UI alive by
                        # polling the last-available chain on a slow, courteous
                        # cadence, overlaying fresh EdgelaneProvider walls if present —
                        # but DISPLAY-ONLY: no persistence, self-eval paused.
                        state.evaluation_active = False
                        await _poll_all(symbols, tradier_client, db, settings, persist=False)
                        await asyncio.sleep(closed_interval)
                    else:
                        # Fully idle off-hours: re-check every 5 minutes.
                        state.evaluation_active = False
                        await asyncio.sleep(300)
                    continue

                state.evaluation_active = True
                await _poll_all(symbols, tradier_client, db, settings, persist=True)
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("poll_loop outer error — sleeping 10s then retrying")
                await asyncio.sleep(10)
    finally:
        state.is_polling = False
        log.info("poll loop stopped")


async def _poll_all(symbols: list[str], tradier_client, db, settings, persist: bool = True) -> None:
    for sym in symbols:
        try:
            out = await poll_symbol(sym, tradier_client, db, settings, persist=persist)
            now = _utc_iso()
            state.latest_by_symbol[sym] = out
            state.last_poll_at[sym] = now
            state.last_error.pop(sym, None)
            state.last_tradier_success_at = now
            pick = out.get("engine_pick")
            log.info(
                "poll OK %s spot=%.2f bias=%s score=%.1f pick=%s",
                sym, out.get("spot") or 0,
                (out.get("bias") or {}).get("bias_label"),
                (out.get("bias") or {}).get("directional_score") or 0,
                pick.get("strategy") if pick else "-",
            )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception("poll failed for %s", sym)
            msg = f"{type(e).__name__}: {e}"
            state.last_error[sym] = msg
            # Surface Tradier-specific failures so /status can highlight them
            # separately from generic per-symbol errors.
            if type(e).__name__.startswith("Tradier"):
                state.last_tradier_error = msg
                state.last_tradier_error_at = _utc_iso()

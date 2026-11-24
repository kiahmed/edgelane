"""The tick loop's single step (docs/level_engine.md §3.2, §6, §7, §8, §9, §13, §14).

`TickRunner.process(feed)` = one tick, deterministic given (feed, the level
rows the level source returns, prior runner state):

  feed ts → ET trade_date (new day → reset ratio / level map / brain day-state)
  → session gate (SESSION window ET + feed.market_open)
  → ratio (RatioTracker; live restarts restore the stored locked ratio)
  → levels (rebuild only when the entered rows' content or the ratio changed:
            convert_levels → build_clusters → LevelMap.rebuild; converted rows
            persisted via replace_converted when they differ from the store's)
  → LevelMap.step → lvl_level_state rows
  → map_scenario
  → lvl_ticks row (filtered chain) → brain(TickContext) → persist BrainOutput.

Decisions (spec ambiguities, resolved conservatively)
-----------------------------------------------------
* REBUILD TRIGGER = a content fingerprint of the ENTERED rows (id, symbol,
  price, name, kind) + the ratio value — not `store.levels_version`. The
  version also moves on our own `replace_converted` write (a self-trigger
  loop) and an admin edit landing between our read and our write would be
  swallowed; the fingerprint has neither problem and is identical for the live
  source and the audit-reconstructed one (so warm start → live needs no
  spurious rebuild). `ctx.levels_changed` = the cluster set (ids, prices,
  names, kinds, strikes) differs from the previous tick's — True on the day's
  first tick.
* CONVERTED ROWS are written only by the live runner (`persist_ticks=True`),
  and only when they differ from the converted rows already in the store
  (restart-safe: no audit churn on every boot).
* SESSION: ticks before SESSION open, or with market_open false before the
  day has opened, are ignored (return None, nothing recorded). The CLOSE TICK
  is the first feed with ts >= SESSION close, or market_open false after the
  day had ticks: it is stepped + recorded like any tick, then the brain runs
  with ctx.session_close=True and the day is closed (no more ticks that day).
  If no such feed ever arrives (backend stops polling at the bell, outage over
  the close), `maybe_close()` (live loop) / the end of a replay call
  `close_session()`: one final brain call on the LAST recorded tick, no new
  state-machine step, no tick row — identical in live and replay.
* THE BRAIN SEES THE RECORDED VIEW of the feed (`feed_from_tick_row` of the
  very row written to lvl_ticks: filtered chain, bias, meta), never the raw
  feed, so a replay hands it byte-identical contexts (§14 "replay exactly").
  JSON-ish values are put in Postgres jsonb key order for the same reason.
* FEED META the brain/ratio need but lvl_ticks has no column for
  (expected_move, dte, expiration, prior closes, market_open, symbol) is stored
  in the tick row's `zone` jsonb under "meta" (zone itself = Zone.to_dict()).
* day_memory(table) returns the store's rows for the day with ts (legs:
  opened_at) strictly before this tick — what a live run would have seen.
  (Legs updated later carry their later fields; nothing to do about that.)
* LEVELS "AS OF": the live runner notes the wall-clock instant just before it
  reads the levels (meta.levels_as_of); replay / warm start rebuild the level
  set from the audit as of THAT instant (fallback: the tick ts), so an edit
  that landed between the poll and our read replays on the same tick.
* WARM START replays the day's lvl_ticks through process() with nothing
  persisted and levels reconstructed from lvl_level_audit as of each tick. The
  brain IS called (outputs discarded) so its in-memory state, latest decision
  and traces are restored exactly — this requires the brain to derive ids
  (leg ids) deterministically from its inputs, not uuid4(). `with_brain=False`
  skips it (traces then come from lvl_beliefs).
* A brain exception is logged and the tick still counts (the tick row and
  level-state rows are already written).
"""
from __future__ import annotations

import copy
import logging
import math
from datetime import date, datetime, timedelta
from typing import Any, Callable

from .clock import ET, now_utc, today_et
from .config import LevelConfig
from .context import BrainOutput, TickContext
from .feed import parse_ts
from .levels import Cluster, build_clusters, convert_levels
from .ratio import RatioTracker
from .scenario import Scenario, map_scenario
from .state_machine import LevelMap
from .store import Store

log = logging.getLogger("level_engine.tick")

CHAIN_COLS = ("strike", "side", "bid", "ask", "mid", "last", "delta", "gamma", "theta", "vega",
              "iv", "open_interest", "volume", "symbol", "expiration")
EM_MULT = 2.0              # chain window = [lowest − 2·EM, highest + 2·EM]
EM_FALLBACK_PCT = 0.01     # no expected_move → 1% of spot
TRACE_N = 50


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _num(v) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _pos(v) -> float | None:
    f = _num(v)
    return f if f is not None and f > 0 else None


def _jsonb_key(k: str):
    b = k.encode("utf-8")
    return (len(b), b)


def canon(obj: Any) -> Any:
    """Deep copy with dict keys in Postgres jsonb order (shorter first, then
    bytewise) and tuples as lists — what a jsonb round trip returns."""
    if isinstance(obj, dict):
        return {str(k): canon(obj[k]) for k in sorted(obj, key=lambda k: _jsonb_key(str(k)))}
    if isinstance(obj, (list, tuple)):
        return [canon(x) for x in obj]
    return obj


def chain_window(clusters: list[Cluster], spot: float, em: float | None) -> tuple[float, float]:
    em = em if em is not None and em > 0 else EM_FALLBACK_PCT * spot
    if clusters:
        lo = min(c.price for c in clusters)
        hi = max(c.price for c in clusters)
    else:
        lo = hi = spot
    return lo - EM_MULT * em, hi + EM_MULT * em


def filter_chain(contracts: list[dict], clusters: list[Cluster], spot: float,
                 em: float | None) -> list[dict]:
    """§5 lvl_ticks.chain: contracts with strike in the level window, spec columns only."""
    lo, hi = chain_window(clusters, spot, em)
    out = []
    for c in contracts or []:
        if not isinstance(c, dict):
            continue
        k = _num(c.get("strike"))
        if k is None or not lo <= k <= hi:
            continue
        out.append({col: c.get(col) for col in CHAIN_COLS})
    out.sort(key=lambda c: (_num(c["strike"]), str(c.get("side")), str(c.get("symbol"))))
    return out


def feed_from_tick_row(row: dict, extras: dict | None = None) -> dict:
    """An lvl_ticks row → a feed-like dict TickRunner.process accepts (replay,
    warm start). `extras` (e.g. engine_pick) are merged on top."""
    zone = row.get("zone") or {}
    meta = zone.get("meta") or {}
    mo = meta.get("market_open")
    feed = {
        "symbol": meta.get("symbol") or "SPX",
        "ts": parse_ts(row["ts"]),
        "spot": _num(row.get("spx")),
        "spy_spot": _num(row.get("spy")),
        "contracts": row.get("chain") or [],
        "bias": row.get("bias"),
        "expected_move": meta.get("expected_move"),
        "dte": meta.get("dte"),
        "expiration": meta.get("expiration"),
        "spx_prevclose": meta.get("spx_prevclose"),
        "spy_prevclose": meta.get("spy_prevclose"),
        "market_open": True if mo is None else bool(mo),
        "levels_as_of": meta.get("levels_as_of"),
    }
    if extras:
        feed.update(extras)
    return feed


def _fingerprint(rows: list[dict]) -> tuple:
    out = []
    for r in rows:
        if (r.get("source") or "entered") == "converted":
            continue
        out.append((str(r.get("id")), str(r.get("symbol") or "").upper(), _num(r.get("price")),
                    r.get("name"), r.get("kind")))
    return tuple(sorted(out, key=lambda x: (x[0], x[1])))


def _conv_key(rows: list[dict]) -> list[tuple]:
    out = []
    for r in rows:
        p, q = _num(r.get("price")), _num(r.get("ratio"))
        out.append((str(r.get("converted_from")), round(p, 6) if p is not None else None,
                    r.get("name"), r.get("kind") or None, round(q, 12) if q is not None else None))
    return sorted(out, key=lambda x: (x[0], x[1] or 0.0))


# ---------------------------------------------------------------------------
# level sources: (trade_date, ts) -> entered lvl_levels rows
# ---------------------------------------------------------------------------
class StoreLevelSource:
    """Live: the store's current entered rows (edits show up next tick, §9)."""

    def __init__(self, store: Store) -> None:
        self.store = store

    def __call__(self, trade_date: date, ts: datetime) -> list[dict]:
        return self.store.list_levels(trade_date, include_converted=False)


class AuditLevelSource:
    """Replay / warm start: the entered level set AS OF each tick, rebuilt by
    applying lvl_level_audit events (add / edit / import → row = `after`,
    delete → gone) with `at <= ts`, in (at, id) order. Entered rows that never
    appear in the audit (written around the store) are present all day.
    'convert' events are ignored — converted rows are recomputed."""

    def __init__(self, store: Store, trade_date: date) -> None:
        self.trade_date = trade_date
        final = store.list_levels(trade_date, include_converted=False)
        evs = []
        for a in store.list_audit(trade_date):
            if a.get("action") not in ("add", "edit", "delete", "import") or not a.get("level_id"):
                continue
            row = a.get("after") if a.get("action") != "delete" else a.get("before")
            if isinstance(row, dict) and row.get("source") == "converted":
                continue
            at = parse_ts(a.get("at"))
            if at is None:
                continue
            evs.append((at, _num(a.get("id")) or 0.0, a))
        evs.sort(key=lambda x: (x[0], x[1]))
        self.events = evs
        audited = {str(a["level_id"]) for _, _, a in evs}
        self.base = [r for r in final if str(r.get("id")) not in audited]
        self.final = final
        self._n = -1
        self._rows: list[dict] = []

    def __call__(self, trade_date: date, ts: datetime) -> list[dict]:
        if not self.events:
            return [copy.deepcopy(r) for r in self.final]
        n = sum(1 for at, _, _ in self.events if at <= ts)
        if n != self._n:
            state: dict[str, dict] = {}
            for _, _, a in self.events[:n]:
                lid = str(a["level_id"])
                if a["action"] == "delete":
                    state.pop(lid, None)
                elif isinstance(a.get("after"), dict):
                    state[lid] = dict(a["after"])
            self._rows = [copy.deepcopy(r) for r in self.base] + list(state.values())
            self._n = n
        return [copy.deepcopy(r) for r in self._rows]


# ---------------------------------------------------------------------------
# the runner
# ---------------------------------------------------------------------------
class TickRunner:
    def __init__(self, cfg: LevelConfig, store: Store, brain: Callable | None = None,
                 today: Callable[[], date] = today_et, persist_ticks: bool = True,
                 persist_outputs: bool = True,
                 level_source: Callable[[date, datetime], list[dict]] | None = None,
                 trace_n: int = TRACE_N, now: Callable[[], datetime] = now_utc) -> None:
        self.cfg = cfg
        self.store = store
        self.brain = brain
        self.today = today
        self.persist_ticks = persist_ticks      # lvl_ticks + converted lvl_levels (live only)
        self.persist_outputs = persist_outputs  # level_state / evidence / beliefs / picks / legs / summary
        self.level_source = level_source or StoreLevelSource(store)
        self.trace_n = trace_n
        self.now = now
        self.feed_status: dict = {"state": "down"}
        self.latest_decision: dict | None = None
        self.trace_tail: tuple = ()
        self._reset_day(None)

    # ---- day lifecycle ----
    def _reset_day(self, trade_date: date | None) -> None:
        self.trade_date = trade_date
        self.ratio = RatioTracker(self.cfg.ratio_lock)
        self.level_map = LevelMap([], self.cfg)
        self.clusters: list[Cluster] = []
        self.levels_count = 0
        self._levels_fp = None
        self._ratio_val: Any = object()       # sentinel: first tick always builds
        self._cluster_key: list | None = None
        self._prev_spot: float | None = None
        self._last_ts: datetime | None = None
        self._last_view: dict | None = None
        self._last_spy: float | None = None
        self._had_open = False
        self.closed = False
        self._session = "closed"
        self._last_pick_id = None
        self.ticks_today = 0
        self.last_tick_at: datetime | None = None
        self.latest_decision = None
        self.trace_tail = ()

    def _new_day(self, trade_date: date) -> None:
        self._reset_day(trade_date)
        if self.persist_ticks:          # live: a restart after 09:35 keeps the day's lock
            try:
                r = self.store.latest_locked_ratio(trade_date)
            except Exception:
                log.exception("latest_locked_ratio failed")
                r = None
            if r:
                self.ratio.restore(r, trade_date)
        if self.brain is not None and hasattr(self.brain, "reset_day"):
            try:
                self.brain.reset_day(trade_date)
            except Exception:
                log.exception("brain.reset_day failed")

    # ---- status ----
    def set_feed_status(self, status: dict) -> None:
        self.feed_status = dict(status or {})

    @property
    def status(self) -> dict:
        return {
            "feed": self.feed_status.get("state", "down"),
            "feed_error": self.feed_status.get("error"),
            "feed_last_ok_at": self.feed_status.get("last_ok_at"),
            "last_tick_at": self.last_tick_at,
            "trade_date": self.trade_date,
            "ratio": self.ratio.ratio,
            "ratio_locked": self.ratio.locked,
            "levels": self.levels_count,
            "clusters": len(self.clusters),
            "session": "closed" if self.closed else self._session,
            "ticks": self.ticks_today,
            "brain": self.brain is not None,
        }

    # ---- the tick ----
    def process(self, feed: dict) -> dict | None:
        cfg = self.cfg
        ts = parse_ts((feed or {}).get("ts"))
        if ts is None:
            return None
        td = today_et(ts)
        if self.trade_date != td:
            if self.trade_date is not None and self._last_ts is not None and not self.closed:
                self.close_session()            # previous day never saw its close
            self._new_day(td)
        if self.closed or (self._last_ts is not None and ts <= self._last_ts):
            return None

        t = ts.astimezone(ET).time()
        mo = feed.get("market_open")
        market_open = True if mo is None else bool(mo)
        if t < cfg.session_open:
            self._session = "closed"
            return None
        closing = t >= cfg.session_close or (not market_open and self._had_open)
        if not closing and not market_open:
            self._session = "closed"
            return None
        spot = _pos(feed.get("spot"))
        if closing and not self._had_open:      # started after the close: nothing to grade
            self.closed = True
            return None
        if spot is None:
            return self.close_session() if closing else None

        spy = _pos(feed.get("spy_spot"))
        spx_pc, spy_pc = _pos(feed.get("spx_prevclose")), _pos(feed.get("spy_prevclose"))
        ratio, locked = self.ratio.update(ts, spot, spy, spx_pc, spy_pc)

        # Live: levels as read NOW (clock taken before the read). Replay / warm
        # start: as of the moment the live run read them (meta.levels_as_of).
        if self.persist_ticks:
            as_of = self.now()
        else:
            as_of = parse_ts(feed.get("levels_as_of")) or ts
        levels_changed = self._refresh_levels(td, as_of, ratio)

        transitions = self.level_map.step(ts, spot, self._prev_spot)
        scenario = map_scenario(self.level_map, spot)
        em = _pos(feed.get("expected_move"))
        chain = filter_chain(feed.get("contracts") or [], self.clusters, spot, em)
        meta = {"symbol": str(feed.get("symbol") or cfg.symbol).upper(), "expected_move": em,
                "dte": _num(feed.get("dte")), "expiration": feed.get("expiration"),
                "spx_prevclose": spx_pc, "spy_prevclose": spy_pc, "market_open": market_open,
                "levels_as_of": as_of.isoformat()}
        if closing:
            meta["session_close"] = True
        row = {"trade_date": td, "ts": ts, "spx": spot, "spy": spy, "ratio": ratio,
               "ratio_locked": bool(locked),
               "zone": canon({**scenario.zone.to_dict(), "meta": meta}),
               "chain": canon(chain), "bias": canon(feed.get("bias"))}
        if self.persist_ticks:
            self.store.insert_tick(row)
        if self.persist_outputs and transitions:
            self.store.insert_many("level_state", [self._state_row(td, x) for x in transitions])

        view = feed_from_tick_row(copy.deepcopy(row))
        prev_spot = self._prev_spot
        self._prev_spot, self._last_ts, self._last_view, self._last_spy = spot, ts, view, spy
        self._had_open = True
        self._session = "closed" if closing else "open"
        self.ticks_today += 1
        self.last_tick_at = ts

        self._run_brain(td, ts, spot, prev_spot, spy, ratio, bool(locked), scenario,
                        transitions, view, levels_changed, closing)
        if closing:
            self.closed = True
        return {"trade_date": td, "ts": ts, "spot": spot, "ratio": ratio,
                "ratio_locked": bool(locked), "levels_changed": levels_changed,
                "zone": scenario.zone.kind, "lean": scenario.lean, "chain": len(chain),
                "session_close": closing,
                "transitions": [{"level_id": x["level_id"], "state": x["state"],
                                 "prev_state": x["prev_state"]} for x in transitions]}

    def _refresh_levels(self, td: date, ts: datetime, ratio: float | None) -> bool:
        rows = self.level_source(td, ts)
        fp = _fingerprint(rows)
        if fp == self._levels_fp and ratio == self._ratio_val:
            return False
        self.levels_count = len(fp)
        resolved, persist_rows = convert_levels(rows, ratio, self.cfg.buffer_pts)
        clusters = build_clusters(resolved, self.cfg.cluster_pts, self.cfg.strike_step)
        key = [c.to_dict() for c in clusters]
        changed = key != self._cluster_key
        if changed:
            self.level_map.rebuild(clusters)
            self.clusters, self._cluster_key = list(self.level_map.clusters), key
        if self.persist_ticks:
            self._persist_converted(td, persist_rows)
        self._levels_fp, self._ratio_val = fp, ratio
        return changed

    def _persist_converted(self, td: date, rows: list[dict]) -> None:
        try:
            have = [r for r in self.store.list_levels(td) if r.get("source") == "converted"]
            if _conv_key(have) == _conv_key(rows):
                return
            self.store.replace_converted(td, rows, self.today())
        except Exception:
            log.exception("replace_converted failed for %s", td)

    @staticmethod
    def _state_row(td: date, x: dict) -> dict:
        detail = dict(x.get("detail") or {})
        detail["prev_state"] = x.get("prev_state")
        return {"trade_date": td, "level_id": x["level_id"], "ts": x["ts"], "state": x["state"],
                "since": x.get("since"), "crosses": x.get("crosses"),
                "last_held_side": x.get("last_held_side"), "detail": canon(detail)}

    # ---- close ----
    def close_session(self) -> dict | None:
        """Final brain call on the last recorded tick (no new step, no tick row)."""
        if self.closed:
            return None
        if self._last_ts is None:
            self.closed = True
            return None
        td, ts, spot = self.trade_date, self._last_ts, self._prev_spot
        scenario = map_scenario(self.level_map, spot)
        self._run_brain(td, ts, spot, spot, self._last_spy, self.ratio.ratio, self.ratio.locked,
                        scenario, [], self._last_view or {}, False, True)
        self.closed = True
        self._session = "closed"
        return {"trade_date": td, "ts": ts, "spot": spot, "session_close": True,
                "transitions": [], "zone": scenario.zone.kind, "lean": scenario.lean}

    def maybe_close(self, feed: dict | None, now: datetime) -> dict | None:
        """Live-loop fallback when no new tick arrived: close the session when
        market_open went false, the wall clock is past SESSION close (+2 ticks),
        or the ET date moved on."""
        if self.closed or self._last_ts is None or self.trade_date is None:
            return None
        if today_et(now) > self.trade_date:
            return self.close_session()
        if feed is not None and feed.get("market_open") is False:
            return self.close_session()
        close_at = datetime.combine(self.trade_date, self.cfg.session_close, ET) \
            + timedelta(seconds=2 * self.cfg.tick_sec)
        if now >= close_at:
            return self.close_session()
        return None

    # ---- brain ----
    def _day_memory(self, td: date, ts: datetime) -> Callable[[str], list[dict]]:
        store = self.store

        def mem(table: str) -> list[dict]:
            try:
                rows = store.list_rows(table, td)
            except Exception:
                return []
            out = []
            for r in rows:
                k = r.get("ts") or r.get("opened_at")
                if k is None or k < ts:
                    out.append(r)
            return out
        return mem

    def _minimal_decision(self, td, ts, spot, scenario: Scenario, status: str,
                          closing: bool) -> dict:
        return {"source": "levels", "symbol": self.cfg.symbol, "trade_date": td, "ts": ts,
                "spot": spot, "zone": scenario.zone.to_dict(), "scenario": scenario.to_dict(),
                "call": None, "pick": None, "strategies": {}, "status": status,
                "session_close": closing}

    def _run_brain(self, td, ts, spot, prev_spot, spy, ratio, locked, scenario, transitions,
                   view, levels_changed, closing) -> None:
        if self.brain is None:
            self.latest_decision = self._minimal_decision(td, ts, spot, scenario, "no_brain",
                                                          closing)
            return
        ctx = TickContext(cfg=self.cfg, trade_date=td, ts=ts, spot=spot, prev_spot=prev_spot,
                          spy=spy, ratio=ratio, ratio_locked=locked, clusters=list(self.clusters),
                          level_map=self.level_map, scenario=scenario,
                          transitions=list(transitions), feed=view,
                          levels_changed=levels_changed, session_close=closing,
                          day_memory=self._day_memory(td, ts))
        try:
            out = self.brain(ctx)
        except Exception:
            log.exception("brain failed at %s", ts)
            out = None
        if out is not None:
            self._absorb(td, ts, out)
        if out is not None and out.decision:
            self.latest_decision = out.decision
        else:
            self.latest_decision = self._minimal_decision(
                td, ts, spot, scenario, "no_decision" if out is None else "ok", closing)

    def _absorb(self, td: date, ts: datetime, out: BrainOutput) -> None:
        if out.belief:
            b = out.belief
            entry = {"ts": ts, "call": b.get("call"), "confidence": b.get("confidence"),
                     "p_up": b.get("p_up"), "p_down": b.get("p_down"),
                     "p_range": b.get("p_range"), "trace": b.get("trace")}
            self.trace_tail = (self.trace_tail + (entry,))[-self.trace_n:]
        new_pick = False
        if out.pick:
            pid = out.pick.get("pick_id")
            new_pick = pid != self._last_pick_id
            self._last_pick_id = pid
        if not self.persist_outputs:
            return
        s = self.store

        def safe(what, fn):
            try:
                fn()
            except Exception:
                log.exception("persist %s failed at %s", what, ts)

        if out.evidence:
            safe("evidence", lambda: s.insert_many(
                "evidence", [{**e, "trade_date": td, "ts": ts} for e in out.evidence]))
        if out.belief:
            safe("belief", lambda: s.insert("beliefs", {**out.belief, "trade_date": td, "ts": ts}))
        if out.pick and new_pick:
            safe("pick", lambda: s.insert("picks", {"trade_date": td, "ts": ts,
                                                    "pick_id": out.pick.get("pick_id"),
                                                    "pick": out.pick}))
        if out.leg_open:
            safe("leg_open", lambda: s.insert("legs", {"opened_at": ts, **out.leg_open,
                                                       "trade_date": td}))
        for u in out.leg_updates or []:
            u = dict(u)
            lid = u.pop("id", None)
            if lid is not None:
                safe("leg_update", lambda lid=lid, u=u: s.update_leg(lid, u))
        if out.day_summary:
            safe("day_summary", lambda: s.upsert_day_summary({**out.day_summary,
                                                              "trade_date": td}))

    # ---- restart safety ----
    def warm_start(self, trade_date: date, with_brain: bool = True) -> int:
        """Rebuild today's in-memory state by replaying the stored lvl_ticks
        (nothing persisted; levels as of each tick from the audit). Returns the
        number of ticks replayed."""
        rows = self.store.list_ticks(trade_date)
        saved = (self.persist_ticks, self.persist_outputs, self.level_source, self.brain)
        self.persist_ticks = self.persist_outputs = False
        if not with_brain:
            self.brain = None
        n = 0
        try:
            self._reset_day(None)
            if rows:
                self.level_source = AuditLevelSource(self.store, trade_date)
                for r in rows:
                    if self.process(feed_from_tick_row(r)) is not None:
                        n += 1
        finally:
            self.persist_ticks, self.persist_outputs, self.level_source, self.brain = saved
        if self.trade_date != trade_date:
            self._new_day(trade_date)
        stored = self.store.latest_locked_ratio(trade_date)
        if stored and (not self.ratio.locked or abs((self.ratio.ratio or 0) - stored) > 1e-12):
            self.ratio.restore(stored, trade_date)
        picks = self.store.list_rows("picks", trade_date)
        if picks:
            self._last_pick_id = picks[-1].get("pick_id")
        if not with_brain or self.brain is None:
            self.trace_tail = tuple(
                {"ts": b.get("ts"), "call": b.get("call"), "confidence": b.get("confidence"),
                 "p_up": b.get("p_up"), "p_down": b.get("p_down"), "p_range": b.get("p_range"),
                 "trace": b.get("trace")}
                for b in self.store.list_rows("beliefs", trade_date)[-self.trace_n:])
        if not self.closed and self.store.get_day_summary(trade_date) is not None:
            self.closed = True
        return n

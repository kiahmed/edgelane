"""Replay harness (docs/level_engine.md §14).

    python -m level_engine.replay --date YYYY-MM-DD [--config PATH] [--config-b PATH]
                                  [--memory] [--label TEXT] [--no-audit-levels]
    make levels-replay DATE=YYYY-MM-DD [CONFIG=…] [CONFIG_B=…]

Re-runs the pipeline over a day's `lvl_ticks` (spot, ratio inputs, stored
chain + bias) with a given config, through a fresh TickRunner
(persist_ticks=False: never writes lvl_ticks or converted levels).

* Connects with LVL_REPLAY_DB_URL (the `facades_levels_engine_replay` role: SELECT on
  live lvl_*, writes only schema lvl_replay — a replay cannot touch live data
  by grant, §5.1). Each config gets its own run_id, registered in
  lvl_replay.runs (config stored WITHOUT DSNs / tokens), and its outputs land
  in lvl_replay.* tagged with it.
* `--memory`: still reads the day from the DB, but writes nothing — outputs
  stay in a MemoryStore (dry run).
* LEVELS AS OF EACH TICK: rebuilt from lvl_level_audit (add/edit/delete in
  order, `at <= tick ts`), so a mid-session edit replays at the moment it
  happened (§9). `--no-audit-levels` uses the day's final level set instead.
* The brain (`level_engine.brain.create_brain(cfg)`) is used when that module
  exists; otherwise the replay covers phase 1 (states/transitions only).
* A day that never recorded a close tick is closed after its last tick
  (`close_session`), exactly as the live loop's fallback does.
* `--config-b`: runs both configs and prints a diff of level-state
  transitions (by ts, level name, state — cluster ids may differ between
  configs), legs and calls.

`run_replay(source, cfg, trade_date, sink=None, brain=None)` works on any
Store (tests use MemoryStore for both).
"""
from __future__ import annotations

import argparse
import importlib
import logging
import sys
import uuid
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from typing import Any, Callable

from .clock import ET
from .config import LevelConfig, load_config
from .store import MemoryStore, Store
from .tick import AuditLevelSource, TickRunner, feed_from_tick_row

log = logging.getLogger("level_engine.replay")

_SECRET_KEYS = ("lvl_db_url", "lvl_replay_db_url", "internal_feed_token")


def load_brain(cfg: LevelConfig) -> Callable | None:
    """`level_engine.brain.create_brain(cfg)`, or None when the module doesn't
    exist yet (phase 1). A brain that exists but fails to import/build is
    logged at ERROR and also yields None (ticks keep recording)."""
    try:
        brain_mod = importlib.import_module(f"{__package__}.brain")
    except ModuleNotFoundError as e:
        if e.name == f"{__package__}.brain":
            return None
        log.exception("level_engine.brain failed to import — running without a brain")
        return None
    except Exception:
        log.exception("level_engine.brain failed to import — running without a brain")
        return None
    factory = getattr(brain_mod, "create_brain", None)
    if factory is None:
        log.error("level_engine.brain has no create_brain(cfg) — running without a brain")
        return None
    try:
        return factory(cfg)
    except Exception:
        log.exception("create_brain(cfg) failed — running without a brain")
        return None


@dataclass
class ReplayResult:
    trade_date: date
    run_id: str | None
    ticks: int
    per_tick: list[tuple[datetime, int]] = field(default_factory=list)   # (ts, n transitions)
    transitions: list[dict] = field(default_factory=list)                # level_state rows
    final_states: dict[str, dict] = field(default_factory=dict)          # cluster id → state
    legs: list[dict] = field(default_factory=list)
    calls: list[tuple[datetime, Any]] = field(default_factory=list)      # call changes
    picks: list[dict] = field(default_factory=list)
    day_summary: dict | None = None
    brain: bool = False


def run_replay(source: Store, cfg: LevelConfig, trade_date: date, *, sink: Store | None = None,
               brain: Callable | None = None, audit_levels: bool = True,
               run_id: str | None = None) -> ReplayResult:
    """Replay `trade_date` from `source` (ticks, levels, audit) into `sink`
    (default: a fresh MemoryStore)."""
    sink = sink if sink is not None else MemoryStore()
    if audit_levels:
        level_source = AuditLevelSource(source, trade_date)
    else:
        final = source.list_levels(trade_date, include_converted=False)
        level_source = lambda d, ts: [dict(r) for r in final]   # noqa: E731
    runner = TickRunner(cfg, sink, brain=brain, today=lambda: trade_date,
                        persist_ticks=False, persist_outputs=True, level_source=level_source)
    res = ReplayResult(trade_date=trade_date, run_id=run_id, ticks=0, brain=brain is not None)
    for row in source.list_ticks(trade_date):
        out = runner.process(feed_from_tick_row(row))
        if out is None:
            continue
        res.ticks += 1
        res.per_tick.append((out["ts"], len(out["transitions"])))
    if not runner.closed:
        runner.close_session()

    res.transitions = sink.list_rows("level_state", trade_date)
    for c in runner.level_map.clusters:
        t = runner.level_map.tracker(c.id)
        res.final_states[c.id] = {"name": c.name, "price": c.price, "strike": c.strike,
                                  "state": t.state if t else None,
                                  "since": t.since if t else None,
                                  "last_event": t.last_event if t else None}
    res.legs = sink.list_rows("legs", trade_date)
    res.picks = sink.list_rows("picks", trade_date)
    last = object()
    for b in sink.list_rows("beliefs", trade_date):
        if b.get("call") != last:
            res.calls.append((b.get("ts"), b.get("call")))
            last = b.get("call")
    res.day_summary = sink.get_day_summary(trade_date)
    return res


# ---------------------------------------------------------------------------
# diff + printing
# ---------------------------------------------------------------------------
def _tkey(r: dict) -> tuple:
    d = r.get("detail") or {}
    return (r.get("ts"), d.get("name"), r.get("state"))


def _lkey(r: dict) -> tuple:
    return (r.get("opened_at"), r.get("call"), r.get("result"), r.get("close_reason"),
            r.get("target_level_id"), r.get("invalidation_level_id"))


def diff_results(a: ReplayResult, b: ReplayResult) -> dict:
    out = {}
    for name, ka, kb in (("transitions", [_tkey(r) for r in a.transitions],
                          [_tkey(r) for r in b.transitions]),
                         ("legs", [_lkey(r) for r in a.legs], [_lkey(r) for r in b.legs]),
                         ("calls", list(a.calls), list(b.calls))):
        sa, sb = set(ka), set(kb)
        out[name] = {"only_a": sorted(sa - sb, key=str), "only_b": sorted(sb - sa, key=str)}
    return out


def _t(v) -> str:
    if isinstance(v, datetime):
        return v.astimezone(ET).strftime("%H:%M:%S")
    return "—" if v is None else str(v)


def print_result(res: ReplayResult, label: str = "", file=None) -> None:
    file = file or sys.stdout
    p = lambda *a: print(*a, file=file)   # noqa: E731
    p(f"== replay {res.trade_date} {label} run_id={res.run_id or '(memory)'} "
      f"ticks={res.ticks} transitions={len(res.transitions)} brain={'yes' if res.brain else 'no'}")
    p("-- ticks with transitions (ET time: count)")
    for ts, n in res.per_tick:
        if n:
            p(f"   {_t(ts)}  {n}")
    p("-- final level states")
    for cid, s in sorted(res.final_states.items(), key=lambda kv: kv[1]["price"]):
        p(f"   {s['price']:>9.2f}  {s['name'][:40]:<40}  {s['state'] or '—':<12} "
          f"since {_t(s['since'])}  last_event={s['last_event'] or '—'}")
    if res.brain:
        p(f"-- calls ({len(res.calls)} changes)")
        for ts, c in res.calls:
            p(f"   {_t(ts)}  {c}")
        p(f"-- legs ({len(res.legs)})")
        for lg in res.legs:
            p(f"   {_t(lg.get('opened_at'))} {lg.get('call') or '—':<6} → "
              f"{lg.get('result') or 'open':<5} {lg.get('close_reason') or '':<13} "
              f"exit={lg.get('exit_spot')} progress={lg.get('progress_pct')} "
              f"mae={lg.get('mae_pts')} pnl={lg.get('strategy_pnl')}")
        if res.day_summary:
            p(f"-- day summary: legs={res.day_summary.get('legs')}")


def print_diff(d: dict, file=None) -> None:
    file = file or sys.stdout
    for name, v in d.items():
        p = lambda *a: print(*a, file=file)   # noqa: E731
        p(f"== diff {name}: only A={len(v['only_a'])} only B={len(v['only_b'])}")
        for k in v["only_a"]:
            p("   A " + " ".join(_t(x) for x in k))
        for k in v["only_b"]:
            p("   B " + " ".join(_t(x) for x in k))


def safe_config(cfg: LevelConfig) -> dict:
    return {k: v for k, v in asdict(cfg).items() if k not in _SECRET_KEYS}


def register_run(store, run_id: str, trade_date: date, label: str, cfg: LevelConfig) -> None:
    """Insert the lvl_replay.runs row (PgStore only)."""
    store.register_run(run_id, trade_date, label, safe_config(cfg))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="level_engine.replay", description=__doc__.split("\n\n")[0])
    ap.add_argument("--date", required=True, type=date.fromisoformat)
    ap.add_argument("--config", default=None, help="facades_levels_engine.config (default: $LEVEL_ENGINE_CONFIG)")
    ap.add_argument("--config-b", default=None, help="second config to diff against")
    ap.add_argument("--memory", action="store_true", help="dry run: write nothing to lvl_replay")
    ap.add_argument("--label", default="", help="label stored in lvl_replay.runs")
    ap.add_argument("--no-audit-levels", action="store_true",
                    help="use the day's final level set for every tick")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    cfg_a = load_config(a.config)
    cfgs = [("A", cfg_a)] + ([("B", load_config(a.config_b))] if a.config_b else [])
    dsn = cfg_a.lvl_replay_db_url
    if not dsn:
        print("LVL_REPLAY_DB_URL is empty — replay needs the facades_levels_engine_replay DSN "
              "(docs/level_engine.md §5.1, §14).", file=sys.stderr)
        return 2
    from .store import PgStore
    source = PgStore(dsn)
    results = []
    try:
        for tag, cfg in cfgs:
            if a.memory:
                sink, run_id = MemoryStore(), None
            else:
                run_id = str(uuid.uuid4())
                sink = PgStore(dsn, replay_run_id=run_id)
                register_run(sink, run_id, a.date, a.label or tag, cfg)
            try:
                res = run_replay(source, cfg, a.date, sink=sink, brain=load_brain(cfg),
                                 audit_levels=not a.no_audit_levels, run_id=run_id)
            finally:
                if not a.memory:
                    sink.close()
            print_result(res, label=f"[{tag}]")
            results.append(res)
        if len(results) == 2:
            print_diff(diff_results(results[0], results[1]))
    finally:
        source.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

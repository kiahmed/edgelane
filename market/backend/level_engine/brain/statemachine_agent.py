"""[1] State-machine agent — turns the §7.1/§7.2 result into board posts.

The state machine itself runs in the runtime (ctx.level_map is already stepped,
ctx.scenario already mapped). This agent only reports it:

* `contested` — a bracketing level is contested (scenario lean 'range') →
  range +WEIGHTS['contested'] (0.6, §8.5), every tick while it lasts.
* `lean` — the §7.2 lean (up / down / range), posted with weight 0 and
  detail.logodds = LEAN_LOGODDS. The manager adds the lean in step 2 (§8.5);
  the row exists so the trace and the stored board show where it came from
  without counting it twice in step 3 or in the thin-evidence sum.

Decisions: lean 'none' / 'done' posts nothing ('done' is handled by the manager).
"""
from __future__ import annotations

from .board import WEIGHTS, Evidence

AGENT = "statemachine"


def run(ctx) -> list[Evidence]:
    sc = ctx.scenario
    ref = sc.reference_level
    ref_d = None if ref is None else {"id": ref.id, "name": ref.name, "price": ref.price}
    posts: list[Evidence] = []
    if sc.lean == "range":
        tr = ctx.level_map.tracker(ref.id) if ref is not None else None
        posts.append(Evidence(
            AGENT, "range", WEIGHTS["contested"], "contested",
            {"level": ref_d, "crosses": tr.crosses_in_window(ctx.ts) if tr else None},
            f"{ref.name if ref else 'level'} contested → range", 0))
    if sc.lean in ("up", "down", "range"):
        what = f"{ref.name} {sc.reference_event}" if ref is not None else "zone"
        pend = " (pending hold)" if sc.pending else ""
        posts.append(Evidence(
            AGENT, sc.lean, 0.0, "lean",
            {"lean": sc.lean, "level": ref_d, "event": sc.reference_event,
             "pending": sc.pending, "logodds": ctx.cfg.lean_logodds},
            f"lean {sc.lean}: {what}{pend}", 0))
    return posts

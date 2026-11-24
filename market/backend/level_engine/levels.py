"""Levels: SPY→SPX conversion (§6), clustering (§7.0), strike mapping.

Spec: docs/level_engine.md §6, §7.0. Pure functions, stdlib only, no I/O.

Input rows are `lvl_levels` dicts as returned by `store.list_levels` (keys:
id, symbol, price, name, kind, source, ...). Rows with `source='converted'` are
derived artifacts of a previous conversion and are ignored here — the entered
rows are the only source of truth.

Decisions (spec ambiguities, resolved conservatively)
-----------------------------------------------------
* nearest_strike: round half UP on the grid (7282.5 → 7285). A 1e-9 nudge
  absorbs float noise in price/step so x.5 boundaries are stable.
* SPX rows with price None are skipped (nothing to anchor). Rows with a symbol
  other than SPX/SPY, or a non-positive / non-numeric price, are skipped.
* SPY rows are skipped entirely while `ratio` is None (no guessed ratio).
* Converted SPX prices are rounded to 2 dp so the in-memory price equals what
  the DB stores (numeric) and replay is exact.
* §6 merge: an SPY-converted level within BUFFER_PTS (inclusive, |Δ| <= buffer)
  of an ENTERED SPX level merges into it: the SPX price wins, the SPX name comes
  first then the SPY name, both ids are kept, kind = the stronger of the two.
  Several candidates → the nearest; tie → lower price, then id.
  Converted levels never merge with each other here (clustering does that).
* Persisted converted rows: EVERY converted SPY level is returned for
  `store.replace_converted` — merged ones too — so the audit shows the ratio
  used for every SPY level. Shape: {price, name, kind, ratio, converted_from}.
* Kind strength: trigger > pivot > resistance = support > None. Equal strength
  (support vs resistance) → the first member in price order (lowest price).
* Clustering: adjacent-gap CHAINING — sorted by price, a level joins the
  current cluster when its gap to the previous level is < CLUSTER_PTS. Chaining
  (rather than anchor-based grouping) is deliberate: it guarantees adjacent
  cluster prices are >= CLUSTER_PTS (>= 2*BUFFER_PTS) apart, so bands never
  overlap (§7.0). Cost: a ladder of levels each < CLUSTER_PTS apart becomes one
  wide cluster; `low`/`high` expose its span so it is visible, not hidden.
* Cluster price = mean of member prices (each resolved level counts once),
  rounded to 2 dp. Cluster id = "c:" + sha1(",".join(sorted(level_ids)))[:10] —
  stable for the same member set across ticks and restarts. NOTE the id does
  not encode price: an edited price keeps the id; LevelMap.rebuild detects the
  price change separately and resets that tracker.
"""
from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass

KIND_STRENGTH = {"trigger": 3, "pivot": 2, "resistance": 1, "support": 1, None: 0}


def nearest_strike(price: float, step: float = 5.0) -> float:
    """Nearest listed strike on a `step` grid, half rounded up."""
    if step <= 0:
        raise ValueError("step must be > 0")
    return float(math.floor(price / step + 0.5 + 1e-9) * step)


def _strongest_kind(kinds) -> str | None:
    best, best_s = None, -1
    for k in kinds:  # first wins on ties
        s = KIND_STRENGTH.get(k, 0)
        if s > best_s:
            best, best_s = k, s
    return best


def _dedupe(seq) -> tuple:
    seen, out = set(), []
    for x in seq:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return tuple(out)


@dataclass(frozen=True)
class ResolvedLevel:
    id: str
    price: float            # SPX points
    name: str
    kind: str | None
    source: str             # 'entered' | 'converted'
    symbol_origin: str      # 'SPX' | 'SPY'
    names: tuple[str, ...]
    level_ids: tuple[str, ...]


def _price(row: dict) -> float | None:
    p = row.get("price")
    if p is None:
        return None
    try:
        p = float(p)
    except (TypeError, ValueError):
        return None
    return p if math.isfinite(p) and p > 0 else None


def convert_levels(rows: list[dict], ratio: float | None,
                   buffer_pts: float) -> tuple[list[ResolvedLevel], list[dict]]:
    """Resolve entered SPX/SPY rows to SPX levels (§6).

    Returns (levels sorted by (price, id), converted rows to persist)."""
    entered = [r for r in rows if (r.get("source") or "entered") != "converted"]
    spx: list[dict] = []          # mutable working copies of SPX levels
    spy: list[tuple[dict, float]] = []
    for r in entered:
        sym = str(r.get("symbol") or "").upper()
        p = _price(r)
        if p is None:
            continue
        if sym == "SPX":
            spx.append({"id": str(r["id"]), "price": p, "kind": r.get("kind") or None,
                        "names": [str(r.get("name") or "")], "ids": [str(r["id"])],
                        "kinds": [r.get("kind") or None]})
        elif sym == "SPY":
            spy.append((r, p))

    ratio_ok = ratio is not None and math.isfinite(ratio) and ratio > 0
    out: list[ResolvedLevel] = []
    persist: list[dict] = []
    if ratio_ok:
        for r, p in spy:
            conv = round(p * ratio, 2)
            kind = r.get("kind") or None
            name = str(r.get("name") or "")
            persist.append({"price": conv, "name": name, "kind": kind,
                            "ratio": ratio, "converted_from": str(r["id"])})
            cands = [s for s in spx if abs(conv - s["price"]) <= buffer_pts + 1e-9]
            if cands:
                tgt = min(cands, key=lambda s: (abs(conv - s["price"]), s["price"], s["id"]))
                tgt["names"].append(name)
                tgt["ids"].append(str(r["id"]))
                tgt["kinds"].append(kind)
            else:
                out.append(ResolvedLevel(id=str(r["id"]), price=conv, name=name, kind=kind,
                                         source="converted", symbol_origin="SPY",
                                         names=(name,), level_ids=(str(r["id"]),)))
    for s in spx:
        names = _dedupe(s["names"])
        out.append(ResolvedLevel(id=s["id"], price=s["price"], name=" / ".join(names),
                                 kind=_strongest_kind(s["kinds"]), source="entered",
                                 symbol_origin="SPX", names=names, level_ids=tuple(s["ids"])))
    out.sort(key=lambda lv: (lv.price, lv.id))
    return out, persist


@dataclass(frozen=True)
class Cluster:
    id: str
    price: float
    name: str
    kind: str | None
    level_ids: tuple[str, ...]
    names: tuple[str, ...]
    strike: float
    low: float = 0.0        # lowest member price (span visibility)
    high: float = 0.0       # highest member price

    def to_dict(self) -> dict:
        return {"id": self.id, "price": self.price, "name": self.name, "kind": self.kind,
                "strike": self.strike, "level_ids": list(self.level_ids),
                "names": list(self.names), "low": self.low, "high": self.high}


def cluster_id(level_ids) -> str:
    return "c:" + hashlib.sha1(",".join(sorted(level_ids)).encode()).hexdigest()[:10]


def build_clusters(levels: list[ResolvedLevel], cluster_pts: float,
                   strike_step: float = 5.0) -> list[Cluster]:
    """Merge levels closer than `cluster_pts` (adjacent-gap chaining), §7.0."""
    lv = sorted(levels, key=lambda x: (x.price, x.id))
    groups: list[list[ResolvedLevel]] = []
    for x in lv:
        if groups and x.price - groups[-1][-1].price < cluster_pts:
            groups[-1].append(x)
        else:
            groups.append([x])
    out = []
    for g in groups:
        ids = _dedupe(i for m in g for i in m.level_ids)
        names = _dedupe(n for m in g for n in m.names)
        price = round(sum(m.price for m in g) / len(g), 2)
        out.append(Cluster(id=cluster_id(ids), price=price, name=" / ".join(names),
                           kind=_strongest_kind(m.kind for m in g), level_ids=ids,
                           names=names, strike=nearest_strike(price, strike_step),
                           low=g[0].price, high=g[-1].price))
    return out

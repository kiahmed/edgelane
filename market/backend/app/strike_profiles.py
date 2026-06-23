"""Per-ticker debit-spread strike selection ("smart picker").

Why this exists
---------------
The default vertical builder anchors a debit spread's LONG leg at
`delta = 1 - target_delta`. With a 0.20-0.30 short-delta target that lands the
long at 0.70-0.80 delta — deep ITM. A deep-ITM debit vertical enters near its
max value: tiny net delta, almost no room to expand, wide markets. It barely
moves on a real directional push (the exact "sluggish / flaky / no gains"
complaint that motivated this module).

Pros build directional debit verticals OTM-OTM: long the gamma-rich strike just
out of the money (~0.40 delta), and SHORT sold *into the expected target* — the
GEX magnet wall, the nearest high-OI strike, or 1x the expected move — i.e.
"buy the move, sell the wall." That structure is cheap, convex, and expands fast
when price travels toward the target.

This module computes those strikes deterministically (no LLM) BEFORE scoring,
driven by a per-ticker `StrikeProfile` persisted in DuckDB. SPX != SPY != a $40
name, so each symbol gets its own row; an unconfigured symbol falls back to a
delta-based generic profile.

Scope: debit verticals only (bull_call / bear_put). Credit spreads, condors and
flies are untouched for now (a future pass can give them their own profiles).
"""
from __future__ import annotations

from dataclasses import dataclass, asdict, fields
from typing import Any

# Sides that this picker handles. Everything else falls through to the legacy
# delta-mirror path in strategy_engine.build_vertical().
DEBIT_STRATEGIES = ("bull_call", "bear_put")


@dataclass
class StrikeProfile:
    """Per-ticker debit strike-selection config.

    Distances are in POINTS of the underlying. `None` on a width bound means
    "derive from the expected move" (so it scales for any price level when a
    symbol hasn't been hand-tuned). `long_offset_pts`, when set, overrides the
    delta band and places the long that many points OTM from spot.
    """
    symbol: str = "DEFAULT"
    enabled: bool = True
    # LONG leg: gamma-rich, just OTM. Pick the listed strike whose |delta| sits
    # in [lo, hi]; center of the band is the ideal. ~0.40 delta = ~5-10 pts OTM
    # on SPX, where premium expands fastest per point of move.
    long_delta_lo: float = 0.38
    long_delta_hi: float = 0.46
    long_offset_pts: float | None = None   # if set, overrides the delta band
    # SHORT leg: sold into the target. Never let it get so far OTM it's a lotto
    # ticket carrying no premium — floor its |delta|.
    short_min_delta: float = 0.12
    # Width window (long->short). None = derive from expected move.
    min_width_pts: float | None = None
    max_width_pts: float | None = None
    # Target the short is sold into: 'wall' (GEX magnet) -> 'oi' -> 'move' chain,
    # 'oi' = nearest high-OI strike beyond long, 'move' = spot +/- expected move.
    target_source: str = "wall"
    # Listed-strike spacing for snapping (SPX = 5). 0/None disables snapping.
    round_snap: float = 0.0
    # Liquidity gate so fills are assured.
    min_oi: int = 0
    min_vol: int = 0

    def width_bounds(self, expected_move: float) -> tuple[float, float]:
        """Resolve (min_width, max_width) in points, deriving from the expected
        move when a bound is left as None."""
        em = max(0.01, float(expected_move or 0))
        lo = self.min_width_pts if self.min_width_pts is not None else 0.5 * em
        hi = self.max_width_pts if self.max_width_pts is not None else 1.5 * em
        if hi < lo:
            hi = lo
        return float(lo), float(hi)

    def to_row(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "StrikeProfile":
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in row.items() if k in known and v is not None})


# Generic fallback: delta-driven, scales to any price level (no point offsets,
# no snapping). Used when a symbol has no saved profile.
DEFAULT_PROFILE = StrikeProfile(symbol="DEFAULT")

# SPX default — hand-tuned to the user's own heuristic (long ~next round strike,
# short >=20 pts away into the magnet), made adaptive. SPX strikes are 5 apart.
SPX_PROFILE = StrikeProfile(
    symbol="SPX",
    long_delta_lo=0.38,
    long_delta_hi=0.46,
    long_offset_pts=None,
    short_min_delta=0.12,
    min_width_pts=20.0,
    max_width_pts=50.0,
    target_source="wall",
    round_snap=5.0,
    min_oi=50,
    min_vol=0,
)

# NDX default — Nasdaq-100 index. Trades ~4x SPX in points and is thinner, so the
# width window and strike-snap scale up while the liquidity gate eases. Strikes are
# commonly 25 apart near the money; tune live via the admin API if the listed grid
# differs for the expirations you trade.
NDX_PROFILE = StrikeProfile(
    symbol="NDX",
    long_delta_lo=0.38,
    long_delta_hi=0.46,
    long_offset_pts=None,
    short_min_delta=0.12,
    min_width_pts=75.0,
    max_width_pts=200.0,
    target_source="wall",
    round_snap=25.0,
    min_oi=10,
    min_vol=0,
)

# Seeded into the DB on first migrate so the picker is live out of the box.
SEED_PROFILES = (DEFAULT_PROFILE, SPX_PROFILE, NDX_PROFILE)


def resolve_profile(db: Any, symbol: str) -> StrikeProfile:
    """Resolve the effective profile for a symbol: its own row, else the saved
    DEFAULT row, else the built-in DEFAULT_PROFILE. Tolerant of a missing/closed
    db (returns the built-in default) so the engine never hard-fails on it."""
    try:
        row = db.get_strike_profile(symbol) if db is not None else None
        if row:
            return StrikeProfile.from_row(row)
        gen = db.get_strike_profile("DEFAULT") if db is not None else None
        if gen:
            return StrikeProfile.from_row(gen)
    except Exception:
        pass
    return DEFAULT_PROFILE


# ─────────────────────────────────────────────────────────────────────────────
# Picker
# ─────────────────────────────────────────────────────────────────────────────
def _snap(value: float, step: float) -> float:
    if not step or step <= 0:
        return value
    return round(value / step) * step


def _otm_legs(contracts: list[dict], side: str, spot: float) -> list[dict]:
    """Tradeable (bid>0) strikes of `side` that are OTM, ordered nearest-OTM
    first (ascending strike for calls, descending for puts)."""
    legs = [
        c for c in contracts
        if c.get("side") == side
        and float(c.get("bid") or 0) > 0
        and ((side == "call" and float(c["strike"]) > spot)
             or (side == "put" and float(c["strike"]) < spot))
    ]
    legs.sort(key=lambda c: float(c["strike"]), reverse=(side == "put"))
    return legs


def _liquid(c: dict, profile: StrikeProfile) -> bool:
    return (int(c.get("open_interest") or 0) >= profile.min_oi
            and int(c.get("volume") or 0) >= profile.min_vol)


def _pick_long(otm: list[dict], profile: StrikeProfile, spot: float,
               side: str, aggressiveness: int) -> dict | None:
    """Choose the long leg: the gamma-rich, just-OTM strike.

    `aggressiveness` shifts the target further OTM (+1) or closer to ATM (-1) so
    the Conservative/Balanced/Aggressive fan still produces three distinct
    structures. Liquidity-gated; relaxes the gate only if nothing qualifies."""
    liq = [c for c in otm if _liquid(c, profile)] or otm
    if profile.long_offset_pts is not None:
        # Point-offset mode: long sits ~offset OTM from spot (aggressiveness
        # nudges it by +/-1 strike-spacing worth of points).
        bump = aggressiveness * (profile.round_snap or 0)
        off = max(0.0, float(profile.long_offset_pts) + bump)
        target = spot + off if side == "call" else spot - off
        return min(liq, key=lambda c: abs(float(c["strike"]) - target))
    # Delta-band mode: center of band, shifted by aggressiveness (more
    # aggressive -> lower delta -> further OTM).
    center = (profile.long_delta_lo + profile.long_delta_hi) / 2 - aggressiveness * 0.05
    return min(liq, key=lambda c: abs(abs(float(c.get("delta") or 0)) - center))


def _resolve_target(profile: StrikeProfile, side: str, spot: float,
                    expected_move: float, walls: dict | None,
                    contracts: list[dict], long_strike: float) -> tuple[float, str]:
    """Where to sell the short: the magnet wall, the nearest high-OI strike, or
    1x expected move — whichever the profile's target_source resolves to first.
    Returns (target_price, source_label)."""
    def beyond(strike: float) -> bool:
        return (side == "call" and strike > long_strike) or (side == "put" and strike < long_strike)

    src = (profile.target_source or "move").lower()
    if src == "wall" and walls and walls.get("strike") is not None:
        w = float(walls["strike"])
        if beyond(w):
            return w, "magnet wall"
    if src in ("wall", "oi"):
        oi_side = [c for c in contracts if c.get("side") == side and beyond(float(c["strike"]))]
        if oi_side:
            top = max(oi_side, key=lambda c: int(c.get("open_interest") or 0))
            if int(top.get("open_interest") or 0) > 0:
                return float(top["strike"]), "high-OI strike"
    em = max(0.01, float(expected_move or 0))
    return (spot + em if side == "call" else spot - em), "1x expected move"


def pick_debit_strikes(
    strategy: str,
    contracts: list[dict],
    spot: float,
    expected_move: float,
    walls: dict | None,
    profile: StrikeProfile,
    aggressiveness: int = 0,
) -> dict | None:
    """Return {'short', 'long', 'logic'} for an OTM-OTM debit vertical, or None
    if the chain can't support one. Both legs are OTM; the long is gamma-rich
    just outside spot and the short is sold into the resolved target, clamped to
    the profile's width window and short-delta floor, snapped to listed strikes,
    and liquidity-gated.
    """
    if strategy not in DEBIT_STRATEGIES or not spot or spot <= 0:
        return None
    side = "call" if strategy == "bull_call" else "put"
    otm = _otm_legs(contracts, side, spot)
    if not otm:
        return None

    long = _pick_long(otm, profile, spot, side, aggressiveness)
    if not long:
        return None
    long_strike = float(long["strike"])

    target, tgt_src = _resolve_target(profile, side, spot, expected_move, walls,
                                      contracts, long_strike)

    # Clamp the short into the width window. Aggressiveness scales the WHOLE
    # window (both bounds) so Conservative/Balanced/Aggressive stay distinct even
    # when the target sits right at the near edge (flat-OI / no-wall chains).
    min_w, max_w = profile.width_bounds(expected_move)
    scale = 1.0 + 0.25 * aggressiveness
    min_w *= scale
    max_w *= scale
    if side == "call":
        short_target = min(max(target, long_strike + min_w), long_strike + max_w)
    else:
        short_target = max(min(target, long_strike - min_w), long_strike - max_w)
    short_target = _snap(short_target, profile.round_snap)

    # Candidate shorts: strictly beyond the long, tradeable, prefer liquid.
    def beyond_long(c: dict) -> bool:
        s = float(c["strike"])
        return (side == "call" and s > long_strike) or (side == "put" and s < long_strike)

    shorts = [c for c in contracts
              if c.get("side") == side and float(c.get("bid") or 0) > 0 and beyond_long(c)]
    liq_shorts = [c for c in shorts if _liquid(c, profile)] or shorts
    if not liq_shorts:
        return None

    # Enforce the short-delta floor: never sell a strike carrying ~no premium.
    eligible = [c for c in liq_shorts if abs(float(c.get("delta") or 0)) >= profile.short_min_delta]
    pool = eligible or liq_shorts
    short = min(pool, key=lambda c: abs(float(c["strike"]) - short_target))

    if float(short["strike"]) == long_strike:
        return None

    width = abs(float(short["strike"]) - long_strike)
    logic = (
        f"OTM debit: long {long_strike:g} (|Δ|{abs(float(long.get('delta') or 0)):.2f}, "
        f"{abs(long_strike - spot):g} pts OTM) · short {float(short['strike']):g} "
        f"(|Δ|{abs(float(short.get('delta') or 0)):.2f}) sold into {tgt_src} "
        f"· width {width:g} pts"
    )
    return {"short": short, "long": long, "logic": logic}

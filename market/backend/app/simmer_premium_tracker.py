"""Overnight premium-decision analyzer — pure functions (docs/simmer.md →
"Overnight movers").

A stock gapped overnight on news. Pre-market already priced a reaction. The
question this answers, once the market opens, is the only one that matters:

    does it make sense to SELL a credit spread on this, and which side?

It compares live option premiums on the strikes *toward the move* against a
FROZEN baseline (last-night close, snapshotted at 09:25) and collapses the whole
thing into ONE verdict — sell bear-call / sell bull-put / wait / skip — plus a
one-line premium trend and the peer context. Decision-first: the sampling and
smoothing stay under the hood; callers surface only the verdict.

Everything here is a pure function of its inputs (premium maps, direction, the
RelativeQs `crowd` tag, the underlying level, a news-is-bad hint) so it is fully
unit-testable with faked inflate / deflate / sideways / reversal scenarios — no
network, no DB. The orchestration (fetch chain, persist, schedule) lives in
`simmer_overnight.py`; the strike picking off a live chain is here too.

It NEVER promotes a trade on its own — it only names a side the deterministic
readiness engine has already built and gated (Simmer's "catalyst never promotes"
rule). The verdict is a heuristic, unproven on day one, logged for calibration.
"""
from __future__ import annotations

from typing import Any, Literal

# How far a mean premium ratio must move off 1.0 to count as building / fading
# rather than holding. Deliberately wide — we want conviction, not noise.
_RATIO_BAND = 0.08
# How far the underlying must travel (as a fraction of the overnight gap) to count
# as extending or retracing rather than holding its gap level.
_UNDERLYING_BAND = 0.33

Call = Literal["sell_bear_call", "sell_bull_put", "wait", "skip"]
Trend = Literal["building", "holding", "fading"]
UTrend = Literal["extending", "holding", "retracing"]


def _mid(c: dict) -> float | None:
    bid, ask = c.get("bid"), c.get("ask")
    if bid is None or ask is None:
        return None
    m = (float(bid) + float(ask)) / 2.0
    return m if m > 0 else None


def strike_key(strike: float) -> str:
    """Canonical string key for a strike, matching how the UI stringifies the
    number in `strikes` (JSON number → String): 180.0→'180', 182.5→'182.5'. Using
    str(float(s)) would give '180.0', which the UI's String(180) would never
    match — the bug that left most premium cells blank."""
    return "%g" % float(strike)


def toward_move_side(direction: str) -> str:
    """The option side whose premiums INFLATE on the move — the strikes we track.
    A drop pumps put premium; a pop pumps call premium."""
    return "put" if str(direction).lower() == "down" else "call"


def pick_strikes(spot: float, chain: list[dict], direction: str, n: int = 3) -> list[float]:
    """A few OTM strikes TOWARD the move, nearest-to-spot first. Down → OTM puts
    below spot; up → OTM calls above spot. These are the strikes whose premiums we
    snapshot at the baseline and re-read live."""
    side = toward_move_side(direction)
    strikes = sorted({float(c["strike"]) for c in chain
                      if c.get("side") == side and c.get("strike") is not None
                      and _mid(c) is not None})
    if not strikes or not spot:
        return []
    if side == "put":                      # OTM puts are BELOW spot, nearest first
        otm = [s for s in strikes if s < spot][::-1]
    else:                                  # OTM calls are ABOVE spot, nearest first
        otm = [s for s in strikes if s > spot]
    return otm[:n]


def premium_map(chain: list[dict], strikes: list[float], side: str) -> dict[str, float]:
    """{strike -> mid premium} for the given side/strikes. Keys are str(strike) so
    the map round-trips cleanly through jsonb."""
    want = {float(s) for s in strikes}
    out: dict[str, float] = {}
    for c in chain:
        if c.get("side") != side or c.get("strike") is None:
            continue
        k = float(c["strike"])
        if k in want:
            m = _mid(c)
            if m is not None:
                out[strike_key(k)] = round(m, 4)
    return out


def volume_map(chain: list[dict], strikes: list[float], side: str) -> dict[str, int]:
    want = {float(s) for s in strikes}
    out: dict[str, int] = {}
    for c in chain:
        if c.get("side") == side and c.get("strike") is not None and float(c["strike"]) in want:
            out[strike_key(float(c["strike"]))] = int(c.get("volume") or 0)
    return out


def mean_ratio(baseline: dict[str, float], latest: dict[str, float]) -> float | None:
    """Mean of latest/baseline across the strikes present in BOTH maps. None when
    there's no overlap yet (e.g. before the open)."""
    ratios = [latest[k] / baseline[k] for k in baseline
              if k in latest and baseline.get(k)]
    return sum(ratios) / len(ratios) if ratios else None


def classify_premium(baseline: dict[str, float], latest: dict[str, float]) -> tuple[Trend | None, float | None]:
    """building / holding / fading off the mean premium ratio, with a dead-band."""
    r = mean_ratio(baseline, latest)
    if r is None:
        return None, None
    if r >= 1.0 + _RATIO_BAND:
        return "building", r
    if r <= 1.0 - _RATIO_BAND:
        return "fading", r
    return "holding", r


def classify_underlying(gap: float, spot_baseline: float, spot_now: float | None) -> UTrend | None:
    """Did the underlying extend the overnight move, hold its gap level, or
    retrace? `gap` is the signed overnight fraction; a same-sign intraday move is
    extending, an opposite move past the band is retracing."""
    if spot_now is None or not spot_baseline:
        return None
    intraday = (spot_now - spot_baseline) / spot_baseline
    # scale the band to the size of the overnight move
    band = abs(gap) * _UNDERLYING_BAND if gap else 0.0
    sign = 1.0 if gap >= 0 else -1.0
    signed = intraday * sign                      # >0 = same direction as the gap
    if signed >= band and band > 0:
        return "extending"
    if signed <= -band and band > 0:
        return "retracing"
    return "holding"


def _confidence(crowd: str | None, premium: Trend | None, underlying: UTrend | None,
                gap: float, news_bad: bool | None) -> int:
    """0–100. Conviction rises when the peer move is systemic, premium and
    underlying agree, the gap is large, and the news read is decisive."""
    score = 25.0
    if crowd in ("sector_wide", "with_market"):
        score += 20
    elif crowd in ("lone", "against_market"):
        score += 5
    if premium in ("building", "fading") and underlying in ("extending", "retracing", "holding"):
        score += 15
    if premium and underlying and (
            (premium in ("building", "holding") and underlying in ("extending", "holding"))
            or (premium == "fading" and underlying == "holding")):
        score += 15                      # premium and tape tell the same story
    score += min(25.0, abs(gap) * 300.0)  # a 6% gap adds ~18
    if news_bad is not None:
        score += 10
    return int(max(0, min(100, round(score))))


def build_verdict(*, direction: str, crowd: str | None, gap: float,
                  premium_trend: Trend | None, underlying_trend: UTrend | None,
                  news_bad: bool | None = None) -> dict[str, Any]:
    """Collapse everything into ONE decision.

    call ∈ sell_bear_call | sell_bull_put | wait | skip. The side we SELL is the
    opposite tail from where the risk now sits: a real, holding/extending drop →
    sell the upside (bear_call); a drop whose fear is crushing while the stock
    holds → sell the downside (bull_put) into the still-rich IV; a move that is
    reversing / premiums collapsing → skip (likely a fade or debunked)."""
    d = str(direction).lower()
    reasons: list[str] = []
    call: Call = "wait"

    reversing = underlying_trend == "retracing"
    collapsing = premium_trend == "fading"

    if reversing and collapsing:
        call = "skip"
        reasons.append("move reversing and premiums collapsing — looks like a fade")
    elif d == "down":
        if premium_trend in ("building", "holding") and underlying_trend in ("extending", "holding") \
                and (crowd in ("sector_wide", "with_market") or news_bad):
            call = "sell_bear_call"
            reasons.append("drop holding with rich, firm downside premium — sell the upside")
        elif premium_trend == "fading" and underlying_trend == "holding":
            call = "sell_bull_put"
            reasons.append("stock holding the drop while fear premium crushes — sell the downside into IV")
        else:
            reasons.append("mixed — drop not confirmed by premium + tape yet")
    elif d == "up":
        if premium_trend in ("building", "holding") and underlying_trend in ("extending", "holding"):
            call = "sell_bull_put"
            reasons.append("pop holding with firm upside premium — sell the downside")
        elif premium_trend == "fading" and underlying_trend == "holding":
            call = "sell_bear_call"
            reasons.append("stock holding the pop while premium crushes — sell the upside into IV")
        else:
            reasons.append("mixed — pop not confirmed by premium + tape yet")

    if crowd in ("sector_wide", "with_market"):
        reasons.append("sector-wide move (peers moved too) — more durable")
    elif crowd in ("lone", "against_market"):
        reasons.append("lone move (peers did not) — idiosyncratic, revert-prone")

    return {
        "call": call,
        "premium_trend": premium_trend,
        "underlying_trend": underlying_trend,
        "confidence": _confidence(crowd, premium_trend, underlying_trend, gap, news_bad),
        "reasons": reasons,
    }


def analyze(*, direction: str, crowd: str | None, gap: float,
            baseline: dict[str, float], latest: dict[str, float] | None,
            spot_baseline: float, spot_now: float | None = None,
            news_bad: bool | None = None) -> dict[str, Any]:
    """Baseline + live → verdict. Before the open (no `latest`) the premium/
    underlying trends are None and the call is 'wait' — table 1 (baseline) is
    populated, table 2 (live) fills after the open."""
    if not latest:
        return {"call": "wait", "premium_trend": None, "underlying_trend": None,
                "confidence": 0, "reasons": ["baseline captured — waiting for the open"]}
    premium_trend, ratio = classify_premium(baseline, latest)
    underlying_trend = classify_underlying(gap, spot_baseline, spot_now)
    verdict = build_verdict(direction=direction, crowd=crowd, gap=gap,
                            premium_trend=premium_trend, underlying_trend=underlying_trend,
                            news_bad=news_bad)
    verdict["mean_ratio"] = round(ratio, 4) if ratio is not None else None
    return verdict

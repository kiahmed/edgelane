"""Level Engine config — `facades_levels_engine.config` (KEY=VALUE, `#` comments).

Spec: docs/level_engine.md §4. Every key has the spec default; the loader
validates the inter-key constraints the state machine relies on (§4, §7):
  TEST_PTS > BUFFER_PTS, KNIFE_PTS >= 2*BUFFER_PTS, CLUSTER_PTS >= 2*BUFFER_PTS.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, fields, replace
from datetime import time
from pathlib import Path

DEFAULT_PATH = Path(__file__).resolve().parent / "facades_levels_engine.config"
ENV_VAR = "LEVEL_ENGINE_CONFIG"


class ConfigError(ValueError):
    pass


def _hhmm(v: str) -> time:
    h, _, m = v.strip().partition(":")
    return time(int(h), int(m))


@dataclass(frozen=True)
class LevelConfig:
    backend_feed_url: str = "http://edgelane-backend:8789/internal/feed"
    internal_feed_token: str = ""
    symbol: str = "SPX"
    tick_sec: int = 20
    session: str = "09:30-16:00"
    ratio_lock_time: str = "09:35"
    buffer_pts: float = 2.5
    test_pts: float = 6.0
    hold_min: float = 10.0
    grace_sec: float = 60.0
    knife_pts: float = 5.0
    cluster_pts: float = 5.0
    contest_crosses: int = 3
    contest_window_min: float = 30.0
    detail_window_min: float = 30.0
    evidence_half_life_min: float = 15.0
    evidence_scale: float = 1.0
    lean_logodds: float = 0.5
    thin_floor: float = 0.15
    decide_margin: float = 0.20
    flip_margin: float = 0.30
    min_dwell_min: float = 5.0
    max_repass: int = 2
    reach: str = "both"
    hit_tol_pts: float = 2.0          # §12: directional win = spot within this of the target
    lvl_db_url: str = ""
    lvl_replay_db_url: str = ""
    read_port: int = 8791
    admin_port: int = 8790
    strike_step: float = 5.0          # SPX listed-strike grid (§6)
    typo_check_pct: float = 30.0      # §17: admin asks to confirm a level > ±this % from the last trade; 0 = off

    # ---- derived ----
    @property
    def session_open(self) -> time:
        return _hhmm(self.session.split("-")[0])

    @property
    def session_close(self) -> time:
        return _hhmm(self.session.split("-")[1])

    @property
    def ratio_lock(self) -> time:
        return _hhmm(self.ratio_lock_time)

    def validate(self) -> "LevelConfig":
        if not self.test_pts > self.buffer_pts:
            raise ConfigError("TEST_PTS must be > BUFFER_PTS")
        if not self.knife_pts >= 2 * self.buffer_pts:
            raise ConfigError("KNIFE_PTS must be >= 2*BUFFER_PTS")
        if not self.cluster_pts >= 2 * self.buffer_pts:
            raise ConfigError("CLUSTER_PTS must be >= 2*BUFFER_PTS")
        if self.reach not in ("both", "near", "extreme"):
            raise ConfigError("REACH must be both|near|extreme")
        if self.hit_tol_pts < 0:
            raise ConfigError("HIT_TOL_PTS must be >= 0")
        if not (self.typo_check_pct >= 0 and self.typo_check_pct != float("inf")):
            raise ConfigError("TYPO_CHECK_PCT must be >= 0 (0 disables the check)")
        if self.tick_sec <= 0 or self.hold_min <= 0:
            raise ConfigError("TICK_SEC and HOLD_MIN must be > 0")
        self.session_open, self.session_close, self.ratio_lock  # parse check
        return self

    def with_overrides(self, **kw) -> "LevelConfig":
        return replace(self, **kw).validate()


def parse_kv(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        out[k.strip().upper()] = v.split(" #")[0].strip().strip('"').strip("'")
    return out


def from_mapping(raw: dict[str, str], base: LevelConfig | None = None) -> LevelConfig:
    base = base or LevelConfig()
    kw = {}
    for f in fields(LevelConfig):
        key = f.name.upper()
        if key not in raw:
            continue
        v = raw[key]
        typ = type(getattr(base, f.name))
        try:
            kw[f.name] = typ(v) if typ is not str else v
        except ValueError as e:
            raise ConfigError(f"{key}: {e}") from e
    return replace(base, **kw).validate()


def load_config(path: str | Path | None = None) -> LevelConfig:
    """Load the config file (path arg → $LEVEL_ENGINE_CONFIG → package default).
    A missing file yields the defaults (tests / replay with no secrets)."""
    p = Path(path or os.environ.get(ENV_VAR) or DEFAULT_PATH)
    raw = parse_kv(p.read_text()) if p.is_file() else {}
    return from_mapping(raw)

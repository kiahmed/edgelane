"""DuckDB schema + connection management for EdgeLane MARKET.

Tables:
    gex_snapshots      one row per (symbol, expiration, strike, ts)
    bias_decisions     one row per derived bias (poller-submitted)
    outcomes           evaluator-populated win/loss per decision
    outcome_daily_summary  one row per (session_date, symbol): end-of-day rollup
                       + data-quality flag (see evaluator.archive_completed_days)
"""
from __future__ import annotations
import os
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
import duckdb


def _utc_iso(v):
    """Render a stored (naive-UTC) datetime as an explicit UTC ISO string (…Z).

    DuckDB hands back naive datetimes; serialized bare they read as local time in
    the browser. Stamping UTC keeps the instant unambiguous. Passes through None
    and already-formatted strings untouched.
    """
    if isinstance(v, datetime):
        if v.tzinfo is None:
            v = v.replace(tzinfo=timezone.utc)
        return v.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    return v


_SCHEMA = """
CREATE SEQUENCE IF NOT EXISTS seq_gex_snapshots;
CREATE TABLE IF NOT EXISTS gex_snapshots (
    id            BIGINT       PRIMARY KEY DEFAULT nextval('seq_gex_snapshots'),
    ts            TIMESTAMP    NOT NULL,
    symbol        VARCHAR      NOT NULL,
    expiration    DATE         NOT NULL,
    spot          DOUBLE       NOT NULL,
    strike        DOUBLE       NOT NULL,
    call_gex      DOUBLE,
    put_gex       DOUBLE,
    net_gex       DOUBLE,
    call_oi       INTEGER,
    put_oi        INTEGER
);

CREATE INDEX IF NOT EXISTS idx_gex_lookup
    ON gex_snapshots (symbol, expiration, ts);

CREATE SEQUENCE IF NOT EXISTS seq_bias_decisions;
CREATE TABLE IF NOT EXISTS bias_decisions (
    id                    BIGINT     PRIMARY KEY DEFAULT nextval('seq_bias_decisions'),
    ts                    TIMESTAMP  NOT NULL,
    symbol                VARCHAR    NOT NULL,
    expiration            DATE       NOT NULL,
    spot_at_decision      DOUBLE     NOT NULL,
    score                 DOUBLE,
    label                 VARCHAR,
    confidence            VARCHAR,
    put_wall_strike       DOUBLE,
    put_wall_strength     VARCHAR,
    put_wall_net_gex      DOUBLE,
    call_wall_strike      DOUBLE,
    call_wall_strength    VARCHAR,
    call_wall_net_gex     DOUBLE,
    recommended_strategies VARCHAR,
    -- Engine-pick capture for spread-outcome eval (see docs/spread_outcome_eval.md).
    -- NULL when the poll produced no pick → the row is never graded.
    pick_legs             VARCHAR,    -- JSON: the pick's legs (strike/side/long_short/symbol)
    pick_entry_mid        DOUBLE,     -- the pick's mid net premium at decision
    pick_spread_type      VARCHAR,    -- 'credit' | 'debit'
    pick_strategy         VARCHAR     -- strategy key (bull_put, bear_call, …)
);
CREATE INDEX IF NOT EXISTS idx_bias_lookup
    ON bias_decisions (symbol, ts);

CREATE TABLE IF NOT EXISTS outcomes (
    decision_id        BIGINT       PRIMARY KEY,
    evaluated_at       TIMESTAMP    NOT NULL,
    spot_at_eval       DOUBLE       NOT NULL,
    elapsed_minutes    DOUBLE       NOT NULL,
    predicted_direction VARCHAR,
    actual_move_pct    DOUBLE,
    result             VARCHAR,
    -- Spread-outcome eval columns (additive; legacy spot fields above are kept
    -- as context). favorable_delta = premium move in the pick's profit direction.
    entry_net_premium  DOUBLE,
    eval_net_premium   DOUBLE,
    favorable_delta    DOUBLE,
    friction_band      DOUBLE,
    spread_type        VARCHAR
);

-- End-of-day rollup + data-quality flag, one row per (session_date, symbol).
-- Written by the evaluator after the ET day rolls over (archive_completed_days).
-- Raw bias_decisions/outcomes are kept forever; this is the modeling-friendly,
-- deduped daily record. `complete` = session was fully/mostly covered with no big
-- polling gaps (partial days are kept but flagged so modeling can filter them out).
CREATE TABLE IF NOT EXISTS outcome_daily_summary (
    session_date  DATE       NOT NULL,
    symbol        VARCHAR    NOT NULL,
    n             INTEGER    NOT NULL,
    wins          INTEGER    NOT NULL,
    losses        INTEGER    NOT NULL,
    neutrals      INTEGER    NOT NULL,
    accuracy_pct  DOUBLE,
    first_ts      TIMESTAMP,
    last_ts       TIMESTAMP,
    span_min      DOUBLE,
    max_gap_min   DOUBLE,
    coverage_pct  DOUBLE,
    complete      BOOLEAN    NOT NULL,
    created_at    TIMESTAMP  NOT NULL,
    PRIMARY KEY (session_date, symbol)
);

-- Additive migrations for DuckDB files created before the spread-outcome eval.
ALTER TABLE bias_decisions ADD COLUMN IF NOT EXISTS pick_legs VARCHAR;
ALTER TABLE bias_decisions ADD COLUMN IF NOT EXISTS pick_entry_mid DOUBLE;
ALTER TABLE bias_decisions ADD COLUMN IF NOT EXISTS pick_spread_type VARCHAR;
ALTER TABLE bias_decisions ADD COLUMN IF NOT EXISTS pick_strategy VARCHAR;
ALTER TABLE outcomes ADD COLUMN IF NOT EXISTS entry_net_premium DOUBLE;
ALTER TABLE outcomes ADD COLUMN IF NOT EXISTS eval_net_premium DOUBLE;
ALTER TABLE outcomes ADD COLUMN IF NOT EXISTS favorable_delta DOUBLE;
ALTER TABLE outcomes ADD COLUMN IF NOT EXISTS friction_band DOUBLE;
ALTER TABLE outcomes ADD COLUMN IF NOT EXISTS spread_type VARCHAR;

CREATE SEQUENCE IF NOT EXISTS seq_gex_profile_snapshots;
CREATE TABLE IF NOT EXISTS gex_profile_snapshots (
    id          BIGINT    PRIMARY KEY DEFAULT nextval('seq_gex_profile_snapshots'),
    ts          TIMESTAMP NOT NULL,
    symbol      VARCHAR   NOT NULL,
    expiration  DATE      NOT NULL,
    spot        DOUBLE    NOT NULL,
    strike      DOUBLE    NOT NULL,
    net_gex     DOUBLE
);
CREATE INDEX IF NOT EXISTS idx_gex_profile_lookup
    ON gex_profile_snapshots (symbol, expiration, ts);

CREATE SEQUENCE IF NOT EXISTS seq_edgelane_provider_display;
CREATE TABLE IF NOT EXISTS edgelane_provider_display (
    id                  BIGINT    PRIMARY KEY DEFAULT nextval('seq_edgelane_provider_display'),
    ts                  TIMESTAMP NOT NULL,
    symbol              VARCHAR   NOT NULL,
    spot                DOUBLE,
    magnet_strike       DOUBLE,
    magnet_score        DOUBLE,
    displayed_target    DOUBLE,
    displayed_sentiment VARCHAR,
    vol_swing           DOUBLE,
    vol_longterm        VARCHAR
);
CREATE INDEX IF NOT EXISTS idx_edgelane_provider_display_lookup
    ON edgelane_provider_display (symbol, ts);

-- Per-ticker debit-spread strike-selection config (see strike_profiles.py).
-- NULL width bounds / offset mean "derive from the expected move".
CREATE TABLE IF NOT EXISTS strike_profiles (
    symbol          VARCHAR PRIMARY KEY,
    enabled         BOOLEAN,
    long_delta_lo   DOUBLE,
    long_delta_hi   DOUBLE,
    long_offset_pts DOUBLE,
    short_min_delta DOUBLE,
    min_width_pts   DOUBLE,
    max_width_pts   DOUBLE,
    target_source   VARCHAR,
    round_snap      DOUBLE,
    min_oi          INTEGER,
    min_vol         INTEGER,
    updated_at      TIMESTAMP
);
"""

_STRIKE_PROFILE_COLS = (
    "symbol", "enabled", "long_delta_lo", "long_delta_hi", "long_offset_pts",
    "short_min_delta", "min_width_pts", "max_width_pts", "target_source",
    "round_snap", "min_oi", "min_vol",
)


class Database:
    """Thin wrapper around a single DuckDB connection.
    DuckDB connections are NOT thread-safe; we serialize via Lock."""

    def __init__(self, path: Path | str):
        self.path = Path(os.path.expanduser(str(path)))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()
        self._conn: duckdb.DuckDBPyConnection | None = None

    def connect(self) -> duckdb.DuckDBPyConnection:
        if self._conn is None:
            self._conn = duckdb.connect(str(self.path))
            self.migrate()
        return self._conn

    def migrate(self) -> None:
        if self._conn is None:
            self._conn = duckdb.connect(str(self.path))
        with self._lock:
            self._conn.execute(_SCHEMA)
        self._seed_strike_profiles()

    # ── Strike profiles (debit smart-picker config) ──────────────────────────
    def _seed_strike_profiles(self) -> None:
        """Insert the built-in DEFAULT + SPX profiles on first run. Uses
        INSERT OR IGNORE so a user's later edits are never clobbered."""
        from .strike_profiles import SEED_PROFILES
        cols = ", ".join(_STRIKE_PROFILE_COLS)
        ph = ", ".join("?" for _ in _STRIKE_PROFILE_COLS)
        with self._lock:
            for p in SEED_PROFILES:
                row = p.to_row()
                self.connect().execute(
                    f"INSERT OR IGNORE INTO strike_profiles ({cols}, updated_at) "
                    f"VALUES ({ph}, now())",
                    [row.get(c) for c in _STRIKE_PROFILE_COLS],
                )

    def get_strike_profile(self, symbol: str) -> dict | None:
        with self._lock:
            cur = self.connect().execute(
                f"SELECT {', '.join(_STRIKE_PROFILE_COLS)} FROM strike_profiles WHERE symbol = ?",
                [symbol.upper()],
            )
            r = cur.fetchone()
        return dict(zip(_STRIKE_PROFILE_COLS, r)) if r else None

    def list_strike_profiles(self) -> list[dict]:
        with self._lock:
            cur = self.connect().execute(
                f"SELECT {', '.join(_STRIKE_PROFILE_COLS)} FROM strike_profiles ORDER BY symbol"
            )
            rows = cur.fetchall()
        return [dict(zip(_STRIKE_PROFILE_COLS, r)) for r in rows]

    def upsert_strike_profile(self, profile: dict) -> None:
        cols = ", ".join(_STRIKE_PROFILE_COLS)
        ph = ", ".join("?" for _ in _STRIKE_PROFILE_COLS)
        with self._lock:
            self.connect().execute(
                f"INSERT OR REPLACE INTO strike_profiles ({cols}, updated_at) "
                f"VALUES ({ph}, now())",
                [profile.get(c) for c in _STRIKE_PROFILE_COLS],
            )

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    def insert_gex_snapshot(self, row: dict) -> None:
        with self._lock:
            self.connect().execute(
                """
                INSERT INTO gex_snapshots
                  (ts, symbol, expiration, spot, strike, call_gex, put_gex, net_gex, call_oi, put_oi)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    row["ts"], row["symbol"], row["expiration"], row["spot"], row["strike"],
                    row.get("call_gex"), row.get("put_gex"), row.get("net_gex"),
                    row.get("call_oi"), row.get("put_oi"),
                ],
            )

    def insert_gex_profile_snapshot(self, row: dict) -> None:
        with self._lock:
            self.connect().execute(
                "INSERT INTO gex_profile_snapshots (ts,symbol,expiration,spot,strike,net_gex) VALUES (?,?,?,?,?,?)",
                [row["ts"], row["symbol"], row["expiration"], row["spot"], row["strike"], row.get("net_gex")],
            )

    def insert_edgelane_provider_display(self, row: dict) -> None:
        with self._lock:
            self.connect().execute(
                "INSERT INTO edgelane_provider_display (ts,symbol,spot,magnet_strike,magnet_score,"
                "displayed_target,displayed_sentiment,vol_swing,vol_longterm) VALUES (?,?,?,?,?,?,?,?,?)",
                [row["ts"], row["symbol"], row.get("spot"), row.get("magnet_strike"), row.get("magnet_score"),
                 row.get("displayed_target"), row.get("displayed_sentiment"), row.get("vol_swing"), row.get("vol_longterm")],
            )

    def insert_bias_decision(self, row: dict) -> int:
        with self._lock:
            cur = self.connect().execute(
                """
                INSERT INTO bias_decisions
                  (ts, symbol, expiration, spot_at_decision, score, label, confidence,
                   put_wall_strike, put_wall_strength, put_wall_net_gex,
                   call_wall_strike, call_wall_strength, call_wall_net_gex,
                   recommended_strategies,
                   pick_legs, pick_entry_mid, pick_spread_type, pick_strategy)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                RETURNING id
                """,
                [
                    row["ts"], row["symbol"], row["expiration"], row["spot_at_decision"],
                    row.get("score"), row.get("label"), row.get("confidence"),
                    row.get("put_wall_strike"), row.get("put_wall_strength"), row.get("put_wall_net_gex"),
                    row.get("call_wall_strike"), row.get("call_wall_strength"), row.get("call_wall_net_gex"),
                    row.get("recommended_strategies"),
                    row.get("pick_legs"), row.get("pick_entry_mid"),
                    row.get("pick_spread_type"), row.get("pick_strategy"),
                ],
            )
            (new_id,) = cur.fetchone()
            return int(new_id)

    def insert_outcome(self, row: dict) -> None:
        with self._lock:
            self.connect().execute(
                """
                INSERT INTO outcomes
                  (decision_id, evaluated_at, spot_at_eval, elapsed_minutes,
                   predicted_direction, actual_move_pct, result,
                   entry_net_premium, eval_net_premium, favorable_delta,
                   friction_band, spread_type)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    row["decision_id"], row["evaluated_at"], row["spot_at_eval"], row["elapsed_minutes"],
                    row.get("predicted_direction"), row.get("actual_move_pct"), row.get("result"),
                    row.get("entry_net_premium"), row.get("eval_net_premium"),
                    row.get("favorable_delta"), row.get("friction_band"), row.get("spread_type"),
                ],
            )

    def fetch_pending_evaluations(self, eval_window_min: int) -> list[tuple]:
        with self._lock:
            cur = self.connect().execute(
                """
                SELECT bd.id, bd.ts, bd.symbol, bd.spot_at_decision, bd.label, bd.score,
                       bd.pick_legs, bd.pick_entry_mid, bd.pick_spread_type, bd.pick_strategy
                FROM bias_decisions bd
                LEFT JOIN outcomes o ON o.decision_id = bd.id
                WHERE o.decision_id IS NULL
                  AND bd.pick_legs IS NOT NULL
                  AND bd.ts <= CURRENT_TIMESTAMP - INTERVAL (?) MINUTE
                ORDER BY bd.ts ASC
                """,
                [eval_window_min],
            )
            return cur.fetchall()

    def fetch_accuracy(self, symbol: str, window: int) -> dict:
        with self._lock:
            cur = self.connect().execute(
                """
                WITH recent AS (
                    SELECT bd.id, bd.label, o.result, bd.recommended_strategies
                    FROM bias_decisions bd
                    JOIN outcomes o ON o.decision_id = bd.id
                    -- Count ONLY spread-outcome grades. Legacy spot-diff rows
                    -- (graded before the premium-based eval) have a NULL
                    -- favorable_delta; excluding them gives a clean start on the
                    -- new metric without deleting the historical rows.
                    WHERE bd.symbol = ? AND o.favorable_delta IS NOT NULL
                    ORDER BY bd.ts DESC
                    LIMIT ?
                )
                SELECT
                    COUNT(*) AS n,
                    SUM(CASE WHEN result = 'win' THEN 1 ELSE 0 END) AS wins,
                    SUM(CASE WHEN result = 'loss' THEN 1 ELSE 0 END) AS losses,
                    SUM(CASE WHEN result = 'neutral' THEN 1 ELSE 0 END) AS neutrals
                FROM recent
                """,
                [symbol, window],
            )
            row = cur.fetchone()
            n, wins, losses, neutrals = (row or (0, 0, 0, 0))
            n = int(n or 0)
            wins = int(wins or 0)
            losses = int(losses or 0)
            neutrals = int(neutrals or 0)
            accuracy = (wins / n * 100) if n > 0 else 0.0
            return {
                "n": n,
                "wins": wins,
                "losses": losses,
                "neutrals": neutrals,
                "accuracy_pct": round(accuracy, 1),
            }

    def fetch_recent_outcomes(self, symbol: str, limit: int = 10) -> list[dict]:
        with self._lock:
            cur = self.connect().execute(
                """
                SELECT bd.id, bd.ts, bd.label, bd.spot_at_decision,
                       o.evaluated_at, o.spot_at_eval, o.actual_move_pct,
                       o.predicted_direction, o.result,
                       bd.pick_strategy, o.spread_type,
                       o.entry_net_premium, o.eval_net_premium,
                       o.favorable_delta, o.friction_band
                FROM bias_decisions bd
                JOIN outcomes o ON o.decision_id = bd.id
                -- Spread-outcome grades only (NULL favorable_delta = legacy spot-diff row).
                WHERE bd.symbol = ? AND o.favorable_delta IS NOT NULL
                ORDER BY o.evaluated_at DESC
                LIMIT ?
                """,
                [symbol, limit],
            )
            cols = ['id', 'ts', 'label', 'spot_at_decision',
                    'evaluated_at', 'spot_at_eval', 'actual_move_pct',
                    'predicted_direction', 'result',
                    'pick_strategy', 'spread_type',
                    'entry_net_premium', 'eval_net_premium',
                    'favorable_delta', 'friction_band']
            out = [dict(zip(cols, row)) for row in cur.fetchall()]
            # Timestamps are stored as naive-UTC; emit them as explicit UTC ISO
            # (…Z) so the browser's new Date() parses the correct instant and
            # localizes it, instead of misreading a bare "T19:59" as local time.
            for r in out:
                for k in ("ts", "evaluated_at"):
                    r[k] = _utc_iso(r.get(k))
            return out

    def fetch_regime_replay(self, per_symbol: int = 200, since=None) -> list[tuple]:
        """Recent spread-outcome results per symbol, oldest→newest, for rebuilding
        the in-memory regime counters after a restart (see evaluator.rehydrate_regime).

        Returns (symbol, result) tuples. `since` (a datetime) scopes the replay to
        the current trading session — yesterday's streak belongs to a different
        market regime and must not carry over (see evaluator.rehydrate_regime).
        Only the last `per_symbol` graded outcomes of each ticker are replayed —
        more than enough to reproduce the current consecutive-loss/win streak (any
        opposite result resets the counter). Legacy spot-diff rows (NULL
        favorable_delta) are excluded, matching fetch_accuracy.
        """
        where = "o.favorable_delta IS NOT NULL"
        params: list = []
        if since is not None:
            where += " AND bd.ts >= ?"
            params.append(since)
        params.append(per_symbol)
        with self._lock:
            cur = self.connect().execute(
                f"""
                SELECT symbol, result FROM (
                    SELECT bd.symbol AS symbol, o.result AS result, bd.ts AS ts,
                           ROW_NUMBER() OVER (
                               PARTITION BY bd.symbol ORDER BY bd.ts DESC
                           ) AS rn
                    FROM bias_decisions bd
                    JOIN outcomes o ON o.decision_id = bd.id
                    WHERE {where}
                )
                WHERE rn <= ?
                ORDER BY symbol ASC, rn DESC
                """,
                params,
            )
            return cur.fetchall()

    def fetch_graded_for_archive(self, before) -> list[tuple]:
        """(symbol, ts, result) for every graded outcome with bd.ts < `before`
        (a UTC datetime = start of today ET). Feeds the end-of-day rollup; the
        caller buckets by ET calendar date (done in Python so the day boundary
        matches the rest of the regime logic — no DuckDB tz math)."""
        with self._lock:
            cur = self.connect().execute(
                """
                SELECT bd.symbol, bd.ts, o.result
                FROM bias_decisions bd
                JOIN outcomes o ON o.decision_id = bd.id
                WHERE o.favorable_delta IS NOT NULL AND bd.ts < ?
                ORDER BY bd.symbol ASC, bd.ts ASC
                """,
                [before],
            )
            return cur.fetchall()

    def summarized_pairs(self) -> set[tuple]:
        """Set of (session_date_iso, symbol) already rolled up — so archival only
        computes days it hasn't seen (idempotent, cheap on repeat runs)."""
        with self._lock:
            cur = self.connect().execute(
                "SELECT session_date, symbol FROM outcome_daily_summary"
            )
            out = set()
            for d, sym in cur.fetchall():
                out.add((d.isoformat() if hasattr(d, "isoformat") else str(d), sym))
            return out

    def upsert_daily_summary(self, row: dict) -> None:
        with self._lock:
            self.connect().execute(
                """
                INSERT OR REPLACE INTO outcome_daily_summary
                  (session_date, symbol, n, wins, losses, neutrals, accuracy_pct,
                   first_ts, last_ts, span_min, max_gap_min, coverage_pct,
                   complete, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    row["session_date"], row["symbol"], row["n"], row["wins"],
                    row["losses"], row["neutrals"], row.get("accuracy_pct"),
                    row.get("first_ts"), row.get("last_ts"), row.get("span_min"),
                    row.get("max_gap_min"), row.get("coverage_pct"),
                    row["complete"], row["created_at"],
                ],
            )

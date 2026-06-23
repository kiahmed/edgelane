"""DuckDB schema + connection management for EdgeLane MARKET.

Tables:
    gex_snapshots      one row per (symbol, expiration, strike, ts)
    bias_decisions     one row per derived bias (poller-submitted)
    outcomes           evaluator-populated win/loss per decision
"""
from __future__ import annotations
import os
from pathlib import Path
from threading import Lock
import duckdb


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
    recommended_strategies VARCHAR
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
    result             VARCHAR
);

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
                   recommended_strategies)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                RETURNING id
                """,
                [
                    row["ts"], row["symbol"], row["expiration"], row["spot_at_decision"],
                    row.get("score"), row.get("label"), row.get("confidence"),
                    row.get("put_wall_strike"), row.get("put_wall_strength"), row.get("put_wall_net_gex"),
                    row.get("call_wall_strike"), row.get("call_wall_strength"), row.get("call_wall_net_gex"),
                    row.get("recommended_strategies"),
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
                   predicted_direction, actual_move_pct, result)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    row["decision_id"], row["evaluated_at"], row["spot_at_eval"], row["elapsed_minutes"],
                    row.get("predicted_direction"), row.get("actual_move_pct"), row.get("result"),
                ],
            )

    def fetch_pending_evaluations(self, eval_window_min: int) -> list[tuple]:
        with self._lock:
            cur = self.connect().execute(
                """
                SELECT bd.id, bd.ts, bd.symbol, bd.spot_at_decision, bd.label, bd.score
                FROM bias_decisions bd
                LEFT JOIN outcomes o ON o.decision_id = bd.id
                WHERE o.decision_id IS NULL
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
                    WHERE bd.symbol = ?
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
                       o.predicted_direction, o.result
                FROM bias_decisions bd
                JOIN outcomes o ON o.decision_id = bd.id
                WHERE bd.symbol = ?
                ORDER BY o.evaluated_at DESC
                LIMIT ?
                """,
                [symbol, limit],
            )
            cols = ['id', 'ts', 'label', 'spot_at_decision',
                    'evaluated_at', 'spot_at_eval', 'actual_move_pct',
                    'predicted_direction', 'result']
            return [dict(zip(cols, row)) for row in cur.fetchall()]

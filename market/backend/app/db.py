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

-- Volume alongside OI. The poller already normalizes contract volume; it was
-- simply never persisted. Vol/OI (is today's volume NEW positioning?) and the
-- day-over-day OI delta (how much of it STUCK?) are the two order-flow signals
-- with peer-reviewed support at a 1-5 day horizon, and both need this column.
ALTER TABLE gex_snapshots ADD COLUMN IF NOT EXISTS call_volume INTEGER;
ALTER TABLE gex_snapshots ADD COLUMN IF NOT EXISTS put_volume  INTEGER;

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

-- ---------------------------------------------------------------------------
-- Simmer — premium-selling readiness engine (see docs/simmer.md).
-- NOTE: none of these tables carries a user identifier. They are market data,
-- computed once per (symbol[, expiration]) and shared across every user
-- watching that ticker; all user-scoped state (watchlists, alerts, settings)
-- lives in Supabase under RLS. Per-user thresholds are applied at READ time.
-- ---------------------------------------------------------------------------

-- Daily IV/RV history. This is the series IV Rank and IV Percentile are computed
-- from, so it only accrues forward -- start recording before anything reads it.
-- atm_iv_xern is EX-EARNINGS: including the earnings hump would let one
-- quarterly spike pin the 52-week max for a year.
CREATE TABLE IF NOT EXISTS simmer_iv_history (
    session_date  DATE    NOT NULL,
    symbol        VARCHAR NOT NULL,
    atm_iv        DOUBLE,          -- 30d constant-maturity, variance-interpolated
    atm_iv_xern   DOUBLE,          -- same, earnings effect removed
    iv60          DOUBLE,
    iv90          DOUBLE,
    rv20_yz       DOUBLE,          -- Yang-Zhang realized vol, 20d
    rv20_cc       DOUBLE,          -- zero-mean close-to-close, 20d (cross-check)
    open_px       DOUBLE,
    high_px       DOUBLE,
    low_px        DOUBLE,
    close_px      DOUBLE,
    skew_25d      DOUBLE,          -- IV(25d put) - IV(25d call), equity convention
    PRIMARY KEY (session_date, symbol)
);
CREATE INDEX IF NOT EXISTS idx_simmer_iv_history_symbol
    ON simmer_iv_history (symbol, session_date);

-- Tier-1 research cache: keyed on SYMBOL ONLY, so a second expiration on the
-- same ticker reuses the whole block. Per-field TTLs are enforced in code via
-- the *_at columns; a catalyst or velocity burst invalidates regardless of age.
CREATE TABLE IF NOT EXISTS simmer_research_cache (
    symbol             VARCHAR PRIMARY KEY,
    sentiment_score    DOUBLE,      -- trailing 5-session weighted mean, -1..+1
    sentiment_n        INTEGER,     -- headline count: distinguishes "0.0 balanced"
                                    -- from "0.0 no news"
    velocity_p         DOUBLE,      -- NB upper-tail p-value
    velocity_tier      VARCHAR,     -- none | warn | alert
    catalyst_flags     VARCHAR,     -- JSON: earnings/ex-div/8-K items in window
    catalyst_blocking  BOOLEAN,
    iv_rank            DOUBLE,
    iv_percentile      DOUBLE,
    iv_rv_ratio        DOUBLE,
    vrp_xsect_pct      DOUBLE,      -- cross-sectional VRP percentile (cold-start path)
    vol_oi_ratio       DOUBLE,
    oi_delta           DOUBLE,
    pcr_volume         DOUBLE,
    short_interest     DOUBLE,      -- v1.1
    days_to_cover      DOUBLE,      -- v1.1
    news_at            TIMESTAMP,
    catalyst_at        TIMESTAMP,
    daily_at           TIMESTAMP,   -- IV rank / RV / OI-delta group
    flow_at            TIMESTAMP,
    updated_at         TIMESTAMP
);

-- The engine's vol inputs must SURVIVE the cache round-trip: transient
-- underscore keys are stripped before upsert, so RV and the IV-history series
-- are persisted here and re-hydrated on read (found live: every cache-hit
-- sweep fed the engine an empty vol block and vetoed vrp_unavailable).
ALTER TABLE simmer_research_cache ADD COLUMN IF NOT EXISTS rv20_yz         DOUBLE;
ALTER TABLE simmer_research_cache ADD COLUMN IF NOT EXISTS rv20_cc         DOUBLE;
ALTER TABLE simmer_research_cache ADD COLUMN IF NOT EXISTS iv_history_json VARCHAR;
ALTER TABLE simmer_research_cache ADD COLUMN IF NOT EXISTS ohlc_json       VARCHAR;
ALTER TABLE simmer_research_cache ADD COLUMN IF NOT EXISTS closes_json     VARCHAR;

-- Tier-2 readiness: keyed (symbol, expiration, ts). Recomputed EVERY sweep and
-- never served from cache across sweeps -- the chain moves, and a stale "ready"
-- would recommend a spread whose credit has since collapsed.
CREATE SEQUENCE IF NOT EXISTS seq_simmer_readiness;
CREATE TABLE IF NOT EXISTS simmer_readiness (
    id                BIGINT    PRIMARY KEY DEFAULT nextval('seq_simmer_readiness'),
    ts                TIMESTAMP NOT NULL,
    symbol            VARCHAR   NOT NULL,
    expiration        DATE      NOT NULL,
    spot              DOUBLE,
    score             DOUBLE,             -- 0-100 composite, NULL when vetoed
    vetoed            BOOLEAN,
    veto_reasons      VARCHAR,            -- JSON array of gate keys that tripped
    components        VARCHAR,            -- JSON: per-component sub-scores
    regime            VARCHAR,            -- calm | risk_off | stressed
    structure         VARCHAR,            -- bull_put | bear_call | iron_condor
    short_strike      DOUBLE,
    long_strike       DOUBLE,
    width             DOUBLE,
    credit_mid        DOUBLE,             -- advertised
    credit_fill       DOUBLE,             -- achievable-fill estimate (what we rank on)
    max_loss          DOUBLE,
    pop_breakeven     DOUBLE,             -- POP at the BREAKEVEN, not the short strike
    expected_value    DOUBLE,             -- payoff-integrated, not the binary formula
    alpha             DOUBLE,             -- EV / MaxLoss
    engine_version    VARCHAR
);
CREATE INDEX IF NOT EXISTS idx_simmer_readiness_lookup
    ON simmer_readiness (symbol, expiration, ts);

-- Scored headlines, deduped by cluster. Cache by article id so the same wire
-- story is never re-scored -- this is the main LLM cost control.
CREATE TABLE IF NOT EXISTS simmer_news (
    article_id     VARCHAR PRIMARY KEY,
    symbol         VARCHAR   NOT NULL,
    cluster_key    VARCHAR,            -- 3-shingle hash; counts clusters, not articles
    headline       VARCHAR,
    source         VARCHAR,
    url            VARCHAR,
    published_at   TIMESTAMP,
    sentiment      DOUBLE,             -- -1..+1, clamped server-side
    confidence     DOUBLE,
    model          VARCHAR,            -- model id: scores are NOT reproducible
    scored_at      TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_simmer_news_symbol_pub
    ON simmer_news (symbol, published_at);
CREATE INDEX IF NOT EXISTS idx_simmer_news_cluster
    ON simmer_news (symbol, cluster_key, published_at);

-- Binary events. acceptance_at (NOT filing_date) is authoritative: an
-- after-hours 8-K carries the NEXT business day's filing date, and that is
-- exactly the filing that gaps the underlying at tomorrow's open.
CREATE TABLE IF NOT EXISTS simmer_catalysts (
    id             VARCHAR PRIMARY KEY,   -- accession no. / vendor id / synthetic
    symbol         VARCHAR   NOT NULL,
    event_type     VARCHAR   NOT NULL,    -- earnings | sec_filing | ex_div | macro
    event_date     DATE,
    session        VARCHAR,               -- bmo | amc | dmh | unknown
    confirmed      BOOLEAN,               -- false => treat as a DISTRIBUTION, block the week
    blocking       BOOLEAN,               -- hard-block item (4.02/1.03/3.01/4.01/NT/424B)
    detail         VARCHAR,               -- JSON: 8-K item numbers, vendor payload
    source         VARCHAR,
    acceptance_at  TIMESTAMP,
    created_at     TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_simmer_catalysts_symbol_date
    ON simmer_catalysts (symbol, event_date);

-- Paper outcomes: did the short strike hold? Recorded from the moment the engine
-- starts emitting, and keyed to the ENGINE's verdict -- never to any user's
-- filtered view -- so calibration stays comparable across configurations.
CREATE TABLE IF NOT EXISTS simmer_outcomes (
    readiness_id      BIGINT PRIMARY KEY,
    symbol            VARCHAR NOT NULL,
    expiration        DATE    NOT NULL,
    evaluated_at      TIMESTAMP,
    spot_at_expiry    DOUBLE,
    short_strike      DOUBLE,
    held              BOOLEAN,            -- short strike never breached at expiry
    touched           BOOLEAN,            -- breached intraday at any point
    max_adverse_pct   DOUBLE,
    score_at_entry    DOUBLE,
    components        VARCHAR,            -- snapshot, so components can be retired
    engine_version    VARCHAR
);
CREATE INDEX IF NOT EXISTS idx_simmer_outcomes_symbol
    ON simmer_outcomes (symbol, expiration);
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
                  (ts, symbol, expiration, spot, strike, call_gex, put_gex, net_gex,
                   call_oi, put_oi, call_volume, put_volume)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    row["ts"], row["symbol"], row["expiration"], row["spot"], row["strike"],
                    row.get("call_gex"), row.get("put_gex"), row.get("net_gex"),
                    row.get("call_oi"), row.get("put_oi"),
                    row.get("call_volume"), row.get("put_volume"),
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

    # ────────────────────────────────────────────────────────────────────────
    # Simmer (Phase 2) — every method resolves the connection BEFORE taking
    # self._lock: Database.connect() migrates under the (non-reentrant) lock on
    # first use, so `with self._lock: self.connect()` on a cold Database would
    # self-deadlock. See simmer_catalysts.persist_catalysts for the precedent.
    # All callers run these via asyncio.to_thread (DuckDB is sync).
    # ────────────────────────────────────────────────────────────────────────

    _SIMMER_RESEARCH_COLS = (
        "symbol", "sentiment_score", "sentiment_n", "velocity_p", "velocity_tier",
        "catalyst_flags", "catalyst_blocking", "iv_rank", "iv_percentile",
        "iv_rv_ratio", "vrp_xsect_pct", "vol_oi_ratio", "oi_delta", "pcr_volume",
        "short_interest", "days_to_cover", "news_at", "catalyst_at", "daily_at",
        "flow_at", "rv20_yz", "rv20_cc", "iv_history_json", "ohlc_json",
        "closes_json",
    )

    def upsert_simmer_research(self, row: dict) -> None:
        """Tier-1 research cache upsert (PK symbol). Keys not present in `row`
        are written as NULL — pass the merged row, not a partial delta."""
        conn = self.connect()
        cols = ", ".join(self._SIMMER_RESEARCH_COLS)
        ph = ", ".join("?" for _ in self._SIMMER_RESEARCH_COLS)
        params = [row.get(c) for c in self._SIMMER_RESEARCH_COLS]
        params[0] = str(row["symbol"]).upper()
        with self._lock:
            conn.execute(
                f"INSERT OR REPLACE INTO simmer_research_cache ({cols}, updated_at) "
                f"VALUES ({ph}, now())",
                params,
            )

    def get_simmer_research(self, symbol: str) -> dict | None:
        conn = self.connect()
        cols = self._SIMMER_RESEARCH_COLS + ("updated_at",)
        with self._lock:
            cur = conn.execute(
                f"SELECT {', '.join(cols)} FROM simmer_research_cache WHERE symbol = ?",
                [str(symbol).upper()],
            )
            r = cur.fetchone()
        return dict(zip(cols, r)) if r else None

    _SIMMER_READINESS_COLS = (
        "ts", "symbol", "expiration", "spot", "score", "vetoed", "veto_reasons",
        "components", "regime", "structure", "short_strike", "long_strike",
        "width", "credit_mid", "credit_fill", "max_loss", "pop_breakeven",
        "expected_value", "alpha", "engine_version",
    )

    def insert_simmer_readiness(self, row: dict) -> int:
        """Tier-2 readiness row (one per sweep per (symbol, expiration)).
        Returns the new id so the paper-outcome evaluator can key on it."""
        conn = self.connect()
        cols = ", ".join(self._SIMMER_READINESS_COLS)
        ph = ", ".join("?" for _ in self._SIMMER_READINESS_COLS)
        with self._lock:
            cur = conn.execute(
                f"INSERT INTO simmer_readiness ({cols}) VALUES ({ph}) RETURNING id",
                [row.get(c) for c in self._SIMMER_READINESS_COLS],
            )
            (new_id,) = cur.fetchone()
            return int(new_id)

    def latest_simmer_readiness_all(self) -> list[dict]:
        """Newest readiness row per (symbol, expiration) — the watcher's startup
        rehydration source. A restart or a market-closed idle period must NOT
        hide results that are sitting on disk: signed-in users see their
        tickers' last computed analysis regardless of process lifetime."""
        conn = self.connect()
        cols = ("id",) + self._SIMMER_READINESS_COLS
        with self._lock:
            cur = conn.execute(
                f"SELECT {', '.join(cols)} FROM simmer_readiness r "
                "WHERE ts = (SELECT max(ts) FROM simmer_readiness r2 "
                "            WHERE r2.symbol = r.symbol AND r2.expiration = r.expiration) "
                "ORDER BY symbol, expiration",
            )
            rows = cur.fetchall()
        out = []
        for r in rows:
            d = dict(zip(cols, r))
            d["ts"] = _utc_iso(d.get("ts"))
            out.append(d)
        return out

    def latest_simmer_readiness(self, symbol: str, expiration) -> dict | None:
        conn = self.connect()
        cols = ("id",) + self._SIMMER_READINESS_COLS
        with self._lock:
            cur = conn.execute(
                f"SELECT {', '.join(cols)} FROM simmer_readiness "
                "WHERE symbol = ? AND expiration = ? ORDER BY ts DESC LIMIT 1",
                [str(symbol).upper(), expiration],
            )
            r = cur.fetchone()
        if not r:
            return None
        out = dict(zip(cols, r))
        out["ts"] = _utc_iso(out.get("ts"))
        return out

    _SIMMER_IV_COLS = (
        "session_date", "symbol", "atm_iv", "atm_iv_xern", "iv60", "iv90",
        "rv20_yz", "rv20_cc", "open_px", "high_px", "low_px", "close_px",
        "skew_25d",
    )

    def insert_simmer_iv_history(self, row: dict) -> None:
        """One row per (session_date, symbol). INSERT OR REPLACE keeps the daily
        job idempotent across restarts within the same session."""
        conn = self.connect()
        cols = ", ".join(self._SIMMER_IV_COLS)
        ph = ", ".join("?" for _ in self._SIMMER_IV_COLS)
        params = [row.get(c) for c in self._SIMMER_IV_COLS]
        params[1] = str(row["symbol"]).upper()
        with self._lock:
            conn.execute(
                f"INSERT OR REPLACE INTO simmer_iv_history ({cols}) VALUES ({ph})",
                params,
            )

    def fetch_simmer_iv_history(self, symbol: str, days: int) -> list[dict]:
        """Most recent `days` rows for `symbol`, returned OLDEST→NEWEST (the
        order the RV estimators and IV-percentile history expect)."""
        conn = self.connect()
        with self._lock:
            cur = conn.execute(
                f"SELECT {', '.join(self._SIMMER_IV_COLS)} FROM simmer_iv_history "
                "WHERE symbol = ? ORDER BY session_date DESC LIMIT ?",
                [str(symbol).upper(), int(days)],
            )
            rows = cur.fetchall()
        out = [dict(zip(self._SIMMER_IV_COLS, r)) for r in rows]
        out.reverse()
        return out

    _SIMMER_OUTCOME_COLS = (
        "readiness_id", "symbol", "expiration", "evaluated_at", "spot_at_expiry",
        "short_strike", "held", "touched", "max_adverse_pct", "score_at_entry",
        "components", "engine_version",
    )

    def insert_simmer_outcome(self, row: dict) -> None:
        conn = self.connect()
        cols = ", ".join(self._SIMMER_OUTCOME_COLS)
        ph = ", ".join("?" for _ in self._SIMMER_OUTCOME_COLS)
        with self._lock:
            conn.execute(
                f"INSERT OR REPLACE INTO simmer_outcomes ({cols}) VALUES ({ph})",
                [row.get(c) for c in self._SIMMER_OUTCOME_COLS],
            )

    def fetch_pending_simmer_outcomes(self, as_of) -> list[dict]:
        """Readiness rows whose expiration has passed (< `as_of`, a date) and
        which have no outcome row yet.

        One row per (symbol, expiration): the LATEST row that actually carried a
        recommendation (short_strike NOT NULL) — the engine's final verdict
        before expiry. Grading every 5-minute sweep row of the same expiry would
        multiply the table ~78× per day for no additional information; earlier
        rows of a graded expiry are deliberately never returned."""
        conn = self.connect()
        cols = ("id", "ts", "symbol", "expiration", "spot", "score", "structure",
                "short_strike", "long_strike", "components", "engine_version")
        with self._lock:
            cur = conn.execute(
                f"""
                SELECT {', '.join(cols)} FROM (
                    SELECT r.*, ROW_NUMBER() OVER (
                        PARTITION BY r.symbol, r.expiration ORDER BY r.ts DESC
                    ) AS rn
                    FROM simmer_readiness r
                    WHERE r.expiration < ? AND r.short_strike IS NOT NULL
                )
                WHERE rn = 1
                  AND id NOT IN (SELECT readiness_id FROM simmer_outcomes)
                ORDER BY expiration ASC, symbol ASC
                """,
                [as_of],
            )
            rows = cur.fetchall()
        return [dict(zip(cols, r)) for r in rows]

    def fetch_simmer_outcome_summary(self) -> dict:
        """Aggregate paper-outcome stats — ENGINE verdicts only (outcome rows are
        keyed to readiness rows the watcher wrote without user settings)."""
        conn = self.connect()
        with self._lock:
            cur = conn.execute(
                """
                SELECT COALESCE(r.structure, 'unknown') AS structure,
                       COUNT(*) AS n,
                       SUM(CASE WHEN o.held THEN 1 ELSE 0 END) AS held,
                       SUM(CASE WHEN o.touched THEN 1 ELSE 0 END) AS touched,
                       AVG(o.score_at_entry) AS avg_score_at_entry
                FROM simmer_outcomes o
                LEFT JOIN simmer_readiness r ON r.id = o.readiness_id
                GROUP BY 1 ORDER BY 1
                """
            )
            rows = cur.fetchall()
        by_structure: dict[str, dict] = {}
        total_n = total_held = 0
        for structure, n, held, touched, avg_score in rows:
            n = int(n or 0)
            held = int(held or 0)
            by_structure[structure] = {
                "n": n,
                "held": held,
                "held_pct": round(held / n * 100.0, 1) if n else None,
                "touched": int(touched or 0),
                "avg_score_at_entry": round(float(avg_score), 2) if avg_score is not None else None,
            }
            total_n += n
            total_held += held
        return {
            "n": total_n,
            "held": total_held,
            "held_pct": round(total_held / total_n * 100.0, 1) if total_n else None,
            "by_structure": by_structure,
        }

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

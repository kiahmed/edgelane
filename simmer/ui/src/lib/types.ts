// Shapes mirrored from the backend's plain-dict responses. The backend is the
// source of truth (market/backend/app/routes/simmer.py + simmer_engine.py's
// evaluate_readiness envelope) — fields here were read from the source, not
// guessed. Everything is optional-tolerant because plain dicts evolve.

export type Decision = 'ready' | 'watch' | 'avoid' | 'vetoed';

export interface StrikesVertical {
	short: number;
	long: number;
	width: number;
}
export interface StrikesCondor {
	put: { short: number; long: number };
	call: { short: number; long: number };
}
export type Strikes = StrikesVertical | StrikesCondor;

export interface ComponentDetail {
	score: number;
	/** Resolved composite weight (after simmer_tickers.json overrides). Emitted
	 *  by the engine so the UI never hardcodes weights. Absent on pre-emit
	 *  cached envelopes. */
	weight?: number;
	[k: string]: unknown;
}

export interface DataQuality {
	iv_history?: number;
	iv_history_days?: number;
	iv_rank_provisional?: boolean;
	realized_vol?: number;
	rv_estimators_agree?: boolean;
	walls?: number;
	news?: number;
	quote?: number;
	macro_calendar?: boolean;
	[k: string]: unknown;
}

export interface Regime {
	state?: string;
	multiplier?: number;
	suppress?: string[];
	[k: string]: unknown;
}

/** evaluate_readiness() envelope (simmer_engine.py:1579), plus the fields the
 *  watcher adds before caching/persisting (computed_at, flow). */
export interface EarningsBlock {
	in_window: boolean;
	view?: 'consider' | 'ignore' | 'veto';
	applied?: boolean;
	held_back?: boolean;
	direction: 'bullish' | 'bearish' | 'neutral';
	confidence: number;
	go: boolean;
	lift?: number;
	close_before_print?: boolean;
	earnings_date?: string | null;
	rationale?: string | null;
	headline_count?: number;
	reasons?: string[];
	source?: string;
}

/** The other-view verdict the engine attaches so the card can switch
 *  consider↔ignore with no re-analysis. */
export interface EarningsAlt {
	view: 'consider' | 'ignore';
	decision: Decision;
	score: number;
	structure: string | null;
	strikes: Strikes | null;
	veto_reasons: string[];
	credit_fill: number | null;
	pop_breakeven: number | null;
	expected_value: number | null;
	alpha: number | null;
}

export interface EarningsResp {
	symbol: string;
	in_window: boolean;
	earnings_date: string | null;
	bias: EarningsBlock | null;
}

export interface ReadinessEnvelope {
	symbol: string;
	expiration: string | null;
	dte: number;
	spot: number;
	engine_version?: string;
	decision: Decision;
	score: number;
	confidence: number;
	regime: Regime | null;
	structure: string | null;
	strikes: Strikes | null;
	credit_mid: number | null;
	credit_fill: number | null;
	max_loss: number | null;
	pop_breakeven: number | null;
	expected_value: number | null;
	alpha: number | null;
	components: Record<string, ComponentDetail>;
	veto_reasons: string[];
	avoid_if: string[];
	data_quality: DataQuality;
	metrics?: Record<string, unknown>;
	management?: {
		profit_target_pct?: number;
		manage_dte?: number;
		stop_credit_multiple?: number;
	};
	candidate?: Record<string, unknown> & {
		pop_breakeven_forecast?: number | null;
		pop_edge?: number | null;
		package_spread?: number | null;
		cw?: number | null;
	};
	rejected?: string[];
	computed_at?: string;
	flow?: Record<string, unknown>;
	/** Earnings block — present only when the expiry is in an earnings window.
	 *  The card's decision already reflects the "consider" view (folded, no hard
	 *  veto). `earnings_alt` carries the "ignore" (pure ex-earnings) verdict so
	 *  the toggle switches views with no re-analysis. */
	earnings?: EarningsBlock | null;
	earnings_alt?: EarningsAlt | null;
	/** true when served from the last persisted sweep (restart/weekend) rather
	 *  than a live evaluation — render "as of computed_at", not "live". */
	rehydrated?: boolean;
	market_open?: boolean;
	market_reason?: string;
}

export interface WatchlistRow {
	id: string;
	symbol: string;
	expiration: string | null;
	position: number;
	active: boolean;
	/** per-ticker overrides of the TOGGLEABLE settings tier (jsonb column) */
	overrides?: Record<string, unknown> | null;
	created_at?: string;
	updated_at?: string;
	readiness: ReadinessEnvelope | null;
}

export interface WatchlistResponse {
	watchlist: WatchlistRow[];
	supabase: boolean;
	write_model: string;
	last_sweep_at: string | null;
}

export interface SimmerStatus {
	alive: boolean;
	is_running: boolean;
	market_open: boolean;
	market_reason: string | null;
	last_sweep_at: string | null;
	sweep_count: number;
	watched_count: number;
	watchlist_source: string | null;
	keys_tracked: string[];
	regime: Regime | null;
	dispersion: Record<string, unknown> | null;
	alerts_fired: number;
	alert_states: Record<string, string>;
	last_error: string | null;
	last_error_by_symbol: Record<string, string>;
	outcomes: { last_run_at: string | null; recorded: number };
	iv_history_last_date: string | null;
	engine_version: string;
}

export interface SimmerConfig {
	engine_version: string;
	decision: { ready: number; watch: number };
	weights: Record<string, number>;
	gates: Record<string, unknown>;
	targets: Record<string, number>;
	management: { profit_target_pct: number; manage_dte: number; stop_credit_multiple: number };
	cadence: Record<string, number>;
	tickers: string[];
	blocked_tickers: string[];
	structures: string[];
	locked_gates: string[];
	toggleable_gates: string[];
	user_clamps: Record<string, [number, number]>;
	alerts: { hysteresis_margin: number; ready_dwell_sec: number };
}

export interface ValidateResponse {
	symbol: string;
	valid: boolean;
	spot: number;
	expirations: string[];
	write_model: string;
}

/** POST /simmer/watchlist → ValidateResponse + written; DELETE → {symbol,
 *  deleted}; PATCH → {symbol, updated: [cols]}. Union kept loose on purpose —
 *  the stores only need to know the call succeeded. */
export interface WatchlistMutationResponse {
	symbol: string;
	written?: boolean;
	deleted?: boolean;
	updated?: string[];
	[k: string]: unknown;
}

export interface ExpirationInfo {
	date: string;
	dte: number;
	in_window: boolean;
	recommended: boolean;
}
export interface ExpirationsResponse {
	symbol: string;
	dte_window: [number, number];
	expirations: ExpirationInfo[];
}

export interface NewsCluster {
	cluster_key: string;
	headline: string | null;
	url: string | null;
	source: string | null;
	published_at: string | null;
	breadth: number;
	sentiment: number | null;
}
export interface NewsResponse {
	symbol: string;
	window_hours: number;
	clusters: NewsCluster[];
	aggregate: {
		sentiment_score: number | null;
		sentiment_n: number | null;
		news_at: string | null;
	};
	velocity: { p: number | null; tier: 'none' | 'warn' | 'alert' | null };
}

export interface SimmerAlert {
	id: string;
	symbol: string;
	expiration: string;
	score: number;
	state: string;
	structure: string | null;
	payload: Record<string, unknown>;
	acknowledged: boolean;
	created_at: string;
}
export interface AlertsResponse {
	alerts: SimmerAlert[];
	limit: number;
	offset: number;
	supabase: boolean;
}

/** simmer_settings row (migration 0010) — column names, verbatim. */
export interface SimmerSettings {
	risk_profile?: 'conservative' | 'balanced' | 'aggressive';
	min_score?: number;
	min_iv_percentile?: number;
	min_dte?: number;
	max_dte?: number;
	short_delta_min?: number;
	short_delta_max?: number;
	regime_strictness?: 'relaxed' | 'balanced' | 'strict';
	max_concurrent?: number;
	structures_enabled?: string[];
	gate_overrides?: Record<string, boolean>;
	notify_email?: boolean;
	prefs?: Record<string, unknown>;
}
export interface SettingsResponse {
	settings: SimmerSettings;
	defaults: {
		min_score: number;
		structures_enabled: string[];
		regime_strictness: string;
	};
}

export interface OutcomesSummary {
	basis: string;
	last_run_at: string | null;
	[k: string]: unknown;
}

// Formatting helpers — ported from Matrix (market/ui/index.html `fmt` + the ET
// time helpers) with a few Simmer-specific additions (score/decision classes).

const nil = (v: unknown): boolean =>
	v === null || v === undefined || (typeof v === 'number' && Number.isNaN(v));

export const money = (v: number | null | undefined, d = 2): string =>
	nil(v) ? '—' : Number(v).toFixed(d);

export const pct = (v: number | null | undefined, d = 1): string =>
	nil(v) ? '—' : `${Number(v).toFixed(d)}%`;

/** 0..1 probability -> percent string. */
export const prob = (v: number | null | undefined, d = 1): string =>
	nil(v) ? '—' : `${(Number(v) * 100).toFixed(d)}%`;

export const strike = (v: number | null | undefined): string =>
	nil(v) ? '—' : Number(v).toLocaleString(undefined, { maximumFractionDigits: 2 });

export const sigInt = (v: number | null | undefined): string =>
	nil(v) ? '—' : Math.round(Number(v)).toLocaleString();

export const when = (iso: string | null | undefined): string => {
	if (!iso) return '—';
	try {
		return new Date(iso).toLocaleTimeString();
	} catch {
		return String(iso);
	}
};

// Market timestamps are UTC; render in exchange time (ET), labeled, so an
// event at 15:59 ET never looks like "7:59 PM" on another machine.
const ET_TZ = 'America/New_York';

export const etDateTime = (iso: string | null | undefined): string => {
	if (!iso) return '—';
	try {
		return (
			new Date(iso).toLocaleString('en-US', {
				timeZone: ET_TZ,
				month: 'numeric',
				day: 'numeric',
				hour: 'numeric',
				minute: '2-digit',
				hour12: true
			}) + ' ET'
		);
	} catch {
		return String(iso);
	}
};

export const etTime = (iso: string | null | undefined): string => {
	if (!iso) return '—';
	try {
		return (
			new Date(iso).toLocaleTimeString('en-US', {
				timeZone: ET_TZ,
				hour: 'numeric',
				minute: '2-digit',
				second: '2-digit',
				hour12: true
			}) + ' ET'
		);
	} catch {
		return String(iso);
	}
};

/** Relative age, e.g. "4m ago" — for computed_at / last_sweep_at chips. */
export const ago = (iso: string | null | undefined, now: Date = new Date()): string => {
	if (!iso) return '—';
	const t = new Date(iso).getTime();
	if (Number.isNaN(t)) return String(iso);
	const s = Math.max(0, Math.round((now.getTime() - t) / 1000));
	if (s < 60) return `${s}s ago`;
	const m = Math.round(s / 60);
	if (m < 60) return `${m}m ago`;
	const h = Math.round(m / 60);
	if (h < 24) return `${h}h ago`;
	return `${Math.round(h / 24)}d ago`;
};

/** Matrix composite color semantics, keyed on the Simmer decision bands
 *  (ready>=70 good, watch>=50 meh, below bad — bands come from /simmer/config). */
export const scoreClass = (score: number | null | undefined, ready = 70, watch = 50): string => {
	if (nil(score)) return 'composite-bad';
	const s = Number(score);
	return s >= ready ? 'composite-good' : s >= watch ? 'composite-meh' : 'composite-bad';
};

export const scoreBarClass = (score: number | null | undefined, ready = 70, watch = 50): string => {
	if (nil(score)) return 'scorebar-bad';
	const s = Number(score);
	return s >= ready ? 'scorebar-good' : s >= watch ? 'scorebar-meh' : 'scorebar-bad';
};

export const DECISION_LABEL: Record<string, string> = {
	ready: 'READY',
	watch: 'WATCH',
	avoid: 'AVOID',
	vetoed: 'VETOED'
};

export const decisionBadgeClass = (decision: string | null | undefined): string => {
	switch (decision) {
		case 'ready':
			return 'badge-healthy';
		case 'watch':
			return 'badge-thin';
		case 'avoid':
			return 'badge-directional';
		default:
			return 'badge-broken';
	}
};

export const STRUCTURE_NAMES: Record<string, string> = {
	bull_put: 'Bull Put',
	bear_call: 'Bear Call',
	iron_condor: 'Iron Condor'
};

export const structureName = (s: string | null | undefined): string =>
	s ? (STRUCTURE_NAMES[s] ?? s) : '—';

/** Sentiment: distinguish "0.0 balanced" (n>0) from "no news" (n==0/null). */
export const sentimentLabel = (
	score: number | null | undefined,
	n: number | null | undefined
): string => {
	if (!n) return 'no news';
	if (nil(score)) return 'unscored';
	const s = Number(score);
	const word = s > 0.15 ? 'positive' : s < -0.15 ? 'negative' : 'balanced';
	return `${s >= 0 ? '+' : ''}${s.toFixed(2)} ${word}`;
};

/** Turn an engine veto reason token into a readable phrase.
 *  e.g. "bull_put:liquidity:spread_pct_of_credit" -> "bull put · liquidity: spread % of credit". */
export const humanizeReason = (reason: string): string =>
	reason
		.split(':')
		.map((part) => part.replace(/_/g, ' '))
		.join(' · ')
		.replace(/\bpct\b/g, '%')
		.replace(/\bdte\b/gi, 'DTE')
		.replace(/\biv\b/gi, 'IV')
		.replace(/\bvrp\b/gi, 'VRP')
		.replace(/\brv\b/gi, 'RV')
		.replace(/\bev\b/gi, 'EV');

/** Only http(s) links are safe to render from untrusted news feeds — a hostile
 *  RSS/news item could carry a `javascript:` (or `data:`) href. Returns the URL
 *  when its scheme is http/https, else null (caller renders plain text). */
export const safeHref = (u: string | null | undefined): string | null => {
	if (!u) return null;
	try {
		const proto = new URL(u, 'https://x.invalid').protocol;
		return proto === 'http:' || proto === 'https:' ? u : null;
	} catch {
		return null;
	}
};

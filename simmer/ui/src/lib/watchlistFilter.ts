// Watchlist filter/sort helpers — pure so they're unit-testable (watchlistFilter.test.ts)
// and the dashboard and the test can't drift apart.
import type { WatchlistRow } from './types';

/** The confidence the card actually SHOWS, on a 0–100 scale. The overnight (reconciled)
 *  read when the name has one — that's the number the Overnight tab displays, e.g. a name
 *  the tape flipped to 'wait' reads ~30% even though its frozen combiner was ~82% — else
 *  the readiness confidence (a 0–1 fraction). The min-conf filter and the sort both judge
 *  a row on this, so a name is filtered on what the user sees, never a stale frozen value. */
export function cardConf(r: WatchlistRow): number {
	return r.overnight_conf != null ? r.overnight_conf : (r.readiness?.confidence ?? 0) * 100;
}

/** Does the row clear the min-confidence filter (percent)? `minConf` 0 disables it. */
export function passesMinConf(r: WatchlistRow, minConf: number): boolean {
	return minConf <= 0 || cardConf(r) >= minConf;
}

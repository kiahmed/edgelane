// Watchlist store. Write model is "api" (routes/simmer.py): every read AND
// write goes through the backend with the bearer JWT. POST /simmer/watchlist
// validates and writes in one call (service-role upsert on behalf of the
// verified user); DELETE and PATCH mutate the caller's own row. The browser
// never touches Supabase tables — in dev-bypass the same calls run with the
// admin token, and the backend's 422 for bypass identities is surfaced
// verbatim.
import { getJSON, postJSON, patchJSON, deleteJSON, ApiError, errorMessage } from '../api';
import type {
	SimmerStatus,
	WatchlistMutationResponse,
	WatchlistResponse,
	WatchlistRow
} from '../types';

const errText = (e: unknown, verb: string): string => {
	if (e instanceof ApiError) return String(e.message); // server detail, verbatim
	return `${verb} failed: ${e instanceof Error ? e.message : e}`;
};

class WatchlistStore {
	rows = $state<WatchlistRow[]>([]);
	lastSweepAt = $state<string | null>(null);
	status = $state<SimmerStatus | null>(null);
	loading = $state(false);
	error = $state<string | null>(null);
	backendUp = $state(true);

	async refresh(): Promise<void> {
		this.loading = true;
		try {
			const [wl, st] = await Promise.all([
				getJSON<WatchlistResponse>('/simmer/watchlist'),
				getJSON<SimmerStatus>('/simmer/status')
			]);
			this.rows = wl.watchlist ?? [];
			this.lastSweepAt = wl.last_sweep_at ?? null;
			this.status = st;
			this.backendUp = true;
			this.error = null;
		} catch (e) {
			this.backendUp = false;
			this.error = errorMessage(e);
		} finally {
			this.loading = false;
		}
	}

	/** Single POST — the backend validates (eligibility + live chain) and
	 *  writes the row in the same call. Returns an error message or null. */
	async add(symbol: string, expiration: string | null = null): Promise<string | null> {
		const sym = symbol.trim().toUpperCase();
		if (!sym) return 'enter a symbol';
		try {
			await postJSON<WatchlistMutationResponse>('/simmer/watchlist', {
				symbol: sym,
				...(expiration ? { expiration } : {})
			});
		} catch (e) {
			return errText(e, 'add');
		}
		await this.refresh();
		return null;
	}

	async remove(row: WatchlistRow): Promise<string | null> {
		try {
			await deleteJSON<WatchlistMutationResponse>(
				`/simmer/watchlist/${encodeURIComponent(row.symbol)}`
			);
		} catch (e) {
			return errText(e, 'remove');
		}
		this.rows = this.rows.filter((r) => r.id !== row.id);
		await this.refresh();
		return null;
	}

	/** Pin (or clear, with null) the single expiration for a row. */
	async pinExpiration(row: WatchlistRow, expiration: string | null): Promise<string | null> {
		try {
			await patchJSON<WatchlistMutationResponse>(
				`/simmer/watchlist/${encodeURIComponent(row.symbol)}`,
				{ expiration }
			);
		} catch (e) {
			return errText(e, 'update');
		}
		await this.refresh();
		return null;
	}
}

export const watchlist = new WatchlistStore();

<script lang="ts">
	// Readiness history — one bar per engine sweep, oldest left.
	//
	// This fills what used to be dead space: on a vetoed card the SCORE MATRIX
	// above reads "weighted sum 0.0 / score 0.0", which is true and useless.
	// Vetoed is the *normal* state, so that column has to earn its place there.
	// A history strip does: it shows whether a refused name is warming up or
	// flat, and whether past READY calls actually held.
	//
	// ── Encoding decisions (dataviz method; deviations are deliberate) ────────
	// HEIGHT  = score. The magnitude channel carries the magnitude.
	// COLOR   = OUTCOME (held / breached / unresolved / vetoed), NOT the score
	//           band. Two reasons. (1) Colouring by band double-encodes what the
	//           height already shows and burns the only free channel — the
	//           "value-ramp" anti-pattern. (2) The green→amber ramp fails CVD:
	//           the validator puts #86efac↔#fde68a at ΔE 13.5 normal / 4.4
	//           protan, i.e. a red-blind reader cannot separate READY from
	//           WATCH. Outcome green↔rose measures ΔE 33.8 normal (PASS) with a
	//           deutan WARN that is legal because every resolved bar also
	//           carries a ✓/✗ glyph — identity is never colour-alone.
	// BANDS   = threshold lines at watch/ready. That is how a decision boundary
	//           is drawn; it keeps the 50/70 cliffs visible without a ramp.
	// VETOED  = a short slate stub, not a zero-height gap. A refusal is a
	//           different KIND of result from a low score, and the refusals are
	//           the product — drawing only scored sweeps would trace a
	//           flattering line through the gaps.
	import { getJSON, errorMessage } from '$lib/api';
	import { etDateTime, humanizeReason } from '$lib/fmt';

	let {
		symbol,
		readyBand = 70,
		watchBand = 50,
		days = 30
	}: { symbol: string; readyBand?: number; watchBand?: number; days?: number } = $props();

	interface HistoryRow {
		id: number;
		ts: string;
		score: number | null;
		vetoed: boolean | null;
		veto_reasons: string | null;
		structure: string | null;
		short_strike: number | null;
		held: boolean | null;
		touched: boolean | null;
	}
	interface HistoryResp {
		rows: HistoryRow[];
		resolved: number;
		held: number;
		hit_rate: number | null;
	}

	let data = $state<HistoryResp | null>(null);
	let error = $state('');
	let loading = $state(true);
	let hover = $state<number | null>(null);
	/** symbol we already have data for — refetching the same one must not flash */
	let loadedFor = $state<string | null>(null);

	$effect(() => {
		const sym = symbol;
		// The card re-renders on every poll. Refetching per poll and flipping
		// `loading` blanked the plot for a frame each time — the flicker. History
		// is daily-bucketed, so it cannot meaningfully change between polls: fetch
		// once per symbol and leave it alone.
		if (sym === loadedFor) return;
		let cancelled = false;
		// Only show the loading state when there is nothing to show yet.
		if (data === null) loading = true;
		error = '';
		getJSON<HistoryResp>(`/simmer/history/${encodeURIComponent(sym)}?days=${days}`)
			.then((d) => {
				if (cancelled) return;
				data = d;
				loadedFor = sym;
			})
			.catch((e) => {
				if (!cancelled) error = errorMessage(e);
			})
			.finally(() => {
				if (!cancelled) loading = false;
			});
		return () => {
			cancelled = true;
		};
	});

	// ── Geometry. Bars are ≤24px and never fill their slot; the leftover is the
	//    2px surface gap that separates neighbours (no strokes). ──────────────
	const H = 120; // plot height, px — taller now the card has room
	const VETO_STUB = 5; // px — visible refusal, clearly below any real score

	// Defensive daily collapse, mirroring the server's bucket="day".
	//
	// The server buckets already, but this component must stay correct against a
	// backend that predates that change (they deploy separately) — and against
	// bucket=raw. The watcher sweeps every ~5 min, so raw rows arrive in the
	// hundreds; at 240 bars each slot is ~1.5px and the 2px surface gap clamps
	// every bar to zero width, i.e. an empty chart. Same tie-break as the SQL:
	// keep the row carrying a paper outcome, else the day's last sweep.
	function collapseByDay(src: HistoryRow[]): HistoryRow[] {
		const best = new Map<string, HistoryRow>();
		for (const r of src) {
			const day = (r.ts ?? '').slice(0, 10);
			const cur = best.get(day);
			if (!cur) { best.set(day, r); continue; }
			const better = r.held != null && cur.held == null;
			const laterSameClass = (r.held != null) === (cur.held != null) && r.ts > cur.ts;
			if (better || laterSameClass) best.set(day, r);
		}
		return [...best.values()].sort((a, b) => (a.ts < b.ts ? -1 : a.ts > b.ts ? 1 : 0));
	}

	const rawRows = $derived(data?.rows ?? []);
	// Only collapse when the payload is clearly un-bucketed; a already-daily
	// series passes through untouched (collapsing it would be a no-op anyway).
	const rows = $derived(rawRows.length > 45 ? collapseByDay(rawRows) : rawRows);
	const slot = $derived(rows.length ? 100 / rows.length : 100); // % width per bar
	const y = (score: number) => H - (Math.max(0, Math.min(100, score)) / 100) * H;

	function barHeight(r: HistoryRow): number {
		if (r.vetoed || r.score == null) return VETO_STUB;
		return Math.max(2, H - y(r.score));
	}

	// Fill by outcome. Unresolved is deliberately quiet — it is not a result yet.
	function fill(r: HistoryRow): string {
		if (r.vetoed || r.score == null) return 'var(--h-veto)';
		if (resolved(r)) return r.held ? 'var(--h-held)' : 'var(--h-breached)';
		return 'var(--h-pending)';
	}

	// veto_reasons arrives as `structure:gate[:detail]`, one entry per
	// structure×gate permutation — 13 entries for what the card header calls
	// "3 of 10 vetoed". Collapse to the DISTINCT GATE (segment 2) so the takeaway
	// is "liquidity, dollar friction, short-delta band" and not a wall of text.
	function vetoGates(r: HistoryRow): string[] {
		if (!r.veto_reasons) return [];
		try {
			const parsed = JSON.parse(r.veto_reasons);
			if (!Array.isArray(parsed)) return [];
			const gates = new Set<string>();
			for (const raw of parsed) {
				const parts = String(raw).split(':');
				gates.add(humanizeReason(parts.length > 1 ? parts[1] : parts[0]).toLowerCase());
			}
			return [...gates];
		} catch {
			return [];
		}
	}

	/** Did the engine actually ISSUE this one? Outcomes are graded on the last
	 *  row before expiry that carried strikes — which can be well below the ready
	 *  line. Badging those as held/breached claims credit for a call that was
	 *  never made, so only ready-band rows count as signals. */
	function isSignal(r: HistoryRow): boolean {
		return r.score != null && r.score >= readyBand;
	}
	function resolved(r: HistoryRow): boolean {
		return isSignal(r) && r.held != null;
	}

	/** One plain sentence: what happened, and what it means. */
	function takeaway(r: HistoryRow): string {
		if (r.vetoed || r.score == null) {
			const g = vetoGates(r);
			if (!g.length) return 'No trade — the engine refused this day.';
			const head = g.slice(0, 3).join(', ');
			const rest = g.length > 3 ? ` +${g.length - 3} more` : '';
			return `No trade — blocked on ${head}${rest}.`;
		}
		const s = Math.round(r.score);
		if (resolved(r)) {
			return r.held
				? `Scored ${s} — the engine called it, and the short strike was still safe at expiry.`
				: `Scored ${s} — the engine called it, and price went through the short strike before expiry.`;
		}
		if (isSignal(r)) return `Scored ${s} — above the ${readyBand} line. Sellable that day.`;
		if (r.score >= watchBand) return `Scored ${s} — warming up, still short of the ${readyBand} line.`;
		return `Scored ${s} — well below the ${readyBand} line. Nothing to do.`;
	}

	// Counted client-side, NOT from data.hit_rate: the server grades the last row
	// before expiry that carried strikes, which can sit well below the ready line.
	// Only calls the engine actually issued belong in a published hit rate.
	const signalsResolved = $derived(rows.filter(resolved));
	const signalsHeld = $derived(signalsResolved.filter((r) => r.held).length);
	const hitPct = $derived(
		signalsResolved.length
			? Math.round((signalsHeld / signalsResolved.length) * 100)
			: null
	);
</script>

<div class="hist">
	<div class="mb-1 flex items-baseline justify-between">
		<span class="hist-h">READINESS HISTORY</span>
		<span class="text-[0.62rem] text-slate-500">last {days}d</span>
	</div>

	{#if loading}
		<div class="hist-empty">loading…</div>
	{:else if error}
		<div class="hist-empty text-rose-400">{error}</div>
	{:else if !rows.length}
		<div class="hist-empty">
			No sweeps recorded yet. The engine stores every evaluation — this fills in as it runs.
		</div>
	{:else}
		<div class="hist-plot" style="height:{H}px">
			<!-- Decision thresholds. Hairline, solid, recessive — a boundary, not data. -->
			<div class="hist-rule" style="top:{y(readyBand)}px">
				<span>ready {readyBand}</span>
			</div>
			<div class="hist-rule hist-rule-dim" style="top:{y(watchBand)}px">
				<span>watch {watchBand}</span>
			</div>

			{#each rows as r, i (r.id)}
				<button
					type="button"
					class="hist-slot"
					style="left:{i * slot}%; width:{slot}%"
					onmouseenter={() => (hover = i)}
					onmouseleave={() => (hover = null)}
					onfocus={() => (hover = i)}
					onblur={() => (hover = null)}
					aria-label="{etDateTime(r.ts)} — {takeaway(r)}"
				>
					<span
						class="hist-bar"
						class:is-veto={r.vetoed || r.score == null}
						style="height:{barHeight(r)}px; background:{fill(r)}"
					></span>
					<!-- Secondary encoding: outcome is never colour-alone. -->
					{#if resolved(r)}
						<span
							class="hist-glyph {r.held ? 'held' : 'breached'}"
							style="bottom:{barHeight(r) + 2}px">{r.held ? '✓' : '✗'}</span
						>
					{/if}
				</button>
			{/each}
		</div>

		{#if hover !== null && rows[hover]}
			{@const r = rows[hover]}
			<div class="hist-tip">
				<span class="text-slate-400">{etDateTime(r.ts)}</span>
				<span class="text-slate-200">{takeaway(r)}</span>
			</div>
		{:else}
			<div class="hist-tip hist-tip-idle">hover a bar for that evaluation</div>
		{/if}

		<div class="hist-legend">
			<span><i class="sw" style="background:var(--h-held)"></i>✓ safe at expiry</span>
			<span><i class="sw" style="background:var(--h-breached)"></i>✗ went through</span>
			<span><i class="sw" style="background:var(--h-pending)"></i>scored, no trade</span>
			<span><i class="sw sw-veto" style="background:var(--h-veto)"></i>refused (stub)</span>
		</div>

		{#if signalsResolved.length}
			<div class="hist-rate">
				<strong class="text-slate-200">{signalsHeld} of {signalsResolved.length}</strong>
				ready calls stayed safe to expiry <span class="text-slate-500">({hitPct}%)</span>
			</div>
		{:else}
			<div class="hist-rate text-slate-500">
				No ready calls have reached expiry yet — nothing graded so far.
			</div>
		{/if}
	{/if}
</div>

<style>
	.hist {
		--h-held: #4ade80;
		--h-breached: #fb7185;
		/* "Open" and "vetoed" share ONE hue on purpose. Rendering the first pass
		   showed two greys a step apart that nobody could tell apart in a 10px
		   swatch — and they never needed a hue to begin with: bar HEIGHT already
		   separates them (a real score vs a 5px stub). So the hue is spent on the
		   outcome, and vetoed is the same slate at low opacity. */
		--h-pending: #475569;
		--h-veto: #475569;
		margin-top: 0.75rem;
	}
	.hist-h {
		font-size: 0.62rem;
		font-weight: 700;
		letter-spacing: 0.1em;
		color: #94a3b8;
	}
	.hist-empty {
		font-size: 0.7rem;
		line-height: 1.5;
		color: #64748b;
		padding: 0.75rem 0;
	}
	.hist-plot {
		position: relative;
		/* Right gutter reserves room for the threshold labels, so a bar can never
		   render underneath "ready 70". Found by screenshotting, not by reading. */
		width: calc(100% - 3.4rem);
		border-bottom: 1px solid rgba(100, 116, 139, 0.35);
	}
	/* Threshold lines: hairline, solid, one step off surface. */
	.hist-rule {
		position: absolute;
		left: 0;
		right: 0;
		height: 0;
		border-top: 1px solid rgba(134, 239, 172, 0.35);
		pointer-events: none;
	}
	.hist-rule-dim {
		border-top-color: rgba(148, 163, 184, 0.25);
	}
	.hist-rule span {
		position: absolute;
		left: 100%;
		padding-left: 0.4rem;
		top: -0.5rem;
		white-space: nowrap;
		font-size: 0.55rem;
		letter-spacing: 0.06em;
		color: #64748b;
	}
	/* The hit target is the whole slot — bigger than the mark, per interaction spec. */
	.hist-slot {
		position: absolute;
		bottom: 0;
		top: 0;
		padding: 0;
		border: none;
		background: transparent;
		cursor: pointer;
		display: flex;
		align-items: flex-end;
		justify-content: center;
	}
	.hist-bar {
		display: block;
		/* ≤24px and never fills the slot: the leftover IS the 2px surface gap. */
		width: min(24px, calc(100% - 2px));
		/* 4px rounded data-end, square at the baseline. */
		border-radius: 4px 4px 0 0;
		transition: filter 0.12s;
	}
	.hist-slot:hover .hist-bar,
	.hist-slot:focus-visible .hist-bar {
		filter: brightness(1.35);
	}
	.hist-bar.is-veto {
		border-radius: 2px 2px 0 0;
		opacity: 0.55;
	}
	.hist-glyph {
		position: absolute;
		font-size: 0.6rem;
		line-height: 1;
		pointer-events: none;
	}
	.hist-glyph.held {
		color: var(--h-held);
	}
	.hist-glyph.breached {
		color: var(--h-breached);
	}
	.hist-tip {
		margin-top: 0.35rem;
		display: flex;
		gap: 0.5rem;
		flex-wrap: wrap;
		font-size: 0.66rem;
		min-height: 1rem;
	}
	.hist-tip-idle {
		color: #475569;
	}
	.hist-legend {
		margin-top: 0.3rem;
		display: flex;
		flex-wrap: wrap;
		gap: 0.6rem;
		font-size: 0.6rem;
		color: #94a3b8;
	}
	.hist-legend .sw {
		display: inline-block;
		width: 0.5rem;
		height: 0.5rem;
		border-radius: 2px;
		margin-right: 0.25rem;
		vertical-align: baseline;
	}
	/* Mirrors the stub in the plot: short and faded, so the legend swatch looks
	   like the thing it labels rather than a full-height block. */
	.hist-legend .sw-veto {
		height: 0.18rem;
		opacity: 0.55;
	}
	.hist-rate {
		margin-top: 0.4rem;
		font-size: 0.68rem;
		color: #94a3b8;
	}
</style>

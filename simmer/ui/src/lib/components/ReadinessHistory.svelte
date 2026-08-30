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

	$effect(() => {
		const sym = symbol;
		let cancelled = false;
		loading = true;
		error = '';
		getJSON<HistoryResp>(`/simmer/history/${encodeURIComponent(sym)}?days=${days}`)
			.then((d) => {
				if (!cancelled) data = d;
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
	const H = 96; // plot height, px
	const VETO_STUB = 5; // px — visible refusal, clearly below any real score

	const rows = $derived(data?.rows ?? []);
	const slot = $derived(rows.length ? 100 / rows.length : 100); // % width per bar
	const y = (score: number) => H - (Math.max(0, Math.min(100, score)) / 100) * H;

	function barHeight(r: HistoryRow): number {
		if (r.vetoed || r.score == null) return VETO_STUB;
		return Math.max(2, H - y(r.score));
	}

	// Fill by outcome. Unresolved is deliberately quiet — it is not a result yet.
	function fill(r: HistoryRow): string {
		if (r.vetoed || r.score == null) return 'var(--h-veto)';
		if (r.held === true) return 'var(--h-held)';
		if (r.held === false) return 'var(--h-breached)';
		return 'var(--h-pending)';
	}

	function vetoList(r: HistoryRow): string[] {
		if (!r.veto_reasons) return [];
		try {
			const p = JSON.parse(r.veto_reasons);
			return Array.isArray(p) ? p.map((k) => humanizeReason(String(k))) : [];
		} catch {
			return [];
		}
	}

	function label(r: HistoryRow): string {
		if (r.vetoed || r.score == null) {
			const v = vetoList(r);
			return `vetoed${v.length ? ` — ${v.join(', ')}` : ''}`;
		}
		const outcome = r.held === true ? 'held' : r.held === false ? 'breached' : 'not yet resolved';
		return `score ${r.score.toFixed(0)} · ${outcome}`;
	}

	const hitPct = $derived(
		data?.hit_rate != null ? Math.round(data.hit_rate * 100) : null
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
					aria-label="{etDateTime(r.ts)} — {label(r)}"
				>
					<span
						class="hist-bar"
						class:is-veto={r.vetoed || r.score == null}
						style="height:{barHeight(r)}px; background:{fill(r)}"
					></span>
					<!-- Secondary encoding: outcome is never colour-alone. -->
					{#if r.held === true}
						<span class="hist-glyph held" style="bottom:{barHeight(r) + 2}px">✓</span>
					{:else if r.held === false}
						<span class="hist-glyph breached" style="bottom:{barHeight(r) + 2}px">✗</span>
					{/if}
				</button>
			{/each}
		</div>

		{#if hover !== null && rows[hover]}
			{@const r = rows[hover]}
			<div class="hist-tip">
				<span class="text-slate-400">{etDateTime(r.ts)}</span>
				<span class="text-slate-200">{label(r)}</span>
			</div>
		{:else}
			<div class="hist-tip hist-tip-idle">hover a bar for that evaluation</div>
		{/if}

		<div class="hist-legend">
			<span><i class="sw" style="background:var(--h-held)"></i>✓ held</span>
			<span><i class="sw" style="background:var(--h-breached)"></i>✗ breached</span>
			<span><i class="sw" style="background:var(--h-pending)"></i>no outcome yet</span>
			<span><i class="sw sw-veto" style="background:var(--h-veto)"></i>vetoed (stub)</span>
		</div>

		{#if data && data.resolved > 0}
			<div class="hist-rate">
				<strong class="text-slate-200">{data.held} of {data.resolved}</strong> resolved signals held
				<span class="text-slate-500">({hitPct}%)</span>
			</div>
		{:else}
			<div class="hist-rate text-slate-500">No signals have reached expiry yet.</div>
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

<script lang="ts">
	// News + sentiment for one symbol (/simmer/news/{symbol}): trailing
	// aggregate (with the crucial "0.0 balanced" vs "no news" distinction),
	// velocity tier chip, and the deduped cluster list with breadth badges.
	// Headlines LINK OUT to the publisher — bodies are never rendered
	// (licensing: we score, we don't republish).
	import { getJSON } from '$lib/api';
	import { ago, sentimentLabel, safeHref} from '$lib/fmt';
	import type { NewsResponse } from '$lib/types';

	let { symbol }: { symbol: string } = $props();

	let news = $state<NewsResponse | null>(null);
	let error = $state<string | null>(null);
	let loading = $state(false);

	$effect(() => {
		if (!symbol) return;
		loading = true;
		error = null;
		getJSON<NewsResponse>(`/simmer/news/${encodeURIComponent(symbol)}`)
			.then((r) => (news = r))
			.catch((e) => (error = e instanceof Error ? e.message : String(e)))
			.finally(() => (loading = false));
	});

	const aggClass = $derived.by(() => {
		const s = news?.aggregate.sentiment_score;
		const n = news?.aggregate.sentiment_n;
		if (!n || s == null) return 'bg-slate-700 text-slate-300';
		return s > 0.15
			? 'bg-emerald-500/20 text-emerald-200'
			: s < -0.15
				? 'bg-rose-500/20 text-rose-200'
				: 'bg-slate-500/20 text-slate-200';
	});

	const velocityClass = (tier: string | null) =>
		tier === 'alert'
			? 'bg-rose-500/20 text-rose-200'
			: tier === 'warn'
				? 'bg-amber-500/20 text-amber-200'
				: 'bg-slate-700 text-slate-400';

	const sentDot = (s: number | null) =>
		s == null ? 'bg-slate-500' : s > 0.15 ? 'bg-emerald-400' : s < -0.15 ? 'bg-rose-400' : 'bg-slate-400';

	// Per-article LLM score, surfaced as a signed number so the mechanics are
	// visible (the top NET is a separate trailing aggregate, not the mean of
	// these). Colour by sign; en-dash for a genuine negative sign.
	const scoreClass = (s: number | null) =>
		s == null ? 'text-slate-600' : s > 0.15 ? 'text-emerald-300' : s < -0.15 ? 'text-rose-300' : 'text-slate-400';
	const fmtSigned = (s: number | null) =>
		s == null ? '—' : (s >= 0 ? '+' : '−') + Math.abs(s).toFixed(2);
</script>

<div class="card p-4">
	<div class="mb-3 flex flex-wrap items-center gap-2">
		<h2 class="text-[0.7rem] font-bold tracking-wider text-slate-400 uppercase">
			News & Sentiment · {symbol}
		</h2>
		{#if news}
			<span class="pill {aggClass}" title="Trailing sentiment aggregate over {news.window_hours}h · n = scored clusters">
				{sentimentLabel(news.aggregate.sentiment_score, news.aggregate.sentiment_n)}
				{#if news.aggregate.sentiment_n}<span class="text-[0.62rem] opacity-70">n={news.aggregate.sentiment_n}</span>{/if}
			</span>
			<span
				class="pill {velocityClass(news.velocity.tier)}"
				title="News velocity vs this name's own baseline (negative-binomial burst test)"
			>
				velocity: {news.velocity.tier ?? 'none'}
			</span>
			<span class="ml-auto text-[0.65rem] text-slate-600">scored {ago(news.aggregate.news_at)}</span>
		{/if}
	</div>

	{#if loading && !news}
		<p class="text-sm text-slate-500">Loading news…</p>
	{:else if error}
		<p class="text-sm text-rose-300">{error}</p>
	{:else if news && !news.clusters.length}
		<p class="text-sm text-slate-500">
			No stories in the last {news.window_hours}h. That's a sentiment of "no news" — not neutral.
		</p>
	{:else if news}
		<div class="space-y-1.5">
			{#each news.clusters as c (c.cluster_key)}
				<div class="flex items-baseline gap-2 text-sm">
					<span class="dot mt-1 shrink-0 {sentDot(c.sentiment)}" title={c.sentiment == null ? 'unscored' : `sentiment ${c.sentiment.toFixed(2)}`}></span>
					{#if safeHref(c.url)}
						<a
							class="min-w-0 flex-1 truncate text-slate-200 hover:text-emerald-200 hover:underline"
							href={safeHref(c.url)}
							target="_blank"
							rel="noopener noreferrer"
							title={c.headline ?? ''}>{c.headline ?? '(untitled)'}</a
						>
					{:else}
						<span class="min-w-0 flex-1 truncate text-slate-200">{c.headline ?? '(untitled)'}</span>
					{/if}
					{#if c.breadth > 1}
						<span
							class="pill bg-sky-500/15 text-sky-300"
							title="Syndication breadth — {c.breadth} outlets carried this wire story. Breadth is the signal; repetition is not."
							>×{c.breadth}</span
						>
					{/if}
					<span
						class="shrink-0 w-11 text-right font-mono text-[0.7rem] {scoreClass(c.sentiment)}"
						title={c.sentiment == null
							? 'unscored'
							: `LLM article sentiment ${c.sentiment.toFixed(2)} (−1 … +1)${c.breadth > 1 ? ' · mean across ' + c.breadth + ' outlets' : ''}`}
						>{fmtSigned(c.sentiment)}</span
					>
					<span class="shrink-0 text-[0.65rem] text-slate-500">{c.source ?? ''}</span>
					<span class="shrink-0 text-[0.65rem] text-slate-600">{ago(c.published_at)}</span>
				</div>
			{/each}
		</div>
	{/if}
</div>

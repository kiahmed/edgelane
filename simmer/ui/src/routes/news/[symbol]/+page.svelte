<script lang="ts">
	import SentimentNewsPanel from '$lib/components/SentimentNewsPanel.svelte';
	import { page } from '$app/state';
	import { resolve } from '$app/paths';

	const symbol = $derived((page.params.symbol ?? '').toUpperCase());
</script>

<div class="space-y-3">
	<a class="footer-link text-[0.75rem]" href={resolve('/')}>← dashboard</a>
	{#if symbol}
		<SentimentNewsPanel {symbol} />
	{/if}
</div>

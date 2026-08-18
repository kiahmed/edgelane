<script lang="ts">
	import type { Snippet } from 'svelte';

	let {
		open = false,
		title = '',
		onclose,
		children
	}: {
		open?: boolean;
		title?: string;
		onclose?: () => void;
		children: Snippet;
	} = $props();
</script>

<svelte:window
	onkeydown={(e) => {
		if (open && e.key === 'Escape') onclose?.();
	}}
/>

<div
	class="modal-mask"
	class:show={open}
	role="presentation"
	onclick={(e) => {
		if (e.target === e.currentTarget) onclose?.();
	}}
>
	<div class="modal-card" role="dialog" aria-modal="true" aria-label={title}>
		<div class="mb-3 flex items-center justify-between">
			<h3>{title}</h3>
			<button class="btn" type="button" onclick={() => onclose?.()}>✕</button>
		</div>
		{#if open}
			{@render children()}
		{/if}
	</div>
</div>

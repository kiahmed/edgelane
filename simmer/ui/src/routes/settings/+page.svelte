<script lang="ts">
	import SettingsPanel from '$lib/components/SettingsPanel.svelte';
	import { settingsStore } from '$lib/stores/settings.svelte';

	$effect(() => {
		if (!settingsStore.config) void settingsStore.load();
	});
</script>

{#if settingsStore.loading && !settingsStore.config}
	<p class="text-sm text-slate-500">Loading settings…</p>
{:else if settingsStore.error && !settingsStore.config}
	<p class="text-sm text-rose-300">{settingsStore.error}</p>
{:else}
	<SettingsPanel />
{/if}

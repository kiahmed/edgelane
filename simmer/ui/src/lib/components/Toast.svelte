<script lang="ts">
	import { toastState } from '$lib/stores/toast.svelte';
</script>

<div class="toast" class:show={toastState.visible} role="status">{toastState.message}</div>

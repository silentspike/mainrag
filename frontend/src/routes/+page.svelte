<script lang="ts">
	import { goto } from '$app/navigation';
	import { onMount } from 'svelte';

	onMount(() => {
		goto('/finanzioso');
	});
</script>

<svelte:head>
	<title>Finanzioso - AI Stock Assistant</title>
</svelte:head>

<div class="loading container">
	<p>Redirecting to Finanzioso...</p>
</div>

<style>
	.loading {
		display: flex;
		justify-content: center;
		align-items: center;
		min-height: 50vh;
		color: var(--color-text-muted);
	}
</style>

<script lang="ts">
	import '../app.css';
	import { auth, isAuthenticated, isAdmin } from '$lib/stores/auth';

	let { children } = $props();
</script>

<div class="app">
	<header>
		<nav class="container">
			<div class="nav-brand">
				<a href="/finanzioso">Finanzioso</a>
				<span class="brand-subtitle">AI Stock Assistant</span>
			</div>
			<div class="nav-links">
				<a href="/finanzioso">Watchlist</a>
			</div>
			<div class="nav-auth">
				{#if $isAuthenticated}
					<span class="user-email">{$auth.email}</span>
					<button class="btn btn-secondary" onclick={() => auth.logout()}>Logout</button>
				{:else}
					<a href="/login" class="btn btn-primary">Login</a>
				{/if}
			</div>
		</nav>
	</header>

	<main>
		{@render children()}
	</main>

	<footer>
		<div class="container">
			<p>Finanzioso - AI Stock Assistant (Educational/Research Only)</p>
		</div>
	</footer>
</div>

<style>
	.app {
		display: flex;
		flex-direction: column;
		min-height: 100vh;
	}

	header {
		background: var(--color-surface);
		border-bottom: 1px solid var(--color-border);
		padding: 1rem 0;
	}

	nav {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 2rem;
	}

	.nav-brand {
		display: flex;
		align-items: baseline;
		gap: 0.5rem;
	}

	.nav-brand a {
		font-size: 1.25rem;
		font-weight: 700;
		color: var(--color-text);
		text-decoration: none;
	}

	.brand-subtitle {
		font-size: 0.75rem;
		color: var(--color-text-muted);
	}

	.nav-links {
		display: flex;
		gap: 1.5rem;
		flex: 1;
	}

	.nav-links a {
		color: var(--color-text-muted);
		text-decoration: none;
		transition: color 0.15s ease;
	}

	.nav-links a:hover {
		color: var(--color-text);
	}

	.nav-auth {
		display: flex;
		align-items: center;
		gap: 1rem;
	}

	.user-email {
		color: var(--color-text-muted);
		font-size: 0.875rem;
	}

	main {
		flex: 1;
		padding: 2rem 0;
	}

	footer {
		background: var(--color-surface);
		border-top: 1px solid var(--color-border);
		padding: 1rem 0;
		text-align: center;
		color: var(--color-text-muted);
		font-size: 0.875rem;
	}
</style>

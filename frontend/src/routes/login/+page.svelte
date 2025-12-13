<script lang="ts">
	import { goto } from '$app/navigation';
	import { auth } from '$lib/stores/auth';

	let token = $state('');
	let error = $state<string | null>(null);

	function handleLogin(e: Event) {
		e.preventDefault();
		error = null;

		if (!token.trim()) {
			error = 'Please enter a JWT token';
			return;
		}

		try {
			auth.login(token.trim());
			goto('/');
		} catch (err) {
			error = 'Invalid token format';
		}
	}
</script>

<svelte:head>
	<title>Login - MAINRAG</title>
</svelte:head>

<div class="login-page container">
	<div class="login-card card">
		<h1>Login</h1>
		<p class="description">Enter your JWT token to access admin features</p>

		<form onsubmit={handleLogin}>
			{#if error}
				<div class="error-message">
					{error}
				</div>
			{/if}

			<div class="form-group">
				<label for="token">JWT Token</label>
				<textarea
					id="token"
					class="input token-input"
					placeholder="eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9..."
					bind:value={token}
					rows="4"
				></textarea>
			</div>

			<button type="submit" class="btn btn-primary login-btn">
				Login
			</button>
		</form>

		<div class="help-text">
			<p>Don't have a token? Contact your administrator.</p>
		</div>
	</div>
</div>

<style>
	.login-page {
		display: flex;
		justify-content: center;
		align-items: center;
		min-height: 60vh;
	}

	.login-card {
		width: 100%;
		max-width: 450px;
	}

	.login-card h1 {
		font-size: 1.5rem;
		margin-bottom: 0.5rem;
	}

	.description {
		color: var(--color-text-muted);
		margin-bottom: 1.5rem;
	}

	.error-message {
		background: rgba(239, 68, 68, 0.1);
		border: 1px solid var(--color-error);
		color: var(--color-error);
		padding: 0.75rem;
		border-radius: 6px;
		margin-bottom: 1rem;
		font-size: 0.875rem;
	}

	.form-group {
		margin-bottom: 1rem;
	}

	.form-group label {
		display: block;
		margin-bottom: 0.5rem;
		font-weight: 500;
	}

	.token-input {
		resize: vertical;
		font-family: var(--font-mono);
		font-size: 0.85rem;
	}

	.login-btn {
		width: 100%;
		padding: 0.75rem;
		margin-top: 0.5rem;
	}

	.help-text {
		margin-top: 1.5rem;
		padding-top: 1rem;
		border-top: 1px solid var(--color-border);
		text-align: center;
		color: var(--color-text-muted);
		font-size: 0.875rem;
	}
</style>

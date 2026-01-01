<script lang="ts">
	import { goto } from '$app/navigation';
	import { auth } from '$lib/stores/auth';

	let email = $state('');
	let password = $state('');
	let error = $state<string | null>(null);
	let isLoading = $state(false);

	async function handleLogin(e: Event) {
		e.preventDefault();
		error = null;

		if (!email.trim() || !password) {
			error = 'Please enter email and password';
			return;
		}

		isLoading = true;

		try {
			const apiBase = ''; // nginx proxies /api/ to API server
			const response = await fetch(`${apiBase}/api/v1/auth/login`, {
				method: 'POST',
				headers: {
					'Content-Type': 'application/json'
				},
				body: JSON.stringify({
					username: email.trim(),
					password: password
				})
			});

			if (!response.ok) {
				const data = await response.json().catch(() => ({}));
				throw new Error(data.error || 'Login failed');
			}

			const data = await response.json();
			auth.login(data.token);
			goto('/finanzioso');
		} catch (err) {
			error = err instanceof Error ? err.message : 'Login failed';
		} finally {
			isLoading = false;
		}
	}
</script>

<svelte:head>
	<title>Login - MAINRAG</title>
</svelte:head>

<div class="login-page container">
	<div class="login-card card">
		<h1>Login</h1>
		<p class="description">Sign in to access Finanzioso and admin features</p>

		<form onsubmit={handleLogin}>
			{#if error}
				<div class="error-message">
					{error}
				</div>
			{/if}

			<div class="form-group">
				<label for="email">Email</label>
				<input
					id="email"
					type="email"
					class="input"
					placeholder="admin@mainrag.local"
					bind:value={email}
					disabled={isLoading}
				/>
			</div>

			<div class="form-group">
				<label for="password">Password</label>
				<input
					id="password"
					type="password"
					class="input"
					placeholder="Enter password"
					bind:value={password}
					disabled={isLoading}
				/>
			</div>

			<button type="submit" class="btn btn-primary login-btn" disabled={isLoading}>
				{isLoading ? 'Signing in...' : 'Login'}
			</button>
		</form>

		<div class="help-text">
			<p>Default: admin@mainrag.local / admin123</p>
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
		max-width: 400px;
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

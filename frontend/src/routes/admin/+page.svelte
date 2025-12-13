<script lang="ts">
	import { onMount } from 'svelte';
	import { goto } from '$app/navigation';
	import { auth, isAdmin } from '$lib/stores/auth';
	import { adminApi, type AdminSource, type SystemStats } from '$lib/api/client';

	let stats = $state<SystemStats | null>(null);
	let sources = $state<AdminSource[]>([]);
	let isLoading = $state(true);
	let error = $state<string | null>(null);

	// New source form
	let showNewSource = $state(false);
	let newSource = $state({ name: '', source_type: 'fs', path: '' });
	let isCreating = $state(false);

	onMount(() => {
		if (!$isAdmin) {
			goto('/login');
			return;
		}
		loadData();
	});

	async function loadData() {
		if (!$auth.token) return;

		isLoading = true;
		error = null;

		try {
			const [statsData, sourcesData] = await Promise.all([
				adminApi.getStats($auth.token),
				adminApi.getSources($auth.token)
			]);
			stats = statsData;
			sources = sourcesData;
		} catch (err) {
			error = err instanceof Error ? err.message : 'Failed to load data';
		} finally {
			isLoading = false;
		}
	}

	async function createSource(e: Event) {
		e.preventDefault();
		if (!$auth.token) return;

		isCreating = true;
		error = null;

		try {
			const created = await adminApi.createSource($auth.token, newSource);
			sources = [...sources, created];
			showNewSource = false;
			newSource = { name: '', source_type: 'fs', path: '' };
			await loadData(); // Refresh stats
		} catch (err) {
			error = err instanceof Error ? err.message : 'Failed to create source';
		} finally {
			isCreating = false;
		}
	}

	async function deleteSource(id: number, name: string) {
		if (!$auth.token) return;
		if (!confirm(`Delete source "${name}"? This will remove all associated files and chunks.`)) {
			return;
		}

		try {
			await adminApi.deleteSource($auth.token, id);
			sources = sources.filter(s => s.id !== id);
			await loadData(); // Refresh stats
		} catch (err) {
			error = err instanceof Error ? err.message : 'Failed to delete source';
		}
	}

	async function syncSource(id: number) {
		if (!$auth.token) return;

		try {
			const result = await adminApi.syncSource($auth.token, id);
			alert(result.message);
		} catch (err) {
			error = err instanceof Error ? err.message : 'Failed to sync source';
		}
	}

	function formatBytes(bytes: number): string {
		if (bytes === 0) return '0 B';
		const k = 1024;
		const sizes = ['B', 'KB', 'MB', 'GB'];
		const i = Math.floor(Math.log(bytes) / Math.log(k));
		return parseFloat((bytes / Math.pow(k, i)).toFixed(1)) + ' ' + sizes[i];
	}

	function formatDate(date: string | null): string {
		if (!date) return 'Never';
		return new Date(date).toLocaleString();
	}
</script>

<svelte:head>
	<title>Admin - MAINRAG</title>
</svelte:head>

<div class="admin-page container">
	<div class="admin-header">
		<h1>Admin Dashboard</h1>
		<button class="btn btn-primary" onclick={() => loadData()}>Refresh</button>
	</div>

	{#if error}
		<div class="error-message">
			{error}
		</div>
	{/if}

	{#if isLoading}
		<div class="loading">Loading...</div>
	{:else}
		<!-- Stats Cards -->
		{#if stats}
			<div class="stats-grid">
				<div class="stat-card card">
					<div class="stat-value">{stats.sources}</div>
					<div class="stat-label">Sources</div>
				</div>
				<div class="stat-card card">
					<div class="stat-value">{stats.files.toLocaleString()}</div>
					<div class="stat-label">Files</div>
				</div>
				<div class="stat-card card">
					<div class="stat-value">{stats.chunks.toLocaleString()}</div>
					<div class="stat-label">Chunks</div>
				</div>
				<div class="stat-card card">
					<div class="stat-value">{stats.postgres_size}</div>
					<div class="stat-label">Database Size</div>
				</div>
			</div>
		{/if}

		<!-- Sources Section -->
		<section class="sources-section">
			<div class="section-header">
				<h2>Sources</h2>
				<button class="btn btn-primary" onclick={() => showNewSource = !showNewSource}>
					{showNewSource ? 'Cancel' : 'Add Source'}
				</button>
			</div>

			{#if showNewSource}
				<form class="new-source-form card" onsubmit={createSource}>
					<h3>New Source</h3>
					<div class="form-row">
						<div class="form-group">
							<label for="name">Name</label>
							<input
								id="name"
								type="text"
								class="input"
								placeholder="my-project"
								bind:value={newSource.name}
								required
							/>
						</div>
						<div class="form-group">
							<label for="type">Type</label>
							<select id="type" class="input" bind:value={newSource.source_type}>
								<option value="fs">Filesystem</option>
								<option value="git">Git Repository</option>
								<option value="web">Web Crawl</option>
							</select>
						</div>
					</div>
					<div class="form-group">
						<label for="path">Path / URL</label>
						<input
							id="path"
							type="text"
							class="input"
							placeholder="/path/to/source or https://github.com/..."
							bind:value={newSource.path}
							required
						/>
					</div>
					<button type="submit" class="btn btn-primary" disabled={isCreating}>
						{isCreating ? 'Creating...' : 'Create Source'}
					</button>
				</form>
			{/if}

			<div class="sources-list">
				{#each sources as source}
					<div class="source-card card">
						<div class="source-header">
							<div class="source-info">
								<h3>{source.name}</h3>
								<span class="badge">{source.source_type}</span>
							</div>
							<div class="source-actions">
								<button class="btn btn-secondary" onclick={() => syncSource(source.id)}>
									Sync
								</button>
								<button class="btn btn-danger" onclick={() => deleteSource(source.id, source.name)}>
									Delete
								</button>
							</div>
						</div>
						<div class="source-path">{source.path}</div>
						<div class="source-stats">
							<span>{source.file_count} files</span>
							<span>{source.chunk_count} chunks</span>
							<span>{formatBytes(source.total_size)}</span>
							<span>Last sync: {formatDate(source.last_synced)}</span>
						</div>
					</div>
				{/each}

				{#if sources.length === 0}
					<div class="no-sources">
						<p>No sources configured yet.</p>
						<p>Add a source to start indexing your codebase.</p>
					</div>
				{/if}
			</div>
		</section>
	{/if}
</div>

<style>
	.admin-page {
		max-width: 1000px;
	}

	.admin-header {
		display: flex;
		justify-content: space-between;
		align-items: center;
		margin-bottom: 2rem;
	}

	.admin-header h1 {
		font-size: 1.75rem;
	}

	.error-message {
		background: rgba(239, 68, 68, 0.1);
		border: 1px solid var(--color-error);
		color: var(--color-error);
		padding: 1rem;
		border-radius: 6px;
		margin-bottom: 1rem;
	}

	.loading {
		text-align: center;
		padding: 3rem;
		color: var(--color-text-muted);
	}

	.stats-grid {
		display: grid;
		grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
		gap: 1rem;
		margin-bottom: 2rem;
	}

	.stat-card {
		text-align: center;
		padding: 1.5rem;
	}

	.stat-value {
		font-size: 2rem;
		font-weight: 700;
		color: var(--color-primary);
	}

	.stat-label {
		color: var(--color-text-muted);
		font-size: 0.875rem;
		margin-top: 0.25rem;
	}

	.sources-section {
		margin-top: 2rem;
	}

	.section-header {
		display: flex;
		justify-content: space-between;
		align-items: center;
		margin-bottom: 1rem;
	}

	.section-header h2 {
		font-size: 1.25rem;
	}

	.new-source-form {
		margin-bottom: 1.5rem;
	}

	.new-source-form h3 {
		font-size: 1rem;
		margin-bottom: 1rem;
	}

	.form-row {
		display: grid;
		grid-template-columns: 1fr 1fr;
		gap: 1rem;
	}

	.form-group {
		margin-bottom: 1rem;
	}

	.form-group label {
		display: block;
		margin-bottom: 0.5rem;
		font-weight: 500;
		font-size: 0.875rem;
	}

	.sources-list {
		display: flex;
		flex-direction: column;
		gap: 1rem;
	}

	.source-card {
		padding: 1rem 1.25rem;
	}

	.source-header {
		display: flex;
		justify-content: space-between;
		align-items: flex-start;
		margin-bottom: 0.5rem;
	}

	.source-info {
		display: flex;
		align-items: center;
		gap: 0.75rem;
	}

	.source-info h3 {
		font-size: 1rem;
		font-weight: 600;
	}

	.source-actions {
		display: flex;
		gap: 0.5rem;
	}

	.source-path {
		font-family: var(--font-mono);
		font-size: 0.8rem;
		color: var(--color-text-muted);
		margin-bottom: 0.75rem;
	}

	.source-stats {
		display: flex;
		flex-wrap: wrap;
		gap: 1rem;
		font-size: 0.8rem;
		color: var(--color-text-muted);
	}

	.no-sources {
		text-align: center;
		padding: 3rem;
		color: var(--color-text-muted);
	}

	@media (max-width: 600px) {
		.form-row {
			grid-template-columns: 1fr;
		}

		.source-header {
			flex-direction: column;
			gap: 0.75rem;
		}

		.source-actions {
			width: 100%;
		}

		.source-actions button {
			flex: 1;
		}
	}
</style>

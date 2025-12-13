<script lang="ts">
	import { api, type SearchResult, type SearchResponse } from '$lib/api/client';

	let query = $state('');
	let results = $state<SearchResult[]>([]);
	let isLoading = $state(false);
	let error = $state<string | null>(null);
	let searchInfo = $state<{ total: number; took_ms: number } | null>(null);

	async function handleSearch(e: Event) {
		e.preventDefault();
		if (!query.trim()) return;

		isLoading = true;
		error = null;
		results = [];
		searchInfo = null;

		try {
			const response: SearchResponse = await api.search({
				query: query.trim(),
				limit: 20
			});
			results = response.results;
			searchInfo = { total: response.total, took_ms: response.took_ms };
		} catch (err) {
			error = err instanceof Error ? err.message : 'Search failed';
		} finally {
			isLoading = false;
		}
	}

	function highlightCode(content: string): string {
		// Simple syntax highlighting - escape HTML and add basic highlighting
		return content
			.replace(/&/g, '&amp;')
			.replace(/</g, '&lt;')
			.replace(/>/g, '&gt;')
			.replace(/(".*?")/g, '<span class="string">$1</span>')
			.replace(/('.*?')/g, '<span class="string">$1</span>')
			.replace(/\b(const|let|var|function|async|await|return|if|else|for|while|import|export|from|class|interface|type|struct|fn|pub|impl|use|mod)\b/g, '<span class="keyword">$1</span>')
			.replace(/(\/\/.*$)/gm, '<span class="comment">$1</span>');
	}
</script>

<svelte:head>
	<title>MAINRAG - Code Search</title>
</svelte:head>

<div class="search-page container">
	<div class="search-header">
		<h1>Code Search</h1>
		<p>Semantic + keyword search across your codebase</p>
	</div>

	<form class="search-form" onsubmit={handleSearch}>
		<div class="search-input-wrapper">
			<input
				type="text"
				class="input search-input"
				placeholder="Search code... (e.g., 'authentication middleware', 'database connection')"
				bind:value={query}
			/>
			<button type="submit" class="btn btn-primary search-btn" disabled={isLoading || !query.trim()}>
				{#if isLoading}
					Searching...
				{:else}
					Search
				{/if}
			</button>
		</div>
	</form>

	{#if error}
		<div class="error-message">
			{error}
		</div>
	{/if}

	{#if searchInfo}
		<div class="search-info">
			Found {searchInfo.total} results in {searchInfo.took_ms}ms
		</div>
	{/if}

	<div class="results">
		{#each results as result}
			<div class="result-card card">
				<div class="result-header">
					<div class="result-path">
						<span class="source-name">{result.source_name}</span>
						<span class="file-path">{result.file_path}</span>
						<span class="line-info">:{result.line_start}-{result.line_end}</span>
					</div>
					<div class="result-meta">
						{#if result.language}
							<span class="badge">{result.language}</span>
						{/if}
						<span class="score">Score: {(result.score * 100).toFixed(1)}%</span>
					</div>
				</div>
				<pre class="result-content"><code>{@html highlightCode(result.content)}</code></pre>
			</div>
		{/each}
	</div>

	{#if !isLoading && !error && results.length === 0 && searchInfo}
		<div class="no-results">
			<p>No results found for "{query}"</p>
			<p class="hint">Try different keywords or a more general query</p>
		</div>
	{/if}
</div>

<style>
	.search-page {
		max-width: 900px;
	}

	.search-header {
		text-align: center;
		margin-bottom: 2rem;
	}

	.search-header h1 {
		font-size: 2rem;
		margin-bottom: 0.5rem;
	}

	.search-header p {
		color: var(--color-text-muted);
	}

	.search-form {
		margin-bottom: 1.5rem;
	}

	.search-input-wrapper {
		display: flex;
		gap: 0.5rem;
	}

	.search-input {
		flex: 1;
		font-size: 1rem;
	}

	.search-btn {
		padding: 0.75rem 1.5rem;
		white-space: nowrap;
	}

	.error-message {
		background: rgba(239, 68, 68, 0.1);
		border: 1px solid var(--color-error);
		color: var(--color-error);
		padding: 1rem;
		border-radius: 6px;
		margin-bottom: 1rem;
	}

	.search-info {
		color: var(--color-text-muted);
		font-size: 0.875rem;
		margin-bottom: 1rem;
	}

	.results {
		display: flex;
		flex-direction: column;
		gap: 1rem;
	}

	.result-card {
		padding: 1rem;
	}

	.result-header {
		display: flex;
		justify-content: space-between;
		align-items: flex-start;
		margin-bottom: 0.75rem;
		gap: 1rem;
	}

	.result-path {
		font-family: var(--font-mono);
		font-size: 0.875rem;
		display: flex;
		flex-wrap: wrap;
		align-items: center;
		gap: 0.25rem;
	}

	.source-name {
		color: var(--color-primary);
		font-weight: 500;
	}

	.source-name::after {
		content: '/';
		color: var(--color-text-muted);
		margin-left: 0.25rem;
	}

	.file-path {
		color: var(--color-text);
	}

	.line-info {
		color: var(--color-text-muted);
	}

	.result-meta {
		display: flex;
		align-items: center;
		gap: 0.5rem;
		flex-shrink: 0;
	}

	.score {
		font-size: 0.75rem;
		color: var(--color-text-muted);
	}

	.result-content {
		margin: 0;
		padding: 1rem;
		font-size: 0.85rem;
		line-height: 1.5;
		max-height: 300px;
		overflow: auto;
	}

	.result-content :global(.keyword) {
		color: #c792ea;
	}

	.result-content :global(.string) {
		color: #c3e88d;
	}

	.result-content :global(.comment) {
		color: #676e95;
		font-style: italic;
	}

	.no-results {
		text-align: center;
		padding: 3rem;
		color: var(--color-text-muted);
	}

	.no-results .hint {
		font-size: 0.875rem;
		margin-top: 0.5rem;
	}
</style>

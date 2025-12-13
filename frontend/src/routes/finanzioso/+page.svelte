<script lang="ts">
	import type { WatchlistData, WatchlistItem } from './+page';

	let { data }: { data: WatchlistData } = $props();

	let newSymbol = $state('');
	let isAdding = $state(false);
	let addError = $state<string | null>(null);

	// Mock items for UI development (remove when API connected)
	let mockItems = $state<WatchlistItem[]>([
		{
			stock: { id: 1, symbol: 'AAPL', name: 'Apple Inc.', exchange: 'NASDAQ' },
			latest_score: { score: 72.5, action: 'BUY', confidence: 'HIGH', safe_mode: false, calculated_at: new Date().toISOString() }
		},
		{
			stock: { id: 2, symbol: 'MSFT', name: 'Microsoft Corp.', exchange: 'NASDAQ' },
			latest_score: { score: 48.3, action: 'HOLD', confidence: 'MEDIUM', safe_mode: false, calculated_at: new Date().toISOString() }
		},
		{
			stock: { id: 3, symbol: 'TSLA', name: 'Tesla Inc.', exchange: 'NASDAQ' },
			latest_score: { score: 28.1, action: 'SELL', confidence: 'LOW', safe_mode: true, calculated_at: new Date().toISOString() }
		}
	]);

	// Use mock items for now, switch to data.items when API ready
	let items = $derived(data.items.length > 0 ? data.items : mockItems);

	async function handleAddStock(e: Event) {
		e.preventDefault();
		const symbol = newSymbol.trim().toUpperCase();
		if (!symbol) return;

		isAdding = true;
		addError = null;

		try {
			// TODO: Connect to real API in A4
			// const response = await fetch('/finanzioso/watchlist', {
			// 	method: 'POST',
			// 	headers: { 'Content-Type': 'application/json' },
			// 	body: JSON.stringify({ symbol })
			// });
			// if (!response.ok) throw new Error('Failed to add stock');

			// Mock: Add to local list
			mockItems = [...mockItems, {
				stock: { id: Date.now(), symbol, name: null, exchange: 'NYSE' },
				latest_score: null
			}];
			newSymbol = '';
		} catch (err) {
			addError = err instanceof Error ? err.message : 'Failed to add stock';
		} finally {
			isAdding = false;
		}
	}

	function getActionClass(action: string): string {
		switch (action) {
			case 'BUY': return 'action-buy';
			case 'SELL': return 'action-sell';
			default: return 'action-hold';
		}
	}

	function getConfidenceClass(confidence: string): string {
		switch (confidence) {
			case 'HIGH': return 'confidence-high';
			case 'LOW': return 'confidence-low';
			default: return 'confidence-medium';
		}
	}

	function formatTime(isoString: string): string {
		const date = new Date(isoString);
		const now = new Date();
		const diffMs = now.getTime() - date.getTime();
		const diffMins = Math.floor(diffMs / 60000);

		if (diffMins < 1) return 'just now';
		if (diffMins < 60) return `${diffMins}m ago`;
		const diffHours = Math.floor(diffMins / 60);
		if (diffHours < 24) return `${diffHours}h ago`;
		const diffDays = Math.floor(diffHours / 24);
		return `${diffDays}d ago`;
	}
</script>

<svelte:head>
	<title>Finanzioso - AI Stock Assistant</title>
</svelte:head>

<div class="finanzioso-page container">
	<div class="page-header">
		<h1>Finanzioso</h1>
		<p class="subtitle">AI Stock Assistant</p>
	</div>

	<div class="disclaimer">
		{data.disclaimer}
	</div>

	<form class="add-stock-form" onsubmit={handleAddStock}>
		<input
			type="text"
			class="input"
			placeholder="Enter symbol (e.g., AAPL)"
			bind:value={newSymbol}
			disabled={isAdding}
		/>
		<button type="submit" class="btn btn-primary" disabled={isAdding || !newSymbol.trim()}>
			{isAdding ? 'Adding...' : 'Add Stock'}
		</button>
	</form>

	{#if addError}
		<div class="error-message">{addError}</div>
	{/if}

	{#if data.error}
		<div class="error-message">{data.error}</div>
	{/if}

	<div class="watchlist-container">
		{#if items.length === 0}
			<div class="empty-state">
				<p>No stocks in your watchlist</p>
				<p class="hint">Add a stock symbol above to get started</p>
			</div>
		{:else}
			<table class="watchlist-table">
				<thead>
					<tr>
						<th>Symbol</th>
						<th>Name</th>
						<th>Action</th>
						<th>Score</th>
						<th>Confidence</th>
						<th>Updated</th>
					</tr>
				</thead>
				<tbody>
					{#each items as item}
						<tr>
							<td class="symbol-cell">
								<a href="/finanzioso/{item.stock.symbol}">{item.stock.symbol}</a>
								<span class="exchange">{item.stock.exchange}</span>
							</td>
							<td class="name-cell">{item.stock.name || '-'}</td>
							<td>
								{#if item.latest_score}
									<span class="action-badge {getActionClass(item.latest_score.action)}">
										{item.latest_score.action}
									</span>
									{#if item.latest_score.safe_mode}
										<span class="safe-mode-badge" title="Safe mode: Limited data available">SAFE</span>
									{/if}
								{:else}
									<span class="pending-badge">Pending</span>
								{/if}
							</td>
							<td class="score-cell">
								{#if item.latest_score}
									<span class="score-value">{item.latest_score.score.toFixed(1)}</span>
								{:else}
									-
								{/if}
							</td>
							<td>
								{#if item.latest_score}
									<span class="confidence-badge {getConfidenceClass(item.latest_score.confidence)}">
										{item.latest_score.confidence}
									</span>
								{:else}
									-
								{/if}
							</td>
							<td class="updated-cell">
								{#if item.latest_score}
									{formatTime(item.latest_score.calculated_at)}
								{:else}
									-
								{/if}
							</td>
						</tr>
					{/each}
				</tbody>
			</table>
		{/if}
	</div>
</div>

<style>
	.finanzioso-page {
		max-width: 1000px;
	}

	.page-header {
		text-align: center;
		margin-bottom: 1.5rem;
	}

	.page-header h1 {
		font-size: 2rem;
		margin-bottom: 0.25rem;
	}

	.subtitle {
		color: var(--color-text-muted);
		font-size: 1rem;
	}

	.disclaimer {
		background: rgba(245, 158, 11, 0.1);
		border: 1px solid rgba(245, 158, 11, 0.3);
		color: #b45309;
		padding: 0.75rem 1rem;
		border-radius: 6px;
		font-size: 0.8rem;
		margin-bottom: 1.5rem;
		text-align: center;
	}

	.add-stock-form {
		display: flex;
		gap: 0.5rem;
		margin-bottom: 1rem;
	}

	.add-stock-form input {
		flex: 1;
		max-width: 300px;
		text-transform: uppercase;
	}

	.error-message {
		background: rgba(239, 68, 68, 0.1);
		border: 1px solid var(--color-error);
		color: var(--color-error);
		padding: 0.75rem 1rem;
		border-radius: 6px;
		margin-bottom: 1rem;
		font-size: 0.875rem;
	}

	.watchlist-container {
		background: var(--color-surface);
		border: 1px solid var(--color-border);
		border-radius: 8px;
		overflow: hidden;
	}

	.empty-state {
		text-align: center;
		padding: 3rem;
		color: var(--color-text-muted);
	}

	.empty-state .hint {
		font-size: 0.875rem;
		margin-top: 0.5rem;
	}

	.watchlist-table {
		width: 100%;
		border-collapse: collapse;
	}

	.watchlist-table th,
	.watchlist-table td {
		padding: 0.75rem 1rem;
		text-align: left;
		border-bottom: 1px solid var(--color-border);
	}

	.watchlist-table th {
		background: var(--color-bg);
		font-weight: 600;
		font-size: 0.75rem;
		text-transform: uppercase;
		letter-spacing: 0.05em;
		color: var(--color-text-muted);
	}

	.watchlist-table tbody tr:hover {
		background: var(--color-bg);
	}

	.watchlist-table tbody tr:last-child td {
		border-bottom: none;
	}

	.symbol-cell a {
		font-weight: 600;
		color: var(--color-primary);
		text-decoration: none;
	}

	.symbol-cell a:hover {
		text-decoration: underline;
	}

	.exchange {
		display: block;
		font-size: 0.7rem;
		color: var(--color-text-muted);
	}

	.name-cell {
		color: var(--color-text-muted);
		font-size: 0.875rem;
	}

	.action-badge {
		display: inline-block;
		padding: 0.25rem 0.5rem;
		border-radius: 4px;
		font-size: 0.75rem;
		font-weight: 600;
	}

	.action-buy {
		background: rgba(34, 197, 94, 0.15);
		color: #16a34a;
	}

	.action-hold {
		background: rgba(107, 114, 128, 0.15);
		color: #6b7280;
	}

	.action-sell {
		background: rgba(239, 68, 68, 0.15);
		color: #dc2626;
	}

	.safe-mode-badge {
		display: inline-block;
		margin-left: 0.25rem;
		padding: 0.125rem 0.375rem;
		background: rgba(245, 158, 11, 0.15);
		color: #b45309;
		border-radius: 4px;
		font-size: 0.625rem;
		font-weight: 600;
	}

	.pending-badge {
		display: inline-block;
		padding: 0.25rem 0.5rem;
		background: rgba(107, 114, 128, 0.1);
		color: var(--color-text-muted);
		border-radius: 4px;
		font-size: 0.75rem;
	}

	.score-cell {
		font-family: var(--font-mono);
	}

	.score-value {
		font-weight: 600;
	}

	.confidence-badge {
		display: inline-block;
		padding: 0.125rem 0.375rem;
		border-radius: 4px;
		font-size: 0.7rem;
		font-weight: 500;
	}

	.confidence-high {
		background: rgba(34, 197, 94, 0.1);
		color: #16a34a;
	}

	.confidence-medium {
		background: rgba(245, 158, 11, 0.1);
		color: #b45309;
	}

	.confidence-low {
		background: rgba(239, 68, 68, 0.1);
		color: #dc2626;
	}

	.updated-cell {
		color: var(--color-text-muted);
		font-size: 0.8rem;
	}
</style>

<script lang="ts">
	import { onMount, onDestroy } from 'svelte';

	interface Stock {
		id: number;
		symbol: string;
		name: string | null;
		exchange: string;
	}

	interface Score {
		score: number;
		action: string;
		confidence: string;
		safe_mode: boolean;
		calculated_at: string;
	}

	interface WatchlistItem {
		stock: Stock;
		latest_score: Score | null;
	}

	interface SearchResult {
		symbol: string;
		name: string;
		exchange: string;
		type_display: string;
	}

	let items = $state<WatchlistItem[]>([]);
	let loading = $state(true);
	let error = $state<string | null>(null);
	let newSymbol = $state('');
	let isAdding = $state(false);
	let addError = $state<string | null>(null);

	// Autocomplete state
	let searchResults = $state<SearchResult[]>([]);
	let showSuggestions = $state(false);
	let isSearching = $state(false);
	let searchTimeout: ReturnType<typeof setTimeout> | null = null;
	let selectedIndex = $state(-1);

	// Polling state for auto-refresh
	let pollInterval: ReturnType<typeof setInterval> | null = null;
	let calculatingSymbols = $state<Set<string>>(new Set());

	// Use relative URLs - nginx proxies /api/ to the API server
	const API_BASE = '';

	function getToken(): string | null {
		const stored = localStorage.getItem('mainrag_auth');
		if (stored) {
			try {
				return JSON.parse(stored).token;
			} catch {
				return null;
			}
		}
		return null;
	}

	async function loadWatchlist(silent = false) {
		const token = getToken();
		if (!token) {
			loading = false;
			error = 'Please log in to view your watchlist';
			return;
		}

		try {
			// Cache-busting to ensure fresh data
			const response = await fetch(`${API_BASE}/api/v1/finanzioso/watchlist?_=${Date.now()}`, {
				headers: {
					'Authorization': `Bearer ${token}`,
					'Cache-Control': 'no-cache'
				}
			});

			if (response.status === 401) {
				error = 'Session expired. Please log in again.';
				loading = false;
				return;
			}

			if (!response.ok) {
				throw new Error('Failed to load watchlist');
			}

			const data = await response.json();
			// Force new array reference for reactivity
			items = [...(data.items || [])];
			if (!silent) error = null;

			// Check if any stocks are still calculating
			checkPendingScores();
		} catch (err) {
			if (!silent) error = err instanceof Error ? err.message : 'Failed to load';
		} finally {
			loading = false;
		}
	}

	function checkPendingScores() {
		const pending = items.filter(item => !item.latest_score);
		const pendingSymbols = new Set(pending.map(p => p.stock.symbol));

		// Update calculating symbols (remove ones that now have scores)
		const newCalculating = new Set<string>();
		for (const symbol of calculatingSymbols) {
			if (pendingSymbols.has(symbol)) {
				newCalculating.add(symbol);
			}
		}
		calculatingSymbols = newCalculating;

		// Start or stop polling based on pending items
		if (pending.length > 0 && calculatingSymbols.size > 0) {
			startPolling();
		} else {
			stopPolling();
		}
	}

	function startPolling() {
		if (pollInterval) return; // Already polling
		pollInterval = setInterval(() => {
			loadWatchlist(true); // Silent refresh
		}, 2000); // Poll every 2 seconds
	}

	function stopPolling() {
		if (pollInterval) {
			clearInterval(pollInterval);
			pollInterval = null;
		}
	}

	function isCalculating(symbol: string): boolean {
		return calculatingSymbols.has(symbol);
	}

	async function addStock(e: Event) {
		e.preventDefault();
		const symbol = newSymbol.trim().toUpperCase();
		if (!symbol) return;

		const token = getToken();
		if (!token) {
			addError = 'Please log in first';
			return;
		}

		isAdding = true;
		addError = null;

		try {
			const response = await fetch(`${API_BASE}/api/v1/finanzioso/watchlist`, {
				method: 'POST',
				headers: {
					'Content-Type': 'application/json',
					'Authorization': `Bearer ${token}`
				},
				body: JSON.stringify({ symbol })
			});

			if (!response.ok) {
				const data = await response.json().catch(() => ({}));
				throw new Error(data.error || 'Failed to add stock');
			}

			const addedStock = await response.json();
			newSymbol = '';

			// Immediately add to list with calculating state
			const newItem: WatchlistItem = {
				stock: {
					id: addedStock.id,
					symbol: addedStock.symbol,
					name: addedStock.name,
					exchange: addedStock.exchange
				},
				latest_score: null
			};

			// Add to items if not already present
			if (!items.some(item => item.stock.symbol === symbol)) {
				items = [...items, newItem];
			}

			// Mark as calculating and start polling
			calculatingSymbols = new Set([...calculatingSymbols, symbol]);
			startPolling();
		} catch (err) {
			addError = err instanceof Error ? err.message : 'Failed to add';
		} finally {
			isAdding = false;
		}
	}

	function getActionClass(action: string): string {
		if (action === 'BUY') return 'action-buy';
		if (action === 'SELL') return 'action-sell';
		return 'action-hold';
	}

	function getConfidenceClass(confidence: string): string {
		if (confidence === 'HIGH') return 'confidence-high';
		if (confidence === 'LOW') return 'confidence-low';
		return 'confidence-medium';
	}

	async function searchSymbols(query: string) {
		if (query.trim().length < 1) {
			searchResults = [];
			showSuggestions = false;
			return;
		}

		isSearching = true;
		try {
			const response = await fetch(`${API_BASE}/api/v1/finanzioso/search?q=${encodeURIComponent(query)}`);
			if (response.ok) {
				const data = await response.json();
				searchResults = data.results || [];
				showSuggestions = searchResults.length > 0;
				selectedIndex = -1;
			}
		} catch {
			// Silently fail search
			searchResults = [];
		} finally {
			isSearching = false;
		}
	}

	function handleInput(e: Event) {
		const value = (e.target as HTMLInputElement).value;
		newSymbol = value;
		addError = null;

		// Debounce search
		if (searchTimeout) clearTimeout(searchTimeout);
		searchTimeout = setTimeout(() => {
			searchSymbols(value);
		}, 300);
	}

	function selectSuggestion(result: SearchResult) {
		newSymbol = result.symbol;
		showSuggestions = false;
		searchResults = [];
	}

	function handleKeydown(e: KeyboardEvent) {
		if (!showSuggestions || searchResults.length === 0) return;

		if (e.key === 'ArrowDown') {
			e.preventDefault();
			selectedIndex = Math.min(selectedIndex + 1, searchResults.length - 1);
		} else if (e.key === 'ArrowUp') {
			e.preventDefault();
			selectedIndex = Math.max(selectedIndex - 1, -1);
		} else if (e.key === 'Enter' && selectedIndex >= 0) {
			e.preventDefault();
			selectSuggestion(searchResults[selectedIndex]);
		} else if (e.key === 'Escape') {
			showSuggestions = false;
		}
	}

	function handleBlur() {
		// Delay to allow click on suggestion
		setTimeout(() => {
			showSuggestions = false;
		}, 200);
	}

	onMount(() => {
		loadWatchlist();
	});

	onDestroy(() => {
		stopPolling();
		if (searchTimeout) clearTimeout(searchTimeout);
	});
</script>

<svelte:head>
	<title>Finanzioso - AI Stock Assistant</title>
	<meta http-equiv="Cache-Control" content="no-cache, no-store, must-revalidate" />
	<meta http-equiv="Pragma" content="no-cache" />
	<meta http-equiv="Expires" content="0" />
</svelte:head>

<div class="finanzioso-page container">
	<div class="page-header">
		<h1>Finanzioso</h1>
		<p class="subtitle">AI Stock Assistant</p>
	</div>

	<div class="disclaimer">
		This is NOT investment advice. Scores are for educational/research purposes only.
	</div>

	<form class="add-stock-form" onsubmit={addStock}>
		<div class="symbol-input-wrapper">
			<input
				type="text"
				class="input"
				placeholder="Search stock (e.g., AAPL, Apple)"
				value={newSymbol}
				oninput={handleInput}
				onkeydown={handleKeydown}
				onblur={handleBlur}
				onfocus={() => { if (searchResults.length > 0) showSuggestions = true; }}
				disabled={isAdding}
				autocomplete="off"
			/>
			{#if isSearching}
				<span class="search-spinner"></span>
			{/if}
			{#if showSuggestions && searchResults.length > 0}
				<ul class="suggestions-dropdown">
					{#each searchResults as result, i}
						<li
							class="suggestion-item"
							class:selected={i === selectedIndex}
							onmousedown={() => selectSuggestion(result)}
						>
							<span class="suggestion-symbol">{result.symbol}</span>
							<span class="suggestion-name">{result.name}</span>
							<span class="suggestion-exchange">{result.exchange}</span>
						</li>
					{/each}
				</ul>
			{/if}
		</div>
		<button type="submit" class="btn btn-primary" disabled={isAdding || !newSymbol.trim()}>
			{isAdding ? 'Adding...' : 'Add Stock'}
		</button>
	</form>

	{#if addError}
		<div class="error-message">{addError}</div>
	{/if}

	{#if error}
		<div class="error-message">{error}</div>
	{/if}

	<div class="watchlist-container">
		{#if loading}
			<div class="empty-state">
				<p>Loading watchlist...</p>
			</div>
		{:else if items.length === 0}
			<div class="empty-state">
				<p>No stocks in your watchlist</p>
				<p class="hint">Add a stock symbol above to get started</p>
			</div>
		{:else}
			<table class="watchlist-table">
				<thead>
					<tr>
						<th>Symbol</th>
						<th>Action</th>
						<th>Score</th>
						<th>Confidence</th>
					</tr>
				</thead>
				<tbody>
					{#each items as item}
						<tr class="clickable-row" onclick={() => window.location.href = `/finanzioso/${item.stock.symbol}`}>
							<td class="symbol-cell">
								<a href="/finanzioso/{item.stock.symbol}" class="stock-link">
									<strong>{item.stock.symbol}</strong>
								</a>
								<span class="exchange">{item.stock.exchange}</span>
							</td>
							<td>
								{#if item.latest_score}
									<span class="action-badge {getActionClass(item.latest_score.action)}">
										{item.latest_score.action}
									</span>
									{#if item.latest_score.safe_mode}
										<span class="safe-mode-badge">SAFE</span>
									{/if}
								{:else if isCalculating(item.stock.symbol)}
									<span class="calculating-badge">
										<span class="calc-spinner"></span>
										Calculating...
									</span>
								{:else}
									<span class="pending-badge">Pending</span>
								{/if}
							</td>
							<td class="score-cell">
								{#if item.latest_score}
									{Number(item.latest_score.score).toFixed(1)}
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
						</tr>
					{/each}
				</tbody>
			</table>
		{/if}
	</div>
</div>

<style>
	.finanzioso-page {
		max-width: 800px;
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

	.symbol-input-wrapper {
		position: relative;
		flex: 1;
		max-width: 350px;
	}

	.symbol-input-wrapper input {
		width: 100%;
		text-transform: uppercase;
	}

	.search-spinner {
		position: absolute;
		right: 10px;
		top: 50%;
		transform: translateY(-50%);
		width: 16px;
		height: 16px;
		border: 2px solid var(--color-border);
		border-top-color: var(--color-primary);
		border-radius: 50%;
		animation: spin 0.8s linear infinite;
	}

	@keyframes spin {
		to { transform: translateY(-50%) rotate(360deg); }
	}

	.suggestions-dropdown {
		position: absolute;
		top: 100%;
		left: 0;
		right: 0;
		background: var(--color-surface);
		border: 1px solid var(--color-border);
		border-top: none;
		border-radius: 0 0 6px 6px;
		max-height: 250px;
		overflow-y: auto;
		z-index: 100;
		list-style: none;
		margin: 0;
		padding: 0;
		box-shadow: 0 4px 6px rgba(0, 0, 0, 0.1);
	}

	.suggestion-item {
		display: flex;
		align-items: center;
		gap: 0.5rem;
		padding: 0.625rem 0.75rem;
		cursor: pointer;
		border-bottom: 1px solid var(--color-border);
	}

	.suggestion-item:last-child {
		border-bottom: none;
	}

	.suggestion-item:hover,
	.suggestion-item.selected {
		background: rgba(59, 130, 246, 0.1);
	}

	.suggestion-symbol {
		font-weight: 600;
		font-size: 0.875rem;
		min-width: 60px;
	}

	.suggestion-name {
		flex: 1;
		font-size: 0.8rem;
		color: var(--color-text-muted);
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
	}

	.suggestion-exchange {
		font-size: 0.7rem;
		color: var(--color-text-muted);
		background: var(--color-bg);
		padding: 0.125rem 0.375rem;
		border-radius: 4px;
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
		color: var(--color-text-muted);
	}

	.watchlist-table tbody tr:last-child td {
		border-bottom: none;
	}

	.clickable-row {
		cursor: pointer;
		transition: background-color 0.15s ease;
	}

	.clickable-row:hover {
		background: rgba(59, 130, 246, 0.05);
	}

	.stock-link {
		color: inherit;
		text-decoration: none;
	}

	.stock-link:hover strong {
		color: var(--color-primary);
	}

	.symbol-cell strong {
		display: block;
	}

	.exchange {
		font-size: 0.7rem;
		color: var(--color-text-muted);
	}

	.action-badge, .pending-badge {
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

	.pending-badge {
		background: rgba(107, 114, 128, 0.1);
		color: var(--color-text-muted);
	}

	.calculating-badge {
		display: inline-flex;
		align-items: center;
		gap: 0.375rem;
		padding: 0.25rem 0.5rem;
		border-radius: 4px;
		font-size: 0.75rem;
		font-weight: 600;
		background: rgba(59, 130, 246, 0.1);
		color: #3b82f6;
	}

	.calc-spinner {
		width: 12px;
		height: 12px;
		border: 2px solid rgba(59, 130, 246, 0.3);
		border-top-color: #3b82f6;
		border-radius: 50%;
		animation: spin 0.8s linear infinite;
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

	.score-cell {
		font-family: var(--font-mono);
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
</style>

<script lang="ts">
	import { onMount } from 'svelte';
	import type { StockDetailData, Signal, ScoreHistoryItem } from './+page';

	let data = $state<StockDetailData | null>(null);
	let loading = $state(true);
	let error = $state<string | null>(null);

	// Track which quotes are expanded (by signal id)
	let expandedQuotes = $state<Set<number>>(new Set());

	const QUOTE_TRUNCATE_LENGTH = 150;

	// Get symbol from URL
	function getSymbol(): string {
		if (typeof window !== 'undefined') {
			const path = window.location.pathname;
			const parts = path.split('/');
			return parts[parts.length - 1]?.toUpperCase() || '';
		}
		return '';
	}

	function getToken(): string | null {
		if (typeof window !== 'undefined') {
			const stored = localStorage.getItem('mainrag_auth');
			if (stored) {
				try {
					return JSON.parse(stored).token;
				} catch {
					return null;
				}
			}
		}
		return null;
	}

	async function loadData() {
		const symbol = getSymbol();
		const token = getToken();

		if (!token) {
			error = 'Please log in to view stock details';
			loading = false;
			return;
		}

		try {
			const response = await fetch(`/api/v1/finanzioso/stock/${symbol}`, {
				headers: {
					'Authorization': `Bearer ${token}`
				}
			});

			if (response.status === 401) {
				error = 'Session expired. Please log in again.';
				loading = false;
				return;
			}

			if (response.status === 404) {
				error = `Stock ${symbol} not found in your watchlist`;
				loading = false;
				return;
			}

			if (!response.ok) {
				throw new Error('Failed to load stock details');
			}

			data = await response.json();
		} catch (err) {
			error = err instanceof Error ? err.message : 'Failed to load';
		} finally {
			loading = false;
		}
	}

	onMount(() => {
		loadData();
	});

	// Helper functions
	function getActionClass(action: string): string {
		if (action === 'BUY') return 'action-buy';
		if (action === 'SELL') return 'action-sell';
		return 'action-hold';
	}

	function getActionEmoji(action: string): string {
		if (action === 'BUY') return '📈';
		if (action === 'SELL') return '📉';
		return '➡️';
	}

	function getConfidenceClass(confidence: string): string {
		if (confidence === 'HIGH') return 'confidence-high';
		if (confidence === 'LOW') return 'confidence-low';
		return 'confidence-medium';
	}

	function getSignalTypeEmoji(type: string): string {
		switch (type) {
			case 'PRICE_MOVE': return '📊';
			case 'NEWS': return '📰';
			case 'FILING': return '📋';
			case 'VOLUME': return '📈';
			case 'WARNING': return '⚠️';
			default: return '•';
		}
	}

	function formatDate(dateStr: string): string {
		const date = new Date(dateStr);
		return date.toLocaleDateString('de-DE', {
			day: '2-digit',
			month: '2-digit',
			year: 'numeric'
		});
	}

	function formatTime(dateStr: string): string {
		const date = new Date(dateStr);
		return date.toLocaleTimeString('de-DE', {
			hour: '2-digit',
			minute: '2-digit'
		});
	}

	// Toggle quote expansion
	function toggleQuoteExpanded(signalId: number) {
		if (expandedQuotes.has(signalId)) {
			expandedQuotes.delete(signalId);
		} else {
			expandedQuotes.add(signalId);
		}
		expandedQuotes = new Set(expandedQuotes); // Trigger reactivity
	}

	// Check if quote should be truncated
	function shouldTruncateQuote(quote: string | null): boolean {
		return (quote?.length ?? 0) > QUOTE_TRUNCATE_LENGTH;
	}

	// Get displayed quote text (truncated or full)
	function getDisplayQuote(signal: Signal, isExpanded: boolean): string {
		const quote = signal.quote ?? '';
		if (!shouldTruncateQuote(quote) || isExpanded) {
			return quote;
		}
		return quote.substring(0, QUOTE_TRUNCATE_LENGTH) + '...';
	}

	// Render quote with highlighting (uses span_start/span_end)
	interface QuotePart {
		text: string;
		highlighted: boolean;
	}

	function getQuoteParts(signal: Signal, isExpanded: boolean): QuotePart[] {
		const quote = signal.quote ?? '';
		const displayQuote = getDisplayQuote(signal, isExpanded);

		// No span info → no highlighting
		if (signal.span_start === null || signal.span_end === null) {
			return [{ text: displayQuote, highlighted: false }];
		}

		const start = signal.span_start;
		const end = signal.span_end;

		// Span is outside the display range
		if (start >= displayQuote.length) {
			return [{ text: displayQuote, highlighted: false }];
		}

		const parts: QuotePart[] = [];

		// Part before highlight
		if (start > 0) {
			parts.push({ text: displayQuote.substring(0, start), highlighted: false });
		}

		// Highlighted part
		const highlightEnd = Math.min(end, displayQuote.length);
		parts.push({ text: displayQuote.substring(start, highlightEnd), highlighted: true });

		// Part after highlight
		if (highlightEnd < displayQuote.length) {
			parts.push({ text: displayQuote.substring(highlightEnd), highlighted: false });
		}

		return parts;
	}

	// Get top signals (positive impact, sorted by evidence_strength)
	function getTopReasons(signals: Signal[]): Signal[] {
		return signals
			.filter(s => (s.impact ?? 0) > 0 && s.signal_type !== 'WARNING')
			.sort((a, b) => (b.evidence_strength ?? 0) - (a.evidence_strength ?? 0))
			.slice(0, 3);
	}

	// Get risk signals (negative impact or warnings)
	function getRisks(signals: Signal[]): Signal[] {
		return signals
			.filter(s => (s.impact ?? 0) < 0 || s.signal_type === 'WARNING')
			.sort((a, b) => Math.abs(b.impact ?? 0) - Math.abs(a.impact ?? 0))
			.slice(0, 2);
	}

	// Simple ASCII chart for score history
	function renderMiniChart(history: ScoreHistoryItem[]): string {
		if (history.length === 0) return '';

		const scores = history.map(h => h.score).reverse();
		const min = Math.min(...scores, 30);
		const max = Math.max(...scores, 70);
		const range = max - min || 1;

		const height = 5;
		const lines: string[] = [];

		for (let row = height - 1; row >= 0; row--) {
			let line = '';
			for (const score of scores) {
				const normalized = (score - min) / range;
				const level = Math.floor(normalized * height);
				if (level >= row) {
					line += '█';
				} else {
					line += ' ';
				}
			}
			lines.push(line);
		}

		return lines.join('\n');
	}
</script>

<svelte:head>
	<title>{getSymbol()} - Finanzioso</title>
</svelte:head>

<div class="stock-detail container">
	<nav class="breadcrumb">
		<a href="/finanzioso">← Watchlist</a>
	</nav>

	{#if loading}
		<div class="loading-state">
			<p>Loading stock details...</p>
		</div>
	{:else if error}
		<div class="error-state">
			<p class="error-message">{error}</p>
			<a href="/finanzioso" class="btn btn-primary">Back to Watchlist</a>
		</div>
	{:else if data}
		<!-- Header with Symbol and Name -->
		<header class="stock-header">
			<div class="stock-identity">
				<h1>{data.stock.symbol}</h1>
				{#if data.stock.name}
					<p class="stock-name">{data.stock.name}</p>
				{/if}
				<span class="exchange-badge">{data.stock.exchange}</span>
			</div>

			{#if data.latest_price}
				<div class="price-info">
					<span class="price">${Number(data.latest_price.close).toFixed(2)}</span>
					<span class="change" class:positive={Number(data.latest_price.change_percent) > 0} class:negative={Number(data.latest_price.change_percent) < 0}>
						{Number(data.latest_price.change_percent) > 0 ? '+' : ''}{Number(data.latest_price.change_percent).toFixed(2)}%
					</span>
				</div>
			{/if}
		</header>

		<!-- Score Card -->
		{#if data.latest_score}
			<section class="score-card {getActionClass(data.latest_score.action)}">
				<div class="score-main">
					<span class="score-emoji">{getActionEmoji(data.latest_score.action)}</span>
					<div class="score-value">{Number(data.latest_score.score).toFixed(1)}</div>
					<span class="action-label">{data.latest_score.action}</span>
				</div>
				<div class="score-meta">
					<span class="confidence-badge {getConfidenceClass(data.latest_score.confidence)}">
						{data.latest_score.confidence} Confidence
					</span>
					{#if data.latest_score.safe_mode}
						<span class="safe-mode-badge">SAFE MODE</span>
					{/if}
					<span class="updated-at">
						Updated: {formatDate(data.latest_score.calculated_at)} {formatTime(data.latest_score.calculated_at)}
					</span>
				</div>
			</section>

			<!-- Safe Mode Explanation -->
			{#if data.latest_score.safe_mode && data.latest_score.safe_mode_reason}
				<section class="safe-mode-explanation">
					<h3>⚠️ Safe Mode Active</h3>
					<p>{data.latest_score.safe_mode_reason}</p>
				</section>
			{/if}
		{:else}
			<section class="no-score">
				<p>No score calculated yet. Score will be available after data collection.</p>
			</section>
		{/if}

		<!-- Score History Chart -->
		{#if data.score_history.length > 0}
			<section class="score-history">
				<h2>Score History</h2>
				<div class="chart-container">
					<pre class="mini-chart">{renderMiniChart(data.score_history)}</pre>
					<div class="chart-labels">
						<span>Older</span>
						<span>Recent</span>
					</div>
				</div>
				<div class="history-list">
					{#each data.score_history.slice(0, 5) as item}
						<div class="history-item">
							<span class="history-score {getActionClass(item.action)}">{Number(item.score).toFixed(1)}</span>
							<span class="history-action">{item.action}</span>
							<span class="history-date">{formatDate(item.calculated_at)}</span>
						</div>
					{/each}
				</div>
			</section>
		{/if}

		<!-- Top 3 Reasons -->
		{#if data.signals.length > 0}
			{@const topReasons = getTopReasons(data.signals)}
			{#if topReasons.length > 0}
				<section class="signals-section reasons">
					<h2>Top Reasons</h2>
					<div class="signals-list">
						{#each topReasons as signal}
							{@const isExpanded = expandedQuotes.has(signal.id)}
							{@const quoteParts = getQuoteParts(signal, isExpanded)}
							<div class="signal-card positive">
								<div class="signal-header">
									<span class="signal-type">{getSignalTypeEmoji(signal.signal_type)} {signal.signal_type}</span>
									<span class="signal-impact">+{Number(signal.impact ?? 0).toFixed(1)}</span>
								</div>
								{#if signal.quote}
									<div class="signal-quote-container">
										<p class="signal-quote">"<!--
											-->{#each quoteParts as part}{#if part.highlighted}<mark class="quote-highlight">{part.text}</mark>{:else}{part.text}{/if}{/each}<!--
											-->"</p>
										{#if shouldTruncateQuote(signal.quote)}
											<button class="expand-btn" onclick={() => toggleQuoteExpanded(signal.id)}>
												{isExpanded ? 'Weniger anzeigen' : 'Mehr anzeigen'}
											</button>
										{/if}
									</div>
								{/if}
								<div class="signal-footer">
									<span class="evidence-strength">
										{'★'.repeat(signal.evidence_strength ?? 0)}{'☆'.repeat(5 - (signal.evidence_strength ?? 0))}
									</span>
									{#if signal.source_url}
										<a href={signal.source_url} target="_blank" rel="noopener noreferrer" class="source-link">Source →</a>
									{/if}
								</div>
							</div>
						{/each}
					</div>
				</section>
			{/if}

			<!-- Top 2 Risks -->
			{@const risks = getRisks(data.signals)}
			{#if risks.length > 0}
				<section class="signals-section risks">
					<h2>Risks</h2>
					<div class="signals-list">
						{#each risks as signal}
							{@const isExpanded = expandedQuotes.has(signal.id)}
							{@const quoteParts = getQuoteParts(signal, isExpanded)}
							<div class="signal-card negative">
								<div class="signal-header">
									<span class="signal-type">{getSignalTypeEmoji(signal.signal_type)} {signal.signal_type}</span>
									<span class="signal-impact">{Number(signal.impact ?? 0).toFixed(1)}</span>
								</div>
								{#if signal.quote}
									<div class="signal-quote-container">
										<p class="signal-quote">"<!--
											-->{#each quoteParts as part}{#if part.highlighted}<mark class="quote-highlight">{part.text}</mark>{:else}{part.text}{/if}{/each}<!--
											-->"</p>
										{#if shouldTruncateQuote(signal.quote)}
											<button class="expand-btn" onclick={() => toggleQuoteExpanded(signal.id)}>
												{isExpanded ? 'Weniger anzeigen' : 'Mehr anzeigen'}
											</button>
										{/if}
									</div>
								{/if}
								<div class="signal-footer">
									<span class="evidence-strength">
										{'★'.repeat(signal.evidence_strength ?? 0)}{'☆'.repeat(5 - (signal.evidence_strength ?? 0))}
									</span>
									{#if signal.source_url}
										<a href={signal.source_url} target="_blank" rel="noopener noreferrer" class="source-link">Source →</a>
									{/if}
								</div>
							</div>
						{/each}
					</div>
				</section>
			{/if}
		{/if}

		<!-- Disclaimer -->
		<footer class="disclaimer">
			{data.disclaimer}
		</footer>
	{/if}
</div>

<style>
	.stock-detail {
		max-width: 800px;
	}

	.breadcrumb {
		margin-bottom: 1.5rem;
	}

	.breadcrumb a {
		color: var(--color-primary);
		text-decoration: none;
	}

	.breadcrumb a:hover {
		text-decoration: underline;
	}

	.loading-state, .error-state {
		text-align: center;
		padding: 3rem;
		background: var(--color-surface);
		border: 1px solid var(--color-border);
		border-radius: 8px;
	}

	.error-message {
		color: var(--color-error);
		margin-bottom: 1rem;
	}

	/* Header */
	.stock-header {
		display: flex;
		justify-content: space-between;
		align-items: flex-start;
		margin-bottom: 1.5rem;
		padding-bottom: 1rem;
		border-bottom: 1px solid var(--color-border);
	}

	.stock-identity h1 {
		font-size: 2rem;
		margin: 0;
	}

	.stock-name {
		color: var(--color-text-muted);
		margin: 0.25rem 0 0.5rem;
	}

	.exchange-badge {
		display: inline-block;
		background: var(--color-bg);
		padding: 0.125rem 0.5rem;
		border-radius: 4px;
		font-size: 0.75rem;
		color: var(--color-text-muted);
	}

	.price-info {
		text-align: right;
	}

	.price {
		display: block;
		font-size: 1.5rem;
		font-weight: 600;
		font-family: var(--font-mono);
	}

	.change {
		font-size: 0.875rem;
		font-family: var(--font-mono);
	}

	.change.positive { color: #16a34a; }
	.change.negative { color: #dc2626; }

	/* Score Card */
	.score-card {
		background: var(--color-surface);
		border: 2px solid;
		border-radius: 12px;
		padding: 1.5rem;
		margin-bottom: 1.5rem;
		text-align: center;
	}

	.score-card.action-buy { border-color: #16a34a; }
	.score-card.action-sell { border-color: #dc2626; }
	.score-card.action-hold { border-color: #6b7280; }

	.score-main {
		display: flex;
		align-items: center;
		justify-content: center;
		gap: 1rem;
		margin-bottom: 1rem;
	}

	.score-emoji {
		font-size: 2rem;
	}

	.score-value {
		font-size: 3rem;
		font-weight: 700;
		font-family: var(--font-mono);
	}

	.action-label {
		font-size: 1.25rem;
		font-weight: 600;
	}

	.score-meta {
		display: flex;
		justify-content: center;
		align-items: center;
		gap: 1rem;
		flex-wrap: wrap;
	}

	.confidence-badge {
		padding: 0.25rem 0.75rem;
		border-radius: 4px;
		font-size: 0.75rem;
		font-weight: 500;
	}

	.confidence-high { background: rgba(34, 197, 94, 0.15); color: #16a34a; }
	.confidence-medium { background: rgba(245, 158, 11, 0.15); color: #b45309; }
	.confidence-low { background: rgba(239, 68, 68, 0.15); color: #dc2626; }

	.safe-mode-badge {
		background: rgba(245, 158, 11, 0.2);
		color: #b45309;
		padding: 0.25rem 0.75rem;
		border-radius: 4px;
		font-size: 0.75rem;
		font-weight: 600;
	}

	.updated-at {
		color: var(--color-text-muted);
		font-size: 0.75rem;
	}

	/* Safe Mode Explanation */
	.safe-mode-explanation {
		background: rgba(245, 158, 11, 0.1);
		border: 1px solid rgba(245, 158, 11, 0.3);
		border-radius: 8px;
		padding: 1rem;
		margin-bottom: 1.5rem;
	}

	.safe-mode-explanation h3 {
		margin: 0 0 0.5rem;
		color: #b45309;
		font-size: 0.875rem;
	}

	.safe-mode-explanation p {
		margin: 0;
		color: var(--color-text-muted);
		font-size: 0.875rem;
	}

	.no-score {
		background: var(--color-surface);
		border: 1px solid var(--color-border);
		border-radius: 8px;
		padding: 2rem;
		text-align: center;
		color: var(--color-text-muted);
		margin-bottom: 1.5rem;
	}

	/* Score History */
	.score-history {
		background: var(--color-surface);
		border: 1px solid var(--color-border);
		border-radius: 8px;
		padding: 1rem;
		margin-bottom: 1.5rem;
	}

	.score-history h2 {
		font-size: 1rem;
		margin: 0 0 1rem;
	}

	.chart-container {
		margin-bottom: 1rem;
	}

	.mini-chart {
		font-family: var(--font-mono);
		font-size: 0.75rem;
		line-height: 1;
		color: var(--color-primary);
		margin: 0;
		padding: 0.5rem;
		background: var(--color-bg);
		border-radius: 4px;
		overflow-x: auto;
	}

	.chart-labels {
		display: flex;
		justify-content: space-between;
		font-size: 0.625rem;
		color: var(--color-text-muted);
		margin-top: 0.25rem;
	}

	.history-list {
		display: flex;
		flex-direction: column;
		gap: 0.5rem;
	}

	.history-item {
		display: flex;
		gap: 1rem;
		font-size: 0.875rem;
	}

	.history-score {
		font-family: var(--font-mono);
		font-weight: 600;
		min-width: 3rem;
	}

	.history-action {
		min-width: 3rem;
	}

	.history-date {
		color: var(--color-text-muted);
	}

	/* Signals */
	.signals-section {
		margin-bottom: 1.5rem;
	}

	.signals-section h2 {
		font-size: 1rem;
		margin: 0 0 1rem;
	}

	.signals-list {
		display: flex;
		flex-direction: column;
		gap: 0.75rem;
	}

	.signal-card {
		background: var(--color-surface);
		border: 1px solid var(--color-border);
		border-radius: 8px;
		padding: 1rem;
	}

	.signal-card.positive { border-left: 3px solid #16a34a; }
	.signal-card.negative { border-left: 3px solid #dc2626; }

	.signal-header {
		display: flex;
		justify-content: space-between;
		align-items: center;
		margin-bottom: 0.5rem;
	}

	.signal-type {
		font-weight: 600;
		font-size: 0.875rem;
	}

	.signal-impact {
		font-family: var(--font-mono);
		font-weight: 600;
	}

	.signal-card.positive .signal-impact { color: #16a34a; }
	.signal-card.negative .signal-impact { color: #dc2626; }

	.signal-quote-container {
		margin: 0.5rem 0;
	}

	.signal-quote {
		font-style: italic;
		color: var(--color-text-muted);
		margin: 0;
		font-size: 0.875rem;
		line-height: 1.4;
	}

	.quote-highlight {
		background: rgba(59, 130, 246, 0.2);
		color: var(--color-text);
		padding: 0.1rem 0.2rem;
		border-radius: 2px;
		font-style: italic;
	}

	.signal-card.positive .quote-highlight {
		background: rgba(34, 197, 94, 0.15);
	}

	.signal-card.negative .quote-highlight {
		background: rgba(239, 68, 68, 0.15);
	}

	.expand-btn {
		background: none;
		border: none;
		color: var(--color-primary);
		font-size: 0.75rem;
		cursor: pointer;
		padding: 0.25rem 0;
		margin-top: 0.25rem;
	}

	.expand-btn:hover {
		text-decoration: underline;
	}

	.signal-footer {
		display: flex;
		justify-content: space-between;
		align-items: center;
		margin-top: 0.5rem;
	}

	.evidence-strength {
		color: #b45309;
		font-size: 0.75rem;
	}

	.source-link {
		color: var(--color-primary);
		font-size: 0.75rem;
		text-decoration: none;
	}

	.source-link:hover {
		text-decoration: underline;
	}

	/* Disclaimer */
	.disclaimer {
		background: rgba(245, 158, 11, 0.1);
		border: 1px solid rgba(245, 158, 11, 0.3);
		color: #b45309;
		padding: 0.75rem 1rem;
		border-radius: 6px;
		font-size: 0.8rem;
		text-align: center;
		margin-top: 2rem;
	}
</style>

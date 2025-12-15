<script lang="ts">
	import { onMount, onDestroy } from 'svelte';

	interface ProcessStats {
		name: string;
		pid: number;
		cpu_percent: number;
		memory_mb: number;
		uptime_sec: number;
		status: string;
	}

	interface ProcessMonitorResponse {
		processes: ProcessStats[];
		timestamp_ms: number;
	}

	let processes = $state<ProcessStats[]>([]);
	let lastUpdate = $state<string>('');
	let isConnected = $state(false);
	let isExpanded = $state(true);
	let eventSource: EventSource | null = null;
	let reconnectAttempts = $state(0);
	const MAX_RECONNECT_ATTEMPTS = 5;
	const BASE_RECONNECT_DELAY_MS = 3000;

	function formatUptime(seconds: number): string {
		if (seconds === 0) return '--:--:--';
		const hours = Math.floor(seconds / 3600);
		const minutes = Math.floor((seconds % 3600) / 60);
		const secs = seconds % 60;
		return `${String(hours).padStart(2, '0')}:${String(minutes).padStart(2, '0')}:${String(secs).padStart(2, '0')}`;
	}

	function connectToStream() {
		try {
			// Connect to API server (port 3001) directly
			const apiUrl = typeof window !== 'undefined' && window.location.hostname
				? `http://${window.location.hostname}:3001/api/v1/processes/stream`
				: '/api/v1/processes/stream';
			eventSource = new EventSource(apiUrl);

			eventSource.onopen = () => {
				isConnected = true;
				reconnectAttempts = 0;
			};

			eventSource.onmessage = (event) => {
				try {
					const data: ProcessMonitorResponse = JSON.parse(event.data);
					processes = data.processes;
					const date = new Date(data.timestamp_ms);
					lastUpdate = date.toLocaleTimeString();
				} catch (err) {
					console.error('ProcessMonitor: Failed to parse message:', err);
				}
			};

			eventSource.onerror = () => {
				isConnected = false;
				eventSource?.close();
				eventSource = null;

				// Exponential backoff reconnect
				if (reconnectAttempts < MAX_RECONNECT_ATTEMPTS) {
					const delayMs = BASE_RECONNECT_DELAY_MS * Math.pow(2, reconnectAttempts);
					reconnectAttempts++;
					console.warn(
						`ProcessMonitor: Connection lost, reconnecting in ${delayMs}ms (attempt ${reconnectAttempts}/${MAX_RECONNECT_ATTEMPTS})`
					);
					setTimeout(connectToStream, delayMs);
				} else {
					console.error(
						`ProcessMonitor: Failed to reconnect after ${MAX_RECONNECT_ATTEMPTS} attempts`
					);
				}
			};
		} catch (err) {
			console.error('ProcessMonitor: Failed to create EventSource:', err);
		}
	}

	function getStatusColor(status: string): string {
		if (status === 'Running' || status === 'Sleep') return '#22c55e';
		if (status === 'Stopped') return '#ef4444';
		return '#f59e0b';
	}

	function getStatusBgColor(status: string): string {
		if (status === 'Running' || status === 'Sleep') return 'rgba(34, 197, 94, 0.1)';
		if (status === 'Stopped') return 'rgba(239, 68, 68, 0.1)';
		return 'rgba(245, 158, 11, 0.1)';
	}

	onMount(() => {
		connectToStream();
	});

	onDestroy(() => {
		if (eventSource) {
			eventSource.close();
			eventSource = null;
		}
	});
</script>

<div class="process-monitor" class:expanded={isExpanded}>
	<div class="monitor-header">
		<button
			class="expand-toggle"
			onclick={() => (isExpanded = !isExpanded)}
			title={isExpanded ? 'Collapse' : 'Expand'}
		>
			<span class="arrow">{isExpanded ? '▼' : '▶'}</span>
			<span class="title">MAINRAG Processes</span>
		</button>
		<div class="status-indicator" class:connected={isConnected} title={isConnected ? 'Connected' : 'Disconnected'}>
			<span class="dot"></span>
		</div>
	</div>

	{#if isExpanded}
		<div class="monitor-content">
			<div class="last-update">Updated: {lastUpdate || '--:--:--'}</div>

			<div class="processes-list">
				{#each processes as proc (proc.name)}
					<div class="process-item">
						<div class="process-name">
							<span class="name">{proc.name}</span>
							<span
								class="status-badge"
								style:background-color={getStatusBgColor(proc.status)}
								style:color={getStatusColor(proc.status)}
							>
								{proc.status}
							</span>
						</div>

						<div class="process-metrics">
							<div class="metric">
								<span class="label">CPU</span>
								<span class="value">{proc.cpu_percent.toFixed(1)}%</span>
							</div>
							<div class="metric">
								<span class="label">RAM</span>
								<span class="value">{proc.memory_mb.toFixed(0)} MB</span>
							</div>
							<div class="metric">
								<span class="label">Uptime</span>
								<span class="value">{formatUptime(proc.uptime_sec)}</span>
							</div>
						</div>

						<div class="process-pid">
							<span class="label">PID</span>
							<span class="value">{proc.pid || 'N/A'}</span>
						</div>
					</div>
				{/each}
			</div>

			{#if !isConnected}
				<div class="connection-status disconnected">
					<span class="icon">⚠️</span>
					<span>Connection lost - Reconnecting...</span>
				</div>
			{/if}
		</div>
	{/if}
</div>

<style>
	.process-monitor {
		position: fixed;
		right: 1rem;
		top: 5rem;
		width: 360px;
		background: var(--color-surface);
		border: 1px solid var(--color-border);
		border-radius: 8px;
		overflow: hidden;
		box-shadow: 0 4px 12px rgba(0, 0, 0, 0.15);
		font-size: 0.8rem;
		font-family: var(--font-mono);
		z-index: 100;
	}

	.process-monitor.expanded {
		max-height: 600px;
		overflow-y: auto;
	}

	.monitor-header {
		display: flex;
		align-items: center;
		justify-content: space-between;
		padding: 0.75rem 1rem;
		background: var(--color-bg-secondary);
		border-bottom: 1px solid var(--color-border);
		cursor: pointer;
		user-select: none;
	}

	.expand-toggle {
		display: flex;
		align-items: center;
		gap: 0.5rem;
		background: none;
		border: none;
		color: inherit;
		cursor: pointer;
		padding: 0;
		font-size: 0.8rem;
		flex: 1;
		text-align: left;
	}

	.expand-toggle:hover {
		opacity: 0.8;
	}

	.arrow {
		display: inline-block;
		width: 1rem;
		transition: transform 0.2s;
	}

	.title {
		font-weight: 600;
		color: var(--color-primary);
	}

	.status-indicator {
		display: flex;
		align-items: center;
		gap: 0.4rem;
		font-size: 0.65rem;
	}

	.status-indicator.connected .dot {
		background-color: #22c55e;
		box-shadow: 0 0 6px rgba(34, 197, 94, 0.6);
	}

	.status-indicator:not(.connected) .dot {
		background-color: #ef4444;
	}

	.dot {
		width: 0.5rem;
		height: 0.5rem;
		border-radius: 50%;
		display: inline-block;
	}

	.monitor-content {
		padding: 0.75rem;
		max-height: 550px;
		overflow-y: auto;
	}

	.last-update {
		text-align: center;
		color: var(--color-text-muted);
		font-size: 0.7rem;
		margin-bottom: 0.75rem;
		padding: 0.4rem;
		background: rgba(0, 0, 0, 0.05);
		border-radius: 4px;
	}

	.processes-list {
		display: flex;
		flex-direction: column;
		gap: 0.75rem;
	}

	.process-item {
		padding: 0.75rem;
		background: var(--color-bg);
		border: 1px solid var(--color-border);
		border-radius: 6px;
		display: flex;
		flex-direction: column;
		gap: 0.5rem;
	}

	.process-name {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 0.5rem;
	}

	.name {
		font-weight: 600;
		color: var(--color-text);
	}

	.status-badge {
		padding: 0.2rem 0.6rem;
		border-radius: 4px;
		font-size: 0.65rem;
		font-weight: 500;
	}

	.process-metrics {
		display: grid;
		grid-template-columns: 1fr 1fr 1fr;
		gap: 0.5rem;
	}

	.metric,
	.process-pid {
		display: flex;
		flex-direction: column;
		gap: 0.2rem;
	}

	.label {
		color: var(--color-text-muted);
		font-size: 0.65rem;
		text-transform: uppercase;
	}

	.value {
		color: var(--color-text);
		font-weight: 500;
	}

	.process-pid {
		margin-top: 0.25rem;
		padding-top: 0.5rem;
		border-top: 1px solid var(--color-border);
	}

	.connection-status {
		margin-top: 0.75rem;
		padding: 0.75rem;
		border-radius: 6px;
		display: flex;
		align-items: center;
		gap: 0.5rem;
		font-size: 0.75rem;
	}

	.connection-status.disconnected {
		background: rgba(239, 68, 68, 0.1);
		color: #ef4444;
		border: 1px solid rgba(239, 68, 68, 0.3);
	}

	.icon {
		display: inline-block;
		flex-shrink: 0;
	}

	/* Scrollbar styling */
	.monitor-content::-webkit-scrollbar {
		width: 4px;
	}

	.monitor-content::-webkit-scrollbar-track {
		background: transparent;
	}

	.monitor-content::-webkit-scrollbar-thumb {
		background: var(--color-border);
		border-radius: 4px;
	}

	.monitor-content::-webkit-scrollbar-thumb:hover {
		background: var(--color-text-muted);
	}
</style>

// MAINRAG API Client
// Base URL is configurable via environment variable

// SvelteKit requires PUBLIC_ prefix for client-side env vars
const API_BASE = import.meta.env.PUBLIC_MAINRAG_API_URL || 'http://localhost:3001';

export interface SearchRequest {
	query: string;
	source_ids?: number[];
	limit?: number;
	offset?: number;
}

export interface SearchResult {
	chunk_id: number;
	file_path: string;
	content: string;
	line_start: number;
	line_end: number;
	source_name: string;
	language: string | null;
	score: number;
}

export interface SearchResponse {
	results: SearchResult[];
	total: number;
	query: string;
	took_ms: number;
}

export interface Source {
	id: number;
	name: string;
	type: string;
	path: string;
	config: Record<string, unknown> | null;
	last_synced: string | null;
	file_count: number;
	total_size: number;
	created_at: string;
}

export interface SourcesResponse {
	sources: Source[];
	total: number;
}

export interface AdminSource {
	id: number;
	name: string;
	source_type: string;
	path: string;
	file_count: number;
	chunk_count: number;
	total_size: number;
	last_synced: string | null;
	created_at: string;
	updated_at: string;
}

export interface SystemStats {
	sources: number;
	files: number;
	chunks: number;
	total_size_bytes: number;
	postgres_size: string;
}

export interface HealthResponse {
	status: string;
	services: {
		postgres: boolean;
		qdrant: boolean;
		tei: boolean;
	};
}

class ApiError extends Error {
	constructor(
		public status: number,
		message: string
	) {
		super(message);
		this.name = 'ApiError';
	}
}

async function request<T>(
	endpoint: string,
	options: RequestInit = {}
): Promise<T> {
	const url = `${API_BASE}${endpoint}`;
	const headers: HeadersInit = {
		'Content-Type': 'application/json',
		...options.headers
	};

	const response = await fetch(url, {
		...options,
		headers
	});

	if (!response.ok) {
		const error = await response.json().catch(() => ({ error: 'Unknown error' }));
		throw new ApiError(response.status, error.error || response.statusText);
	}

	return response.json();
}

function authRequest<T>(
	endpoint: string,
	token: string,
	options: RequestInit = {}
): Promise<T> {
	return request<T>(endpoint, {
		...options,
		headers: {
			...options.headers,
			Authorization: `Bearer ${token}`
		}
	});
}

// Public API
export const api = {
	// Health
	health: () => request<HealthResponse>('/health'),

	// Search
	search: (req: SearchRequest) =>
		request<SearchResponse>('/api/v1/search', {
			method: 'POST',
			body: JSON.stringify(req)
		}),

	keywordSearch: (req: SearchRequest) =>
		request<SearchResponse>('/api/v1/search/keyword', {
			method: 'POST',
			body: JSON.stringify(req)
		}),

	// Sources (public, read-only)
	getSources: () => request<SourcesResponse>('/api/v1/sources'),

	getSource: (id: number) => request<Source>(`/api/v1/sources/${id}`)
};

// Admin API (requires JWT token)
export const adminApi = {
	// Stats
	getStats: (token: string) =>
		authRequest<SystemStats>('/api/v1/admin/stats', token),

	// Sources management
	getSources: (token: string) =>
		authRequest<AdminSource[]>('/api/v1/admin/sources', token),

	createSource: (
		token: string,
		data: { name: string; source_type: string; path: string }
	) =>
		authRequest<AdminSource>('/api/v1/admin/sources', token, {
			method: 'POST',
			body: JSON.stringify(data)
		}),

	updateSource: (token: string, id: number, data: { name?: string }) =>
		authRequest<AdminSource>(`/api/v1/admin/sources/${id}`, token, {
			method: 'PATCH',
			body: JSON.stringify(data)
		}),

	deleteSource: (token: string, id: number) =>
		authRequest<void>(`/api/v1/admin/sources/${id}`, token, {
			method: 'DELETE'
		}),

	syncSource: (token: string, id: number) =>
		authRequest<{ status: string; source_id: number; message: string }>(
			`/api/v1/admin/sources/${id}/sync`,
			token,
			{ method: 'POST' }
		)
};

export { ApiError };

import type { PageLoad } from './$types';

export interface WatchlistItem {
	stock: {
		id: number;
		symbol: string;
		name: string | null;
		exchange: string;
	};
	latest_score: {
		score: number;
		action: 'BUY' | 'HOLD' | 'SELL';
		confidence: 'HIGH' | 'MEDIUM' | 'LOW';
		safe_mode: boolean;
		calculated_at: string;
	} | null;
}

export interface WatchlistData {
	items: WatchlistItem[];
	count: number;
	disclaimer: string;
	error: string | null;
}

export const load: PageLoad = async ({ fetch }): Promise<WatchlistData> => {
	try {
		// Get token from localStorage (client-side only)
		let token: string | null = null;
		if (typeof window !== 'undefined') {
			const stored = localStorage.getItem('mainrag_auth');
			if (stored) {
				const auth = JSON.parse(stored);
				token = auth.token;
			}
		}

		if (!token) {
			return {
				items: [],
				count: 0,
				disclaimer: 'This is NOT investment advice. Scores are algorithmic calculations for educational/research purposes only.',
				error: 'Please log in to view your watchlist'
			};
		}

		const response = await fetch('/api/v1/finanzioso/watchlist', {
			headers: {
				'Authorization': `Bearer ${token}`
			}
		});

		if (!response.ok) {
			if (response.status === 401) {
				return {
					items: [],
					count: 0,
					disclaimer: 'This is NOT investment advice.',
					error: 'Session expired. Please log in again.'
				};
			}
			throw new Error('Failed to load watchlist');
		}

		return await response.json();
	} catch (err) {
		return {
			items: [],
			count: 0,
			disclaimer: 'This is NOT investment advice.',
			error: err instanceof Error ? err.message : 'Failed to load watchlist'
		};
	}
};

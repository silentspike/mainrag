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
	// TODO: Connect to real API in A4
	// For now, return mock data for UI development

	try {
		// Uncomment when API is ready:
		// const response = await fetch('/finanzioso/watchlist');
		// if (!response.ok) throw new Error('Failed to load watchlist');
		// return await response.json();

		// Mock data for UI shell
		return {
			items: [],
			count: 0,
			disclaimer: 'This is NOT investment advice. Scores are algorithmic calculations for educational/research purposes only.',
			error: null
		};
	} catch (err) {
		return {
			items: [],
			count: 0,
			disclaimer: 'This is NOT investment advice.',
			error: err instanceof Error ? err.message : 'Failed to load watchlist'
		};
	}
};

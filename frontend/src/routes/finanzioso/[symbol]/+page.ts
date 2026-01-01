import type { PageLoad } from './$types';

export interface Signal {
	id: number;
	signal_type: string;
	quote: string | null;
	span_start: number | null;
	span_end: number | null;
	impact: number | null;
	evidence_strength: number | null;
	source_url: string | null;
	created_at: string;
}

export interface Score {
	id: number;
	score: number;
	action: 'BUY' | 'HOLD' | 'SELL';
	confidence: 'HIGH' | 'MEDIUM' | 'LOW';
	safe_mode: boolean;
	safe_mode_reason: string | null;
	calculated_at: string;
}

export interface PriceData {
	date: string;
	close: number;
	change_percent: number;
}

export interface ScoreHistoryItem {
	score: number;
	action: string;
	calculated_at: string;
}

export interface Stock {
	id: number;
	symbol: string;
	name: string | null;
	exchange: string;
}

export interface StockDetailData {
	stock: Stock;
	latest_score: Score | null;
	signals: Signal[];
	latest_price: PriceData | null;
	score_history: ScoreHistoryItem[];
	disclaimer: string;
	error: string | null;
}

export const load: PageLoad = async ({ params, fetch }): Promise<StockDetailData> => {
	const symbol = params.symbol.toUpperCase();

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
				stock: { id: 0, symbol, name: null, exchange: '' },
				latest_score: null,
				signals: [],
				latest_price: null,
				score_history: [],
				disclaimer: 'This is NOT investment advice.',
				error: 'Please log in to view stock details'
			};
		}

		const response = await fetch(`/api/v1/finanzioso/stock/${symbol}`, {
			headers: {
				'Authorization': `Bearer ${token}`
			}
		});

		if (!response.ok) {
			if (response.status === 401) {
				return {
					stock: { id: 0, symbol, name: null, exchange: '' },
					latest_score: null,
					signals: [],
					latest_price: null,
					score_history: [],
					disclaimer: 'This is NOT investment advice.',
					error: 'Session expired. Please log in again.'
				};
			}
			if (response.status === 404) {
				return {
					stock: { id: 0, symbol, name: null, exchange: '' },
					latest_score: null,
					signals: [],
					latest_price: null,
					score_history: [],
					disclaimer: 'This is NOT investment advice.',
					error: `Stock ${symbol} not found in your watchlist`
				};
			}
			throw new Error('Failed to load stock details');
		}

		const data = await response.json();
		return {
			...data,
			error: null
		};
	} catch (err) {
		return {
			stock: { id: 0, symbol, name: null, exchange: '' },
			latest_score: null,
			signals: [],
			latest_price: null,
			score_history: [],
			disclaimer: 'This is NOT investment advice.',
			error: err instanceof Error ? err.message : 'Failed to load stock details'
		};
	}
};

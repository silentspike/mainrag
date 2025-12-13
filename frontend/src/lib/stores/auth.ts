import { writable, derived } from 'svelte/store';
import { browser } from '$app/environment';

interface AuthState {
	token: string | null;
	isAdmin: boolean;
	email: string | null;
}

const initialState: AuthState = {
	token: null,
	isAdmin: false,
	email: null
};

function createAuthStore() {
	// Load from localStorage if in browser
	const stored = browser ? localStorage.getItem('mainrag_auth') : null;
	const initial = stored ? JSON.parse(stored) : initialState;

	const { subscribe, set, update } = writable<AuthState>(initial);

	return {
		subscribe,
		login: (token: string) => {
			// Decode JWT payload (base64)
			try {
				const payload = JSON.parse(atob(token.split('.')[1]));
				const state: AuthState = {
					token,
					isAdmin: payload.is_admin || false,
					email: payload.email || null
				};
				set(state);
				if (browser) {
					localStorage.setItem('mainrag_auth', JSON.stringify(state));
				}
			} catch {
				console.error('Invalid token format');
			}
		},
		logout: () => {
			set(initialState);
			if (browser) {
				localStorage.removeItem('mainrag_auth');
			}
		}
	};
}

export const auth = createAuthStore();
export const isAuthenticated = derived(auth, ($auth) => !!$auth.token);
export const isAdmin = derived(auth, ($auth) => $auth.isAdmin);

import type { Handle } from '@sveltejs/kit';

export const handle: Handle = async ({ event, resolve }) => {
	const response = await resolve(event);

	// Aggressive no-cache headers for HTML pages
	if (response.headers.get('content-type')?.includes('text/html')) {
		response.headers.set('Cache-Control', 'no-store, no-cache, must-revalidate, proxy-revalidate');
		response.headers.set('Pragma', 'no-cache');
		response.headers.set('Expires', '0');
	}

	// Also prevent caching of JS files during development
	if (event.url.pathname.includes('/_app/')) {
		response.headers.set('Cache-Control', 'no-store, max-age=0');
	}

	return response;
};

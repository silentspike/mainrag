// k6 FTS Baseline Load Test
// Usage: k6 run scripts/k6/fts_baseline.js
// Env vars: API_URL, API_TOKEN
//
// Measures search latency under 20 concurrent virtual users for 60 seconds.

import http from 'k6/http';
import { check, sleep } from 'k6';
import { Trend, Counter } from 'k6/metrics';

const API_URL = __ENV.API_URL || 'http://localhost:3001';
const API_TOKEN = __ENV.API_TOKEN || '';

if (!API_TOKEN) {
    console.error('API_TOKEN env var required. Get one via: curl -X POST .../api/v1/auth/login');
}

// Custom metrics
const searchDuration = new Trend('search_duration_ms', true);
const searchErrors = new Counter('search_errors');

// Test queries covering different patterns
const queries = [
    'function',
    'error handling',
    'database connection',
    'authentication',
    'search',
    'parse',
    'config',
    'async',
    'test',
    'impl',
];

export const options = {
    scenarios: {
        steady_state: {
            executor: 'constant-vus',
            vus: 20,
            duration: '60s',
        },
    },
    thresholds: {
        'http_req_duration{name:search}': ['p(50)<5000', 'p(95)<10000'],
        'search_errors': ['count<10'],
    },
};

export default function () {
    const query = queries[Math.floor(Math.random() * queries.length)];

    const payload = JSON.stringify({
        query: query,
        limit: 20,
    });

    const params = {
        headers: {
            'Authorization': `Bearer ${API_TOKEN}`,
            'Content-Type': 'application/json',
        },
        tags: { name: 'search' },
    };

    const start = Date.now();
    const res = http.post(`${API_URL}/api/v1/search`, payload, params);
    const duration = Date.now() - start;

    searchDuration.add(duration);

    const ok = check(res, {
        '200 OK': (r) => r.status === 200,
        'has results': (r) => {
            try {
                const body = JSON.parse(r.body);
                return body.results && body.results.length > 0;
            } catch (e) {
                return false;
            }
        },
    });

    if (!ok) {
        searchErrors.add(1);
        if (res.status !== 200) {
            console.error(`Search failed: ${res.status} ${res.body}`);
        }
    }

    sleep(0.5); // Small pause between requests
}

export function handleSummary(data) {
    // Output JSON summary for comparison
    const summary = {
        timestamp: new Date().toISOString(),
        scenario: 'steady_state_20vus_60s',
        metrics: {
            search_duration_ms: {
                p50: data.metrics.search_duration_ms?.values?.['p(50)'] || 0,
                p95: data.metrics.search_duration_ms?.values?.['p(95)'] || 0,
                p99: data.metrics.search_duration_ms?.values?.['p(99)'] || 0,
                avg: data.metrics.search_duration_ms?.values?.avg || 0,
                min: data.metrics.search_duration_ms?.values?.min || 0,
                max: data.metrics.search_duration_ms?.values?.max || 0,
            },
            http_req_duration: {
                p50: data.metrics.http_req_duration?.values?.['p(50)'] || 0,
                p95: data.metrics.http_req_duration?.values?.['p(95)'] || 0,
                p99: data.metrics.http_req_duration?.values?.['p(99)'] || 0,
            },
            errors: data.metrics.search_errors?.values?.count || 0,
            total_requests: data.metrics.http_reqs?.values?.count || 0,
        },
    };

    return {
        stdout: JSON.stringify(summary, null, 2) + '\n',
        'docs/fts_baseline_results.json': JSON.stringify(summary, null, 2),
    };
}

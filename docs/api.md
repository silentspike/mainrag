# HTTP API

> Last verified: 2026-04-24 via commit `2d597cb`

MainRag exposes an axum-based HTTP API on port `3001` (configurable via
`MAINRAG_API_PORT`). Endpoints are grouped by authentication requirement and
rate-limit policy.

- **Base URL:** `http://localhost:3001`
- **Content-Type:** `application/json` for request/response bodies
- **Auth:** API-Key (`X-API-Key` header) for agents, JWT (`Authorization: Bearer …`) for admin

## Authentication model

Two independent credential types, both terminated in middleware before the
handler layer:

| Credential       | Header                        | Use case                      |
| ---------------- | ----------------------------- | ----------------------------- |
| API-Key          | `X-API-Key: mrk_…`            | CLI, MCP, programmatic agents |
| JWT (HS256)      | `Authorization: Bearer eyJ…`  | Admin UI, interactive users   |

API-Key prefix `mrk_` is client-visible; the server stores only a
pepper-hashed digest (`API_KEY_PEPPER` + `argon2`), so a DB dump does not
leak usable keys.

JWTs carry `sub`, `role`, `exp`, `jti`. A short-TTL `revoked_tokens` cache
blocks `jti` reuse after logout. Dual-key rotation (`JWT_SECRET` +
`JWT_SECRET_PREVIOUS`) allows zero-downtime secret changes.

## Public endpoints (no auth)

| Method | Path       | Purpose                                        |
| ------ | ---------- | ---------------------------------------------- |
| GET    | `/healthz` | Liveness probe, always `200 OK` if process up  |
| GET    | `/readyz`  | Readiness probe (same as healthz currently)    |
| GET    | `/metrics` | Prometheus scrape endpoint                     |
| POST   | `/api/v1/auth/login`        | JWT issuance (rate-limited 10/min per IP) |
| POST   | `/api/v1/auth/refresh`      | Rotate access token                       |

## Authenticated endpoints

Require either `X-API-Key` or `Authorization: Bearer …`. Body size limit:
1 MB. No rate limit (search is the hot path).

### Retrieval

| Method | Path                      | Purpose                                   |
| ------ | ------------------------- | ----------------------------------------- |
| POST   | `/api/v1/search`          | Hybrid search (FTS + vector + rerank)     |
| POST   | `/api/v1/search/keyword`  | FTS-only (debug / low-latency path)       |
| GET    | `/api/v1/health`          | Detailed health (DB, Qdrant, TEI)         |

**`POST /api/v1/search`** request:

```json
{
  "query": "how does hybrid_search work",
  "limit": 10,
  "source_id": null,
  "ef_search": 64,
  "rerank_top_n": 64
}
```

Response (abbreviated):

```json
{
  "results": [
    {
      "chunk_id": 8421,
      "source_name": "mainrag",
      "file_path": "api/src/services/search.rs",
      "line_range": "412-458",
      "score": 0.89,
      "text": "pub async fn hybrid_search(…) { … }",
      "parent_context": "impl SearchService",
      "llm_guide": "Entry point for the hybrid retrieval pipeline. Runs FTS + Qdrant in parallel, merges via RRF, reranks with cross-encoder."
    }
  ],
  "timings_ms": {
    "embed": 8,
    "fts": 21,
    "qdrant": 18,
    "fusion": 2,
    "rerank": 45,
    "total": 132
  }
}
```

### Code intelligence

| Method | Path                                                       | Purpose                                            |
| ------ | ---------------------------------------------------------- | -------------------------------------------------- |
| GET    | `/api/v1/intelligence/symbols`                             | Search symbols by name/kind/language               |
| GET    | `/api/v1/intelligence/symbols/:id/callgraph`               | Call-graph neighborhood for a symbol               |
| GET    | `/api/v1/intelligence/files/:file_id/symbols`              | List all symbols in a file                         |
| GET    | `/api/v1/intelligence/callers?name=foo`                    | Who calls this function                            |
| GET    | `/api/v1/intelligence/callees?name=foo`                    | What this function calls                           |
| GET    | `/api/v1/intelligence/call-chain?from=a&to=b&depth=4`      | N-hop path from `a` to `b` (BFS, capped at `depth`) |
| GET    | `/api/v1/intelligence/cards`                               | Browse symbol cards                                |
| GET    | `/api/v1/intelligence/cards/:id`                           | Single symbol card (signature + doc + refs)        |
| POST   | `/api/v1/intelligence/explain_path`                        | Explain why a path exists between two symbols      |
| POST   | `/api/v1/intelligence/negative_evidence`                   | Record "X does not do Y" annotations               |
| GET    | `/api/v1/intelligence/ownership`                           | Symbol ownership / heat map                        |
| POST   | `/api/v1/intelligence/explore`                             | Guided exploration from a seed                     |

See [`intelligence.md`](intelligence.md) for semantics and query patterns.

### MCP (Model Context Protocol)

| Method | Path                  | Purpose                             |
| ------ | --------------------- | ----------------------------------- |
| GET    | `/api/v1/mcp/tools`   | List MCP tools exposed to clients   |
| POST   | `/api/v1/mcp/call`    | Invoke an MCP tool by name          |

### Streaming (SSE)

SSE endpoints bypass the 30-second request timeout (streams must stay open):

| Method | Path                                         | Purpose                             |
| ------ | -------------------------------------------- | ----------------------------------- |
| GET    | `/api/v1/admin/processes/stream`             | Realtime process stats (admin)      |

## Admin endpoints (auth + admin role)

Body limit: 10 MB. Timeouts: 10 min for sync/backfill I/O paths.

| Method | Path                                                             | Purpose                                |
| ------ | ---------------------------------------------------------------- | -------------------------------------- |
| POST   | `/api/v1/admin/sources/:id/sync`                                 | Trigger full re-index of a source      |
| POST   | `/api/v1/admin/sources/:id/sync-files`                           | Incremental sync for listed files      |
| POST   | `/api/v1/admin/backfill/orphaned`                                | Repair chunks without embeddings       |
| POST   | `/api/v1/admin/backfill/qdrant-user-ids`                         | Fix RLS payload drift                  |

## Errors

Errors use RFC 7807-style JSON problem responses:

```json
{
  "error": "unauthorized",
  "message": "JWT signature invalid",
  "request_id": "01J…"
}
```

Common codes: `400 invalid_request`, `401 unauthorized`, `403 forbidden`,
`404 not_found`, `413 payload_too_large`, `429 rate_limited`,
`503 service_unavailable`.

## Security headers

All responses include (Sprint 6.2):

- `X-Content-Type-Options: nosniff`
- `X-Frame-Options: DENY`
- `Referrer-Policy: no-referrer`
- `Cache-Control: no-store` (L1: prevents auth-token caching)

CORS: allowed origins are explicit via `CORS_ORIGINS`; misconfigured entries
fail fast at startup rather than silently disabling cross-origin.

# Architecture

> Last verified: 2026-04-24 via commit `2d597cb`

> **Architecture status:** This document describes the current chunk-based
> PostgreSQL/Qdrant system. [Storage v2](storage-v2.md) is a planned additive
> architecture and is not active. Its contracts must not be read as current
> runtime, migration, deployment, or release evidence.

MainRag is a hybrid retrieval system optimized for source code, technical
documentation, and long-context conversations. This document describes the
system components, their responsibilities, and the data/request flow through
the retrieval pipeline.

The target storage-v2 identity, generation, reconstruction, retrieval,
compatibility, and activation contracts are specified separately in
[`storage-v2.md`](storage-v2.md). Until its controlled activation is accepted,
the tables and flows below remain the supported architecture.

## High-level components

```
                   ┌──────────────────────────────────────┐
  User / Agent ──▶ │  mainrag CLI  ·  MCP server  ·  curl │
                   └────────────────┬─────────────────────┘
                                    │ HTTPS / JSON
                   ┌────────────────▼─────────────────────┐
                   │            axum API (:3001)          │
                   │  JWT + API-Key auth · rate-limit     │
                   │  CORS · security headers · tracing   │
                   └──┬──────────────┬──────────────┬─────┘
                      │              │              │
           ┌──────────▼───┐    ┌─────▼─────┐   ┌────▼────────┐
           │   PostgreSQL │    │  Qdrant   │   │ TEI (GPU)   │
           │   18 · RLS   │    │HNSW + INT8│   │ GTE embed   │
           │   FTS · PG-  │    │   :6333   │   │  :8091      │
           │   vector     │    │           │   │ GTE rerank  │
           │   symbols    │    │ 859k vec  │   │  :8082      │
           │   callgraph  │    │           │   └─────────────┘
           └──────────────┘    └───────────┘
```

Services in the current deployment:

- **`mainrag-api`** (systemd) — axum server, Rust binary (`api/` crate).
- **`mainrag-tei-gte`** (Docker, port 8091) — Hugging Face TEI runtime serving `Alibaba-NLP/gte-modernbert-base` embeddings.
- **`mainrag-tei-reranker`** (Docker, port 8082) — TEI runtime serving `BAAI/bge-reranker-base` cross-encoder.
- **`qdrant-mainrag`** (Docker, port 6333/6334) — vector store, single collection `mainrag_chunks_gte` with HNSW + INT8 scalar quantization.
- **PostgreSQL 18** (native) — primary store for chunks, metadata, symbols, call-graph edges, users, rate-limit buckets.

Operational details (systemd vs Docker, health probes, env files) live in
[`operations.md`](operations.md).

## Data model

### PostgreSQL

| Table              | Role                                           |
| ------------------ | ---------------------------------------------- |
| `sources`          | One row per indexed source (git/fs/web/export) |
| `files`            | One row per file within a source               |
| `chunks`           | Immutable text chunks with `tsvector_simple` + `tsvector_english` FTS columns |
| `chunk_embeddings` | `pgvector` 768-d vector per chunk (GTE) — tracked for incremental reprocessing; live retrieval uses Qdrant |
| `chunk_embeddings_bge_backup` | Historical BGE embeddings, retained as rollback |
| `symbols`          | Extracted symbols (functions, methods, types, modules) with start/end line + language |
| `call_graph`       | Directed edges `caller_symbol_id → callee_symbol_id` |
| `symbol_cards`     | Pre-computed "what is this symbol" cards with signature, doc, references |
| `users`, `api_keys`, `revoked_tokens`, `rate_limit_buckets` | Auth/security |

Row-Level-Security (RLS) is enabled on content tables; the API sets
`request.user_id` per request so Postgres enforces tenant isolation in the
database layer rather than in application code.

### Qdrant

Single collection `mainrag_chunks_gte`:

- **Vector dim:** 768
- **Distance:** Cosine
- **Index:** HNSW (`m=16`, `ef_construct=200`), `ef_search` tunable per request (default 64)
- **Quantization:** Scalar INT8, always keep original in RAM disabled (on-disk originals, INT8 in RAM) — fits the ~860k-vector corpus on a 4 GB GPU + 16 GB RAM host
- **Payload:** `chunk_id`, `source_id`, `file_id`, `user_id` (for RLS mirror), timestamps

## Indexing pipeline

```
source ──▶ plugin (fs|git|web|export|pdf)
          │
          ▼
      parser (tree-sitter for code, lang-aware for text)
          │
          ▼
      chunker
        ├─ semantic (code: function/class granularity)
        ├─ token (fixed-size with overlap, fallback)
        └─ jsonl (conversation transcripts: Claude/Codex/Gemini formats)
          │
          ▼
      enrichment
        ├─ symbol extraction + call-graph edges
        └─ domain profile (source-specific boost tokens)
          │
          ▼
      embed (TEI GTE, batched 32)  ──▶  Qdrant upsert
          │
          └─ chunks + FTS tsvectors ──▶  PostgreSQL
```

Large conversation files (Claude/Codex JSONL) stream through the pipeline
rather than being loaded whole, keeping peak memory under ~500 MB for
multi-GB transcripts. Watch-mode reruns only the affected chunks on file
changes using debounced file-system events.

## Retrieval pipeline

A `POST /api/v1/search` request flows through six phases. Per-phase timings
are logged in structured JSON under `phase=<n> duration_ms=<x>` so
operational tuning is grounded in data, not speculation.

```
                ┌──────────────────────────────────────────────┐
Phase 1 Embed   │  query ──▶ TEI GTE (dim=768, truncated 8192) │
                └──────────────┬───────────────────────────────┘
                               │
                ┌──────────────▼───────────────────────────────┐
Phase 2 Hybrid  │  parallel:                                   │
                │    FTS  (UNION ALL simple+english, ts_rank_cd)│
                │    Qdrant (HNSW, ef_search tunable)          │
                └──────────────┬───────────────────────────────┘
                               │
                ┌──────────────▼───────────────────────────────┐
Phase 3 Fusion  │  RRF merge                                   │
                │  + call-graph popularity boost               │
                │  + symbol-expansion scoring                  │
                │  + parent-context lookup                     │
                └──────────────┬───────────────────────────────┘
                               │
                ┌──────────────▼───────────────────────────────┐
Phase 4 Filter  │  RLS + source scoping + dedup (by chunk_id)  │
                └──────────────┬───────────────────────────────┘
                               │
                ┌──────────────▼───────────────────────────────┐
Phase 5 Rerank  │  TEI cross-encoder on top-N                  │
                │  (default N=64, configurable)                │
                └──────────────┬───────────────────────────────┘
                               │
                ┌──────────────▼───────────────────────────────┐
Phase 6 Format  │  assemble response:                          │
                │    chunk text · file path · line range       │
                │    parent_context · llm_guide · scores       │
                └──────────────────────────────────────────────┘
```

### Why `UNION ALL` over the default tsvector config

The `simple` configuration preserves identifiers like `hybrid_search` and
`CursorTrackCursorClipProxy` without stemming; the `english` configuration
handles natural-language queries. Running both in a UNION ALL and letting
RRF merge the two lists recovers identifier hits that `english` stems away
(`delegation` → `deleg`) while still answering "how to delete a clip from
arranger" correctly. This is the single change that moved recall from 50 %
to 70 % GOOD on the reference set, ahead of the embedder upgrade.

### Ranking signals

| Signal             | Source                          | Weight (default) |
| ------------------ | ------------------------------- | ---------------- |
| FTS rank (simple)  | `ts_rank_cd` on `tsvector_simple`  | RRF k=60      |
| FTS rank (english) | `ts_rank_cd` on `tsvector_english` | RRF k=60      |
| Vector similarity  | Qdrant cosine                   | RRF k=60         |
| Call-graph popularity | In-edges on caller's symbol  | log-damped bonus |
| Symbol-name match  | Exact/prefix match on extracted symbol | additive      |
| Cross-encoder score | BGE-reranker-base               | final sort       |

Weights are configurable via `api/config/search.toml`.

## Operational constraints

- **Single-GPU homelab:** total VRAM ~4 GB (RTX 3050 Ti). Embedder uses
  ~55 % budget, reranker ~25 %, remaining 20 % reserved for concurrent
  requests.
- **Memory-ordered corpus:** 859k chunks @ 768 d INT8 ≈ 660 MB in Qdrant
  RAM, fits comfortably in 16 GB system RAM alongside Postgres page cache.
- **Graceful credential rotation:** `JWT_SECRET_PREVIOUS` and
  `API_KEY_PEPPER_PREVIOUS` allow zero-downtime key rotation — tokens
  signed with the previous secret remain valid until expiry.

## Trade-offs and known limits

- **Qdrant auth: server-side `api_key` is enabled** in
  `docker-compose.yml` (`QDRANT__SERVICE__API_KEY`). Anonymous requests
  to `/collections` return HTTP 401. The client-side `QDRANT_API_KEY`
  in `mainrag.env` must match the server-side value. See `operations.md`
  for env-var details and `SECURITY.md` for the threat model.
- **Polyglot decision:** all runtime code is Rust. TypeScript and Python
  are explicitly not part of the runtime path; they are reserved for
  offline evaluation scripts (`scripts/`) and potential MCP sidecars.
- **Recall ceiling:** 70 % GOOD on the reference set is measured, not
  modeled. Queries requiring code-execution semantics (e.g. "what does
  this class do at runtime") are the dominant failure mode; Intelligence
  Layer path-explanation partially closes this gap (see `intelligence.md`).

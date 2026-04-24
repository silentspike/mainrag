# MainRag

> Hybrid retrieval & context system for code understanding.
> PostgreSQL FTS + Qdrant (HNSW + INT8) + GTE-ModernBERT embeddings + cross-encoder reranking + code intelligence (symbols, call-graph, N-hop traversal).

[![License: Apache-2.0](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](LICENSE)
[![Recall@10: 70%](https://img.shields.io/badge/Recall%4010-70%25-brightgreen)](docs/search-baseline-gte-modernbert.md)
[![p50 Latency: 132ms](https://img.shields.io/badge/p50-132ms-blue)](data/benchmarks/)
[![Rust: 2021](https://img.shields.io/badge/rust-2021-orange)](Cargo.toml)

MainRag is a self-hosted retrieval backend that turns a heterogeneous corpus
(source code, Markdown docs, PDFs, web crawls, chat transcripts) into a
queryable knowledge base. It is built for LLM agents and human developers
who need *grounded, citable, low-latency* answers over large private codebases
(~860k chunks tested) without sending data to a third party.

- **Embedding model:** `Alibaba-NLP/gte-modernbert-base` (768d, 8192-token context)
- **Reranker:** `BAAI/bge-reranker-base` (cross-encoder)
- **Vector store:** Qdrant 1.16 with HNSW + Scalar Quantization (INT8)
- **Lexical index:** PostgreSQL 18 FTS (GIN, `UNION ALL simple+english`)
- **Intelligence layer:** Tree-sitter symbol extraction, call-graph edges, N-hop BFS traversal

> Last verified: 2026-04-24 via commit `2d597cb`

## Why MainRag

Pure vector search overfits to paraphrase. Pure keyword search misses
synonyms. Most hybrid stacks stop at RRF. MainRag adds:

1. **Multi-signal ranking:** RRF (BM25 + vector) + call-graph popularity +
   symbol-expansion (identifier tokenization) + parent-context boosting.
2. **UNION ALL FTS:** the `simple` and `english` tsvector configurations run
   in parallel; identifier substrings (`hybrid_search`) and natural-language
   queries (`how to delete a clip`) both hit.
3. **Cross-encoder rerank:** top-N from hybrid fusion is re-scored by a
   ModernBERT cross-encoder before being returned.
4. **Code intelligence, not just text:** tree-sitter parses 25+ languages
   into symbols, edges are stored as a proper graph, and N-hop call chains
   are reachable via a single API call.

## Performance

Measured on a single workstation (AMD Ryzen 9 5900HS, RTX 3050 Ti 4 GB, 16 GB RAM),
corpus size 859k chunks, 10 canonical queries × 3 repetitions (n=30, wall-clock
including CLI startup overhead ~30–50 ms).

| Metric        | Value  |
| ------------- | ------ |
| **p50**       | 132 ms |
| **p95**       | 187 ms |
| **p99**       | 208 ms |
| mean ± stdev  | 131 ± 36 ms |
| min / max     | 68 / 213 ms |

Evidence: [`data/benchmarks/search_latency_20260424T140514Z.json`](data/benchmarks/search_latency_20260424T140514Z.json),
script: [`scripts/benchmark-search.py`](scripts/benchmark-search.py).

### Quality baseline

Relevance is tracked through a 10-query reference set, manually rated GOOD /
PARTIAL / WEAK by inspecting the top-5 results per query.

| Model                          | GOOD | PARTIAL | WEAK |
| ------------------------------ | ---- | ------- | ---- |
| `BAAI/bge-base-en-v1.5`        | 50 % | 20 %    | 30 % |
| `Alibaba-NLP/gte-modernbert-base` | **70 %** (+20 pp) | 20 % | 10 % |

Evidence: [`docs/search-baseline-bge-base.md`](docs/search-baseline-bge-base.md),
[`docs/search-baseline-gte-modernbert.md`](docs/search-baseline-gte-modernbert.md).

## Architecture at a glance

```
                      ┌────────────────────────────┐
                      │    mainrag CLI / MCP       │
                      └─────────────┬──────────────┘
                                    │ HTTP / JSON
                      ┌─────────────▼──────────────┐
                      │    axum API  (port 3001)   │
                      │  auth · rate-limit · CORS  │
                      └──┬──────────────────────┬──┘
                         │                      │
     ┌───────────────────┼──────────────────────┼───────────────────┐
     │                   │                      │                   │
┌────▼────┐        ┌─────▼─────┐         ┌──────▼──────┐      ┌─────▼─────┐
│PostgreSQL│        │  Qdrant   │         │  TEI GTE   │      │ TEI GTE   │
│FTS + RLS │        │HNSW + INT8│         │  Embedder  │      │ Reranker  │
│ symbols  │        │ 860k vec  │         │  :8091     │      │  :8082    │
│callgraph │        │           │         │            │      │           │
└──────────┘        └───────────┘         └────────────┘      └───────────┘
```

Full diagram and data-flow: [`docs/architecture.md`](docs/architecture.md).

## Quickstart

> Requires: Docker + nvidia-container-toolkit, PostgreSQL 18, Rust 1.75+.

```bash
# 1. Build workspace
cargo build --release --workspace

# 2. Start embedder + reranker + Qdrant
docker compose up -d

# 3. Apply schema
psql "$DATABASE_URL" -f schema_intelligence.sql

# 4. Run the API
./target/release/mainrag-api

# 5. From another shell: index a source, then search
./target/release/mainrag source add ./path/to/code --name my-repo
./target/release/mainrag search "how does hybrid_search work"
```

See [`docs/operations.md`](docs/operations.md) for deployment, service
topology, model requirements (~600 MB for GTE embedder + reranker), and
`mainrag.env` reference.

## Features

- **Hybrid retrieval** — BM25 ⊕ vector ⊕ cross-encoder rerank. See [`docs/architecture.md`](docs/architecture.md).
- **Code intelligence** — symbol extraction (25+ languages via tree-sitter), call-graph with N-hop BFS. See [`docs/intelligence.md`](docs/intelligence.md).
- **HTTP API + MCP server** — axum on `:3001`, MCP tools for Claude/agents. See [`docs/api.md`](docs/api.md).
- **Watch mode** — incremental re-indexing on file changes, PDF/export/git/web plugins.
- **Security** — Row-Level-Security on PostgreSQL, dual-key JWT rotation, rate limiting, pepper-hashed API keys, request-size limits, security headers.

## Repository layout

```
.
├── api/            Rust axum server + retrieval pipeline + intelligence
├── cli/            Rust CLI (mainrag binary)
├── docs/           Public docs (architecture, api, operations, intelligence) + baselines
├── ops/            systemd units, migration/backup infrastructure
├── scripts/        Python utilities (benchmark, migration, enrichment)
├── data/           Benchmark artifacts (gitignored except JSON results)
├── docker-compose.yml
├── schema_intelligence.sql
└── Cargo.toml      workspace root
```

## Documentation

| Doc | Scope |
| --- | --- |
| [`docs/architecture.md`](docs/architecture.md) | System components, data flow, ranking pipeline |
| [`docs/api.md`](docs/api.md) | HTTP endpoints, auth, request/response shapes |
| [`docs/operations.md`](docs/operations.md) | Deployment, services, env vars, health checks |
| [`docs/intelligence.md`](docs/intelligence.md) | Call-graph, N-hop traversal, symbol cards |
| [`docs/search-baseline-gte-modernbert.md`](docs/search-baseline-gte-modernbert.md) | Current relevance evidence (10 queries) |
| [`docs/search-baseline-bge-base.md`](docs/search-baseline-bge-base.md) | Prior BGE baseline (historical) |

## Status

This is an early public preview (`v0.1.0-alpha.1`). The system runs
production traffic on a single node but public-facing APIs, CI, and the
plugin interface are not yet stabilized. Expect breaking changes.

## License

Licensed under the Apache License, Version 2.0. See [`LICENSE`](LICENSE).

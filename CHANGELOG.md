# Changelog

All notable changes to MainRag are tracked here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed

- Removed the legacy root `ARCHITECTURE.md`. The single source of truth
  for system architecture is `docs/architecture.md`; the root file was
  an older draft that overclaimed unimplemented techniques and is no
  longer reconcilable with the running code.

### Known limits

- **Not for production multi-tenant use.** MainRag v0.1.0-alpha is a
  single-tenant developer preview. The transactional outbox + the
  `DEFAULT_USER_ID` rework are scoped for v0.2 (multi-tenant beta);
  tracked in [#10](https://github.com/silentspike/mainrag/issues/10).

## [0.1.0-alpha.1] — 2026-04-24

First public preview of MainRag as a standalone Apache-2.0 project at
`github.com/silentspike/mainrag`.

### Added

- **Retrieval pipeline** — hybrid search that fuses PostgreSQL FTS
  (GIN, `simple` + `english` tsvector via UNION ALL), Qdrant vector
  search (HNSW + INT8 scalar quantization), and a cross-encoder reranker.
  Multi-signal ranking combines RRF, call-graph popularity, symbol
  expansion, and parent-context lookup.
- **Embedding stack** — Alibaba-NLP/gte-modernbert-base (768d, 8192-token
  context) served via Hugging Face TEI at `:8091`. Reranker is
  BAAI/bge-reranker-base at `:8082`.
- **Intelligence layer** — tree-sitter symbol extraction across 25+
  languages, call-graph edges, N-hop BFS traversal capped at depth 10.
  HTTP surface: `/api/v1/intelligence/{symbols,call-chain,cards,explain_path,
  explore,ownership,negative_evidence}`.
- **HTTP API** — axum on `:3001` with JWT + API-Key auth, Row-Level-
  Security in Postgres, rate limiting on auth routes, security headers,
  request-size limits, CORS.
- **CLI** — `mainrag` binary with `search`, `symbols`, `callgraph`,
  `card`, `explain`, `explore`, `layers`, `ownership`, `dead-end`
  subcommands.
- **Watch mode** — incremental re-indexing on file changes (notify +
  notify-debouncer-mini), plugins for fs/git/web/pdf/export sources,
  streaming processing for multi-GB conversation transcripts.
- **docker-compose.yml** as SSOT for TEI + Qdrant with server-side API-key
  auth on Qdrant, nvidia-container-toolkit GPU passthrough.
- **systemd units** for `mainrag-api`, `mainrag-svelte`, backup + Qdrant
  snapshot timers.
- **Public docs** — `docs/architecture.md`, `docs/api.md`,
  `docs/operations.md`, `docs/intelligence.md`, each with a
  `Last verified` header pointing at a specific commit.
- **Evidence artefacts** — `docs/search-baseline-bge-base.md` and
  `docs/search-baseline-gte-modernbert.md` track the 10-query reference
  set that showed +20 pp GOOD rate from the BGE → GTE migration.
  `data/benchmarks/search_latency_20260424T140514Z.json` records
  p50 = 132 ms, p95 = 187 ms, p99 = 208 ms on the 859k-chunk corpus.
- **CI** — `ci.yml` (fmt + clippy + check + doc-link sanity),
  `codeql.yml` (rust + actions), `nightly.yml` (test + doc + audit),
  `pr-lint.yml` (Conventional Commits), `auto-label.yml`
  (`.github/labeler.yml` path mapping), `release.yml`
  (tag-triggered binary release), `dependabot.yml` (weekly cargo +
  github-actions updates, grouped).

### Changed

- Workspace layout: previously two loose crates (`cli/` + `api/`), now a
  proper Cargo workspace with shared `[workspace.package]` metadata.
- License: MIT → Apache-2.0.
- `mainrag.env` is no longer tracked; deploy via `/etc/mainrag/mainrag.env`
  (systemd `EnvironmentFile`) with `mainrag.env.example` as template.
- `credentials.json` is gone; `credentials.example.json` ships as a
  placeholder-filled template.

### Security

- Full git history was rewritten with `git-filter-repo` to scrub:
  tracked secrets (DB passwords, JWT secrets, pepper, Qdrant API key,
  admin JWT), legacy homelab hostnames and IPs, author-email identifiers,
  ~1 GB of `ops/migration/export/` blobs, and ~40 obsolete planning
  documents. `.git` shrunk from 816 MB to 940 KB. Both `gitleaks` and
  `trufflehog` report zero findings on the rewritten history.
- Credential rotation before the history rewrite: DB, Qdrant, JWT, and
  API-key-pepper all reissued with fresh random values, previous values
  preserved in `_PREVIOUS` slots for graceful rolling.
- Qdrant server-side `api_key` auth enabled in `docker-compose.yml`;
  anonymous access now returns HTTP 401.

### Known limits

- Several `services::chunker::semantic::tests::*` require a tokenizer
  model file at `/data/models/bge-base-en-v1.5/tokenizer.json` and are
  skipped on CI runners. Tracked as a follow-up.
- `cargo audit` reports `fxhash` and `paste` as unmaintained advisories.
  Neither is a security vulnerability; migration to `rustc-hash` is a
  follow-up.
- CodeQL on Rust needs GitHub Advanced Security for private repos; the
  job is marked `continue-on-error` so the signal surfaces without
  gating merges.

[Unreleased]: https://github.com/silentspike/mainrag/compare/v0.1.0-alpha.1...HEAD
[0.1.0-alpha.1]: https://github.com/silentspike/mainrag/releases/tag/v0.1.0-alpha.1

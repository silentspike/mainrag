# Operations

> Last verified: 2026-04-24 via commit `2d597cb`

This document is the deployment and day-2 reference for MainRag: service
topology, env vars, model requirements, health checks, backups, and
credential rotation.

## Service topology

MainRag is a split deployment: GPU-bound services run in Docker (so the
nvidia-container-toolkit can do the passthrough cleanly), CPU-bound
services run under systemd (for reboot-resilience and log integration).

| Layer          | Runtime  | Port(s)       | Service unit / container           |
| -------------- | -------- | ------------- | ---------------------------------- |
| API            | systemd  | 3001          | `mainrag-api.service`              |
| Watchers       | systemd  | —             | `mainrag-watcher*.service`         |
| SvelteKit UI   | systemd  | 5173 (dev)    | `mainrag-svelte.service`           |
| Embedder (GTE-ModernBERT)      | Docker | 8091 | `mainrag-tei-gte`           |
| Reranker (BGE-reranker-base)   | Docker | 8082 | `mainrag-tei-reranker`      |
| Qdrant         | Docker   | 6333 (HTTP) / 6334 (gRPC) | `qdrant-mainrag`        |
| PostgreSQL 18  | native   | 5432          | system package                     |

> docker-compose.yml is the SSOT for Docker services. systemd units are the
> SSOT for native services. The legacy `mainrag-tei.service` (BGE era) is
> kept on disk but `disabled` — it will be removed once downstream
> consumers confirm they only use the GTE endpoint.

## Model requirements

The retrieval stack pulls two model directories on first start.
Pre-stage them via the TEI container's HuggingFace cache to avoid the
Hugging Face download at deploy time:

```
/data/models/models--Alibaba-NLP--gte-modernbert-base/   # embedder, ~310 MB
/data/models/models--BAAI--bge-reranker-base/            # reranker, ~280 MB
```

Both trees are mounted into the TEI containers as read-only volumes (see
`docker-compose.yml`). Checksums (`SHA256`) of `tokenizer.json` are pinned
via `TOKENIZER_ASSET_SHA256` in `mainrag.env` so drift is noticed on start.

## Environment configuration

The API reads `/etc/mainrag/mainrag.env` (systemd `EnvironmentFile=`). An
example file with all keys and redacted secrets ships as `mainrag.env.example`
(generated in Phase 14 of the public-readiness sprint).

| Variable                        | Purpose                                   |
| ------------------------------- | ----------------------------------------- |
| `DATABASE_URL`                  | `postgresql://user:pw@host:5432/db`       |
| `QDRANT_URL` / `QDRANT_REST_URL` / `QDRANT_API_KEY` | Qdrant endpoint + client key |
| `QDRANT_CHUNK_COLLECTION`       | Default `mainrag_chunks_gte`              |
| `QDRANT_EF_SEARCH`              | HNSW ef_search (default 64)               |
| `TEI_URL` / `TEI_REST_URL`      | Embedder REST endpoint (default `http://localhost:8091`) |
| `TEI_MODEL`                     | `Alibaba-NLP/gte-modernbert-base`         |
| `TEI_RERANKER_MODEL`            | `BAAI/bge-reranker-base` (docker-compose default) |
| `TEI_RERANKER_URL`              | Reranker REST endpoint (default `http://localhost:8082`) |
| `TOKENIZER_VERSION`             | `hf_gte_modernbert`                       |
| `TOKENIZER_ASSET_PATH`          | `/data/models/gte-modernbert-base/tokenizer.json` |
| `TOKENIZER_ASSET_SHA256`        | Checksum of pinned tokenizer              |
| `EMBEDDING_BATCH_SIZE`          | TEI batch size (default 32)               |
| `EMBEDDING_WITH_CCH`            | Enable call-chain handle in payload       |
| `JWT_SECRET` / `JWT_SECRET_PREVIOUS`   | HS256 signing key + graceful predecessor |
| `API_KEY_PEPPER` / `API_KEY_PEPPER_PREVIOUS` | Argon2 pepper for API keys        |
| `CORS_ORIGINS`                  | Comma-separated allowed origins           |
| `RUST_LOG`                      | Tracing level, e.g. `info,mainrag_api=debug` |

## Health checks

```bash
curl -sf http://localhost:3001/healthz        # API process
curl -sf http://localhost:8091/health         # TEI embedder
curl -sf http://localhost:8082/health         # TEI reranker
curl -sf http://localhost:6333/readyz         # Qdrant
psql "$DATABASE_URL" -c 'SELECT 1'             # Postgres
```

`/api/v1/health` (auth required) returns a structured report including
version, DB connection pool stats, and last indexing timestamp.

## Deployment workflow

```bash
# 1. System prerequisites (one-time)
sudo apt install postgresql-18 postgresql-18-pgvector
docker compose --version
nvidia-smi                          # must report the GPU

# 2. Clone + build
git clone https://github.com/silentspike/mainrag
cd mainrag
cargo build --release --workspace    # or: cargo remote -c -- build --release

# 3. Pre-stage models
sudo mkdir -p /data/models
HF_HOME=/data/models hf_hub download Alibaba-NLP/gte-modernbert-base
HF_HOME=/data/models hf_hub download BAAI/bge-reranker-base

# 4. Start GPU services
docker compose up -d

# 5. Schema
psql "$DATABASE_URL" -f schema_intelligence.sql

# 6. Install systemd units
sudo cp ops/systemd/mainrag-api.service /etc/systemd/system/
sudo cp mainrag.env.example /etc/mainrag/mainrag.env
sudo chmod 600 /etc/mainrag/mainrag.env
# edit secrets, then:
sudo systemctl daemon-reload
sudo systemctl enable --now mainrag-api.service
```

## Backup strategy

Two systemd timers ship in `ops/systemd/`:

- **`mainrag-backup.timer`** — daily `pg_dump` of the MainRag database,
  rotated 14-day retention. Writes to `/var/backups/mainrag/pg/`.
- **`mainrag-qdrant-snapshot.timer`** — daily Qdrant collection snapshot
  via REST API, retained 7 days. Writes to `/var/backups/mainrag/qdrant/`.

Restore procedure is documented inline in the units (`ExecStartPre`
comments include the recovery commands).

## Credential rotation

All secrets support graceful rotation:

- `JWT_SECRET` → new; `JWT_SECRET_PREVIOUS` → the outgoing value. Tokens
  signed with either remain valid until expiry (max TTL = access-token
  lifetime).
- `API_KEY_PEPPER` → new; `API_KEY_PEPPER_PREVIOUS` → the outgoing value.
  The verify path tries current, then previous.

```bash
# 1. Generate new secrets
NEW_JWT=$(openssl rand -base64 48)
NEW_PEPPER=$(openssl rand -hex 20)

# 2. Shift the existing current into PREVIOUS, write the new as current
sudo editor /etc/mainrag/mainrag.env

# 3. Reload the API (no downtime — new process picks up env on next start)
sudo systemctl restart mainrag-api

# 4. After max TTL has elapsed, clear *_PREVIOUS
```

Postgres and Qdrant credentials are rotated the same day with
`ALTER USER … WITH PASSWORD '…'` and a Qdrant restart with the new
`QDRANT__SERVICE__API_KEY`.

## Known operational limits

- **Single node, single GPU.** MainRag has no clustering story. Scale by
  running independent instances behind a tenant-aware load balancer.
- **Qdrant server-side auth is enabled in the shipped compose file.**
  `QDRANT__SERVICE__API_KEY` is set from `mainrag.env`'s `QDRANT_API_KEY`,
  and the client uses the same key. Anonymous requests return HTTP 401.
  Qdrant should still not be exposed beyond `localhost`/the private VPN
  in alpha — there is no rate-limit per remote IP on Qdrant itself.
- **Disk-bound vector payloads.** The collection is configured with
  originals on disk. First-query latency after restart is higher until
  HNSW traversal warms the page cache (~30 s for the 860k-chunk corpus).

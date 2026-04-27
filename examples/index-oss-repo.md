# Example: index a small open-source repository

## Use case

Take a public repo, point MainRag at it, and have it queryable in
under 5 minutes — without sending a single byte to a third-party.

`tokio-rs/axum` is a good first target: ~25k lines of idiomatic Rust,
rich call-graph topology, public so anyone can reproduce.

## Setup

```bash
# 0. Bring up backing services
docker compose up -d
psql "$DATABASE_URL" -f schema_intelligence.sql

# 1. Build the workspace
cargo build --release --workspace

# 2. Start the API
./target/release/mainrag-api &

# 3. Clone the demo repo
git clone --depth 1 https://github.com/tokio-rs/axum.git /tmp/axum

# 4. Authenticate (admin) — exchange username/password for a JWT
TOKEN=$(curl -sf -X POST http://localhost:3001/api/v1/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username":"admin","password":"<your-admin-password>"}' \
  | jq -r .token)
```

## Commands

Register the corpus through the admin source endpoint:

```bash
SRC_ID=$(curl -sf -X POST http://localhost:3001/api/v1/admin/sources \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"name":"axum","source_type":"fs","path":"/tmp/axum"}' \
  | jq -r .id)
```

Trigger the initial sync (tree-sitter parse + embed + Qdrant upsert):

```bash
curl -sf -X POST "http://localhost:3001/api/v1/admin/sources/$SRC_ID/sync" \
  -H "Authorization: Bearer $TOKEN" | jq '.status'
```

Inspect progress and final stats:

```bash
mainrag source list | grep -A2 axum
curl -sf "http://localhost:3001/api/v1/admin/sources/$SRC_ID/stats" \
  -H "Authorization: Bearer $TOKEN" | jq
```

## Expected output

The `sync` endpoint streams progress and finishes with a summary:

```
Source registered: axum (id=42)
Discovered 612 files (5,128 chunks)
Embedded 5,128 chunks (GTE-ModernBERT, batch=64) in 38s
Upserted 5,128 vectors to Qdrant
Indexed call-graph: 4,217 edges
```

Sanity-check that retrieval works (CLI hits the same backend):

```bash
mainrag search "where does the router match a path" --limit 5
```

You should see a top-5 list with `axum/src/routing/`, `axum-extra/`,
and related paths, each with `file:line` anchors.

## Cleanup

```bash
curl -sf -X DELETE "http://localhost:3001/api/v1/admin/sources/$SRC_ID" \
  -H "Authorization: Bearer $TOKEN"
rm -rf /tmp/axum
```

## See also

- [`docs/demo-mcp-codex.md`](../docs/demo-mcp-codex.md) — the same
  flow continued into an MCP/Codex grounded patch loop.
- [`mcp-tool-call.md`](mcp-tool-call.md) — call MCP tools against
  the indexed corpus.

# MainRag MCP for Codex (and other coding agents) — 3-Minute Demo

This document walks through connecting MainRag to a coding agent
(Codex CLI, Claude Code, Cursor, ...) over the Model Context Protocol
and using it to ground a code-modification task on a private codebase.

![mainrag MCP demo: docker compose · 13 tools · cited search](images/mcp-codex-demo.gif)

> The cast above is reproducible from
> [`docs/images/mcp-codex-demo.tape`](images/mcp-codex-demo.tape):
> `vhs docs/images/mcp-codex-demo.tape` regenerates the GIF and an
> MP4 next to it. It records against the live API on `localhost:3001`
> with the JWT in `~/.config/mainrag/token`.

## 1. Setup (≈ 60 seconds)

Bring up the embedder, reranker, Qdrant, and PostgreSQL services and
index a small open-source repo. The example uses
[`tokio-rs/axum`](https://github.com/tokio-rs/axum) because it is small
(~25k LoC), idiomatic Rust, and has rich call-graph topology.

```bash
# Clone MainRag and start backing services
git clone https://github.com/silentspike/mainrag.git
cd mainrag
cp mainrag.env.example mainrag.env   # fill in DATABASE_URL, JWT_SECRET, API_KEY_PEPPER, QDRANT_API_KEY
docker compose up -d
psql "$DATABASE_URL" -f schema_intelligence.sql
cargo build --release --workspace
./target/release/mainrag-api &

# Authenticate as admin and capture a JWT
TOKEN=$(curl -sf -X POST http://localhost:3001/api/v1/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username":"admin","password":"<your-admin-password>"}' \
  | jq -r .token)

# Index axum as a corpus through the admin source endpoint
git clone --depth 1 https://github.com/tokio-rs/axum.git /tmp/axum
SRC_ID=$(curl -sf -X POST http://localhost:3001/api/v1/admin/sources \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"name":"axum","source_type":"fs","path":"/tmp/axum"}' | jq -r .id)
curl -sf -X POST "http://localhost:3001/api/v1/admin/sources/$SRC_ID/sync" \
  -H "Authorization: Bearer $TOKEN" | jq '.status'
```

After indexing finishes, sanity check that retrieval works:

```bash
mainrag search "where does the router match a path" --limit 5
```

## 2. Connect the agent over MCP

MainRag exposes its MCP surface on the same axum server as the HTTP
API:

| Endpoint                              | Purpose                          |
| ------------------------------------- | -------------------------------- |
| `GET  /api/v1/mcp/tools`              | List available tools (JSON)      |
| `POST /api/v1/mcp/tools/execute`      | Execute a tool (`tool_name` + `params`) |
| `GET  /api/v1/mcp/protocol`           | Protocol metadata (server name, version) |

Authentication is the same as for the rest of the API: a JWT or a
peppered API key in the `Authorization: Bearer ...` header. See
[`docs/api.md`](api.md) for details.

### Codex CLI (`~/.codex/config.toml`)

```toml
[[mcp_servers]]
name = "mainrag"
transport = "http"
url = "http://localhost:3001/api/v1/mcp"
headers = { Authorization = "Bearer ${MAINRAG_API_KEY}" }
```

### Claude Code (`~/.config/claude-code/mcp.json`)

```json
{
  "mcpServers": {
    "mainrag": {
      "transport": "http",
      "url": "http://localhost:3001/api/v1/mcp",
      "headers": {
        "Authorization": "Bearer ${MAINRAG_API_KEY}"
      }
    }
  }
}
```

After restarting the agent, all 13 MainRag tools (`search_code`,
`search_symbols`, `find_callers`, `find_callees`, `get_symbol_card`,
`get_symbol_callgraph`, `explain_path`, `browse_layers`, `explore`,
`get_ownership`, `report_dead_end`, `list_sources`,
`get_source_stats`) are available to the agent.

## 3. Three example calls

The same calls work via the agent's tool-use mechanism or directly via
HTTP. The HTTP form is shown here so the contract is unambiguous.

### a) `search_code` — hybrid retrieval with citations

```bash
curl -s -X POST http://localhost:3001/api/v1/mcp/tools/execute \
  -H "Authorization: Bearer $MAINRAG_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "tool_name": "search_code",
    "params": {
      "query": "how does the Router match a path against extractors",
      "source_name": "axum",
      "limit": 5
    }
  }'
```

The response is a `SearchResult[]` with `chunk_id`, `file_path`,
`line_start`/`line_end`, `score_components` (BM25, vector, rerank,
call-graph popularity), and `parent_context`. The agent uses
`file_path:line_start` to cite sources in its answer.

### b) `find_callers` — call-graph navigation

```bash
curl -s -X POST http://localhost:3001/api/v1/mcp/tools/execute \
  -H "Authorization: Bearer $MAINRAG_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "tool_name": "find_callers",
    "params": { "function_name": "Router::route", "max_hops": 2 }
  }'
```

Returns the BFS-expanded set of functions that transitively reach
`Router::route` within 2 hops, with edge metadata. Useful for impact
analysis before touching a function the agent does not yet understand.

### c) `get_symbol_card` — focused context bundle

```bash
curl -s -X POST http://localhost:3001/api/v1/mcp/tools/execute \
  -H "Authorization: Bearer $MAINRAG_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "tool_name": "get_symbol_card",
    "params": { "name": "MethodFilter" }
  }'
```

Returns the symbol's definition, doc-comment, call-sites, and direct
neighbours in the call-graph. This is the cheapest way to feed an agent
"everything it needs to know about one symbol" without a full
file-load.

## 4. Codex patch workflow

A typical agent loop on a private codebase:

1. **Plan**: agent receives a user request ("add a metrics middleware to
   the axum router").
2. **Ground**: agent calls `search_code` and `find_callers` against
   MainRag to identify the existing extractor / layer plumbing. Cited
   chunks are added to the agent's working context with `file:line`
   anchors.
3. **Reason**: agent constructs a patch using only the cited material.
4. **Verify**: before applying, agent calls `find_callees` on the new
   middleware function to ensure no name-collision.
5. **Apply**: agent runs the patch through Codex's normal `apply_patch`
   tool. The MainRag citations end up in the PR description for human
   review.

Without MainRag, step 2 either degrades to "open the files Codex already
has in its context" (poor coverage on large repos) or to a third-party
RAG service (private code leaves the network boundary). MainRag closes
that gap on-prem.

## 5. Out-of-the-box test

To prove the wiring on a fresh checkout:

```bash
# Returns 200 + a JSON list of 13 tool descriptors
curl -sf http://localhost:3001/api/v1/mcp/tools \
  -H "Authorization: Bearer $MAINRAG_API_KEY" | jq '.tools | length'
# Expected output: 13

# Smoke-test a search
curl -sf -X POST http://localhost:3001/api/v1/mcp/tools/execute \
  -H "Authorization: Bearer $MAINRAG_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"tool_name":"list_sources","params":{}}' | jq '.result | length'
# Expected output: ≥ 1 (axum was indexed in step 1)
```

If both calls return as expected, the MCP surface is reachable and the
agent will be able to use it. If you get `401`, re-check
`Authorization` and the JWT/api-key value in `mainrag.env`.

## See also

- [`../README.md#in-a-codex-rollout`](../README.md#in-a-codex-rollout)
  — the customer-scenario framing that introduces this demo
- [`api.md`](api.md) — full HTTP API reference (auth, rate limits,
  error shapes)
- [`architecture.md`](architecture.md) — retrieval pipeline and ranking
  signals
- [`intelligence.md`](intelligence.md) — call-graph schema, N-hop BFS
  traversal, symbol cards

# Example: call MCP tools from the command line

## Use case

You have a corpus indexed (see [`index-oss-repo.md`](index-oss-repo.md))
and want to call MainRag's MCP tools directly — no agent in the
loop yet, just `curl` against the API. This is the cheapest way to
verify that the MCP surface works end-to-end.

## Setup

```bash
# Make sure the API is running and reachable
curl -sf http://localhost:3001/healthz

# Set the API key once (any pepper-hashed key will do; see docs/api.md)
export MAINRAG_API_KEY="$(grep '^MAINRAG_API_KEY=' mainrag.env | cut -d= -f2)"
```

## Commands

### List the 13 tools

```bash
curl -sf -H "Authorization: Bearer $MAINRAG_API_KEY" \
  http://localhost:3001/api/v1/mcp/tools | jq '.tools | length'
```

### `search_code` — hybrid retrieval with citations

```bash
curl -sf -X POST -H "Authorization: Bearer $MAINRAG_API_KEY" \
  -H "Content-Type: application/json" \
  http://localhost:3001/api/v1/mcp/tools/execute \
  -d '{
    "tool_name": "search_code",
    "params": { "query": "how does Router match a path", "source_name": "axum", "limit": 5 }
  }' | jq '.result.results[0]'
```

### `find_callers` — call-graph navigation (2 hops)

```bash
curl -sf -X POST -H "Authorization: Bearer $MAINRAG_API_KEY" \
  -H "Content-Type: application/json" \
  http://localhost:3001/api/v1/mcp/tools/execute \
  -d '{
    "tool_name": "find_callers",
    "params": { "function_name": "Router::route", "max_hops": 2 }
  }' | jq '.result.callers | length'
```

### `get_symbol_card` — focused context bundle

```bash
curl -sf -X POST -H "Authorization: Bearer $MAINRAG_API_KEY" \
  -H "Content-Type: application/json" \
  http://localhost:3001/api/v1/mcp/tools/execute \
  -d '{ "tool_name": "get_symbol_card", "params": { "name": "MethodFilter" } }' | jq '.result'
```

## Expected output

- Tool list returns the integer **`13`**.
- `search_code` returns a `SearchResult[]` with `chunk_id`,
  `file_path`, `line_start`/`line_end`, score components, and
  `parent_context`.
- `find_callers` returns a non-zero count for any function that has
  callers in the indexed corpus.
- `get_symbol_card` returns definition + doc-comment + call-sites.

If any call returns `401`, re-check `Authorization` and the JWT/api-key
value in `mainrag.env`.

## Cleanup

No state to clean up; tool calls are read-only.

## See also

- [`docs/api.md`](../docs/api.md) — full HTTP API reference (auth,
  rate limits, error shapes).
- [`agent-with-context.md`](agent-with-context.md) — same tools
  driven by a coding agent end-to-end.

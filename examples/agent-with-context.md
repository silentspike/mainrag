# Example: ground a coding agent in MainRag context

## Use case

A coding agent (Codex CLI, Claude Code, Cursor, ...) on its own
sees only the open files in your editor. For non-trivial changes
on a private codebase, that is not enough. This example shows how
to wire MainRag's MCP server into the agent so it can ground its
reasoning in cited results from the actual corpus.

## Setup

Prerequisites: the API and the corpus from
[`index-oss-repo.md`](index-oss-repo.md) are running and reachable
on `http://localhost:3001`.

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
      "headers": { "Authorization": "Bearer ${MAINRAG_API_KEY}" }
    }
  }
}
```

Restart the agent. All 13 MainRag tools become available alongside
its built-in tools.

## Commands

### Ask the agent a grounded question

In the agent shell:

> "Add a metrics middleware to the axum router that records request
>  duration. Use MainRag to find the existing extractor / layer
>  plumbing before writing the patch."

The agent should:

1. Call `search_code` for "axum middleware layer extractor".
2. Call `find_callers` on `Router::route` and `Router::layer` to
   understand impact.
3. Construct the patch using only the cited material; cite
   `file:line` anchors in the answer.
4. (Optional) Call `find_callees` on the new middleware to make
   sure no name collides.

### Inspect what the agent retrieved

```bash
curl -sf -H "Authorization: Bearer $MAINRAG_API_KEY" \
  http://localhost:3001/api/v1/admin/queries/recent | jq '.[0]'
```

The latest entry shows the query, ranking signals, and which
chunks were returned to the agent.

## Expected output

A patch that:

- Cites real `axum` files by `path:line`.
- Compiles without modifying functions outside the cited range.
- Has a PR description containing the cited anchors.

If the agent hallucinates a function name not present in the corpus,
that is the failure mode MainRag closes — re-check that the
`mainrag` MCP server appears in the agent's tool list.

## Cleanup

```bash
# Roll back any uncommitted patch
git -C /tmp/axum restore .
```

## See also

- [`docs/demo-mcp-codex.md`](../docs/demo-mcp-codex.md) — the
  reference walkthrough with screenshots and the full ground-and-
  patch loop.
- [`mcp-tool-call.md`](mcp-tool-call.md) — the same MCP surface,
  driven directly by `curl` for debugging.

# MainRag MCP demo cast — render with: `vhs docs/images/mcp-codex-demo.tape`
#
# Renders to docs/images/mcp-codex-demo.{gif,mp4}.
# Drives the live API on http://localhost:3001 and the docker services
# named in mainrag.env. Run from the repo root with the API service
# already up and a JWT in ~/.config/mainrag/token.

Output docs/images/mcp-codex-demo.gif
Output docs/images/mcp-codex-demo.mp4

Set FontSize 16
Set Width 1280
Set Height 720
Set Theme "TokyoNight"
Set TypingSpeed 40ms
Set PlaybackSpeed 1.0

# Pull the JWT from the standard CLI location, off-screen
Hide
Type "TOKEN=$(cat ~/.config/mainrag/token)"
Enter
Type "clear"
Enter
Show

# 1) Backing services — TEI embedder + Qdrant + TEI reranker
Type "docker ps --filter name=mainrag --filter name=qdrant --format 'table {{.Names}}\t{{.Status}}'"
Sleep 700ms
Enter
Sleep 3s

# 2) MCP surface — list the live tools
Type 'curl -sf -H "Authorization: Bearer $TOKEN" http://localhost:3001/api/v1/mcp/tools | jq ".tools | length"'
Sleep 700ms
Enter
Sleep 2500ms

Type 'curl -sf -H "Authorization: Bearer $TOKEN" http://localhost:3001/api/v1/mcp/tools | jq -r ".tools[].name"'
Sleep 700ms
Enter
Sleep 4s

# 3) Drive the search_code tool — hybrid retrieval with citations
Type 'curl -sf -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \'
Sleep 250ms
Enter
Type '  -d ''{"tool_name":"search_code","params":{"query":"hybrid retrieval rerank","limit":2}}'' \'
Sleep 250ms
Enter
Type '  http://localhost:3001/api/v1/mcp/tools/execute | jq ".result.results[0]"'
Sleep 700ms
Enter
Sleep 5s

# 4) Same hybrid retrieval through the CLI (uses the same backend)
Type "mainrag search 'hybrid retrieval combine BM25 vector cross-encoder' --limit 2"
Sleep 700ms
Enter
Sleep 6s

# 5) Closing line
Type "echo '13 MCP tools live · cited results · self-hosted'"
Sleep 200ms
Enter
Sleep 2s

# Intelligence Layer

> Last verified: 2026-04-24 via commit `2d597cb`

MainRag extracts structured facts about source code — symbols, call edges,
ownership, documentation — and exposes them as a queryable graph on top of
the unstructured retrieval pipeline. This document describes what the
Intelligence Layer knows, how it derives that knowledge, and how to query
it.

## What it indexes

Per analyzed file, the Intelligence Layer extracts:

- **Symbols:** functions, methods, classes, types, modules, traits, macros,
  enums. Each row includes language, file, line range, signature, and an
  optional doc-comment.
- **Call-graph edges:** directed `caller → callee` edges, keyed by
  `symbol_id`. Unresolved callees (e.g. dynamic dispatch) are stored by
  name so partial graphs remain useful.
- **Symbol cards:** pre-computed explainer objects per symbol with the
  signature, doc, top callers, top callees, and a one-line `llm_guide`.
- **Ownership:** aggregate edits per file/symbol (via git plugin), useful
  for "who to ask" queries.
- **Annotations (negative evidence):** user-supplied facts of the form
  "symbol X does not do Y, see path Z" — stored separately from code so
  they survive re-indexing.

## Languages

Tree-sitter grammars are loaded for the following languages (API-level
`SELECT DISTINCT language FROM symbols`): Rust, Python, TypeScript,
JavaScript, Go, C, C++, Java, C#, Ruby, PHP, Lua, Zig, Scheme, JSON, TOML,
YAML, Bash, Markdown, HTML, CSS, XML, SQL (sequel grammar).

Not all grammars support symbol extraction — some (JSON/TOML/YAML) are
loaded for chunker support only. The `symbols.language` column is
authoritative for extraction coverage.

## Call-graph semantics

The call-graph is a directed multigraph. Edges are produced by the
tree-sitter extractor when it can statically resolve a call site; all other
call sites go into an `unresolved_calls` bag, keyed by callee name + caller
`symbol_id`. At query time, unresolved names are re-resolved opportunistically
against the live symbol table — this recovers calls that become resolvable
after their target is indexed in a later source.

### Popularity boost

Retrieval results use in-edge count per symbol as a log-damped boost:
highly called functions surface slightly higher, all else equal. The effect
is deliberately small (≤ 10 % of the final score) so rare but relevant
symbols are not drowned out.

## N-hop call chain traversal

The headline feature of the Intelligence Layer:

```http
GET /api/v1/intelligence/call-chain?from=initialize&to=qdrant_upsert&depth=6
```

Implementation: iterative BFS in `services/intelligence.rs::find_call_chain`,
capped at `depth` (default 6, max 10). Returns every simple path from
`from` to `to` up to the cap, each path as an ordered list of symbol-card
stubs so the caller can render "how does A reach B" without a second
lookup.

Complexity: worst case O(b^d) where `b` is branching factor, `d` is depth.
In the 474k-symbol / 1.2M-edge corpus, `depth=6` typically completes in
<150 ms on a single node because:

1. Target-set pruning — the BFS frontier is intersected with the set of
   symbols reachable *into* `to` (precomputed reverse-BFS of bounded
   depth), cutting explored nodes by 10-50×.
2. Postgres-native traversal — the recursive CTE never leaves the database
   round-trip, so page-cache locality is excellent on a warmed corpus.

## Symbol cards

```http
GET /api/v1/intelligence/cards/:id
```

```json
{
  "id": 104421,
  "name": "hybrid_search",
  "kind": "function",
  "language": "rust",
  "file_path": "api/src/services/search.rs",
  "line_range": "412-458",
  "signature": "pub async fn hybrid_search(&self, req: SearchRequest) -> Result<SearchResponse>",
  "doc": "Entry point for the hybrid retrieval pipeline. …",
  "top_callers": [ { "id": 98110, "name": "search_handler", "path": "…" } ],
  "top_callees": [ { "id": 104320, "name": "fts_search" }, { "id": 104380, "name": "vector_search" } ],
  "in_edges": 14,
  "out_edges": 11,
  "llm_guide": "Runs FTS and Qdrant in parallel, merges via RRF, reranks with the GTE cross-encoder. Phase-logged."
}
```

The `llm_guide` field is derived on indexing: the first sentence of the
doc-comment, truncated to 240 chars and stripped of markdown. It is what
the retrieval pipeline injects above raw chunks to give agents a compact
orientation before they read the code.

## Path explanation

```http
POST /api/v1/intelligence/explain_path
{
  "from": "receive_webhook",
  "to": "qdrant_upsert",
  "max_paths": 3
}
```

Combines N-hop traversal with chunk retrieval for each hop so the response
is a narrated path, not a bare list. Used by the CLI `mainrag explain`
command to answer "how does data flow from X to Y".

## Negative evidence

Structured "this is *not* the case" facts, attached to a symbol:

```http
POST /api/v1/intelligence/negative_evidence
{
  "symbol_id": 104421,
  "concept": "caching",
  "reason": "hybrid_search does NOT cache results; it is fully request-scoped.",
  "source": "doc comment L414"
}
```

These records survive re-indexing (not regenerated from code) and are
weighted heavily by the reranker when a query contains the negated
concept — so "does hybrid_search cache results" returns the
negative-evidence annotation before the code body.

## CLI shortcuts

The Intelligence Layer has first-class CLI surface beyond raw HTTP:

- `mainrag symbols --name foo` — symbol search
- `mainrag callgraph foo` — immediate neighborhood
- `mainrag card foo` — rendered symbol card
- `mainrag explore foo` — guided traversal, interactive
- `mainrag explain A B` — path explanation
- `mainrag layers` — ownership / heat map
- `mainrag dead-end add …` — record negative evidence

See `cli/src/commands/` for the full surface; the HTTP endpoints are
1:1 with the CLI subcommands.

## Limits and failure modes

- **Dynamic dispatch:** tree-sitter is static. Virtual calls, function
  pointers, and reflective lookups land in the `unresolved_calls` bag and
  will not appear in N-hop paths unless the target name resolves
  unambiguously.
- **Cross-language calls:** FFI boundaries are not connected. A Rust
  function calling a C symbol produces two unrelated subgraphs.
- **Macro expansion:** not evaluated. Call edges inside macros are
  attributed to the macro-use site, not the expansion.
- **No type inference:** the extractor relies on syntactic shape. Strongly
  typed languages with overloads (C++, Java) can merge overloads under a
  single symbol unless the signature disambiguates them.

#!/usr/bin/env python3
"""
Expand MainRAG Golden Set to 200 Queries

Generates additional test queries based on:
1. Codebase structure (files, functions, modules)
2. Common search patterns (CRUD, error handling, etc.)
3. German and English queries
4. Different search modes (hybrid, keyword, semantic)

Usage:
    python expand_golden_set.py --source /path/to/mainrag/api/src --output golden-set-expanded.jsonl
"""

import argparse
import json
import re
from pathlib import Path
from typing import Dict, List, Set, Tuple
from dataclasses import dataclass, asdict


@dataclass
class GoldenQuery:
    """A golden set query with expected results."""
    id: str
    mode: str
    query: str
    source: str
    k: int
    expect_files: List[str]

    def to_jsonl(self) -> str:
        return json.dumps(asdict(self))


# === QUERY TEMPLATES ===

# Template categories with German/English variations
QUERY_TEMPLATES = {
    # CRUD operations
    "crud": [
        ("create-{noun}", "create {noun}", ["hybrid"]),
        ("add-{noun}", "add new {noun}", ["hybrid"]),
        ("erstellen-{noun}", "{noun} erstellen", ["hybrid"]),
        ("read-{noun}", "read {noun} from database", ["hybrid"]),
        ("get-{noun}", "get {noun} by id", ["keyword"]),
        ("lesen-{noun}", "{noun} lesen", ["hybrid"]),
        ("update-{noun}", "update {noun}", ["hybrid"]),
        ("modify-{noun}", "modify {noun} properties", ["hybrid"]),
        ("ändern-{noun}", "{noun} ändern", ["hybrid"]),
        ("delete-{noun}", "delete {noun}", ["hybrid"]),
        ("remove-{noun}", "remove {noun} from system", ["hybrid"]),
        ("löschen-{noun}", "{noun} löschen", ["hybrid"]),
    ],

    # Search patterns
    "search": [
        ("search-{noun}", "search for {noun}", ["hybrid"]),
        ("find-{noun}", "find {noun}", ["hybrid"]),
        ("lookup-{noun}", "lookup {noun}", ["keyword"]),
        ("query-{noun}", "query {noun}", ["hybrid"]),
        ("suchen-{noun}", "{noun} suchen", ["hybrid"]),
    ],

    # Error handling
    "errors": [
        ("error-{noun}", "{noun} error handling", ["hybrid"]),
        ("fehler-{noun}", "{noun} Fehlerbehandlung", ["hybrid"]),
        ("validate-{noun}", "validate {noun}", ["hybrid"]),
        ("check-{noun}", "check {noun} validity", ["hybrid"]),
    ],

    # Configuration
    "config": [
        ("config-{noun}", "{noun} configuration", ["hybrid"]),
        ("setup-{noun}", "setup {noun}", ["hybrid"]),
        ("init-{noun}", "initialize {noun}", ["hybrid"]),
        ("konfiguration-{noun}", "{noun} Konfiguration", ["hybrid"]),
    ],
}

# Domain-specific nouns to combine with templates
DOMAIN_NOUNS = {
    "mainrag": [
        ("source", ["services/index.rs", "db/models.rs"]),
        ("chunk", ["services/index.rs", "services/chunker/"]),
        ("embedding", ["services/embeddings.rs", "services/index.rs"]),
        ("search", ["services/search.rs", "api/handlers/search.rs"]),
        ("user", ["auth/", "db/models.rs"]),
        ("token", ["auth/middleware.rs", "auth/jwt.rs"]),
        ("file", ["services/index.rs", "plugins/"]),
        ("query", ["services/search.rs", "services/query_expander.rs"]),
        ("result", ["services/search.rs", "db/models.rs"]),
        ("database", ["db/", "services/index.rs"]),
        ("api", ["api/handlers/", "api/routes.rs"]),
        ("handler", ["api/handlers/"]),
        ("middleware", ["auth/middleware.rs", "api/"]),
        ("service", ["services/"]),
        ("config", ["config.rs", "config/"]),
        ("error", ["error.rs", "error/"]),
        ("parser", ["services/parser.rs", "plugins/"]),
        ("reranker", ["services/rerank.rs"]),
        ("compressor", ["services/compressor.rs"]),
        ("synonym", ["services/query_expander.rs"]),
    ],
}

# Direct code-based queries (function names, struct names, etc.)
CODE_QUERIES = {
    "mainrag": [
        # Functions
        ("fn-hybrid-search", "keyword", "hybrid_search", ["services/search.rs"]),
        ("fn-keyword-search", "keyword", "keyword_search", ["services/search.rs"]),
        ("fn-semantic-search", "keyword", "semantic_search", ["services/search.rs"]),
        ("fn-embed", "keyword", "embed", ["services/embeddings.rs"]),
        ("fn-rerank", "keyword", "rerank", ["services/rerank.rs"]),
        ("fn-compress", "keyword", "compress", ["services/compressor.rs"]),
        ("fn-expand", "keyword", "expand", ["services/query_expander.rs"]),
        ("fn-upsert", "keyword", "upsert", ["db/qdrant.rs", "db/postgres.rs"]),
        ("fn-validate", "keyword", "validate", ["auth/"]),

        # Structs
        ("struct-searchresult", "keyword", "SearchResult", ["db/models.rs"]),
        ("struct-chunk", "keyword", "Chunk", ["services/chunker/"]),
        ("struct-appstate", "keyword", "AppState", ["lib.rs", "main.rs"]),
        ("struct-config", "keyword", "Config", ["config.rs"]),
        ("struct-claims", "keyword", "Claims", ["auth/"]),

        # Traits
        ("trait-sourceplugin", "keyword", "SourcePlugin", ["plugins/mod.rs"]),
        ("trait-chunker", "keyword", "Chunker", ["services/chunker/"]),

        # Error types
        ("error-apperror", "keyword", "AppError", ["error.rs"]),

        # API endpoints
        ("api-search", "hybrid", "POST /search", ["api/handlers/search.rs", "api/routes.rs"]),
        ("api-sources", "hybrid", "GET /sources", ["api/handlers/admin.rs"]),
        ("api-health", "hybrid", "health check endpoint", ["api/handlers/"]),

        # Concepts
        ("concept-rls", "hybrid", "row level security postgres", ["db/", "schema"]),
        ("concept-jwt", "hybrid", "JWT token authentication", ["auth/"]),
        ("concept-hybrid", "hybrid", "hybrid search vector keyword", ["services/search.rs"]),
        ("concept-chunking", "hybrid", "semantic chunking tree-sitter", ["services/chunker/"]),
        ("concept-cch", "hybrid", "contextual chunk headers", ["services/chunker/"]),
        ("concept-compression", "hybrid", "contextual compression imports", ["services/compressor.rs"]),
        ("concept-reranking", "hybrid", "cross encoder reranking", ["services/rerank.rs"]),
        ("concept-expansion", "hybrid", "query expansion synonyms", ["services/query_expander.rs"]),
    ],
}

# German-specific queries
GERMAN_QUERIES = {
    "mainrag": [
        ("de-suche", "hybrid", "Suche in Code Dateien", ["services/search.rs"]),
        ("de-authentifizierung", "hybrid", "Benutzer Authentifizierung", ["auth/"]),
        ("de-fehlerbehandlung", "hybrid", "Fehlerbehandlung API", ["error.rs", "api/"]),
        ("de-datenbank", "hybrid", "Datenbank Verbindung Pool", ["db/"]),
        ("de-konfiguration", "hybrid", "Server Konfiguration laden", ["config.rs"]),
        ("de-embeddings", "hybrid", "Embeddings generieren", ["services/embeddings.rs"]),
        ("de-index", "hybrid", "Index erstellen aktualisieren", ["services/index.rs"]),
        ("de-chunks", "hybrid", "Text in Chunks aufteilen", ["services/chunker/"]),
        ("de-api-handler", "hybrid", "API Handler implementieren", ["api/handlers/"]),
        ("de-middleware", "hybrid", "Middleware Authentifizierung", ["auth/middleware.rs"]),
    ],
}

# Edge cases and negative tests
EDGE_CASES = {
    "mainrag": [
        # Very specific
        ("specific-upsert-chunks-batch", "keyword", "upsert_chunks_batch", ["db/qdrant.rs"]),
        ("specific-apply-rls-context", "keyword", "apply_rls_context", ["db/"]),
        ("specific-min-similarity", "keyword", "MIN_SIMILARITY_THRESHOLD", ["services/query_expander.rs", "services/search.rs"]),

        # Multi-word
        ("multi-hybrid-search-vector", "hybrid", "hybrid search combining vector and keyword", ["services/search.rs"]),
        ("multi-jwt-token-claims", "hybrid", "extract claims from jwt token", ["auth/"]),
        ("multi-batch-embedding", "hybrid", "batch embedding generation TEI", ["services/embeddings.rs"]),

        # Typo tolerance (should still find)
        ("typo-serach", "hybrid", "serach results", ["services/search.rs"]),
        ("typo-databse", "hybrid", "databse connection", ["db/"]),
    ],
}


def generate_template_queries(source: str) -> List[GoldenQuery]:
    """Generate queries from templates combined with domain nouns."""
    queries = []
    nouns = DOMAIN_NOUNS.get(source, [])

    for category, templates in QUERY_TEMPLATES.items():
        for noun, expect_files in nouns[:10]:  # Limit to avoid explosion
            for template_id, template_query, modes in templates[:3]:  # Limit templates
                for mode in modes:
                    query_id = template_id.format(noun=noun)
                    query_text = template_query.format(noun=noun)

                    queries.append(GoldenQuery(
                        id=f"{category}-{query_id}",
                        mode=mode,
                        query=query_text,
                        source=source,
                        k=10,
                        expect_files=expect_files
                    ))

    return queries


def generate_code_queries(source: str) -> List[GoldenQuery]:
    """Generate queries from code-specific patterns."""
    queries = []
    code_queries = CODE_QUERIES.get(source, [])

    for query_id, mode, query_text, expect_files in code_queries:
        queries.append(GoldenQuery(
            id=query_id,
            mode=mode,
            query=query_text,
            source=source,
            k=10,
            expect_files=expect_files
        ))

    return queries


def generate_german_queries(source: str) -> List[GoldenQuery]:
    """Generate German-language queries."""
    queries = []
    german_queries = GERMAN_QUERIES.get(source, [])

    for query_id, mode, query_text, expect_files in german_queries:
        queries.append(GoldenQuery(
            id=query_id,
            mode=mode,
            query=query_text,
            source=source,
            k=10,
            expect_files=expect_files
        ))

    return queries


def generate_edge_case_queries(source: str) -> List[GoldenQuery]:
    """Generate edge case and negative test queries."""
    queries = []
    edge_cases = EDGE_CASES.get(source, [])

    for query_id, mode, query_text, expect_files in edge_cases:
        queries.append(GoldenQuery(
            id=query_id,
            mode=mode,
            query=query_text,
            source=source,
            k=10,
            expect_files=expect_files
        ))

    return queries


def load_existing_golden_set(path: Path) -> Set[str]:
    """Load existing query IDs to avoid duplicates."""
    existing_ids = set()
    if path.exists():
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    try:
                        data = json.loads(line)
                        existing_ids.add(data.get("id", ""))
                    except json.JSONDecodeError:
                        pass
    return existing_ids


def deduplicate_queries(queries: List[GoldenQuery], existing_ids: Set[str]) -> List[GoldenQuery]:
    """Remove duplicate queries by ID."""
    seen_ids = existing_ids.copy()
    unique = []

    for q in queries:
        if q.id not in seen_ids:
            seen_ids.add(q.id)
            unique.append(q)

    return unique


def main():
    parser = argparse.ArgumentParser(
        description='Expand MainRAG golden set to 200 queries'
    )
    parser.add_argument('--source', '-s', default='mainrag',
                       help='Source name (default: mainrag)')
    parser.add_argument('--existing', '-e', type=Path,
                       help='Existing golden set to merge with')
    parser.add_argument('--output', '-o', type=Path, required=True,
                       help='Output JSONL file')
    parser.add_argument('--target', '-t', type=int, default=200,
                       help='Target number of queries (default: 200)')

    args = parser.parse_args()

    print(f"Generating golden set queries for source: {args.source}")

    # Load existing queries
    existing_ids = set()
    existing_queries = []
    if args.existing and args.existing.exists():
        existing_ids = load_existing_golden_set(args.existing)
        with open(args.existing) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    existing_queries.append(line)
        print(f"Loaded {len(existing_queries)} existing queries")

    # Generate new queries
    all_queries = []

    # Code-based queries (highest quality)
    code_queries = generate_code_queries(args.source)
    all_queries.extend(code_queries)
    print(f"Generated {len(code_queries)} code-based queries")

    # German queries
    german_queries = generate_german_queries(args.source)
    all_queries.extend(german_queries)
    print(f"Generated {len(german_queries)} German queries")

    # Edge cases
    edge_queries = generate_edge_case_queries(args.source)
    all_queries.extend(edge_queries)
    print(f"Generated {len(edge_queries)} edge case queries")

    # Template-based queries (fill to target)
    template_queries = generate_template_queries(args.source)
    all_queries.extend(template_queries)
    print(f"Generated {len(template_queries)} template queries")

    # Deduplicate
    unique_queries = deduplicate_queries(all_queries, existing_ids)
    print(f"After deduplication: {len(unique_queries)} new queries")

    # Combine with existing
    total_target = args.target - len(existing_queries)
    if total_target > 0:
        selected = unique_queries[:total_target]
    else:
        selected = []

    # Write output
    with open(args.output, 'w') as f:
        # Write existing queries first
        for line in existing_queries:
            f.write(line + '\n')

        # Write new queries
        for q in selected:
            f.write(q.to_jsonl() + '\n')

    total = len(existing_queries) + len(selected)
    print(f"\nWritten {total} queries to {args.output}")
    print(f"  - Existing: {len(existing_queries)}")
    print(f"  - New: {len(selected)}")


if __name__ == '__main__':
    main()

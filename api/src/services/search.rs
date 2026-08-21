use std::collections::HashMap;
use std::sync::Arc;

use crate::db::models::SearchResult;
use crate::db::PostgresPool;
use crate::error::Result;
use crate::services::circuit_breaker::CircuitBreaker;
use crate::services::gpu_semaphore::GpuSemaphores;
pub use crate::services::qdrant::TenantContext;
use crate::services::{QdrantClient, QueryExpander, RerankerService, TeiClient};

/// RRF constant (k) - higher values give more weight to lower-ranked results
const RRF_K: f32 = 60.0;

/// Default FTS weight multiplier
const FTS_WEIGHT_DEFAULT: f32 = 1.0;

/// FTS weight for code-like queries (identifiers, paths, symbols)
const FTS_WEIGHT_CODE: f32 = 1.5;

/// FTS weight for natural language queries
/// Code RAG: NL queries about code still benefit from strong FTS
const FTS_WEIGHT_NL: f32 = 1.2;

/// Multiplicative boost for results found in BOTH FTS and semantic
/// (Previously additive 0.25 which was 15x larger than rank-1 RRF scores,
/// causing overlap to dominate relevance. Multiplicative preserves rank ordering.)
const OVERLAP_MULTIPLIER: f32 = 1.5;

/// Minimum RRF score threshold — results below this are noise and get filtered
const SCORE_FLOOR: f32 = 0.005;

// ============================================================================
// Multi-Signal Relevance Boosting
// ============================================================================

/// Chunk-type relevance boost — code constructs rank higher than prose
fn chunk_type_boost(chunk_type: &str) -> f32 {
    match chunk_type {
        "function" => 1.4,
        "class" => 1.35,
        "module" => 1.3,
        "type" => 1.25,
        "code" => 1.2,
        "conversation" => 1.1,
        "text" => 0.7,
        _ => 1.0,
    }
}

/// File-path relevance boost — implementation files rank higher than docs/changelogs
fn file_path_boost(file_path: &str) -> f32 {
    let path_lower = file_path.to_lowercase();

    // Changelogs are almost never what an agent wants
    if path_lower.contains("changelog") || path_lower.contains("changes") {
        return 0.15;
    }

    // Eval/golden-set files contain queries + expected results, not actual code
    if path_lower.contains("golden-set")
        || path_lower.contains("golden_set")
        || (path_lower.contains("/eval/") && path_lower.ends_with(".jsonl"))
    {
        return 0.1;
    }

    // License, support, contributing — rarely useful for code search
    if path_lower.contains("license")
        || path_lower.contains("support.md")
        || path_lower.contains("contributing")
        || path_lower.contains("code_of_conduct")
    {
        return 0.2;
    }

    // Test fixtures/data — almost never useful for conceptual search
    if path_lower.contains("/testdata/")
        || path_lower.contains("/test/fixtures/")
        || path_lower.contains("/test/testdata/")
        || path_lower.contains("_testdata")
        || path_lower.contains("/conformance/testdata/")
    {
        return 0.15;
    }

    // Test files — useful for "how to test X" but not for conceptual queries
    if path_lower.contains("/test")
        || path_lower.contains("_test.")
        || path_lower.contains(".test.")
        || path_lower.contains("/spec/")
        || path_lower.contains("_test_")
        || path_lower.contains("/tests/")
    {
        return 0.4;
    }

    // Vendor/third-party code — less relevant than own code
    if path_lower.contains("/vendor/")
        || path_lower.contains("/third_party/")
        || path_lower.contains("/node_modules/")
    {
        return 0.6;
    }

    // Generated code (protobuf, swagger, etc.)
    if path_lower.contains(".pb.")
        || path_lower.contains("generated")
        || path_lower.contains("/zz_generated")
        || path_lower.contains(".gen.")
    {
        return 0.5;
    }

    // OpenAPI specs — large JSON files that match many keywords but have low signal
    if path_lower.contains("openapi-spec")
        || path_lower.contains("swagger")
        || (path_lower.contains("openapi") && path_lower.ends_with(".json"))
    {
        return 0.3;
    }

    // Boost by file extension
    if let Some(ext) = file_path.rsplit('.').next() {
        match ext {
            // Implementation code — highest boost
            "rs" | "go" | "py" | "ts" | "js" | "java" | "c" | "cpp" | "cs" | "rb" | "php"
            | "lua" | "zig" | "swift" | "kt" => 1.2,
            // Schema/config — medium
            "sql" | "toml" | "yaml" | "yml" | "json" => 1.0,
            // Shell scripts
            "sh" | "bash" | "zsh" => 0.95,
            // Documentation
            "md" | "txt" | "rst" | "adoc" => 0.6,
            // Data files (jsonl may be conversation transcripts — don't over-penalize)
            "jsonl" => 0.85,
            "csv" | "xml" => 0.5,
            // HTML/CSS
            "html" | "css" | "scss" => 0.8,
            _ => 0.9,
        }
    } else {
        0.9
    }
}

/// Content length normalization — very short or very long chunks are less useful
fn content_length_boost(content_len: usize) -> f32 {
    match content_len {
        0..=30 => 0.3,      // Almost empty (imports, single-line comments)
        31..=80 => 0.6,     // Very short (declarations, type aliases)
        81..=200 => 0.85,   // Short but may have useful info
        201..=3000 => 1.0,  // Sweet spot for code chunks
        3001..=6000 => 0.9, // Getting long, some noise
        _ => 0.75,          // Very long — diluted content
    }
}

/// Hierarchy level boost — function-level chunks are most specific
fn level_boost(level: i16) -> f32 {
    match level {
        2 => 1.15, // Function/method — most specific and useful
        1 => 1.05, // Class/section — good structural context
        0 => 0.9,  // File-level — often just the header
        _ => 1.0,
    }
}

/// Domain-scoped boost for enriched sources (only active when domain profile matches).
/// Boosts function/class chunks and penalizes large file-level chunks.
/// Returns 1.0 (neutral) when no domain is active.
fn domain_boost(chunk_type: Option<&str>, level: Option<i16>, is_domain_source: bool) -> f32 {
    if !is_domain_source {
        return 1.0;
    }
    // In domain sources, strongly prefer symbol-level chunks (function, class)
    let type_boost = match chunk_type.unwrap_or("code") {
        "function" => 1.3, // Methods are the primary symbol cards
        "class" => 1.25,   // Class definitions show ownership/structure
        "type" => 1.2,     // Interfaces, enums
        "module" => 1.1,   // Impl blocks
        "code" => 0.85,    // Generic code chunks — less targeted
        "text" => 0.5,     // Comments/docs — rarely what LLM needs for code nav
        _ => 1.0,
    };
    // Prefer deeper hierarchy (function > class > file)
    let level_boost = match level.unwrap_or(0) {
        2 => 1.2, // Function-level — most specific
        1 => 1.1, // Class-level
        0 => 0.8, // File-level — too broad for domain exploration
        _ => 1.0,
    };
    type_boost * level_boost
}

/// Combined relevance boost for a single result
fn compute_relevance_boost(
    chunk_type: Option<&str>,
    file_path: &str,
    content_len: usize,
    level: Option<i16>,
) -> f32 {
    let ct = chunk_type_boost(chunk_type.unwrap_or("code"));
    let fp = file_path_boost(file_path);
    let cl = content_length_boost(content_len);
    let lv = level_boost(level.unwrap_or(1));

    // Multiplicative combination — each signal contributes independently
    ct * fp * cl * lv
}

/// Sprint 7.1: Query type detection for adaptive search weighting
#[derive(Debug, Clone, Copy, PartialEq)]
enum QueryType {
    /// Code-like query: identifiers, paths, symbols
    Code,
    /// Natural language query
    Natural,
    /// Mixed or ambiguous
    Mixed,
}

/// Detect query type based on heuristics
fn detect_query_type(query: &str) -> QueryType {
    let has_dot = query.contains('.');
    let has_double_colon = query.contains("::");
    let has_arrow = query.contains("->");
    let has_underscore = query.contains('_');
    let has_slash = query.contains('/');

    // CamelCase detection: lowercase followed by uppercase
    let has_camel_case = query
        .chars()
        .zip(query.chars().skip(1))
        .any(|(a, b)| a.is_lowercase() && b.is_uppercase());

    // Code indicators
    let code_signals = [
        has_dot,
        has_double_colon,
        has_arrow,
        has_underscore,
        has_slash,
        has_camel_case,
    ]
    .iter()
    .filter(|&&x| x)
    .count();

    // Word count (NL queries tend to have more words)
    let word_count = query.split_whitespace().count();

    if code_signals >= 2 || (code_signals >= 1 && word_count <= 3) {
        QueryType::Code
    } else if word_count >= 4 && code_signals == 0 {
        QueryType::Natural
    } else {
        QueryType::Mixed
    }
}

/// Get FTS weight based on query type
fn fts_weight_for_query(query_type: QueryType) -> f32 {
    match query_type {
        QueryType::Code => FTS_WEIGHT_CODE,
        QueryType::Natural => FTS_WEIGHT_NL,
        QueryType::Mixed => FTS_WEIGHT_DEFAULT,
    }
}

/// Search results with total count and expansion info for pagination
pub struct SearchResults {
    pub results: Vec<SearchResult>,
    pub total: usize,
    /// Expanded FTS query (if query expansion was applied)
    pub expanded_query: Option<String>,
    /// Expansion terms (if query expansion was applied)
    pub expansion_terms: Vec<String>,
}

/// Detect if query contains phrase markers (quoted strings)
/// Returns (cleaned_query, is_phrase_query)
fn detect_phrase_query(query: &str) -> (String, bool) {
    let trimmed = query.trim();
    // Check for quoted phrase: "exact phrase" or 'exact phrase'
    if (trimmed.starts_with('"') && trimmed.ends_with('"'))
        || (trimmed.starts_with('\'') && trimmed.ends_with('\''))
    {
        // Remove quotes and return as phrase
        let inner = &trimmed[1..trimmed.len() - 1];
        (inner.to_string(), true)
    } else {
        (trimmed.to_string(), false)
    }
}

/// Build appropriate tsquery based on query type
/// - Phrase queries use phraseto_tsquery for exact sequence matching
/// - Regular queries use websearch_to_tsquery for flexible matching
fn build_tsquery_sql(is_phrase: bool, param_num: &str) -> String {
    if is_phrase {
        format!("phraseto_tsquery('simple', {})", param_num)
    } else {
        format!("websearch_to_tsquery('simple', {})", param_num)
    }
}

/// Sprint 7.6: Search mode indicates degraded operation
#[derive(Debug, Clone, Copy, PartialEq)]
pub enum SearchMode {
    /// All services operational
    Full,
    /// TEI embeddings down — no vector search, FTS + optional rerank
    DegradedNoVectors,
    /// TEI reranker down — vector + FTS but no reranking
    DegradedNoRerank,
    /// Both TEI services down — FTS only
    DegradedFtsOnly,
}

impl SearchMode {
    pub fn header_value(&self) -> &'static str {
        match self {
            SearchMode::Full => "full",
            SearchMode::DegradedNoVectors => "degraded-no-vectors",
            SearchMode::DegradedNoRerank => "degraded-no-rerank",
            SearchMode::DegradedFtsOnly => "degraded-fts-only",
        }
    }
}

pub struct SearchService {
    db: PostgresPool,
    tei: Arc<TeiClient>,
    qdrant: Arc<QdrantClient>,
    reranker: Arc<RerankerService>,
    query_expander: Arc<QueryExpander>,
    /// Sprint 7.6: Circuit breakers for degraded mode
    cb_tei_embed: Arc<CircuitBreaker>,
    cb_tei_rerank: Arc<CircuitBreaker>,
    cb_qdrant: Arc<CircuitBreaker>,
    /// Sprint 7.3b: GPU-aware concurrency semaphores
    semaphores: Arc<GpuSemaphores>,
    /// K4: When true, Qdrant results are post-filtered against PG-RLS (backfill phase)
    backfill_active: bool,
    /// K4: Oversampling factor for post-filter (fetch N*factor from Qdrant, then trim)
    backfill_oversampling_factor: u64,
    /// Domain-scoped ranking: source names that have enriched symbol cards.
    /// Populated from DomainProfileRegistry at startup. Empty = no domain boost.
    domain_source_names: std::collections::HashSet<String>,
}

impl SearchService {
    /// Create SearchService with Qdrant + PostgreSQL hybrid search + reranking + query expansion
    #[allow(clippy::too_many_arguments)]
    pub fn new(
        db: PostgresPool,
        tei: Arc<TeiClient>,
        qdrant: Arc<QdrantClient>,
        reranker: Arc<RerankerService>,
        query_expander: Arc<QueryExpander>,
        backfill_active: bool,
        backfill_oversampling_factor: u64,
        domain_source_names: std::collections::HashSet<String>,
    ) -> Self {
        let cb_threshold: u32 = std::env::var("CB_FAILURE_THRESHOLD")
            .ok()
            .and_then(|v| v.parse().ok())
            .unwrap_or(5);
        let cb_recovery_s: u64 = std::env::var("CB_RECOVERY_TIMEOUT_S")
            .ok()
            .and_then(|v| v.parse().ok())
            .unwrap_or(30);
        let recovery = std::time::Duration::from_secs(cb_recovery_s);

        if backfill_active {
            tracing::info!(
                "SearchService: PG-RLS post-filter ACTIVE (oversampling {}x)",
                backfill_oversampling_factor
            );
        }

        Self {
            db,
            tei,
            qdrant,
            reranker,
            query_expander,
            cb_tei_embed: Arc::new(CircuitBreaker::new("tei_embed", cb_threshold, recovery)),
            cb_tei_rerank: Arc::new(CircuitBreaker::new("tei_rerank", cb_threshold, recovery)),
            cb_qdrant: Arc::new(CircuitBreaker::new("qdrant", cb_threshold, recovery)),
            semaphores: Arc::new(GpuSemaphores::from_env()),
            backfill_active,
            backfill_oversampling_factor,
            domain_source_names,
        }
    }

    /// Sprint 7.6: Determine current search mode based on circuit breaker states
    fn current_search_mode(&self) -> SearchMode {
        let embed_ok = self.cb_tei_embed.should_allow();
        let qdrant_ok = self.cb_qdrant.should_allow();
        let rerank_ok = self.cb_tei_rerank.should_allow();

        match (embed_ok && qdrant_ok, rerank_ok) {
            (true, true) => SearchMode::Full,
            (true, false) => SearchMode::DegradedNoRerank,
            (false, true) => SearchMode::DegradedNoVectors,
            (false, false) => SearchMode::DegradedFtsOnly,
        }
    }

    /// Get current search mode (for response header)
    pub fn search_mode(&self) -> SearchMode {
        self.current_search_mode()
    }

    /// True hybrid search using RRF (Reciprocal Rank Fusion)
    /// Combines semantic search (Qdrant) with full-text search (PostgreSQL FTS)
    ///
    /// # Arguments
    /// * `query` - Search query (supports "phrase" for exact matching)
    /// * `source_id` - Optional source filter
    /// * `limit` - Max results
    /// * `rerank` - Whether to apply BGE reranking (adds ~100-200ms latency)
    /// * `agent_id` - Optional agent ID for tenant-scoped cache keys
    ///
    /// Returns SearchResults with total count for pagination
    pub async fn hybrid_search(
        &self,
        query: &str,
        source_id: Option<i64>,
        limit: u32,
        rerank: bool,
        agent_id: Option<&str>,
        tenant: &TenantContext,
    ) -> Result<SearchResults> {
        let start = std::time::Instant::now();

        // Detect phrase query and extract clean query
        let (clean_query, is_phrase) = detect_phrase_query(query);

        // Sprint 7.1: Detect query type for adaptive weighting
        let query_type = detect_query_type(&clean_query);
        let fts_weight = fts_weight_for_query(query_type);
        tracing::info!(query_type = ?query_type, fts_weight = fts_weight, "Adaptive search weighting");
        let phase_start = std::time::Instant::now();

        // Sprint 7.6: Determine search mode FIRST — before any TEI calls
        // This prevents 500 errors when TEI is down (expand() needs TEI embed)
        let search_mode = self.current_search_mode();
        if search_mode != SearchMode::Full {
            tracing::info!(mode = ?search_mode, "Search operating in degraded mode");
            metrics::counter!("mainrag_search_mode", "mode" => search_mode.header_value())
                .increment(1);
        }

        // Only expand query if TEI embeddings are available (expand() calls TEI embed)
        let can_embed = matches!(search_mode, SearchMode::Full | SearchMode::DegradedNoRerank);
        let expanded = if can_embed {
            // Sprint 7.3b: Acquire embed semaphore before TEI call
            let _embed_permit = self.semaphores.embed.acquire().await.map_err(|_| {
                crate::error::AppError::Internal("Embed semaphore closed".to_string())
            })?;
            let result = self.query_expander.expand(&clean_query, agent_id).await?;
            drop(_embed_permit);

            // Log expansion for debugging
            if !result.synonyms.is_empty() {
                tracing::debug!(
                    "Query expansion: '{}' -> FTS: '{}', {} synonyms found",
                    clean_query,
                    result.fts_query,
                    result.synonyms.len()
                );
            }
            result
        } else {
            // TEI down — skip expansion, use plain query with empty embedding
            tracing::debug!(
                "Skipping query expansion (TEI unavailable in {:?} mode)",
                search_mode
            );
            self.query_expander.expand_fts_only(&clean_query).await
        };
        tracing::info!(
            elapsed_ms = phase_start.elapsed().as_millis() as u64,
            "Phase 1: Query expansion + embedding"
        );
        let phase_start = std::time::Instant::now();

        // Wave 2b: Decouple candidate pool from response limit
        let candidate_pool: u64 = std::env::var("CANDIDATE_POOL_SIZE")
            .ok()
            .and_then(|v| v.parse().ok())
            .unwrap_or(100);
        let fetch_limit = candidate_pool.max(limit as u64 * 3);

        // Sprint 7.6: Conditional search paths based on service availability
        let can_do_semantic =
            matches!(search_mode, SearchMode::Full | SearchMode::DegradedNoRerank);

        // Wave 2a: Wire expanded FTS query instead of clean_query
        // Phrase queries bypass expansion (they need exact sequence matching)
        let fts_query_str = if expanded.fts_query != expanded.original && !is_phrase {
            &expanded.fts_query
        } else {
            &clean_query
        };

        // Run searches in parallel (semantic only if services are available)
        let (semantic_results, fts_results) = if can_do_semantic {
            let (semantic_result, fts_result) = tokio::join!(
                self.semantic_search_with_embedding_cb(
                    &expanded.embedding,
                    source_id,
                    fetch_limit,
                    tenant
                ),
                self.fts_search_internal(
                    fts_query_str,
                    source_id,
                    fetch_limit as u32,
                    is_phrase,
                    tenant
                )
            );
            // If semantic fails at runtime (circuit breaker was half-open), fall back gracefully
            let semantic = match semantic_result {
                Ok(r) => r,
                Err(e) => {
                    tracing::warn!("Semantic search failed (degrading to FTS-only): {}", e);
                    vec![]
                }
            };
            (semantic, fts_result?)
        } else {
            // FTS-only mode
            let fts_result = self
                .fts_search_internal(
                    fts_query_str,
                    source_id,
                    fetch_limit as u32,
                    is_phrase,
                    tenant,
                )
                .await;
            (vec![], fts_result?)
        };

        tracing::info!(
            elapsed_ms = phase_start.elapsed().as_millis() as u64,
            "Phase 2: FTS + Qdrant parallel search"
        );
        let phase_start = std::time::Instant::now();

        // Build RRF scores
        // Map: chunk_id -> (rrf_score, semantic_rank, fts_rank)
        let mut rrf_scores: HashMap<i64, (f32, Option<usize>, Option<usize>)> = HashMap::new();

        // Add semantic results with RRF contribution
        for (rank, (chunk_id, _score)) in semantic_results.iter().enumerate() {
            let rrf_contribution = 1.0 / (RRF_K + (rank + 1) as f32);
            rrf_scores
                .entry(*chunk_id)
                .and_modify(|(score, sem_rank, _)| {
                    *score += rrf_contribution;
                    *sem_rank = Some(rank + 1);
                })
                .or_insert((rrf_contribution, Some(rank + 1), None));
        }

        // Add FTS results with boosted RRF contribution (keyword matches are more precise)
        // Sprint 7.1: fts_weight is adaptive based on query type (code vs NL)
        for (rank, (chunk_id, _score)) in fts_results.iter().enumerate() {
            let rrf_contribution = fts_weight / (RRF_K + (rank + 1) as f32);
            rrf_scores
                .entry(*chunk_id)
                .and_modify(|(score, _, fts_rank)| {
                    *score += rrf_contribution;
                    *fts_rank = Some(rank + 1);
                })
                .or_insert((rrf_contribution, None, Some(rank + 1)));
        }

        // Apply overlap boost: results found in BOTH FTS and semantic are more trustworthy
        // Multiplicative: preserves rank ordering instead of dominating scores
        for (_chunk_id, (score, sem_rank, fts_rank)) in rrf_scores.iter_mut() {
            if sem_rank.is_some() && fts_rank.is_some() {
                *score *= OVERLAP_MULTIPLIER;
            }
        }

        // Sort by RRF score
        let mut sorted_chunks: Vec<_> = rrf_scores.into_iter().collect();
        sorted_chunks.sort_by(|a, b| {
            b.1 .0
                .partial_cmp(&a.1 .0)
                .unwrap_or(std::cmp::Ordering::Equal)
        });

        // Wave 2b: Use candidate_pool for dedup limit too (aligned with fetch)
        let dedup_fetch_limit = candidate_pool as usize;

        // Extract expansion info for response
        let expanded_query = if expanded.fts_query != expanded.original {
            Some(expanded.fts_query.clone())
        } else {
            None
        };
        let expansion_terms: Vec<String> =
            expanded.synonyms.iter().map(|s| s.term.clone()).collect();

        if sorted_chunks.is_empty() {
            return Ok(SearchResults {
                results: vec![],
                total: 0,
                expanded_query,
                expansion_terms,
            });
        }

        // Symbol-aware expansion: if query looks like a symbol name (Code type, 1-3 words),
        // look up callees in call_graph and add their chunks to results with reduced score.
        // This helps LLMs discover delegation chains (e.g., "createClip" → also shows "createClipImpl").
        let total_found = sorted_chunks.len();
        let client = self.db.get().await?;

        if query_type == QueryType::Code && clean_query.split_whitespace().count() <= 2 {
            // Single-symbol query — check for callees
            let callee_rows = client
                .query(
                    "SELECT DISTINCT cg.callee_name
                 FROM call_graph cg
                 JOIN symbols s ON cg.caller_symbol_id = s.id
                 WHERE s.name = $1
                 LIMIT 10",
                    &[&clean_query],
                )
                .await
                .unwrap_or_default();

            if !callee_rows.is_empty() {
                let callee_names: Vec<String> =
                    callee_rows.iter().map(|r| r.get::<_, String>(0)).collect();

                // Find chunks for callees: use callee_symbol_id → symbol → file+line → chunk
                // This avoids scanning all chunks — goes through the call_graph directly
                if let Ok(callee_fts) = client
                    .query(
                        "SELECT DISTINCT c.id as chunk_id
                     FROM call_graph cg
                     JOIN symbols caller ON cg.caller_symbol_id = caller.id
                     JOIN symbols callee ON cg.callee_symbol_id = callee.id
                     JOIN chunks c ON c.file_id = callee.file_id
                       AND c.start_line <= callee.line_start AND c.end_line >= callee.line_end
                     WHERE caller.name = $1 AND cg.callee_symbol_id IS NOT NULL
                     LIMIT 10",
                        &[&clean_query],
                    )
                    .await
                {
                    let existing_ids: std::collections::HashSet<i64> =
                        sorted_chunks.iter().map(|(id, _)| *id).collect();
                    let base_score = sorted_chunks
                        .first()
                        .map(|(_, (s, _, _))| *s)
                        .unwrap_or(0.5)
                        * 0.3;

                    for row in &callee_fts {
                        let chunk_id: i64 = row.get(0);
                        if !existing_ids.contains(&chunk_id) {
                            sorted_chunks.push((chunk_id, (base_score, None, None)));
                        }
                    }

                    if !callee_fts.is_empty() {
                        tracing::info!(
                            "Symbol expansion: '{}' → {} callees, {} extra chunks",
                            clean_query,
                            callee_names.len(),
                            callee_fts.len()
                        );
                    }
                }
            }
        }

        tracing::info!(
            elapsed_ms = phase_start.elapsed().as_millis() as u64,
            "Phase 3: RRF merge + symbol expansion"
        );
        let phase_start = std::time::Instant::now();

        // Rebuild top_chunk_ids after potential symbol expansion
        let top_chunk_ids: Vec<i64> = sorted_chunks
            .iter()
            .take(dedup_fetch_limit)
            .map(|(id, _)| *id)
            .collect();

        // Build score map for final results
        let score_map: HashMap<i64, f32> = sorted_chunks
            .iter()
            .map(|(id, (score, _, _))| (*id, *score))
            .collect();
        // Fetch results with call-graph popularity: how many callers reference
        // the symbol that this chunk belongs to. Functions with many callers are
        // more important (e.g., a widely-used API method vs. an internal helper).
        let base_sql = r#"
            SELECT
                c.id as chunk_id,
                f.path as file_path,
                c.content_text as content,
                c.start_line as line_start,
                c.end_line as line_end,
                s.name as source_name,
                f.language,
                c.context_prefix,
                c.chunk_type,
                c.level,
                COALESCE(pop.caller_count, 0)::int as caller_count
            FROM chunks c
            JOIN files f ON c.file_id = f.id
            JOIN sources s ON f.source_id = s.id
            LEFT JOIN LATERAL (
                SELECT COUNT(*) as caller_count
                FROM call_graph cg
                JOIN symbols sym ON cg.callee_symbol_id = sym.id
                WHERE sym.file_id = f.id
                  AND c.start_line >= sym.line_start
                  AND c.start_line <= sym.line_end
                LIMIT 1
            ) pop ON c.chunk_type IN ('function', 'class', 'type')
            WHERE c.id = ANY($1)
            "#;

        // Wave 1 Fix: Source-isolation guard on final fetch
        let rows = if let Some(sid) = source_id {
            let sql = format!("{} AND f.source_id = $2", base_sql);
            client.query(&sql, &[&top_chunk_ids, &sid]).await?
        } else {
            client.query(base_sql, &[&top_chunk_ids]).await?
        };

        // Build results with RRF scores + multi-signal relevance boost
        let mut results: Vec<SearchResult> = rows
            .iter()
            .map(|row| {
                let chunk_id: i64 = row.get("chunk_id");
                let rrf_score = score_map.get(&chunk_id).copied().unwrap_or(0.0);
                let context_prefix: Option<String> = row.get("context_prefix");
                let chunk_type: Option<String> = row.get("chunk_type");
                let level: Option<i16> = row.get("level");
                let file_path: String = row.get("file_path");
                let content: String = row.get("content");

                // Multi-signal relevance boost
                let boost = compute_relevance_boost(
                    chunk_type.as_deref(),
                    &file_path,
                    content.len(),
                    level,
                );
                let source_name: String = row.get("source_name");
                let is_domain = self.domain_source_names.contains(&source_name);
                let d_boost = domain_boost(chunk_type.as_deref(), level, is_domain);

                // Call-graph popularity boost: functions with many callers rank higher
                let caller_count: i32 = row.get("caller_count");
                let pop_boost = if caller_count > 0 {
                    1.0 + (caller_count as f32 + 1.0).log2() * 0.1
                } else {
                    1.0
                };

                let boosted_score = rrf_score * boost * d_boost * pop_boost;

                SearchResult {
                    chunk_id,
                    file_path,
                    content,
                    snippet: None,
                    line_start: row.get("line_start"),
                    line_end: row.get("line_end"),
                    source_name: row.get("source_name"),
                    language: row.get("language"),
                    score: boosted_score,
                    context_prefix,
                    location: None,
                    chunk_type,
                    level,
                    parent_context: None, // Populated below
                    external_hit_id: None,
                    successor_metadata: None,
                    score_explanation: None,
                    degradation: None,
                }
            })
            .collect();

        // Score floor: remove noise results
        let pre_floor = results.len();
        results.retain(|r| r.score >= SCORE_FLOOR);
        if results.len() < pre_floor {
            tracing::debug!(
                "Score floor removed {} noise results (threshold: {})",
                pre_floor - results.len(),
                SCORE_FLOOR
            );
        }

        tracing::info!(
            elapsed_ms = phase_start.elapsed().as_millis() as u64,
            "Phase 4: Result fetch + popularity boost"
        );
        let phase_start = std::time::Instant::now();

        // Sort by boosted score descending
        results.sort_by(|a, b| {
            b.score
                .partial_cmp(&a.score)
                .unwrap_or(std::cmp::Ordering::Equal)
        });

        // Parent-context enrichment: for function-level chunks, fetch the parent class/module
        // signature so the LLM understands the surrounding context.
        let function_chunk_ids: Vec<i64> = results
            .iter()
            .filter(|r| r.chunk_type.as_deref() == Some("function") && r.level == Some(2))
            .take(20) // Max 20 parent lookups per search
            .map(|r| r.chunk_id)
            .collect();

        if !function_chunk_ids.is_empty() {
            if let Ok(parent_rows) = client
                .query(
                    "SELECT c.id as chunk_id, LEFT(pc.content_text, 200) as parent_text
                 FROM chunks c
                 JOIN chunks pc ON pc.id = c.parent_chunk_id
                 WHERE c.id = ANY($1) AND c.parent_chunk_id IS NOT NULL",
                    &[&function_chunk_ids],
                )
                .await
            {
                let parent_map: HashMap<i64, String> = parent_rows
                    .iter()
                    .filter_map(|r| {
                        let cid: i64 = r.get("chunk_id");
                        let text: Option<String> = r.get("parent_text");
                        text.map(|t| (cid, t))
                    })
                    .collect();

                for result in &mut results {
                    if let Some(parent_text) = parent_map.get(&result.chunk_id) {
                        // Extract first meaningful line (class/struct/interface declaration)
                        let sig = parent_text
                            .lines()
                            .find(|l| {
                                let t = l.trim();
                                t.starts_with("pub ")
                                    || t.starts_with("class ")
                                    || t.starts_with("interface ")
                                    || t.starts_with("struct ")
                                    || t.starts_with("impl ")
                                    || t.starts_with("public ")
                                    || t.starts_with("abstract ")
                                    || t.starts_with("func ")
                                    || t.starts_with("def ")
                                    || t.starts_with("type ")
                                    || t.starts_with("module ")
                            })
                            .unwrap_or_else(|| parent_text.lines().next().unwrap_or(""));

                        if !sig.trim().is_empty() {
                            result.parent_context = Some(sig.trim().to_string());
                        }
                    }
                }
            }
        }

        // Deduplicate near-identical results (same text across different files, e.g. changelogs)
        // Note: chunk-level embeddings are not fetched from Qdrant in the search path,
        // so cosine dedup is skipped here (None). If embeddings become available in the
        // future (e.g. via Qdrant with_vectors), pass them as Some(&embedding_map).
        results = deduplicate_results(results, limit as usize, None);

        // Apply reranking if requested and results exist
        // Sprint 7.6: Skip reranking if reranker circuit breaker is open
        tracing::info!(
            elapsed_ms = phase_start.elapsed().as_millis() as u64,
            "Phase 5: Parent context + dedup + sort"
        );

        let can_rerank = matches!(
            search_mode,
            SearchMode::Full | SearchMode::DegradedNoVectors
        );
        if rerank && can_rerank && !results.is_empty() {
            let rerank_start = std::time::Instant::now();
            match self.rerank_results_cb(query, &results).await {
                Ok(reranked) => {
                    tracing::info!(
                        elapsed_ms = rerank_start.elapsed().as_millis() as u64,
                        "Phase 6: Reranking"
                    );
                    // Record search metrics
                    metrics::histogram!("search_duration_seconds", "type" => "hybrid", "rerank" => "true")
                        .record(start.elapsed().as_secs_f64());
                    metrics::histogram!("search_results_count", "type" => "hybrid")
                        .record(reranked.len() as f64);
                    return Ok(SearchResults {
                        results: reranked,
                        total: total_found,
                        expanded_query: expanded_query.clone(),
                        expansion_terms: expansion_terms.clone(),
                    });
                }
                Err(e) => {
                    // Log error but don't fail - fall back to RRF results
                    tracing::warn!("Reranking failed, using RRF results: {}", e);
                }
            }
        }

        // Record search metrics
        metrics::histogram!("search_duration_seconds", "type" => "hybrid", "rerank" => "false")
            .record(start.elapsed().as_secs_f64());
        metrics::histogram!("search_results_count", "type" => "hybrid")
            .record(results.len() as f64);

        Ok(SearchResults {
            results,
            total: total_found,
            expanded_query,
            expansion_terms,
        })
    }

    /// Internal semantic search using Qdrant - returns (chunk_id, score) pairs
    /// K4: Uses tenant-aware Qdrant search for data isolation
    #[allow(dead_code)]
    async fn semantic_search_internal(
        &self,
        query: &str,
        source_id: Option<i64>,
        limit: u64,
        tenant: &TenantContext,
    ) -> Result<Vec<(i64, f32)>> {
        let embedding = self.tei.embed(query).await?;
        self.semantic_search_with_embedding(&embedding, source_id, limit, tenant)
            .await
    }

    /// Semantic search with pre-computed embedding (for query expansion)
    /// K4: Uses tenant-aware Qdrant search for data isolation
    /// K4-FIX1: When backfill_active, post-filters Qdrant results against PG-RLS
    /// K4-FIX2: When backfill_active, oversamples from Qdrant (limit * factor)
    async fn semantic_search_with_embedding(
        &self,
        embedding: &[f32],
        source_id: Option<i64>,
        limit: u64,
        tenant: &TenantContext,
    ) -> Result<Vec<(i64, f32)>> {
        // K4-FIX2: Oversample when post-filter is active (some results will be filtered)
        let effective_limit =
            if self.backfill_active && matches!(tenant, TenantContext::Agent { .. }) {
                limit * self.backfill_oversampling_factor
            } else {
                limit
            };

        // K4: Use tenant-aware search (user_id filter in Qdrant)
        let qdrant_results = self
            .qdrant
            .search_chunks_with_tenant(embedding.to_vec(), effective_limit, tenant, source_id)
            .await?;

        let mut results: Vec<(i64, f32)> = qdrant_results
            .into_iter()
            .map(|(id, score)| (id as i64, score))
            .collect();

        // K4-FIX1: Post-filter against PG-RLS during backfill (Qdrant points may lack user_id)
        if self.backfill_active {
            if let TenantContext::Agent { user_id } = tenant {
                let pre_filter_count = results.len();
                results = self.post_filter_by_rls(&results, *user_id).await?;
                let filtered = pre_filter_count - results.len();
                if filtered > 0 {
                    tracing::debug!(
                        pre_filter = pre_filter_count,
                        post_filter = results.len(),
                        filtered_out = filtered,
                        "K4 backfill post-filter removed cross-tenant results"
                    );
                    metrics::counter!("mainrag_backfill_postfilter_removed")
                        .increment(filtered as u64);
                }
                // Trim back to original limit after filtering
                results.truncate(limit as usize);
            }
            // Admin: no post-filter needed (sees everything)
        }

        Ok(results)
    }

    /// K4-FIX1: Post-filter Qdrant results against PostgreSQL for tenant isolation.
    /// Returns only chunk_ids that belong to sources owned by the user.
    async fn post_filter_by_rls(
        &self,
        results: &[(i64, f32)],
        user_id: uuid::Uuid,
    ) -> Result<Vec<(i64, f32)>> {
        if results.is_empty() {
            return Ok(vec![]);
        }

        let chunk_ids: Vec<i64> = results.iter().map(|(id, _)| *id).collect();

        let client = self.db.get().await?;
        let rows = client
            .query(
                "SELECT c.id FROM chunks c \
                 JOIN files f ON c.file_id = f.id \
                 JOIN sources s ON f.source_id = s.id \
                 WHERE c.id = ANY($1::bigint[]) AND s.user_id = $2",
                &[&chunk_ids, &user_id],
            )
            .await?;

        let allowed_ids: std::collections::HashSet<i64> =
            rows.iter().map(|r| r.get::<_, i64>("id")).collect();

        // Preserve original order and scores
        Ok(results
            .iter()
            .filter(|(id, _)| allowed_ids.contains(id))
            .copied()
            .collect())
    }

    /// Internal FTS search - returns (chunk_id, score) pairs
    /// Direct query without transaction overhead (FORCE RLS removed — table owner bypasses RLS).
    /// Application-layer auth (TenantContext) is the primary access control.
    ///
    /// Supports:
    /// - phrase queries (is_phrase=true): uses phraseto_tsquery
    /// - expanded queries (contains |): uses to_tsquery directly
    /// - regular queries: uses websearch_to_tsquery
    async fn fts_search_internal(
        &self,
        query: &str,
        source_id: Option<i64>,
        limit: u32,
        is_phrase: bool,
        tenant: &TenantContext,
    ) -> Result<Vec<(i64, f32)>> {
        let fts_start = std::time::Instant::now();
        let client = self.db.get().await?;

        // Build tsquery based on query type
        let is_expanded = query.contains(" | ");

        // Sanitize expanded queries: remove empty terms and leading/trailing pipes
        let effective_query: String;
        let tsquery = if is_expanded {
            let cleaned: Vec<&str> = query
                .split(" | ")
                .map(|t| t.trim())
                .filter(|t| !t.is_empty())
                .collect();
            if cleaned.is_empty() {
                tracing::warn!("Empty expanded query after sanitization, falling back to original");
                effective_query = query.to_string();
                build_tsquery_sql(is_phrase, "$1")
            } else {
                effective_query = cleaned.join(" | ");
                "to_tsquery('simple', $1)".to_string()
            }
        } else {
            effective_query = query.to_string();
            build_tsquery_sql(is_phrase, "$1")
        };
        let query = effective_query.as_str();

        // Tenant-aware FTS query — JOIN sources for user_id filtering
        // Dual-channel: 'simple' (exact tokens, good for code) + 'english' (stemmed, good for NL)
        // GREATEST picks the better score; english channel gets 0.8 weight to prefer exact matches
        // Normalization flag 1 = divide rank by (1 + log(doc length)) — prevents long docs from dominating
        // Dual-channel FTS via UNION ALL.
        // Each branch uses its own GIN index. JOINs on files/sources are deferred
        // to after the GIN scan to avoid Nested Loop over all files.
        // The GIN index scan + ts_rank_cd is the expensive part — we limit to top 500
        // candidates per channel BEFORE joining to files/sources for tenant filtering.
        const FTS_CHANNEL_LIMIT: i64 = 500;
        let base_query = format!(
            r#"
            SELECT dual.chunk_id, MAX(dual.score)::real as score,
                   f.source_id, s.user_id
            FROM (
                (SELECT c.id as chunk_id,
                    ts_rank_cd(c.fts_vector, {tsquery}, 1)::real as score,
                    c.file_id
                FROM chunks c
                WHERE c.fts_vector @@ {tsquery}
                ORDER BY score DESC LIMIT {channel_limit})
                UNION ALL
                (SELECT c.id as chunk_id,
                    (ts_rank_cd(c.fts_vector_english, {tsquery_en}, 1) * 0.8)::real as score,
                    c.file_id
                FROM chunks c
                WHERE c.fts_vector_english @@ {tsquery_en}
                ORDER BY score DESC LIMIT {channel_limit})
            ) dual
            JOIN files f ON f.id = dual.file_id
            JOIN sources s ON s.id = f.source_id
            "#,
            channel_limit = FTS_CHANNEL_LIMIT,
            tsquery = tsquery,
            tsquery_en = if is_expanded {
                "to_tsquery('english', $1)".to_string()
            } else if is_phrase {
                "phraseto_tsquery('english', $1)".to_string()
            } else {
                "websearch_to_tsquery('english', $1)".to_string()
            }
        );

        // Source-diversity: limit results per source to prevent one large repo from
        // monopolizing the FTS candidate pool. Uses window function to rank within each source.
        // When a specific source_id is given, skip diversity (user wants that source).
        // Per-source FTS diversity: limits how many results a single source can contribute
        // to the candidate pool. Higher = better recall for single-source queries,
        // lower = better diversity across sources. 100 is a good balance:
        // - Still allows 100 results from the queried codebase (enough for recall)
        // - Caps mega-repos (kubernetes 391K chunks) from drowning other sources
        const FTS_PER_SOURCE_LIMIT: i64 = 100;

        // Tenant/source filters applied on the outer wrapper after JOINs.
        let rows = match tenant {
            TenantContext::Agent { user_id } => {
                if let Some(sid) = source_id {
                    let full_query = format!(
                        "{} WHERE s.user_id = $2 AND f.source_id = $3 GROUP BY dual.chunk_id, f.source_id, s.user_id ORDER BY score DESC LIMIT $4",
                        base_query);
                    client
                        .query(&full_query, &[&query, user_id, &sid, &(limit as i64)])
                        .await?
                } else {
                    let diverse_query = format!(
                        "SELECT chunk_id, score FROM (\
                            SELECT dual.chunk_id, MAX(dual.score)::real as score, f.source_id, \
                                ROW_NUMBER() OVER (PARTITION BY f.source_id ORDER BY MAX(dual.score) DESC) as rn \
                            FROM ({inner}) dual \
                            JOIN files f ON f.id = dual.file_id \
                            JOIN sources s ON s.id = f.source_id \
                            WHERE s.user_id = $2 GROUP BY dual.chunk_id, f.source_id\
                        ) sub WHERE rn <= $3 ORDER BY score DESC LIMIT $4",
                        inner = format!(
                            "(SELECT c.id as chunk_id, ts_rank_cd(c.fts_vector, {tsquery}, 1)::real as score, c.file_id \
                             FROM chunks c WHERE c.fts_vector @@ {tsquery} ORDER BY score DESC LIMIT {cl}) \
                             UNION ALL \
                             (SELECT c.id as chunk_id, (ts_rank_cd(c.fts_vector_english, {tsquery_en}, 1) * 0.8)::real as score, c.file_id \
                             FROM chunks c WHERE c.fts_vector_english @@ {tsquery_en} ORDER BY score DESC LIMIT {cl})",
                            tsquery = tsquery,
                            tsquery_en = if is_expanded {
                                "to_tsquery('english', $1)".to_string()
                            } else if is_phrase {
                                "phraseto_tsquery('english', $1)".to_string()
                            } else {
                                "websearch_to_tsquery('english', $1)".to_string()
                            },
                            cl = FTS_CHANNEL_LIMIT
                        )
                    );
                    client
                        .query(
                            &diverse_query,
                            &[&query, user_id, &FTS_PER_SOURCE_LIMIT, &(limit as i64)],
                        )
                        .await?
                }
            }
            TenantContext::Admin => {
                if let Some(sid) = source_id {
                    let full_query = format!(
                        "{} WHERE f.source_id = $2 GROUP BY dual.chunk_id, f.source_id, s.user_id ORDER BY score DESC LIMIT $3",
                        base_query);
                    client
                        .query(&full_query, &[&query, &sid, &(limit as i64)])
                        .await?
                } else {
                    let diverse_query = format!(
                        "SELECT chunk_id, score FROM (\
                            SELECT dual.chunk_id, MAX(dual.score)::real as score, f.source_id, \
                                ROW_NUMBER() OVER (PARTITION BY f.source_id ORDER BY MAX(dual.score) DESC) as rn \
                            FROM ({inner}) dual \
                            JOIN files f ON f.id = dual.file_id \
                            JOIN sources s ON s.id = f.source_id \
                            GROUP BY dual.chunk_id, f.source_id\
                        ) sub WHERE rn <= $2 ORDER BY score DESC LIMIT $3",
                        inner = format!(
                            "(SELECT c.id as chunk_id, ts_rank_cd(c.fts_vector, {tsquery}, 1)::real as score, c.file_id \
                             FROM chunks c WHERE c.fts_vector @@ {tsquery} ORDER BY score DESC LIMIT {cl}) \
                             UNION ALL \
                             (SELECT c.id as chunk_id, (ts_rank_cd(c.fts_vector_english, {tsquery_en}, 1) * 0.8)::real as score, c.file_id \
                             FROM chunks c WHERE c.fts_vector_english @@ {tsquery_en} ORDER BY score DESC LIMIT {cl})",
                            tsquery = tsquery,
                            tsquery_en = if is_expanded {
                                "to_tsquery('english', $1)".to_string()
                            } else if is_phrase {
                                "phraseto_tsquery('english', $1)".to_string()
                            } else {
                                "websearch_to_tsquery('english', $1)".to_string()
                            },
                            cl = FTS_CHANNEL_LIMIT
                        )
                    );
                    client
                        .query(
                            &diverse_query,
                            &[&query, &FTS_PER_SOURCE_LIMIT, &(limit as i64)],
                        )
                        .await?
                }
            }
        };

        // Record FTS-specific duration metric
        metrics::histogram!("fts_query_duration_seconds", "strategy" => "direct")
            .record(fts_start.elapsed().as_secs_f64());

        Ok(rows
            .iter()
            .map(|row| (row.get::<_, i64>("chunk_id"), row.get::<_, f32>("score")))
            .collect())
    }

    /// Pure semantic search using Qdrant
    /// For semantic search, snippet shows first N words as context (no FTS highlighting)
    /// Semantic search using embeddings via Qdrant
    /// Returns SearchResults with total count (total is approximate for semantic search)
    pub async fn semantic_search(
        &self,
        query: &str,
        source_id: Option<i64>,
        limit: u32,
        tenant: &TenantContext,
    ) -> Result<SearchResults> {
        let embedding = self.tei.embed(query).await?;

        // K4: Use tenant-aware search
        let qdrant_results = self
            .qdrant
            .search_chunks_with_tenant(embedding, limit as u64, tenant, source_id)
            .await?;

        if qdrant_results.is_empty() {
            return Ok(SearchResults {
                results: vec![],
                total: 0,
                expanded_query: None,
                expansion_terms: vec![],
            });
        }

        // Convert Qdrant results to chunk IDs
        let chunk_ids: Vec<i64> = qdrant_results.iter().map(|(id, _)| *id as i64).collect();

        // Create score map from Qdrant results
        let score_map: std::collections::HashMap<i64, f32> = qdrant_results
            .into_iter()
            .map(|(id, score)| (id as i64, score))
            .collect();

        // Direct connection — FORCE RLS removed, table owner bypasses RLS
        let client = self.db.get().await?;

        // Fetch full chunk data — skip ts_headline (LLMs receive content_text directly)
        let base_sql = r#"
            SELECT
                c.id as chunk_id,
                f.path as file_path,
                c.content_text as content,
                c.start_line as line_start,
                c.end_line as line_end,
                s.name as source_name,
                f.language,
                c.context_prefix
            FROM chunks c
            JOIN files f ON c.file_id = f.id
            JOIN sources s ON f.source_id = s.id
            WHERE c.id = ANY($1)
        "#;

        let rows = if let Some(sid) = source_id {
            let sql = format!("{} AND f.source_id = $2", base_sql);
            client.query(&sql, &[&chunk_ids, &sid]).await?
        } else {
            client.query(base_sql, &[&chunk_ids]).await?
        };

        let results: Vec<SearchResult> = rows
            .iter()
            .map(|row| {
                let chunk_id: i64 = row.get("chunk_id");
                let context_prefix: Option<String> = row.get("context_prefix");
                SearchResult {
                    chunk_id,
                    file_path: row.get("file_path"),
                    content: row.get("content"),
                    snippet: None,
                    line_start: row.get("line_start"),
                    line_end: row.get("line_end"),
                    source_name: row.get("source_name"),
                    language: row.get("language"),
                    score: score_map.get(&chunk_id).copied().unwrap_or(0.0),
                    context_prefix,
                    location: None,
                    chunk_type: None, // Not fetched for semantic-only search
                    level: None,
                    parent_context: None,
                    external_hit_id: None,
                    successor_metadata: None,
                    score_explanation: None,
                    degradation: None,
                }
            })
            .collect();

        // For semantic search, total is the number of results found
        let total = results.len();
        Ok(SearchResults {
            results,
            total,
            expanded_query: None,
            expansion_terms: vec![],
        })
    }

    /// Keyword search using PostgreSQL FTS
    /// Supports phrase queries with quotes ("exact phrase")
    /// Returns SearchResults with total count for pagination
    pub async fn keyword_search(
        &self,
        query: &str,
        source_id: Option<i64>,
        limit: u32,
        tenant: &TenantContext,
    ) -> Result<SearchResults> {
        let client = self.db.get().await?;

        // Detect phrase query
        let (clean_query, is_phrase) = detect_phrase_query(query);
        let tsquery = build_tsquery_sql(is_phrase, "$1");

        // Single query: FTS ranking + data fetch (no COUNT, no ts_headline — LLMs don't need either)
        let base_query = format!(
            r#"
            SELECT
                c.id as chunk_id,
                f.path as file_path,
                c.content_text as content,
                c.start_line as line_start,
                c.end_line as line_end,
                s.name as source_name,
                f.language,
                c.context_prefix,
                ts_rank_cd(c.fts_vector, {tsquery}) as score
            FROM chunks c
            JOIN files f ON c.file_id = f.id
            JOIN sources s ON f.source_id = s.id
            WHERE c.fts_vector @@ {tsquery}
            "#,
            tsquery = tsquery
        );

        let limit_i64 = limit as i64;
        let rows = match tenant {
            TenantContext::Agent { user_id } => {
                if let Some(sid) = source_id {
                    let full_query = format!(
                        "{} AND s.user_id = $2 AND f.source_id = $3 ORDER BY score DESC LIMIT $4",
                        base_query
                    );
                    let params: [&(dyn tokio_postgres::types::ToSql + Sync); 4] =
                        [&clean_query, user_id, &sid, &limit_i64];
                    client.query(&full_query, &params).await?
                } else {
                    let full_query = format!(
                        "{} AND s.user_id = $2 ORDER BY score DESC LIMIT $3",
                        base_query
                    );
                    let params: [&(dyn tokio_postgres::types::ToSql + Sync); 3] =
                        [&clean_query, user_id, &limit_i64];
                    client.query(&full_query, &params).await?
                }
            }
            TenantContext::Admin => {
                if let Some(sid) = source_id {
                    let full_query = format!(
                        "{} AND f.source_id = $2 ORDER BY score DESC LIMIT $3",
                        base_query
                    );
                    let params: [&(dyn tokio_postgres::types::ToSql + Sync); 3] =
                        [&clean_query, &sid, &limit_i64];
                    client.query(&full_query, &params).await?
                } else {
                    let full_query = format!("{} ORDER BY score DESC LIMIT $2", base_query);
                    let params: [&(dyn tokio_postgres::types::ToSql + Sync); 2] =
                        [&clean_query, &limit_i64];
                    client.query(&full_query, &params).await?
                }
            }
        };

        let total = rows.len();
        let results: Vec<SearchResult> = rows
            .iter()
            .map(|row| {
                let context_prefix: Option<String> = row.get("context_prefix");
                SearchResult {
                    chunk_id: row.get("chunk_id"),
                    file_path: row.get("file_path"),
                    content: row.get("content"),
                    snippet: None,
                    line_start: row.get("line_start"),
                    line_end: row.get("line_end"),
                    source_name: row.get("source_name"),
                    language: row.get("language"),
                    score: row.get("score"),
                    context_prefix,
                    location: None,
                    chunk_type: None, // Not fetched for keyword-only search
                    level: None,
                    parent_context: None,
                    external_hit_id: None,
                    successor_metadata: None,
                    score_explanation: None,
                    degradation: None,
                }
            })
            .collect();

        Ok(SearchResults {
            results,
            total,
            expanded_query: None,
            expansion_terms: vec![],
        })
    }

    /// Sprint 7.6: Semantic search with circuit breaker + semaphore tracking
    async fn semantic_search_with_embedding_cb(
        &self,
        embedding: &[f32],
        source_id: Option<i64>,
        limit: u64,
        tenant: &TenantContext,
    ) -> Result<Vec<(i64, f32)>> {
        // Sprint 7.3b: Acquire Qdrant semaphore permit before searching
        let _qdrant_permit =
            self.semaphores.qdrant.acquire().await.map_err(|_| {
                crate::error::AppError::Internal("Qdrant semaphore closed".to_string())
            })?;

        let start = std::time::Instant::now();
        let result = self
            .semantic_search_with_embedding(embedding, source_id, limit, tenant)
            .await;
        metrics::histogram!("mainrag_qdrant_semaphore_wait_ms")
            .record(start.elapsed().as_millis() as f64);

        match result {
            Ok(results) => {
                self.cb_tei_embed.record_success();
                self.cb_qdrant.record_success();
                Ok(results)
            }
            Err(e) => {
                // Determine which service failed based on error message
                let err_str = format!("{}", e);
                if err_str.contains("Qdrant") || err_str.contains("qdrant") {
                    self.cb_qdrant.record_failure();
                } else {
                    self.cb_tei_embed.record_failure();
                }
                Err(e)
            }
        }
    }

    /// Sprint 7.6: Rerank with circuit breaker + semaphore tracking
    async fn rerank_results_cb(
        &self,
        query: &str,
        results: &[SearchResult],
    ) -> Result<Vec<SearchResult>> {
        // Sprint 7.3b: Acquire rerank semaphore permit before reranking
        let _rerank_permit =
            self.semaphores.rerank.acquire().await.map_err(|_| {
                crate::error::AppError::Internal("Rerank semaphore closed".to_string())
            })?;

        let start = std::time::Instant::now();
        let result = self.rerank_results(query, results).await;
        metrics::histogram!("mainrag_gpu_semaphore_wait_ms", "service" => "rerank")
            .record(start.elapsed().as_millis() as f64);

        match result {
            Ok(r) => {
                self.cb_tei_rerank.record_success();
                Ok(r)
            }
            Err(e) => {
                self.cb_tei_rerank.record_failure();
                Err(e)
            }
        }
    }

    /// Rerank search results using BGE reranker
    async fn rerank_results(
        &self,
        query: &str,
        results: &[SearchResult],
    ) -> Result<Vec<SearchResult>> {
        if results.is_empty() {
            return Ok(vec![]);
        }

        // Limit candidates for reranking — top 30 is enough, beyond that quality gain is negligible
        let rerank_limit: usize = std::env::var("RERANK_CANDIDATE_LIMIT")
            .ok()
            .and_then(|v| v.parse().ok())
            .unwrap_or(30);
        let rerank_input = if results.len() > rerank_limit {
            &results[..rerank_limit]
        } else {
            results
        };

        // Extract content for reranking (safely truncate at char boundary)
        let texts: Vec<String> = rerank_input
            .iter()
            .map(|r| {
                // GTE reranker supports 8192 tokens (~24000 bytes).
                const RERANKER_MAX_BYTES: usize = 8000;
                let content_preview = if r.content.len() <= RERANKER_MAX_BYTES {
                    &r.content[..]
                } else {
                    let mut end = RERANKER_MAX_BYTES;
                    while end > 0 && !r.content.is_char_boundary(end) {
                        end -= 1;
                    }
                    &r.content[..end]
                };
                format!("{}: {}", r.file_path, content_preview)
            })
            .collect();

        // Call reranker
        let reranked_indices =
            self.reranker.rerank(query, texts).await.map_err(|e| {
                crate::error::AppError::Internal(format!("Reranking failed: {}", e))
            })?;

        // Sprint 7.4: Reorder results based on reranker scores,
        // preserving RRF score information via weighted combination
        let mut reranked_results = Vec::new();
        for (rerank_pos, idx) in reranked_indices.iter().enumerate() {
            if *idx < rerank_input.len() {
                let mut result = rerank_input[*idx].clone();
                let rerank_score = 1.0 / (rerank_pos as f32 + 1.0);
                let original_rrf = result.score;
                // Weighted combination: 70% reranker + 30% original RRF
                result.score = 0.7 * rerank_score + 0.3 * original_rrf;
                reranked_results.push(result);
            }
        }

        Ok(reranked_results)
    }
}

/// Compute cosine similarity between two embedding vectors.
/// Returns dot_product / (norm_a * norm_b), or 0.0 if either norm is zero.
fn cosine_similarity(a: &[f32], b: &[f32]) -> f32 {
    if a.len() != b.len() || a.is_empty() {
        return 0.0;
    }
    let mut dot = 0.0f32;
    let mut norm_a = 0.0f32;
    let mut norm_b = 0.0f32;
    for (va, vb) in a.iter().zip(b.iter()) {
        dot += va * vb;
        norm_a += va * va;
        norm_b += vb * vb;
    }
    let denom = norm_a.sqrt() * norm_b.sqrt();
    if denom == 0.0 {
        0.0
    } else {
        dot / denom
    }
}

/// Dynamic cosine similarity threshold based on chunk content length.
/// Shorter chunks need higher similarity to be considered duplicates (more sensitive).
fn cosine_threshold_for_length(content_len: usize) -> f32 {
    if content_len < 400 {
        0.95 // Short chunks: require very high similarity
    } else if content_len <= 1200 {
        0.92 // Medium chunks
    } else {
        0.88 // Long chunks: lower threshold (more content = more variance)
    }
}

/// Sprint 7.5: Deduplicate near-identical results using word-level Jaccard similarity
/// and cosine similarity (when embeddings are available).
///
/// Uses dynamic thresholds based on chunk length for cosine dedup.
/// Hybrid formula: `is_duplicate = jaccard > 0.4 || (has_embeddings && cosine > threshold)`
///
/// Keeps the highest-scored result from each group of near-duplicates.
/// Returns at most `limit` diverse results.
fn deduplicate_results(
    results: Vec<SearchResult>,
    limit: usize,
    embeddings: Option<&HashMap<i64, Vec<f32>>>,
) -> Vec<SearchResult> {
    use rustc_hash::FxHashSet;
    use std::collections::HashMap as StdHashMap;

    /// Jaccard threshold -- chunks with higher Jaccard are considered duplicates
    const JACCARD_THRESHOLD: f32 = 0.40;

    /// Max chunks per unique file_path — prevents one file from dominating results
    const MAX_CHUNKS_PER_FILE: usize = 3;

    /// Max chunks per source in final results — prevents one large repo from monopolizing.
    /// Set high enough that single-source queries (typical for code search) still work well,
    /// but low enough that mega-repos (kubernetes 391K chunks) don't push out everything.
    /// At limit=10: effectively allows 8 from one source + 2 from others.
    const MAX_CHUNKS_PER_SOURCE: usize = 8;

    // Extract word sets for each result (lowercase, alphanumeric tokens)
    let word_sets: Vec<FxHashSet<String>> = results
        .iter()
        .map(|r| {
            r.content
                .split(|c: char| !c.is_alphanumeric() && c != '_')
                .filter(|w| w.len() > 2)
                .map(|w| w.to_lowercase())
                .collect()
        })
        .collect();

    let mut kept: Vec<usize> = Vec::new(); // indices into results
    let mut dedup_count_jaccard: u32 = 0;
    let mut dedup_count_cosine: u32 = 0;
    let mut dedup_count_file: u32 = 0;
    let mut dedup_count_source: u32 = 0;
    let mut file_counts: StdHashMap<&str, usize> = StdHashMap::new();
    let mut source_counts: StdHashMap<&str, usize> = StdHashMap::new();

    for i in 0..results.len() {
        if kept.len() >= limit {
            break;
        }

        // Source-level dedup: prevent one large repo from monopolizing results
        let source_name = results[i].source_name.as_str();
        let source_count = source_counts.get(source_name).copied().unwrap_or(0);
        if source_count >= MAX_CHUNKS_PER_SOURCE {
            dedup_count_source += 1;
            continue;
        }

        // File-path dedup: limit chunks per file to increase diversity
        let file_path = results[i].file_path.as_str();
        let current_count = file_counts.get(file_path).copied().unwrap_or(0);
        if current_count >= MAX_CHUNKS_PER_FILE {
            dedup_count_file += 1;
            continue;
        }

        let mut is_dup_jaccard = false;
        let mut is_dup_cosine = false;

        for &j in &kept {
            // Jaccard check
            let intersection = word_sets[i].intersection(&word_sets[j]).count();
            let union = word_sets[i].union(&word_sets[j]).count();
            if union > 0 {
                let jaccard = intersection as f32 / union as f32;
                if jaccard > JACCARD_THRESHOLD {
                    is_dup_jaccard = true;
                    break;
                }
            }

            // Cosine check (only if embeddings available for both chunks)
            if let Some(emb_map) = embeddings {
                if let (Some(emb_i), Some(emb_j)) = (
                    emb_map.get(&results[i].chunk_id),
                    emb_map.get(&results[j].chunk_id),
                ) {
                    let cosine = cosine_similarity(emb_i, emb_j);
                    let threshold = cosine_threshold_for_length(results[i].content.len());
                    if cosine > threshold {
                        is_dup_cosine = true;
                        break;
                    }
                }
            }
        }

        if is_dup_jaccard {
            dedup_count_jaccard += 1;
        } else if is_dup_cosine {
            dedup_count_cosine += 1;
        } else {
            kept.push(i);
            *file_counts.entry(file_path).or_insert(0) += 1;
            *source_counts.entry(source_name).or_insert(0) += 1;
        }
    }

    if dedup_count_jaccard > 0 {
        metrics::counter!("mainrag_search_dedup_removed", "reason" => "jaccard")
            .increment(dedup_count_jaccard as u64);
    }
    if dedup_count_cosine > 0 {
        metrics::counter!("mainrag_search_dedup_removed", "reason" => "cosine")
            .increment(dedup_count_cosine as u64);
    }
    if dedup_count_file > 0 {
        metrics::counter!("mainrag_search_dedup_removed", "reason" => "file_path")
            .increment(dedup_count_file as u64);
    }
    if dedup_count_source > 0 {
        metrics::counter!("mainrag_search_dedup_removed", "reason" => "source")
            .increment(dedup_count_source as u64);
    }

    kept.into_iter().map(|i| results[i].clone()).collect()
}

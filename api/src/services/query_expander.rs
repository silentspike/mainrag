//! Query Expansion Service for MainRAG
//!
//! Expands search queries using semantic synonym lookup in Qdrant.
//! Supports both FTS query expansion (OR-based) and vector embedding averaging.

use std::collections::HashSet;
use std::sync::Arc;

use reqwest::Client;
use serde::{Deserialize, Serialize};

use crate::config::QdrantConfig;
use crate::error::{AppError, Result};
use crate::services::TeiClient;

/// Stopwords to filter out during expansion (DE + EN)
const STOPWORDS: &[&str] = &[
    // German
    "und", "oder", "der", "die", "das", "ein", "eine", "ist", "sind", "war", "waren",
    "für", "mit", "von", "zu", "bei", "nach", "über", "unter", "durch", "aus",
    // English
    "the", "a", "an", "and", "or", "is", "are", "was", "were", "be", "been",
    "for", "with", "of", "to", "at", "by", "from", "in", "on", "as", "it",
    // Code-related but too generic
    "function", "class", "method", "variable", "file", "code", "data",
];

/// Minimum similarity score to consider a synonym match
const MIN_SIMILARITY_THRESHOLD: f32 = 0.5;

/// Maximum number of synonyms to return per query
const MAX_SYNONYMS: usize = 5;

/// Weights for embedding averaging
const ORIGINAL_WEIGHT: f32 = 2.0;
const SYNONYM_WEIGHT: f32 = 1.0;

/// Synonym match from Qdrant
#[derive(Debug, Clone)]
pub struct SynonymMatch {
    pub term: String,
    pub aliases: Vec<String>,
    pub category: String,
    pub score: f32,
}

/// Expanded query result
#[derive(Debug, Clone)]
pub struct ExpandedQuery {
    /// Original query
    pub original: String,
    /// Expanded FTS query (original OR synonym1 OR synonym2...)
    pub fts_query: String,
    /// Weighted average embedding for vector search
    pub embedding: Vec<f32>,
    /// Matched synonyms for debugging/logging
    pub synonyms: Vec<SynonymMatch>,
}

/// Query Expansion Service
pub struct QueryExpander {
    client: Client,
    tei: Arc<TeiClient>,
    qdrant_url: String,
    api_key: String,
    synonyms_collection: String,
    enabled: bool,
    /// Sprint 7.2: Query embedding cache (avoids repeated TEI calls for same query)
    /// Key: query string, Value: embedding vector
    embedding_cache: moka::sync::Cache<String, Vec<f32>>,
    /// B8/Step6: Synonym embedding cache (avoids repeated TEI calls for same synonym terms)
    /// Key: synonym text ("term alias1 alias2"), Value: embedding vector
    synonym_embedding_cache: moka::sync::Cache<String, Vec<f32>>,
}

#[derive(Debug, Serialize)]
struct QdrantSearchRequest {
    vector: Vec<f32>,
    limit: usize,
    with_payload: bool,
}

#[derive(Debug, Deserialize)]
struct QdrantSearchResponse {
    result: Vec<QdrantSearchResult>,
}

#[derive(Debug, Deserialize)]
struct QdrantSearchResult {
    #[allow(dead_code)]
    id: serde_json::Value,
    score: f32,
    payload: Option<SynonymPayload>,
}

#[derive(Debug, Deserialize, Clone)]
struct SynonymPayload {
    term: String,
    aliases: Vec<String>,
    category: String,
    #[allow(dead_code)]
    language: Option<String>,
}

impl QueryExpander {
    /// Create a new QueryExpander
    ///
    /// # Arguments
    /// * `config` - Qdrant configuration
    /// * `tei` - TEI embedding client
    /// * `enabled` - Whether query expansion is enabled (feature flag)
    pub fn new(config: &QdrantConfig, tei: Arc<TeiClient>, enabled: bool) -> Self {
        // Sprint 7.2: Query embedding cache — 10k entries, 2h TTL
        let embedding_cache = moka::sync::Cache::builder()
            .max_capacity(10_000)
            .time_to_live(std::time::Duration::from_secs(7200))
            .build();

        // Step 6: Synonym embedding cache — 5k entries, 4h TTL (synonyms change rarely)
        let synonym_embedding_cache = moka::sync::Cache::builder()
            .max_capacity(5_000)
            .time_to_live(std::time::Duration::from_secs(14400))
            .build();

        Self {
            client: Client::new(),
            tei,
            qdrant_url: config.url.clone(),
            api_key: config.api_key.clone().unwrap_or_default(),
            synonyms_collection: config
                .synonyms_collection
                .clone()
                .unwrap_or_else(|| "synonyms_v1".to_string()),
            enabled,
            embedding_cache,
            synonym_embedding_cache,
        }
    }

    /// Check if query expansion is enabled
    pub fn is_enabled(&self) -> bool {
        self.enabled
    }

    /// Expand a query with synonyms
    ///
    /// FTS-only fallback when TEI is unavailable (degraded mode)
    /// Returns ExpandedQuery with empty embedding — only usable for FTS search
    /// Still applies camelCase splitting for better Java code search.
    pub async fn expand_fts_only(&self, query: &str) -> ExpandedQuery {
        let fts_query = self.build_fts_query(query, &[]);
        ExpandedQuery {
            original: query.to_string(),
            fts_query,
            embedding: vec![],
            synonyms: vec![],
        }
    }

    /// Returns ExpandedQuery with:
    /// - FTS query expanded with OR-joined synonyms
    /// - Weighted average embedding for vector search
    ///
    /// # Arguments
    /// * `query` - Search query text
    /// * `agent_id` - Optional agent/user ID for tenant-scoped cache keys (prevents cross-agent leakage)
    pub async fn expand(&self, query: &str, agent_id: Option<&str>) -> Result<ExpandedQuery> {
        // Sprint 7.2: Check embedding cache first
        // K2: Tenant-scoped cache key to prevent cross-agent leakage
        let cache_key = format!("{}:{}", agent_id.unwrap_or("global"), query.to_lowercase());

        // If disabled, return original query with standard embedding
        if !self.enabled {
            let embedding = if let Some(cached) = self.embedding_cache.get(&cache_key) {
                metrics::counter!("mainrag_query_embedding_cache_hits").increment(1);
                cached
            } else {
                metrics::counter!("mainrag_query_embedding_cache_misses").increment(1);
                let emb = self.tei.embed(query).await?;
                self.embedding_cache.insert(cache_key.clone(), emb.clone());
                emb
            };
            return Ok(ExpandedQuery {
                original: query.to_string(),
                fts_query: query.to_string(),
                embedding,
                synonyms: vec![],
            });
        }

        // Sprint 7.2: Generate query embedding using TEI, with cache
        let query_embedding = if let Some(cached) = self.embedding_cache.get(&cache_key) {
            metrics::counter!("mainrag_query_embedding_cache_hits").increment(1);
            cached
        } else {
            metrics::counter!("mainrag_query_embedding_cache_misses").increment(1);
            let emb = self.tei.embed(query).await?;
            self.embedding_cache.insert(cache_key, emb.clone());
            emb
        };

        // Search for synonyms in Qdrant
        let synonyms = self.find_synonyms(&query_embedding).await?;

        // Build FTS query with OR expansion
        let fts_query = self.build_fts_query(query, &synonyms);

        // Build weighted average embedding
        let expanded_embedding = self.build_weighted_embedding(&query_embedding, &synonyms).await?;

        Ok(ExpandedQuery {
            original: query.to_string(),
            fts_query,
            embedding: expanded_embedding,
            synonyms,
        })
    }

    /// Find synonyms in Qdrant using the query embedding
    async fn find_synonyms(&self, query_embedding: &[f32]) -> Result<Vec<SynonymMatch>> {
        let url = format!(
            "{}/collections/{}/points/search",
            self.qdrant_url, self.synonyms_collection
        );

        let request = QdrantSearchRequest {
            vector: query_embedding.to_vec(),
            limit: MAX_SYNONYMS * 2, // Fetch more, filter later
            with_payload: true,
        };

        let response = self
            .client
            .post(&url)
            .header("api-key", &self.api_key)
            .json(&request)
            .send()
            .await
            .map_err(|e| AppError::Qdrant(format!("Synonym search failed: {}", e)))?;

        if !response.status().is_success() {
            let status = response.status();
            let body = response.text().await.unwrap_or_default();
            // Don't fail the whole search, just return empty synonyms
            tracing::warn!("Synonym lookup failed ({}): {}", status, body);
            return Ok(vec![]);
        }

        let search_response: QdrantSearchResponse = response
            .json()
            .await
            .map_err(|e| AppError::Qdrant(format!("Failed to parse synonym response: {}", e)))?;

        // Filter by score threshold and convert to SynonymMatch
        let matches: Vec<SynonymMatch> = search_response
            .result
            .into_iter()
            .filter(|r| r.score >= MIN_SIMILARITY_THRESHOLD)
            .filter_map(|r| {
                r.payload.map(|p| SynonymMatch {
                    term: p.term,
                    aliases: p.aliases,
                    category: p.category,
                    score: r.score,
                })
            })
            .take(MAX_SYNONYMS)
            .collect();

        Ok(matches)
    }

    /// Build expanded FTS query with OR-joined synonyms and camelCase splitting.
    ///
    /// Example: "fehler" -> "fehler OR error OR bug OR fault"
    /// Example: "createEmptyClip" -> "createemptyclip | create | empty | clip"
    fn build_fts_query(&self, original: &str, synonyms: &[SynonymMatch]) -> String {
        // Tokenize original query
        let original_terms: Vec<&str> = original
            .split_whitespace()
            .filter(|t| !self.is_stopword(t))
            .collect();

        if original_terms.is_empty() {
            return original.to_string();
        }

        // Collect expansion terms (deduplicated)
        let mut expansion_terms: HashSet<String> = HashSet::new();

        // Add original terms + camelCase splits
        for term in &original_terms {
            let sanitized = self.sanitize_fts_term(term);
            if !sanitized.is_empty() {
                expansion_terms.insert(sanitized);
            }
            // CamelCase/PascalCase split: "createEmptyClip" -> ["create", "empty", "clip"]
            let splits = split_camel_case(term);
            for part in &splits {
                let lower = part.to_lowercase();
                if lower.len() >= 2 && !self.is_stopword(&lower) {
                    expansion_terms.insert(lower);
                }
            }
        }

        // Add synonym terms and aliases (limit to 1 alias per synonym to avoid query dilution)
        for syn in synonyms {
            let syn_term = self.sanitize_fts_term(&syn.term);
            if !syn_term.is_empty() {
                expansion_terms.insert(syn_term);
            }
            for alias in syn.aliases.iter().take(1) {
                let sanitized = self.sanitize_fts_term(alias);
                if !sanitized.is_empty() && !self.is_stopword(&sanitized) {
                    expansion_terms.insert(sanitized);
                }
            }
        }

        // Build OR query using PostgreSQL tsquery syntax (| for OR)
        // Sort for stable, deterministic ordering (HashSet iteration is random)
        let mut terms: Vec<String> = expansion_terms.into_iter()
            .filter(|t| !t.is_empty())
            .collect();
        terms.sort();
        if terms.len() == 1 {
            terms[0].clone()
        } else {
            // Use | separator for to_tsquery compatibility
            terms.join(" | ")
        }
    }

    /// Build weighted average embedding from original + synonyms
    ///
    /// Formula: (original * 2.0 + syn1 * 1.0 + syn2 * 1.0 + ...) / total_weight
    /// Then L2-normalized
    async fn build_weighted_embedding(
        &self,
        original: &[f32],
        synonyms: &[SynonymMatch],
    ) -> Result<Vec<f32>> {
        if synonyms.is_empty() {
            // No synonyms, just return original (already normalized by TEI)
            return Ok(original.to_vec());
        }

        // Collect synonym texts for embedding
        let synonym_texts: Vec<String> = synonyms
            .iter()
            .map(|s| format!("{} {}", s.term, s.aliases.join(" ")))
            .collect();

        // Step 6: Check synonym embedding cache, only embed uncached synonyms
        let mut cached_embeddings: Vec<(usize, Vec<f32>)> = Vec::new();
        let mut uncached_texts: Vec<(usize, &str)> = Vec::new();

        for (idx, text) in synonym_texts.iter().enumerate() {
            if let Some(cached) = self.synonym_embedding_cache.get(text) {
                cached_embeddings.push((idx, cached));
                metrics::counter!("mainrag_synonym_embedding_cache_hits").increment(1);
            } else {
                uncached_texts.push((idx, text.as_str()));
                metrics::counter!("mainrag_synonym_embedding_cache_misses").increment(1);
            }
        }

        // Sprint 3.3: Batch embed only uncached synonyms
        let mut all_embeddings: Vec<(usize, Vec<f32>)> = cached_embeddings;

        if !uncached_texts.is_empty() {
            let texts_to_embed: Vec<&str> = uncached_texts.iter().map(|(_, t)| *t).collect();
            match self.tei.embed_batch(&texts_to_embed).await {
                Ok(embeddings) => {
                    for (i, embedding) in embeddings.into_iter().enumerate() {
                        let (idx, text) = &uncached_texts[i];
                        self.synonym_embedding_cache.insert(text.to_string(), embedding.clone());
                        all_embeddings.push((*idx, embedding));
                    }
                }
                Err(e) => {
                    tracing::warn!("Failed to batch-embed synonyms: {}", e);
                    // Continue with original + cached embeddings only
                }
            }
        }

        let mut weighted_sum: Vec<f32> = original.iter().map(|v| v * ORIGINAL_WEIGHT).collect();
        let mut total_weight = ORIGINAL_WEIGHT;

        for (_idx, syn_embedding) in &all_embeddings {
            for (i, v) in syn_embedding.iter().enumerate() {
                if i < weighted_sum.len() {
                    weighted_sum[i] += v * SYNONYM_WEIGHT;
                }
            }
            total_weight += SYNONYM_WEIGHT;
        }

        // Divide by total weight
        for v in &mut weighted_sum {
            *v /= total_weight;
        }

        // L2 normalize
        let norm: f32 = weighted_sum.iter().map(|v| v * v).sum::<f32>().sqrt();
        if norm > 0.0 {
            for v in &mut weighted_sum {
                *v /= norm;
            }
        }

        Ok(weighted_sum)
    }

    /// Check if a term is a stopword
    fn is_stopword(&self, term: &str) -> bool {
        let lower = term.to_lowercase();
        STOPWORDS.contains(&lower.as_str())
    }

    /// Sanitize term for FTS (remove special chars that break tsquery)
    fn sanitize_fts_term(&self, term: &str) -> String {
        term.chars()
            .filter(|c| c.is_alphanumeric() || *c == '_' || *c == '-')
            .collect::<String>()
            .to_lowercase()
    }
}

/// Split camelCase/PascalCase identifiers into component words.
///
/// Examples:
/// - "createEmptyClip" -> ["create", "Empty", "Clip"]
/// - "ClipLauncherSlot" -> ["Clip", "Launcher", "Slot"]
/// - "isControlSurfaceThread" -> ["is", "Control", "Surface", "Thread"]
/// - "r3B" -> ["r3", "B"] (short/obfuscated — caller filters by length)
/// - "simple" -> [] (no splits found)
pub fn split_camel_case(s: &str) -> Vec<String> {
    let mut parts = Vec::new();
    let mut current = String::new();
    let chars: Vec<char> = s.chars().collect();

    for i in 0..chars.len() {
        let c = chars[i];
        let is_split = if i > 0 {
            // Split on: lowercase→uppercase, letter→digit, digit→letter
            let prev = chars[i - 1];
            (prev.is_lowercase() && c.is_uppercase())
                || (prev.is_alphabetic() && c.is_ascii_digit())
                || (prev.is_ascii_digit() && c.is_alphabetic())
        } else {
            false
        };

        if is_split && !current.is_empty() {
            parts.push(current.clone());
            current.clear();
        }
        current.push(c);
    }
    if !current.is_empty() {
        parts.push(current);
    }

    // Only return splits if we actually split something
    if parts.len() <= 1 {
        return vec![];
    }
    parts
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_is_stopword() {
        let expander = create_test_expander();
        assert!(expander.is_stopword("und"));
        assert!(expander.is_stopword("the"));
        assert!(expander.is_stopword("THE"));
        assert!(!expander.is_stopword("fehler"));
        assert!(!expander.is_stopword("error"));
    }

    #[test]
    fn test_sanitize_fts_term() {
        let expander = create_test_expander();
        assert_eq!(expander.sanitize_fts_term("Hello!"), "hello");
        assert_eq!(expander.sanitize_fts_term("foo_bar"), "foo_bar");
        assert_eq!(expander.sanitize_fts_term("Test@123"), "test123");
    }

    #[test]
    fn test_build_fts_query_no_synonyms() {
        let expander = create_test_expander();
        let synonyms: Vec<SynonymMatch> = vec![];
        let result = expander.build_fts_query("error handling", &synonyms);
        assert!(result.contains("error"));
        assert!(result.contains("handling"));
    }

    #[test]
    fn test_build_fts_query_with_synonyms() {
        let expander = create_test_expander();
        let synonyms = vec![SynonymMatch {
            term: "fehler".to_string(),
            aliases: vec!["error".to_string(), "bug".to_string()],
            category: "programming".to_string(),
            score: 0.8,
        }];
        let result = expander.build_fts_query("fehler", &synonyms);
        assert!(result.contains("fehler"));
        assert!(result.contains("error"));
        // Note: "bug" is the 2nd alias, but build_fts_query takes only 1 alias per synonym
        // to avoid query dilution. So "bug" may not be present.
        // Uses PostgreSQL tsquery OR syntax (|)
        assert!(result.contains(" | "));
    }

    #[test]
    fn test_split_camel_case() {
        assert_eq!(split_camel_case("createEmptyClip"), vec!["create", "Empty", "Clip"]);
        assert_eq!(split_camel_case("ClipLauncherSlot"), vec!["Clip", "Launcher", "Slot"]);
        assert_eq!(split_camel_case("isControlSurfaceThread"), vec!["is", "Control", "Surface", "Thread"]);
        assert_eq!(split_camel_case("r3B"), vec!["r", "3", "B"]);  // letter→digit, digit→letter
        assert!(split_camel_case("simple").is_empty()); // no split
        assert!(split_camel_case("x").is_empty()); // too short
        assert_eq!(split_camel_case("getTrack2Bank"), vec!["get", "Track", "2", "Bank"]);
    }

    #[test]
    fn test_build_fts_query_with_camel_case() {
        let expander = create_test_expander();
        let synonyms: Vec<SynonymMatch> = vec![];
        let result = expander.build_fts_query("createEmptyClip", &synonyms);
        // Should contain both the full term and the splits
        assert!(result.contains("createemptyclip"));
        assert!(result.contains("create"));
        assert!(result.contains("empty"));
        assert!(result.contains("clip"));
    }

    fn create_test_expander() -> QueryExpander {
        use crate::config::TeiConfig;

        let tei_config = TeiConfig {
            url: "http://localhost:8080".to_string(),
            reranker_url: None,
            model: None,
            embedding_dim: None,
        };

        // Create a minimal test expander (won't make real network calls)
        QueryExpander {
            client: Client::new(),
            tei: Arc::new(TeiClient::new(&tei_config)),
            qdrant_url: "http://localhost:6333".to_string(),
            api_key: "test".to_string(),
            synonyms_collection: "synonyms_v1".to_string(),
            enabled: true,
            embedding_cache: moka::sync::Cache::builder().max_capacity(100).build(),
            synonym_embedding_cache: moka::sync::Cache::builder().max_capacity(100).build(),
        }
    }
}

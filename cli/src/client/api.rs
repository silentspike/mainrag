use anyhow::{anyhow, Context, Result};
use reqwest::Client;
use serde::{Deserialize, Serialize};
use std::fs;
use std::path::PathBuf;

// ============================================================================
// Request/Response Types
// ============================================================================

#[derive(Serialize, Deserialize, Debug, Clone)]
pub struct SearchResult {
    pub chunk_id: i64,
    pub file_path: String,
    pub content: String,
    /// Highlighted snippet showing match context (with **term** markers)
    #[serde(skip_serializing_if = "Option::is_none")]
    pub snippet: Option<String>,
    pub line_start: i32,
    pub line_end: i32,
    pub source_name: String,
    pub language: Option<String>,
    pub score: f32,
    /// CCH (Contextual Chunk Header) prefix for hierarchical context
    /// Format: "[source] path > parent_context"
    #[serde(skip_serializing_if = "Option::is_none", default)]
    pub context_prefix: Option<String>,
    /// Compact location reference (e.g., "src/main.rs:10-25")
    #[serde(skip_serializing_if = "Option::is_none", default)]
    pub location: Option<String>,
    /// Chunk type (code, conversation, function, class, etc.)
    #[serde(skip_serializing_if = "Option::is_none", default)]
    pub chunk_type: Option<String>,
    /// Hierarchy level
    #[serde(default)]
    pub level: Option<i32>,
    /// Parent context (e.g., class signature for a function chunk)
    #[serde(skip_serializing_if = "Option::is_none", default)]
    pub parent_context: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none", default)]
    pub external_hit_id: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none", default)]
    pub successor_metadata: Option<serde_json::Value>,
    #[serde(skip_serializing_if = "Option::is_none", default)]
    pub score_explanation: Option<serde_json::Value>,
    #[serde(skip_serializing_if = "Option::is_none", default)]
    pub degradation: Option<serde_json::Value>,
}

#[derive(Serialize, Deserialize, Debug, Clone)]
pub struct SearchResponse {
    /// LLM context explaining how to interpret the results
    #[serde(default)]
    pub llm_context: Option<String>,
    pub results: Vec<SearchResult>,
    pub total: usize,
    pub took_ms: u64,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub quality_tier: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub reranked: Option<bool>,
    #[serde(skip_serializing_if = "Option::is_none", default)]
    pub read_path: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none", default)]
    pub generation: Option<i64>,
    #[serde(skip_serializing_if = "Option::is_none", default)]
    pub fully_scored_views: Option<i64>,
}

#[derive(Debug, Clone, Default)]
pub struct SearchOptions<'a> {
    pub read_path: Option<&'a str>,
    pub generation: Option<&'a str>,
    pub path_prefix: Option<&'a str>,
    pub occurred_from: Option<&'a str>,
    pub occurred_to: Option<&'a str>,
    pub role: Option<&'a str>,
    pub graph_profile: Option<&'a str>,
    pub semantic_profile: Option<&'a str>,
    pub rerank_profile: Option<&'a str>,
}

#[derive(Serialize, Deserialize, Debug, Clone)]
pub struct CreateSourceRequest {
    #[serde(skip_serializing_if = "Option::is_none")]
    pub name: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub source_type: Option<String>, // fs, git, web (auto-detected if not provided)
    pub path: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub config: Option<serde_json::Value>,
}

#[derive(Serialize, Deserialize, Debug, Clone)]
pub struct Source {
    pub id: i64,
    pub name: String,
    pub source_type: String,
    pub path: String,
    #[serde(default)]
    pub config: Option<serde_json::Value>,
    pub file_count: i32,
    pub total_size: i64,
    pub last_synced: Option<String>,
    pub created_at: String,
    #[serde(default)]
    pub updated_at: Option<String>,
    #[serde(default)]
    pub chunk_count: Option<i64>,
}

#[derive(Deserialize, Debug, Clone)]
pub struct SourcesResponse {
    pub sources: Vec<Source>,
    pub total: usize,
}

#[derive(Deserialize, Debug, Clone)]
pub struct AuthResponse {
    pub token: String,
    #[allow(dead_code)]
    pub token_type: String,
    pub expires_in: i64,
    pub user: AuthUser,
}

#[derive(Deserialize, Debug, Clone)]
pub struct AuthUser {
    pub id: String,
    pub username: String,
    pub email: String,
    pub is_admin: bool,
}

#[derive(Deserialize, Debug, Clone)]
pub struct StatsResponse {
    #[serde(rename = "sources")]
    pub sources_count: i64,
    #[serde(rename = "files")]
    pub files_count: i64,
    #[serde(rename = "chunks")]
    pub chunks_count: i64,
    pub total_size_bytes: i64,
    #[serde(default)]
    #[allow(dead_code)]
    pub postgres_size: Option<String>,
}

#[derive(Deserialize, Debug, Clone)]
pub struct HealthResponse {
    pub status: String,
    pub services: HealthServices,
}

#[derive(Deserialize, Debug, Clone)]
pub struct HealthServices {
    pub postgres: bool,
    pub qdrant: bool,
    pub tei: bool,
}

#[derive(Deserialize, Debug, Clone)]
pub struct SyncSourceResponse {
    pub source_id: i64,
    pub status: String,
    pub stats: SyncStats,
    #[serde(default)]
    #[allow(dead_code)]
    pub error_details: Vec<String>,
}

#[derive(Deserialize, Debug, Clone)]
pub struct SyncStats {
    pub files_processed: i64,
    pub chunks_created: i64,
    #[serde(default)]
    pub embeddings_generated: i64,
    #[serde(default)]
    #[allow(dead_code)]
    pub errors: i64,
}

#[derive(Deserialize, Debug, Clone)]
pub struct SourceDeletionStats {
    pub chunks: i64,
    pub symbols: i64,
    pub call_graph: i64,
    pub qdrant_vectors: i64,
}

// ============================================================================
// Main API Client
// ============================================================================

pub struct ApiClient {
    client: Client,
    base_url: String,
    token: Option<String>,
}

impl ApiClient {
    /// Create new API client
    pub fn new(base_url: &str) -> Result<Self> {
        // Long timeout: sync operations can run for hours on large sources (100GB+, 24k files)
        let client = Client::builder()
            .timeout(std::time::Duration::from_secs(24 * 3600)) // 24h
            .build()?;
        Ok(ApiClient {
            client,
            base_url: base_url.trim_end_matches('/').to_string(),
            token: None,
        })
    }

    /// Get base URL
    pub fn base_url(&self) -> &str {
        &self.base_url
    }

    /// Raw authenticated GET request, returns response body as string
    pub async fn raw_get(&self, url: &str) -> Result<String> {
        let response = self
            .client
            .get(url)
            .bearer_auth(self.token.as_deref().unwrap_or(""))
            .send()
            .await
            .context("raw_get failed")?;
        if !response.status().is_success() {
            return Err(anyhow!("HTTP {}", response.status()));
        }
        response.text().await.context("read response body")
    }

    /// Load token from config file if exists
    pub fn load_token_from_file() -> Option<String> {
        let config_dir =
            directories::ProjectDirs::from("", "", "mainrag").map(|d| d.config_dir().to_path_buf());

        if let Some(dir) = config_dir {
            let token_file = dir.join("token");
            if let Ok(token) = fs::read_to_string(&token_file) {
                return Some(token.trim().to_string());
            }
        }
        None
    }

    /// Save token to config file
    pub fn save_token_to_file(&self, token: &str) -> Result<()> {
        let config_dir = directories::ProjectDirs::from("", "", "mainrag")
            .ok_or_else(|| anyhow!("Could not determine config directory"))?
            .config_dir()
            .to_path_buf();

        fs::create_dir_all(&config_dir)?;
        let token_path = config_dir.join("token");
        fs::write(&token_path, token)?;
        // Sprint 4.5: Set token file permissions to 0o600 (owner-only read/write)
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            fs::set_permissions(&token_path, fs::Permissions::from_mode(0o600))?;
        }
        Ok(())
    }

    /// Delete token file
    pub fn delete_token_file() -> Result<()> {
        let config_dir = directories::ProjectDirs::from("", "", "mainrag")
            .ok_or_else(|| anyhow!("Could not determine config directory"))?
            .config_dir()
            .to_path_buf();

        let token_file = config_dir.join("token");
        if token_file.exists() {
            fs::remove_file(token_file)?;
        }
        Ok(())
    }

    /// Set authentication token
    pub fn set_token(&mut self, token: String) {
        self.token = Some(token);
    }

    /// Get authentication token
    pub fn token(&self) -> Option<&str> {
        self.token.as_deref()
    }

    // ========================================================================
    // Health & Status
    // ========================================================================

    pub async fn health(&self) -> Result<HealthResponse> {
        let url = format!("{}/health", self.base_url);
        let response = self
            .client
            .get(&url)
            .send()
            .await
            .context("Failed to connect to API server")?;

        if !response.status().is_success() {
            return Err(anyhow!("Health check failed: {}", response.status()));
        }

        response
            .json::<HealthResponse>()
            .await
            .context("Failed to parse health response")
    }

    pub async fn stats(&self) -> Result<StatsResponse> {
        let url = format!("{}/api/v1/admin/stats", self.base_url);
        let response = self
            .client
            .get(&url)
            .bearer_auth(self.token.as_deref().unwrap_or(""))
            .send()
            .await
            .context("Failed to fetch stats")?;

        if response.status() == 401 {
            return Err(anyhow!(
                "Unauthorized. Please login first with: mainrag login"
            ));
        }

        if !response.status().is_success() {
            return Err(anyhow!("Stats request failed: {}", response.status()));
        }

        response
            .json::<StatsResponse>()
            .await
            .context("Failed to parse stats response")
    }

    // ========================================================================
    // Search
    // ========================================================================

    pub async fn search(
        &self,
        query: &str,
        mode: &str,
        limit: u32,
        source: Option<&str>,
    ) -> Result<SearchResponse> {
        self.search_with_options(query, mode, limit, source, &SearchOptions::default())
            .await
    }

    pub async fn search_with_options(
        &self,
        query: &str,
        mode: &str,
        limit: u32,
        source: Option<&str>,
        options: &SearchOptions<'_>,
    ) -> Result<SearchResponse> {
        // Use keyword endpoint for keyword mode, hybrid for others
        let endpoint = if mode == "keyword" {
            "search/keyword"
        } else {
            "search"
        };
        let url = format!("{}/api/v1/{}", self.base_url, endpoint);

        // Build JSON request body (matches API's SearchRequest struct)
        let mut body = serde_json::json!({
            "query": query,
            "limit": limit,
        });

        // Add source_id if provided (resolve name to ID if needed)
        if let Some(s) = source {
            let source_id = if let Ok(id) = s.parse::<i64>() {
                id
            } else {
                self.get_source_id_by_name(s).await?
            };
            body["source_id"] = serde_json::json!(source_id);
        }

        for (name, value) in [
            ("read_path", options.read_path),
            ("generation", options.generation),
            ("path_prefix", options.path_prefix),
            ("occurred_from", options.occurred_from),
            ("occurred_to", options.occurred_to),
            ("role", options.role),
            ("graph_profile", options.graph_profile),
            ("semantic_profile", options.semantic_profile),
            ("rerank_profile", options.rerank_profile),
        ] {
            if let Some(value) = value {
                body[name] = serde_json::json!(value);
            }
        }

        // Add quality tier based on mode
        if mode == "balanced" || mode == "deep" || mode == "verified" {
            body["quality"] = serde_json::json!(mode);
        }

        let response = self
            .client
            .post(&url)
            .json(&body)
            .bearer_auth(self.token.as_deref().unwrap_or(""))
            .send()
            .await
            .context("Failed to execute search")?;

        if !response.status().is_success() {
            let error_text = response
                .text()
                .await
                .unwrap_or_else(|_| "Unknown error".to_string());
            return Err(anyhow!("Search failed: {}", error_text));
        }

        response
            .json::<SearchResponse>()
            .await
            .context("Failed to parse search results")
    }

    // ========================================================================
    // Sources
    // ========================================================================

    pub async fn list_sources(&self) -> Result<SourcesResponse> {
        // Use public sources endpoint (works without auth)
        let url = format!("{}/api/v1/sources", self.base_url);

        let mut req = self.client.get(&url);
        // Add auth if available (for RLS-filtered results)
        if let Some(token) = &self.token {
            req = req.bearer_auth(token);
        }

        let response = req.send().await.context("Failed to fetch sources")?;

        if !response.status().is_success() {
            return Err(anyhow!("Failed to list sources: {}", response.status()));
        }

        response
            .json::<SourcesResponse>()
            .await
            .context("Failed to parse sources response")
    }

    pub async fn create_source(&self, req: CreateSourceRequest) -> Result<Source> {
        let url = format!("{}/api/v1/admin/sources", self.base_url);

        let response = self
            .client
            .post(&url)
            .bearer_auth(self.token.as_deref().unwrap_or(""))
            .json(&req)
            .send()
            .await
            .context("Failed to create source")?;

        if response.status() == 401 {
            return Err(anyhow!(
                "Unauthorized. Please login first with: mainrag login"
            ));
        }

        if !response.status().is_success() {
            let error_text = response
                .text()
                .await
                .unwrap_or_else(|_| "Unknown error".to_string());
            return Err(anyhow!("Failed to create source: {}", error_text));
        }

        response
            .json::<Source>()
            .await
            .context("Failed to parse source response")
    }

    /// Resolve a source name to its ID
    async fn get_source_id_by_name(&self, source_name: &str) -> Result<i64> {
        let sources = self.list_sources().await?;

        // Try exact match first
        if let Some(source) = sources.sources.iter().find(|s| s.name == source_name) {
            return Ok(source.id);
        }

        // Try case-insensitive match
        if let Some(source) = sources
            .sources
            .iter()
            .find(|s| s.name.eq_ignore_ascii_case(source_name))
        {
            return Ok(source.id);
        }

        // Try parsing as ID directly
        if let Ok(id) = source_name.parse::<i64>() {
            if sources.sources.iter().any(|s| s.id == id) {
                return Ok(id);
            }
        }

        Err(anyhow!(
            "Source '{}' not found. Use 'mainrag source list' to see available sources.",
            source_name
        ))
    }

    pub async fn sync_source(&self, source_name: &str) -> Result<SyncSourceResponse> {
        // Resolve name to ID
        let source_id = self.get_source_id_by_name(source_name).await?;

        let url = format!("{}/api/v1/admin/sources/{}/sync", self.base_url, source_id);

        let response = self
            .client
            .post(&url)
            .bearer_auth(self.token.as_deref().unwrap_or(""))
            .send()
            .await
            .context("Failed to sync source")?;

        if response.status() == 401 {
            return Err(anyhow!(
                "Unauthorized. Please login first with: mainrag login"
            ));
        }

        if !response.status().is_success() {
            let error_text = response
                .text()
                .await
                .unwrap_or_else(|_| "Unknown error".to_string());
            return Err(anyhow!("Failed to sync source: {}", error_text));
        }

        response
            .json::<SyncSourceResponse>()
            .await
            .context("Failed to parse sync response")
    }

    /// Sync specific files incrementally (for watch mode)
    /// This is much faster than full sync as it only processes the specified files.
    pub async fn sync_files(
        &self,
        source_name: &str,
        files: &[PathBuf],
    ) -> Result<SyncSourceResponse> {
        // Resolve name to ID
        let source_id = self.get_source_id_by_name(source_name).await?;

        let url = format!(
            "{}/api/v1/admin/sources/{}/sync-files",
            self.base_url, source_id
        );

        // Convert paths to strings
        let file_paths: Vec<String> = files
            .iter()
            .map(|p| p.to_string_lossy().to_string())
            .collect();

        let body = serde_json::json!({
            "files": file_paths
        });

        let response = self
            .client
            .post(&url)
            .bearer_auth(self.token.as_deref().unwrap_or(""))
            .json(&body)
            .send()
            .await
            .context("Failed to sync files")?;

        if response.status() == 401 {
            return Err(anyhow!(
                "Unauthorized. Please login first with: mainrag login"
            ));
        }

        if !response.status().is_success() {
            let error_text = response
                .text()
                .await
                .unwrap_or_else(|_| "Unknown error".to_string());
            return Err(anyhow!("Failed to sync files: {}", error_text));
        }

        response
            .json::<SyncSourceResponse>()
            .await
            .context("Failed to parse sync response")
    }

    pub async fn delete_source(&self, source_name: &str) -> Result<()> {
        // Resolve name to ID
        let source_id = self.get_source_id_by_name(source_name).await?;

        let url = format!("{}/api/v1/admin/sources/{}", self.base_url, source_id);

        let response = self
            .client
            .delete(&url)
            .bearer_auth(self.token.as_deref().unwrap_or(""))
            .send()
            .await
            .context("Failed to delete source")?;

        if response.status() == 401 {
            return Err(anyhow!(
                "Unauthorized. Please login first with: mainrag login"
            ));
        }

        if !response.status().is_success() {
            return Err(anyhow!("Failed to delete source: {}", response.status()));
        }

        Ok(())
    }

    /// Get detailed stats for a source before deletion
    /// Returns chunk count, symbol count, call-graph entries, and qdrant vectors
    pub async fn get_source_deletion_stats(
        &self,
        source_name: &str,
    ) -> Result<SourceDeletionStats> {
        let source_id = self.get_source_id_by_name(source_name).await?;

        let url = format!("{}/api/v1/admin/sources/{}/stats", self.base_url, source_id);

        let response = self
            .client
            .get(&url)
            .bearer_auth(self.token.as_deref().unwrap_or(""))
            .send()
            .await
            .context("Failed to get source stats")?;

        if !response.status().is_success() {
            // Return zeroed stats if endpoint doesn't exist yet
            return Ok(SourceDeletionStats {
                chunks: 0,
                symbols: 0,
                call_graph: 0,
                qdrant_vectors: 0,
            });
        }

        response
            .json::<SourceDeletionStats>()
            .await
            .context("Failed to parse source stats")
    }

    // ========================================================================
    // Authentication
    // ========================================================================

    pub async fn login(&self, username: &str, password: &str) -> Result<AuthResponse> {
        let url = format!("{}/api/v1/auth/login", self.base_url);

        let req = serde_json::json!({
            "username": username,
            "password": password,
        });

        let response = self
            .client
            .post(&url)
            .json(&req)
            .send()
            .await
            .context("Failed to connect to login endpoint")?;

        if response.status() == 401 {
            return Err(anyhow!("Invalid username or password"));
        }

        if !response.status().is_success() {
            let error_text = response
                .text()
                .await
                .unwrap_or_else(|_| "Unknown error".to_string());
            return Err(anyhow!("Login failed: {}", error_text));
        }

        response
            .json::<AuthResponse>()
            .await
            .context("Failed to parse auth response")
    }

    /// Search for symbols (functions, classes, etc.)
    pub async fn search_symbols(
        &self,
        query: &str,
        symbol_type: Option<&str>,
        limit: u32,
    ) -> Result<Vec<SymbolInfo>> {
        let mut url = format!(
            "{}/api/v1/intelligence/symbols?query={}&limit={}",
            self.base_url,
            urlencoding::encode(query),
            limit
        );

        if let Some(st) = symbol_type {
            url.push_str(&format!("&symbol_type={}", st));
        }

        let mut req = self.client.get(&url);
        if let Some(token) = &self.token {
            req = req.header("Authorization", format!("Bearer {}", token));
        }

        let response = req.send().await.context("Failed to search symbols")?;

        if !response.status().is_success() {
            let error_text = response
                .text()
                .await
                .unwrap_or_else(|_| "Unknown error".to_string());
            return Err(anyhow!("Symbol search failed: {}", error_text));
        }

        response
            .json::<Vec<SymbolInfo>>()
            .await
            .context("Failed to parse symbol search response")
    }

    /// Get callers of a function (direct endpoint)
    pub async fn find_callers(
        &self,
        function_name: &str,
        source: Option<&str>,
    ) -> Result<Vec<CallerInfo>> {
        let mut url = format!(
            "{}/api/v1/intelligence/callers?function={}",
            self.base_url,
            urlencoding::encode(function_name)
        );
        if let Some(s) = source {
            url.push_str(&format!("&source={}", urlencoding::encode(s)));
        }

        let mut req = self.client.get(&url);
        if let Some(token) = &self.token {
            req = req.header("Authorization", format!("Bearer {}", token));
        }

        let response = req.send().await.context("Failed to find callers")?;

        if !response.status().is_success() {
            let error_text = response
                .text()
                .await
                .unwrap_or_else(|_| "Unknown error".to_string());
            return Err(anyhow!("Find callers failed: {}", error_text));
        }

        response
            .json::<Vec<CallerInfo>>()
            .await
            .context("Failed to parse callers response")
    }

    /// Get callees of a function (direct endpoint)
    pub async fn find_callees(
        &self,
        function_name: &str,
        source: Option<&str>,
    ) -> Result<Vec<String>> {
        let mut url = format!(
            "{}/api/v1/intelligence/callees?function={}",
            self.base_url,
            urlencoding::encode(function_name)
        );
        if let Some(s) = source {
            url.push_str(&format!("&source={}", urlencoding::encode(s)));
        }

        let mut req = self.client.get(&url);
        if let Some(token) = &self.token {
            req = req.header("Authorization", format!("Bearer {}", token));
        }

        let response = req.send().await.context("Failed to find callees")?;

        if !response.status().is_success() {
            let error_text = response
                .text()
                .await
                .unwrap_or_else(|_| "Unknown error".to_string());
            return Err(anyhow!("Find callees failed: {}", error_text));
        }

        response
            .json::<Vec<String>>()
            .await
            .context("Failed to parse callees response")
    }

    /// N-hop call chain traversal
    pub async fn find_call_chain(
        &self,
        function_name: &str,
        direction: &str,
        depth: i32,
        source: Option<&str>,
    ) -> Result<Vec<CallChainEntry>> {
        let mut url = format!(
            "{}/api/v1/intelligence/call-chain?function={}&direction={}&depth={}",
            self.base_url,
            urlencoding::encode(function_name),
            direction,
            depth
        );
        if let Some(s) = source {
            url.push_str(&format!("&source={}", urlencoding::encode(s)));
        }

        let mut req = self.client.get(&url);
        if let Some(token) = &self.token {
            req = req.header("Authorization", format!("Bearer {}", token));
        }

        let response = req.send().await.context("Failed to find call chain")?;
        if !response.status().is_success() {
            let error_text = response.text().await.unwrap_or_default();
            return Err(anyhow!("Call chain failed: {}", error_text));
        }

        let body: serde_json::Value = response.json().await?;
        let entries: Vec<CallChainEntry> = serde_json::from_value(
            body.get("entries")
                .cloned()
                .unwrap_or(serde_json::json!([])),
        )?;
        Ok(entries)
    }

    /// Trigger orphaned chunk backfill (admin-only maintenance)
    /// Finds chunks without embeddings and processes them in batches
    pub async fn backfill_orphaned(&self) -> Result<BackfillResult> {
        let url = format!("{}/api/v1/admin/backfill/orphaned", self.base_url);

        let response = self
            .client
            .post(&url)
            .bearer_auth(self.token.as_deref().unwrap_or(""))
            .send()
            .await
            .context("Failed to trigger backfill")?;

        if response.status() == 401 {
            return Err(anyhow!(
                "Unauthorized. Please login first with: mainrag auth login"
            ));
        }

        if !response.status().is_success() {
            let error_text = response
                .text()
                .await
                .unwrap_or_else(|_| "Unknown error".to_string());
            return Err(anyhow!("Backfill failed: {}", error_text));
        }

        response
            .json::<BackfillResult>()
            .await
            .context("Failed to parse backfill response")
    }

    // =================================================================
    // Intelligence Layer Methods
    // =================================================================

    pub async fn get_symbol_cards(
        &self,
        name: &str,
        source: Option<&str>,
    ) -> Result<Vec<SymbolCard>> {
        let mut url = format!(
            "{}/api/v1/intelligence/cards?name={}",
            self.base_url,
            urlencoding::encode(name)
        );
        if let Some(s) = source {
            url.push_str(&format!("&source_name={}", urlencoding::encode(s)));
        }
        let response = self
            .client
            .get(&url)
            .bearer_auth(self.token.as_deref().unwrap_or(""))
            .send()
            .await
            .context("get_symbol_cards request failed")?;
        if !response.status().is_success() {
            return Err(anyhow!("get_symbol_cards failed: {}", response.status()));
        }
        response.json().await.context("parse symbol cards")
    }

    pub async fn shadow_intelligence(
        &self,
        command: &str,
        source: &str,
        generation: &str,
        query: &[(&str, Option<&str>)],
    ) -> Result<serde_json::Value> {
        let sources = self.list_sources().await?;
        let source_id = sources
            .sources
            .iter()
            .find(|candidate| candidate.name == source)
            .map(|candidate| candidate.id)
            .ok_or_else(|| anyhow!("source '{}' not found", source))?;
        let mut url = format!(
            "{}/api/v1/intelligence/shadow?source_id={}&generation={}&command={}",
            self.base_url,
            source_id,
            urlencoding::encode(generation),
            urlencoding::encode(command)
        );
        for (key, value) in query {
            if let Some(value) = value {
                url.push('&');
                url.push_str(key);
                url.push('=');
                url.push_str(&urlencoding::encode(value));
            }
        }
        let response = self
            .client
            .get(&url)
            .bearer_auth(self.token.as_deref().unwrap_or(""))
            .send()
            .await
            .context("shadow intelligence request failed")?;
        if !response.status().is_success() {
            return Err(anyhow!(
                "shadow intelligence request failed: {}",
                response.status()
            ));
        }
        response.json().await.context("parse shadow intelligence")
    }

    pub async fn explain_path(
        &self,
        symbol_name: &str,
        source: Option<&str>,
        max_depth: Option<u32>,
    ) -> Result<Vec<DelegationChain>> {
        let url = format!("{}/api/v1/intelligence/explain_path", self.base_url);
        let mut body = serde_json::json!({"symbol_name": symbol_name});
        if let Some(d) = max_depth {
            body["max_depth"] = serde_json::json!(d);
        }
        if let Some(s) = source {
            // Resolve source name to source_id
            if let Ok(sources) = self.list_sources().await {
                if let Some(src) = sources.sources.iter().find(|si| si.name == s) {
                    body["source_id"] = serde_json::json!(src.id);
                }
            }
        }
        let response = self
            .client
            .post(&url)
            .bearer_auth(self.token.as_deref().unwrap_or(""))
            .json(&body)
            .send()
            .await
            .context("explain_path request failed")?;
        if !response.status().is_success() {
            return Err(anyhow!("explain_path failed: {}", response.status()));
        }
        response.json().await.context("parse delegation chains")
    }

    pub async fn create_negative_evidence(
        &self,
        concept: &str,
        path_description: &str,
        reason: &str,
        symbols: &[String],
        source: Option<&str>,
    ) -> Result<i64> {
        let url = format!("{}/api/v1/intelligence/negative_evidence", self.base_url);
        let mut body = serde_json::json!({
            "concept": concept,
            "path_description": path_description,
            "reason": reason,
            "symbols": symbols,
        });
        if let Some(s) = source {
            if let Ok(sources) = self.list_sources().await {
                if let Some(src) = sources.sources.iter().find(|si| si.name == s) {
                    body["source_id"] = serde_json::json!(src.id);
                }
            }
        }
        let response = self
            .client
            .post(&url)
            .bearer_auth(self.token.as_deref().unwrap_or(""))
            .json(&body)
            .send()
            .await
            .context("create_negative_evidence failed")?;
        if !response.status().is_success() {
            return Err(anyhow!(
                "create_negative_evidence failed: {}",
                response.status()
            ));
        }
        let result: serde_json::Value = response.json().await?;
        Ok(result["id"].as_i64().unwrap_or(0))
    }

    pub async fn search_negative_evidence(&self, concept: &str) -> Result<Vec<NegativeEvidence>> {
        let url = format!(
            "{}/api/v1/intelligence/negative_evidence?concept={}",
            self.base_url,
            urlencoding::encode(concept)
        );
        let response = self
            .client
            .get(&url)
            .bearer_auth(self.token.as_deref().unwrap_or(""))
            .send()
            .await
            .context("search_negative_evidence failed")?;
        if !response.status().is_success() {
            return Err(anyhow!(
                "search_negative_evidence failed: {}",
                response.status()
            ));
        }
        response.json().await.context("parse negative evidence")
    }

    pub async fn explore(&self, query: &str, source: Option<&str>) -> Result<ExploreResponse> {
        let url = format!("{}/api/v1/intelligence/explore", self.base_url);
        let mut body = serde_json::json!({"query": query});
        if let Some(s) = source {
            body["source"] = serde_json::json!(s);
        }
        let response = self
            .client
            .post(&url)
            .bearer_auth(self.token.as_deref().unwrap_or(""))
            .json(&body)
            .send()
            .await
            .context("explore request failed")?;
        if !response.status().is_success() {
            return Err(anyhow!("explore failed: {}", response.status()));
        }
        response.json().await.context("parse explore response")
    }
}

// Intelligence types
#[derive(Serialize, Deserialize, Debug, Clone)]
pub struct SymbolInfo {
    pub id: i64,
    pub name: String,
    #[serde(rename = "symbol_type")]
    pub symbol_type: String,
    pub file_path: String,
    pub line_start: i32,
    pub line_end: i32,
    pub context: Option<String>,
}

#[derive(Serialize, Deserialize, Debug, Clone)]
pub struct CallerInfo {
    pub name: String,
    pub file_path: String,
    pub line: i32,
}

#[derive(Serialize, Deserialize, Debug, Clone)]
pub struct CallChainEntry {
    pub depth: u32,
    pub from_name: String,
    pub to_name: String,
    pub file_path: String,
    pub line: i32,
}

// Admin/Maintenance types
#[derive(Serialize, Deserialize, Debug, Clone)]
pub struct BackfillResult {
    pub processed: usize,
    pub batches: usize,
    pub message: String,
}

// =============================================================================
// Intelligence Layer Types
// =============================================================================

#[derive(Serialize, Deserialize, Debug, Clone)]
pub struct SymbolCard {
    pub symbol_id: i64,
    pub name: String,
    pub qualified_name: Option<String>,
    pub symbol_type: String,
    pub signature: Option<String>,
    pub file_path: String,
    pub line_start: i32,
    pub line_end: i32,
    pub source_name: String,
    #[serde(default)]
    pub visibility: Option<String>,
    #[serde(default)]
    pub layer: Option<String>,
    #[serde(default)]
    pub side_effect_type: Option<String>,
    #[serde(default)]
    pub affected_resource: Option<String>,
    #[serde(default)]
    pub delegation_targets: Option<serde_json::Value>,
    #[serde(default)]
    pub thread_requirement: Option<String>,
    #[serde(default)]
    pub preconditions: Option<String>,
    #[serde(default)]
    pub summary: Option<String>,
    #[serde(default)]
    pub classification_confidence: Option<f32>,
    #[serde(default)]
    pub domain_profile: Option<String>,
}

#[derive(Serialize, Deserialize, Debug, Clone)]
pub struct DelegationStep {
    pub symbol: SymbolCard,
    pub role: String,
    pub dispatch_via: Option<String>,
    pub code_snippet: Option<String>,
    #[serde(default)]
    pub step_annotations: Vec<AnnotationInfo>,
}

#[derive(Serialize, Deserialize, Debug, Clone)]
pub struct DelegationChain {
    pub entry_point: SymbolCard,
    pub steps: Vec<DelegationStep>,
    #[serde(default)]
    pub annotations: Vec<AnnotationInfo>,
}

#[derive(Serialize, Deserialize, Debug, Clone)]
pub struct AnnotationInfo {
    pub annotation_type: String,
    pub value: String,
    pub confidence: f32,
}

#[derive(Serialize, Deserialize, Debug, Clone)]
pub struct ExploreResponse {
    pub query: String,
    #[serde(default)]
    pub intent: Option<String>,
    #[serde(default)]
    pub domain: Option<String>,
    #[serde(default)]
    pub candidate_paths: Vec<CandidatePath>,
    #[serde(default)]
    pub negative_evidence: Vec<NegativeEvidence>,
    #[serde(default)]
    pub suggested_next: Vec<SuggestedQuery>,
    pub formatted: String,
}

#[derive(Serialize, Deserialize, Debug, Clone)]
pub struct CandidatePath {
    pub rank: u32,
    pub title: String,
    pub confidence: String,
    pub chain: DelegationChain,
    #[serde(default)]
    pub why_relevant: Option<String>,
    #[serde(default)]
    pub why_might_not_work: Option<String>,
}

#[derive(Serialize, Deserialize, Debug, Clone)]
pub struct SuggestedQuery {
    pub query: String,
    pub rationale: String,
}

#[derive(Serialize, Deserialize, Debug, Clone)]
pub struct NegativeEvidence {
    pub id: i64,
    pub concept: String,
    pub path_description: String,
    pub reason: String,
    #[serde(default)]
    pub symbols: serde_json::Value,
    pub severity: String,
    pub created_by: Option<String>,
    pub domain_profile: Option<String>,
}

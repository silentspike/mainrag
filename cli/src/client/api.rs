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
        Ok(ApiClient {
            client: Client::new(),
            base_url: base_url.trim_end_matches('/').to_string(),
            token: None,
        })
    }

    /// Load token from config file if exists
    pub fn load_token_from_file() -> Option<String> {
        let config_dir = directories::ProjectDirs::from("", "", "mainrag")
            .map(|d| d.config_dir().to_path_buf());

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
            return Err(anyhow!(
                "Health check failed: {}",
                response.status()
            ));
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
        // Use keyword endpoint for keyword mode, hybrid for others
        let endpoint = if mode == "keyword" { "search/keyword" } else { "search" };
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

        let response = req
            .send()
            .await
            .context("Failed to fetch sources")?;

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
        if let Some(source) = sources.sources.iter().find(|s| s.name.eq_ignore_ascii_case(source_name)) {
            return Ok(source.id);
        }

        // Try parsing as ID directly
        if let Ok(id) = source_name.parse::<i64>() {
            if sources.sources.iter().any(|s| s.id == id) {
                return Ok(id);
            }
        }

        Err(anyhow!("Source '{}' not found. Use 'mainrag source list' to see available sources.", source_name))
    }

    pub async fn sync_source(&self, source_name: &str) -> Result<SyncSourceResponse> {
        // Resolve name to ID
        let source_id = self.get_source_id_by_name(source_name).await?;

        let url = format!(
            "{}/api/v1/admin/sources/{}/sync",
            self.base_url, source_id
        );

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
    pub async fn sync_files(&self, source_name: &str, files: &[PathBuf]) -> Result<SyncSourceResponse> {
        // Resolve name to ID
        let source_id = self.get_source_id_by_name(source_name).await?;

        let url = format!(
            "{}/api/v1/admin/sources/{}/sync-files",
            self.base_url, source_id
        );

        // Convert paths to strings
        let file_paths: Vec<String> = files.iter()
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
    pub async fn get_source_deletion_stats(&self, source_name: &str) -> Result<SourceDeletionStats> {
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
    pub async fn search_symbols(&self, query: &str, symbol_type: Option<&str>, limit: u32) -> Result<Vec<SymbolInfo>> {
        let mut url = format!("{}/api/v1/intelligence/symbols?query={}&limit={}",
            self.base_url, urlencoding::encode(query), limit);

        if let Some(st) = symbol_type {
            url.push_str(&format!("&symbol_type={}", st));
        }

        let mut req = self.client.get(&url);
        if let Some(token) = &self.token {
            req = req.header("Authorization", format!("Bearer {}", token));
        }

        let response = req.send().await.context("Failed to search symbols")?;

        if !response.status().is_success() {
            let error_text = response.text().await.unwrap_or_else(|_| "Unknown error".to_string());
            return Err(anyhow!("Symbol search failed: {}", error_text));
        }

        response.json::<Vec<SymbolInfo>>().await.context("Failed to parse symbol search response")
    }

    /// Get callers of a function (direct endpoint)
    pub async fn find_callers(&self, function_name: &str) -> Result<Vec<CallerInfo>> {
        let url = format!("{}/api/v1/intelligence/callers?function={}",
            self.base_url, urlencoding::encode(function_name));

        let mut req = self.client.get(&url);
        if let Some(token) = &self.token {
            req = req.header("Authorization", format!("Bearer {}", token));
        }

        let response = req.send().await.context("Failed to find callers")?;

        if !response.status().is_success() {
            let error_text = response.text().await.unwrap_or_else(|_| "Unknown error".to_string());
            return Err(anyhow!("Find callers failed: {}", error_text));
        }

        response.json::<Vec<CallerInfo>>().await.context("Failed to parse callers response")
    }

    /// Get callees of a function (direct endpoint)
    pub async fn find_callees(&self, function_name: &str) -> Result<Vec<String>> {
        let url = format!("{}/api/v1/intelligence/callees?function={}",
            self.base_url, urlencoding::encode(function_name));

        let mut req = self.client.get(&url);
        if let Some(token) = &self.token {
            req = req.header("Authorization", format!("Bearer {}", token));
        }

        let response = req.send().await.context("Failed to find callees")?;

        if !response.status().is_success() {
            let error_text = response.text().await.unwrap_or_else(|_| "Unknown error".to_string());
            return Err(anyhow!("Find callees failed: {}", error_text));
        }

        response.json::<Vec<String>>().await.context("Failed to parse callees response")
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
            return Err(anyhow!("Unauthorized. Please login first with: mainrag auth login"));
        }

        if !response.status().is_success() {
            let error_text = response.text().await.unwrap_or_else(|_| "Unknown error".to_string());
            return Err(anyhow!("Backfill failed: {}", error_text));
        }

        response
            .json::<BackfillResult>()
            .await
            .context("Failed to parse backfill response")
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

// Admin/Maintenance types
#[derive(Serialize, Deserialize, Debug, Clone)]
pub struct BackfillResult {
    pub processed: usize,
    pub batches: usize,
    pub message: String,
}

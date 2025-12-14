use anyhow::{anyhow, Context, Result};
use reqwest::Client;
use serde::{Deserialize, Serialize};
use std::fs;
use std::path::PathBuf;
use thiserror::Error;

/// Custom error types for API operations
#[derive(Error, Debug)]
pub enum ApiError {
    #[error("HTTP error: {0}")]
    HttpError(String),
    #[error("API error: {0}")]
    ApiErrorResponse(String),
    #[error("Authentication failed: {0}")]
    AuthError(String),
    #[error("Network error: {0}")]
    NetworkError(String),
}

// ============================================================================
// Request/Response Types
// ============================================================================

#[derive(Serialize, Deserialize, Debug, Clone)]
pub struct SearchRequest {
    pub query: String,
    pub mode: String, // hybrid, keyword, semantic
    pub limit: u32,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub source: Option<String>,
}

#[derive(Serialize, Deserialize, Debug, Clone)]
pub struct SearchResult {
    pub file_path: String,
    pub chunk_id: i64,
    pub content: String,
    pub score: f32,
    pub source_name: String,
}

#[derive(Serialize, Deserialize, Debug, Clone)]
pub struct SearchResponse {
    pub results: Vec<SearchResult>,
    pub count: usize,
    pub query_time_ms: u32,
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
    pub file_count: i64,
    pub total_size: i64,
    pub last_synced: Option<String>,
    pub created_at: String,
}

#[derive(Deserialize, Debug, Clone)]
pub struct SourcesResponse {
    pub sources: Vec<Source>,
    pub count: usize,
}

#[derive(Serialize, Deserialize, Debug, Clone)]
pub struct LoginRequest {
    pub username: String,
    pub password: String,
}

#[derive(Deserialize, Debug, Clone)]
pub struct AuthResponse {
    pub token: String,
    pub user_id: String,
    pub username: String,
    pub expires_at: String,
}

#[derive(Deserialize, Debug, Clone)]
pub struct StatsResponse {
    pub sources_count: i64,
    pub files_count: i64,
    pub chunks_count: i64,
    pub total_size_bytes: i64,
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
    pub source_name: String,
    pub status: String,
    pub files_processed: i64,
    pub chunks_created: i64,
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
        fs::write(config_dir.join("token"), token)?;
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
        let url = format!("{}/api/v1/search", self.base_url);

        let mut params = vec![
            ("q".to_string(), query.to_string()),
            ("mode".to_string(), mode.to_string()),
            ("limit".to_string(), limit.to_string()),
        ];

        if let Some(s) = source {
            params.push(("source".to_string(), s.to_string()));
        }

        let response = self
            .client
            .get(&url)
            .query(&params)
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
        let url = format!("{}/api/v1/admin/sources", self.base_url);

        let response = self
            .client
            .get(&url)
            .bearer_auth(self.token.as_deref().unwrap_or(""))
            .send()
            .await
            .context("Failed to fetch sources")?;

        if response.status() == 401 {
            return Err(anyhow!(
                "Unauthorized. Please login first with: mainrag login"
            ));
        }

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

    pub async fn sync_source(&self, source_name: &str) -> Result<SyncSourceResponse> {
        let url = format!(
            "{}/api/v1/admin/sources/sync",
            self.base_url
        );

        let response = self
            .client
            .post(&url)
            .bearer_auth(self.token.as_deref().unwrap_or(""))
            .json(&serde_json::json!({ "source_name": source_name }))
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

    pub async fn delete_source(&self, source_name: &str) -> Result<()> {
        let url = format!("{}/api/v1/admin/sources/{}", self.base_url, source_name);

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

    pub async fn register(&self, username: &str, password: &str, email: Option<&str>) -> Result<AuthResponse> {
        let url = format!("{}/api/v1/auth/register", self.base_url);

        let mut req = serde_json::json!({
            "username": username,
            "password": password,
        });

        if let Some(e) = email {
            req["email"] = serde_json::json!(e);
        }

        let response = self
            .client
            .post(&url)
            .json(&req)
            .send()
            .await
            .context("Failed to connect to register endpoint")?;

        if response.status() == 409 {
            return Err(anyhow!("Username already exists"));
        }

        if !response.status().is_success() {
            let error_text = response
                .text()
                .await
                .unwrap_or_else(|_| "Unknown error".to_string());
            return Err(anyhow!("Registration failed: {}", error_text));
        }

        response
            .json::<AuthResponse>()
            .await
            .context("Failed to parse auth response")
    }
}

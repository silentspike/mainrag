use reqwest::Client;
use serde::{Deserialize, Serialize};
use std::time::Duration;
use uuid::Uuid;

use crate::config::QdrantConfig;
use crate::error::{AppError, Result};

mod bootstrap;

/// M6: Max retries for transient Qdrant errors (502, 503, 429, network)
const MAX_RETRIES: u32 = 3;
/// M6: Base delay for exponential backoff (doubles each retry)
const BASE_DELAY_MS: u64 = 100;

/// Type-safe tenant context for Qdrant queries (Sprint 1.5).
/// Forces every search to explicitly specify tenant isolation.
/// Using an enum instead of Option<Uuid> makes it impossible to
/// accidentally skip the tenant filter.
#[derive(Debug, Clone)]
pub enum TenantContext {
    /// Agent: filter results to only this user's data
    Agent { user_id: Uuid },
    /// Admin: no filter, sees all data (only reachable via admin_middleware)
    Admin,
}

#[derive(Debug, Clone)]
pub struct QdrantClient {
    client: Client,
    base_url: String,
    api_key: String,
    chunk_collection: String,
    code_collection: String,
}

#[derive(Debug, Serialize)]
struct SearchRequest {
    vector: Vec<f32>,
    limit: u64,
    with_payload: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    filter: Option<serde_json::Value>,
    /// HNSW search params — higher ef = better recall at cost of latency
    #[serde(skip_serializing_if = "Option::is_none")]
    params: Option<serde_json::Value>,
}

/// Qdrant search result - ID can be numeric or UUID string
#[derive(Debug, Deserialize)]
struct SearchResult {
    /// Point ID - can be numeric (u64) or UUID string
    id: serde_json::Value,
    score: f32,
    #[serde(default)]
    payload: Option<serde_json::Value>,
}

impl SearchResult {
    /// Extract chunk_id: prefer payload.chunk_id, then try to parse point id
    fn get_chunk_id(&self) -> Option<u64> {
        // First try to get chunk_id from payload (our format)
        if let Some(ref payload) = self.payload {
            if let Some(chunk_id) = payload.get("chunk_id").and_then(|v| v.as_i64()) {
                return Some(chunk_id as u64);
            }
        }

        // Fall back to point ID if it's numeric
        match &self.id {
            serde_json::Value::Number(n) => n.as_u64(),
            serde_json::Value::String(s) => s.parse::<u64>().ok(),
            _ => None,
        }
    }
}

#[derive(Debug, Deserialize)]
struct SearchResponse {
    result: Vec<SearchResult>,
}

#[derive(Debug, Serialize)]
struct UpsertRequest {
    points: Vec<Point>,
}

#[derive(Debug, Serialize, Clone)]
pub struct Point {
    pub id: u64,
    pub vector: Vec<f32>,
    pub payload: serde_json::Value,
}

/// M6: Check if an HTTP status code is retryable (transient server error)
fn is_retryable_status(status: reqwest::StatusCode) -> bool {
    matches!(status.as_u16(), 429 | 502 | 503 | 504)
}

impl QdrantClient {
    pub fn new(config: &QdrantConfig) -> Self {
        let client = Client::builder()
            .connect_timeout(Duration::from_secs(10))
            .timeout(Duration::from_secs(30))
            .build()
            .unwrap_or_else(|_| Client::new());

        Self {
            client,
            base_url: config.url.clone(),
            api_key: config.api_key.clone().unwrap_or_default(),
            chunk_collection: config.chunk_collection.clone(),
            code_collection: config.code_collection.clone(),
        }
    }

    /// M6: Send HTTP request with exponential backoff retry for transient errors
    async fn send_with_retry<T: Serialize>(
        &self,
        method: &str,
        url: &str,
        body: &T,
        operation: &str,
    ) -> Result<reqwest::Response> {
        let mut last_err = String::new();
        for attempt in 0..=MAX_RETRIES {
            if attempt > 0 {
                let delay = Duration::from_millis(BASE_DELAY_MS * 2u64.pow(attempt - 1));
                tracing::warn!(
                    attempt,
                    delay_ms = delay.as_millis() as u64,
                    operation,
                    "Qdrant retry"
                );
                tokio::time::sleep(delay).await;
            }

            let request = match method {
                "PUT" => self.client.put(url),
                _ => self.client.post(url),
            };

            match request
                .header("api-key", &self.api_key)
                .json(body)
                .send()
                .await
            {
                Ok(resp) if resp.status().is_success() => {
                    if attempt > 0 {
                        metrics::counter!("qdrant_retries_succeeded").increment(1);
                    }
                    return Ok(resp);
                }
                Ok(resp) if is_retryable_status(resp.status()) => {
                    last_err = format!("Qdrant returned {}", resp.status());
                    metrics::counter!("qdrant_retries_total").increment(1);
                    continue;
                }
                Ok(resp) => {
                    let status = resp.status();
                    let body = resp.text().await.unwrap_or_default();
                    return Err(AppError::Qdrant(format!(
                        "Qdrant {} returned {}: {}",
                        operation, status, body
                    )));
                }
                Err(e) if e.is_timeout() || e.is_connect() => {
                    last_err = format!("Network error: {}", e);
                    metrics::counter!("qdrant_retries_total").increment(1);
                    continue;
                }
                Err(e) => {
                    return Err(AppError::Qdrant(format!(
                        "{} request failed: {}",
                        operation, e
                    )));
                }
            }
        }

        metrics::counter!("qdrant_retries_exhausted").increment(1);
        Err(AppError::Qdrant(format!(
            "{} failed after {} retries: {}",
            operation, MAX_RETRIES, last_err
        )))
    }

    /// Health check for Qdrant service
    /// Note: Qdrant uses /healthz endpoint (Kubernetes-style), not /health
    pub async fn health_check(&self) -> Result<bool> {
        let url = format!("{}/healthz", self.base_url);
        let response = self
            .client
            .get(&url)
            .header("api-key", &self.api_key)
            .send()
            .await
            .map_err(|e| AppError::Qdrant(format!("Health check failed: {}", e)))?;

        Ok(response.status().is_success())
    }

    /// Search in the chunks collection (NO tenant isolation!)
    /// DEPRECATED(K4): Use search_chunks_with_tenant() instead.
    /// This method has no user_id filter and can leak cross-tenant data.
    #[deprecated(note = "K4: Use search_chunks_with_tenant() for tenant-isolated search")]
    pub async fn search_chunks(&self, vector: Vec<f32>, limit: u64) -> Result<Vec<(u64, f32)>> {
        self.search_collection(&self.chunk_collection, vector, limit, None)
            .await
    }

    /// Search chunks filtered by source_id (NO tenant isolation!)
    /// DEPRECATED(K4): Use search_chunks_with_tenant() instead.
    #[deprecated(note = "K4: Use search_chunks_with_tenant() for tenant-isolated search")]
    pub async fn search_chunks_by_source(
        &self,
        vector: Vec<f32>,
        limit: u64,
        source_id: i64,
    ) -> Result<Vec<(u64, f32)>> {
        self.search_collection(&self.chunk_collection, vector, limit, Some(source_id))
            .await
    }

    /// Search in the code collection
    /// Returns vector of (point_id, score) tuples
    pub async fn search_code(&self, vector: Vec<f32>, limit: u64) -> Result<Vec<(u64, f32)>> {
        self.search_collection(&self.code_collection, vector, limit, None)
            .await
    }

    /// Tenant-aware search in the chunks collection (Sprint 1.5).
    /// Uses TenantContext enum to enforce user_id filtering at Qdrant level.
    pub async fn search_chunks_with_tenant(
        &self,
        vector: Vec<f32>,
        limit: u64,
        tenant: &TenantContext,
        source_id: Option<i64>,
    ) -> Result<Vec<(u64, f32)>> {
        let mut must_filters = Vec::new();

        // Tenant isolation filter
        match tenant {
            TenantContext::Agent { user_id } => {
                must_filters.push(serde_json::json!({
                    "key": "user_id",
                    "match": {"value": user_id.to_string()}
                }));
            }
            TenantContext::Admin => {
                // No filter — admin sees all data
            }
        }

        // Optional source_id filter
        if let Some(sid) = source_id {
            must_filters.push(serde_json::json!({
                "key": "source_id",
                "match": {"value": sid}
            }));
        }

        let filter = if must_filters.is_empty() {
            None
        } else {
            Some(serde_json::json!({"must": must_filters}))
        };

        let url = format!(
            "{}/collections/{}/points/search",
            self.base_url, self.chunk_collection
        );

        let ef_search: u64 = std::env::var("QDRANT_EF_SEARCH")
            .ok()
            .and_then(|v| v.parse().ok())
            .unwrap_or(256);

        let request = SearchRequest {
            vector,
            limit,
            with_payload: true,
            filter,
            params: Some(serde_json::json!({"hnsw_ef": ef_search})),
        };

        // M6: Retry with exponential backoff
        let response = self
            .send_with_retry("POST", &url, &request, "search_tenant")
            .await?;

        let search_response: SearchResponse = response
            .json()
            .await
            .map_err(|e| AppError::Qdrant(format!("Failed to parse response: {}", e)))?;

        Ok(search_response
            .result
            .into_iter()
            .filter_map(|r| r.get_chunk_id().map(|id| (id, r.score)))
            .collect())
    }

    /// Internal search method for any collection
    async fn search_collection(
        &self,
        collection: &str,
        vector: Vec<f32>,
        limit: u64,
        source_id: Option<i64>,
    ) -> Result<Vec<(u64, f32)>> {
        let url = format!("{}/collections/{}/points/search", self.base_url, collection);

        // Build source filter for Qdrant (pushdown filtering)
        let filter = source_id.map(|sid| {
            serde_json::json!({
                "must": [{
                    "key": "source_id",
                    "match": {"value": sid}
                }]
            })
        });

        let ef_search: u64 = std::env::var("QDRANT_EF_SEARCH")
            .ok()
            .and_then(|v| v.parse().ok())
            .unwrap_or(256);

        let request = SearchRequest {
            vector,
            limit,
            with_payload: true,
            filter,
            params: Some(serde_json::json!({"hnsw_ef": ef_search})),
        };

        // M6: Retry with exponential backoff
        let response = self
            .send_with_retry("POST", &url, &request, "search")
            .await?;

        let search_response: SearchResponse = response
            .json()
            .await
            .map_err(|e| AppError::Qdrant(format!("Failed to parse response: {}", e)))?;

        // Extract chunk_id from each result, filtering out invalid entries
        Ok(search_response
            .result
            .into_iter()
            .filter_map(|r| r.get_chunk_id().map(|id| (id, r.score)))
            .collect())
    }

    /// Upsert points into chunks collection
    pub async fn upsert_chunks(&self, points: Vec<Point>) -> Result<()> {
        self.upsert_collection(&self.chunk_collection, points).await
    }

    /// Upsert points into code collection
    pub async fn upsert_code(&self, points: Vec<Point>) -> Result<()> {
        self.upsert_collection(&self.code_collection, points).await
    }

    /// Internal upsert method
    async fn upsert_collection(&self, collection: &str, points: Vec<Point>) -> Result<()> {
        if points.is_empty() {
            return Ok(());
        }

        let url = format!("{}/collections/{}/points", self.base_url, collection);
        let request = UpsertRequest { points };

        // M6: Retry with exponential backoff
        self.send_with_retry("PUT", &url, &request, "upsert")
            .await?;

        Ok(())
    }

    /// Delete points from chunks collection
    pub async fn delete_chunks(&self, point_ids: Vec<u64>) -> Result<()> {
        self.delete_collection(&self.chunk_collection, point_ids)
            .await
    }

    /// Delete points from code collection
    pub async fn delete_code(&self, point_ids: Vec<u64>) -> Result<()> {
        self.delete_collection(&self.code_collection, point_ids)
            .await
    }

    /// Delete all points for a specific source from chunks collection
    /// This uses a filter-based delete for efficient bulk cleanup
    pub async fn delete_by_source(&self, source_id: i64) -> Result<u64> {
        let url = format!(
            "{}/collections/{}/points/delete",
            self.base_url, self.chunk_collection
        );

        // Use filter-based deletion (more efficient than fetching IDs first)
        let payload = serde_json::json!({
            "filter": {
                "must": [{
                    "key": "source_id",
                    "match": { "value": source_id }
                }]
            }
        });

        let response = self
            .client
            .post(&url)
            .header("api-key", &self.api_key)
            .json(&payload)
            .send()
            .await
            .map_err(|e| AppError::Qdrant(format!("Delete by source failed: {}", e)))?;

        if !response.status().is_success() {
            let status = response.status();
            let body = response.text().await.unwrap_or_default();
            return Err(AppError::Qdrant(format!(
                "Qdrant delete by source returned {}: {}",
                status, body
            )));
        }

        // Parse response to get operation info (Qdrant returns operation status)
        let result: serde_json::Value = response.json().await.unwrap_or_default();
        let deleted = result["result"]["operation_id"].as_u64().unwrap_or(0);

        Ok(deleted)
    }

    /// Count points for a specific source in chunks collection
    pub async fn count_by_source(&self, source_id: i64) -> Result<u64> {
        let url = format!(
            "{}/collections/{}/points/count",
            self.base_url, self.chunk_collection
        );

        let payload = serde_json::json!({
            "filter": {
                "must": [{
                    "key": "source_id",
                    "match": { "value": source_id }
                }]
            },
            "exact": true
        });

        let response = self
            .client
            .post(&url)
            .header("api-key", &self.api_key)
            .json(&payload)
            .send()
            .await
            .map_err(|e| AppError::Qdrant(format!("Count by source failed: {}", e)))?;

        if !response.status().is_success() {
            let status = response.status();
            let body = response.text().await.unwrap_or_default();
            return Err(AppError::Qdrant(format!(
                "Qdrant count by source returned {}: {}",
                status, body
            )));
        }

        let result: serde_json::Value = response.json().await.unwrap_or_default();
        let count = result["result"]["count"].as_u64().unwrap_or(0);

        Ok(count)
    }

    /// Internal delete method
    async fn delete_collection(&self, collection: &str, point_ids: Vec<u64>) -> Result<()> {
        if point_ids.is_empty() {
            return Ok(());
        }

        let url = format!("{}/collections/{}/points/delete", self.base_url, collection);
        let payload = serde_json::json!({"points": point_ids});

        let response = self
            .client
            .post(&url)
            .header("api-key", &self.api_key)
            .json(&payload)
            .send()
            .await
            .map_err(|e| AppError::Qdrant(format!("Delete request failed: {}", e)))?;

        if !response.status().is_success() {
            let status = response.status();
            let body = response.text().await.unwrap_or_default();
            return Err(AppError::Qdrant(format!(
                "Qdrant returned {}: {}",
                status, body
            )));
        }

        Ok(())
    }

    /// Set payload fields on points matching a filter (Qdrant set_payload API).
    /// Used for backfilling user_id on existing points without re-uploading vectors.
    pub async fn set_payload_by_source(
        &self,
        source_id: i64,
        payload: serde_json::Value,
    ) -> Result<()> {
        let url = format!(
            "{}/collections/{}/points/payload",
            self.base_url, self.chunk_collection
        );

        let request_body = serde_json::json!({
            "payload": payload,
            "filter": {
                "must": [{
                    "key": "source_id",
                    "match": { "value": source_id }
                }]
            }
        });

        let response = self
            .client
            .post(&url)
            .header("api-key", &self.api_key)
            .json(&request_body)
            .send()
            .await
            .map_err(|e| AppError::Qdrant(format!("Set payload failed: {}", e)))?;

        if !response.status().is_success() {
            let status = response.status();
            let body = response.text().await.unwrap_or_default();
            return Err(AppError::Qdrant(format!(
                "Qdrant set_payload returned {}: {}",
                status, body
            )));
        }

        Ok(())
    }

    /// Create a payload index for efficient filtering.
    /// K4: user_id keyword index for tenant isolation.
    pub async fn create_payload_index(&self, field_name: &str, field_schema: &str) -> Result<()> {
        let url = format!(
            "{}/collections/{}/index",
            self.base_url, self.chunk_collection
        );

        let request_body = serde_json::json!({
            "field_name": field_name,
            "field_schema": field_schema
        });

        let response = self
            .client
            .put(&url)
            .header("api-key", &self.api_key)
            .json(&request_body)
            .send()
            .await
            .map_err(|e| AppError::Qdrant(format!("Create index failed: {}", e)))?;

        if !response.status().is_success() {
            let status = response.status();
            let body = response.text().await.unwrap_or_default();
            return Err(AppError::Qdrant(format!(
                "Qdrant create_index returned {}: {}",
                status, body
            )));
        }

        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_qdrant_client_creation() {
        let config = QdrantConfig {
            url: "http://localhost:6333".to_string(),
            api_key: Some("test_key".to_string()),
            chunk_collection: "mainrag_chunks".to_string(),
            code_collection: "mainrag_code".to_string(),
            synonyms_collection: Some("synonyms_v1".to_string()),
        };
        let client = QdrantClient::new(&config);
        assert_eq!(client.base_url, "http://localhost:6333");
    }
}

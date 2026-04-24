use reqwest::Client;
use serde::Serialize;
use std::time::Duration;

use crate::config::TeiConfig;
use crate::error::{AppError, Result};

/// M6: Max retries for transient TEI errors (502, 503, 429, network)
const MAX_RETRIES: u32 = 3;
/// M6: Base delay for exponential backoff (doubles each retry)
const BASE_DELAY_MS: u64 = 100;

pub struct TeiClient {
    client: Client,
    base_url: String,
    /// Model name (e.g., "bge-base-en-v1.5", "nomic-embed-text-v1.5", "bge-m3")
    pub model: Option<String>,
    /// Embedding dimension (e.g., 768, 1024)
    pub embedding_dim: Option<usize>,
}

#[derive(Debug, Serialize)]
struct EmbedRequest {
    inputs: String,
}

#[derive(Debug, Serialize)]
struct BatchEmbedRequest {
    inputs: Vec<String>,
}

/// M6: Check if an HTTP status code is retryable (transient server error)
fn is_retryable_status(status: reqwest::StatusCode) -> bool {
    matches!(
        status.as_u16(),
        429 | 502 | 503 | 504
    )
}

impl TeiClient {
    pub fn new(config: &TeiConfig) -> Self {
        let client = Client::builder()
            .connect_timeout(Duration::from_secs(10))
            .timeout(Duration::from_secs(30))
            .build()
            .unwrap_or_else(|_| Client::new());

        Self {
            client,
            base_url: config.url.clone(),
            model: config.model.clone(),
            embedding_dim: config.embedding_dim,
        }
    }

    /// Get default embedding dimension if not configured
    pub fn get_embedding_dim(&self) -> usize {
        self.embedding_dim.unwrap_or(768)  // Default to BGE-base dimension
    }

    /// Get model name for display/logging
    pub fn get_model_name(&self) -> &str {
        self.model.as_deref().unwrap_or("BAAI/bge-base-en-v1.5")  // Default model name with namespace
    }

    pub async fn health_check(&self) -> Result<bool> {
        let url = format!("{}/health", self.base_url);
        let response = self.client
            .get(&url)
            .send()
            .await
            .map_err(|e| AppError::Tei(format!("Health check failed: {}", e)))?;

        Ok(response.status().is_success())
    }

    pub async fn embed(&self, text: &str) -> Result<Vec<f32>> {
        let start = std::time::Instant::now();
        let url = format!("{}/embed", self.base_url);
        let body = EmbedRequest { inputs: text.to_string() };

        // M6: Exponential backoff retry for transient errors
        let mut last_err = String::new();
        for attempt in 0..=MAX_RETRIES {
            if attempt > 0 {
                let delay = Duration::from_millis(BASE_DELAY_MS * 2u64.pow(attempt - 1));
                tracing::warn!(attempt, delay_ms = delay.as_millis() as u64, "TEI embed retry");
                tokio::time::sleep(delay).await;
            }

            match self.client.post(&url).json(&body).send().await {
                Ok(resp) if resp.status().is_success() => {
                    let embeddings: Vec<Vec<f32>> = resp
                        .json()
                        .await
                        .map_err(|e| AppError::Tei(format!("Failed to parse response: {}", e)))?;

                    metrics::histogram!("embedding_duration_seconds").record(start.elapsed().as_secs_f64());
                    metrics::histogram!("embedding_batch_size").record(1.0);
                    if attempt > 0 {
                        metrics::counter!("tei_retries_succeeded").increment(1);
                    }

                    return embeddings
                        .into_iter()
                        .next()
                        .ok_or_else(|| AppError::Tei("Empty embedding response".to_string()));
                }
                Ok(resp) if is_retryable_status(resp.status()) => {
                    let status = resp.status();
                    last_err = format!("TEI returned {}", status);
                    metrics::counter!("tei_retries_total").increment(1);
                    continue;
                }
                Ok(resp) => {
                    let status = resp.status();
                    let body = resp.text().await.unwrap_or_default();
                    return Err(AppError::Tei(format!("TEI returned {}: {}", status, body)));
                }
                Err(e) if e.is_timeout() || e.is_connect() => {
                    last_err = format!("Network error: {}", e);
                    metrics::counter!("tei_retries_total").increment(1);
                    continue;
                }
                Err(e) => {
                    return Err(AppError::Tei(format!("Embed request failed: {}", e)));
                }
            }
        }

        metrics::counter!("tei_retries_exhausted").increment(1);
        Err(AppError::Tei(format!("Embed failed after {} retries: {}", MAX_RETRIES, last_err)))
    }

    /// Get reranker model name from TEI reranker (typically port 8082)
    /// Returns None if reranker is offline or doesn't expose /info endpoint
    pub async fn get_reranker_model_name(&self, reranker_url: Option<&str>) -> Option<String> {
        let url = reranker_url.unwrap_or("http://localhost:8082");
        let info_url = format!("{}/info", url);

        match self.client.get(&info_url).send().await {
            Ok(resp) if resp.status().is_success() => {
                if let Ok(info) = resp.json::<serde_json::Value>().await {
                    // TEI returns model_id in /info response
                    info.get("model_id")
                        .and_then(|v| v.as_str())
                        .map(|s| s.to_string())
                } else {
                    None
                }
            }
            _ => None, // Reranker offline or error
        }
    }

    /// Batch embed multiple texts
    pub async fn embed_batch(&self, texts: &[&str]) -> Result<Vec<Vec<f32>>> {
        if texts.is_empty() {
            return Ok(vec![]);
        }

        let start = std::time::Instant::now();
        let batch_size = texts.len();
        let url = format!("{}/embed", self.base_url);
        let inputs: Vec<String> = texts.iter().map(|s| s.to_string()).collect();
        let body = BatchEmbedRequest { inputs };

        // M6: Exponential backoff retry for transient errors
        let mut last_err = String::new();
        for attempt in 0..=MAX_RETRIES {
            if attempt > 0 {
                let delay = Duration::from_millis(BASE_DELAY_MS * 2u64.pow(attempt - 1));
                tracing::warn!(attempt, delay_ms = delay.as_millis() as u64, batch_size, "TEI batch embed retry");
                tokio::time::sleep(delay).await;
            }

            match self.client.post(&url).json(&body).send().await {
                Ok(resp) if resp.status().is_success() => {
                    let embeddings: Vec<Vec<f32>> = resp
                        .json()
                        .await
                        .map_err(|e| AppError::Tei(format!("Failed to parse batch response: {}", e)))?;

                    metrics::histogram!("embedding_duration_seconds").record(start.elapsed().as_secs_f64());
                    metrics::histogram!("embedding_batch_size").record(batch_size as f64);
                    if attempt > 0 {
                        metrics::counter!("tei_retries_succeeded").increment(1);
                    }

                    return Ok(embeddings);
                }
                Ok(resp) if is_retryable_status(resp.status()) => {
                    let status = resp.status();
                    last_err = format!("TEI returned {}", status);
                    metrics::counter!("tei_retries_total").increment(1);
                    continue;
                }
                Ok(resp) => {
                    let status = resp.status();
                    let body = resp.text().await.unwrap_or_default();
                    return Err(AppError::Tei(format!("TEI returned {}: {}", status, body)));
                }
                Err(e) if e.is_timeout() || e.is_connect() => {
                    last_err = format!("Network error: {}", e);
                    metrics::counter!("tei_retries_total").increment(1);
                    continue;
                }
                Err(e) => {
                    return Err(AppError::Tei(format!("Batch embed request failed: {}", e)));
                }
            }
        }

        metrics::counter!("tei_retries_exhausted").increment(1);
        Err(AppError::Tei(format!("Batch embed failed after {} retries: {}", MAX_RETRIES, last_err)))
    }
}

//! First-boot collection creation. Existing collections are never modified.

use super::*;
use reqwest::{StatusCode, Url};
use serde_json::{json, Value};

fn validate_collection(body: &Value, dimension: usize) -> Result<()> {
    let vectors = &body["result"]["config"]["params"]["vectors"];
    if vectors["size"].as_u64() != Some(dimension as u64)
        || vectors["distance"].as_str() != Some("Cosine")
    {
        return Err(AppError::Qdrant(
            "Chunk collection must use an unnamed Cosine vector with the configured embedding dimension; existing data was not modified".into(),
        ));
    }
    Ok(())
}

impl QdrantClient {
    /// Create only after confirmed absence; validate compatible existing state.
    /// CPU mode performs no network work, including for an unavailable endpoint.
    pub async fn ensure_chunk_collection(&self, cpu_mode: bool, dimension: usize) -> Result<()> {
        if cpu_mode {
            return Ok(());
        }
        if dimension == 0 || self.chunk_collection.is_empty() {
            return Err(AppError::Qdrant(
                "Chunk collection name and embedding dimension must be nonempty/positive".into(),
            ));
        }
        let mut url = Url::parse(&self.base_url)
            .map_err(|_| AppError::Qdrant("Invalid Qdrant bootstrap URL".into()))?;
        url.path_segments_mut()
            .map_err(|_| AppError::Qdrant("Invalid Qdrant bootstrap URL path".into()))?
            .pop_if_empty()
            .push("collections")
            .push(&self.chunk_collection);

        if let Some(body) = self.read_bootstrap_collection(&url, false).await? {
            return validate_collection(&body, dimension);
        }
        let response = self
            .client
            .put(url.clone())
            .header("api-key", &self.api_key)
            .json(&json!({
                "vectors": {"size": dimension, "distance": "Cosine", "on_disk": true},
                "hnsw_config": {"m": 16, "ef_construct": 200},
                "quantization_config": {"scalar": {"type": "int8", "always_ram": false}}
            }))
            .send()
            .await
            .map_err(|_| AppError::Qdrant("Chunk collection creation transport failed".into()))?;
        // Qdrant v1.16.3 maps AlreadyExists to 409. Only that failed status
        // permits race recovery; failed create/permission/transport errors fail.
        if !response.status().is_success() && response.status() != StatusCode::CONFLICT {
            return Err(AppError::Qdrant(format!(
                "Chunk collection creation returned {}",
                response.status()
            )));
        }
        let body = self
            .read_bootstrap_collection(&url, true)
            .await?
            .ok_or_else(|| {
                AppError::Qdrant("Chunk collection absent after creation/readback".into())
            })?;
        validate_collection(&body, dimension)
    }

    async fn read_bootstrap_collection(
        &self,
        url: &Url,
        after_create: bool,
    ) -> Result<Option<Value>> {
        // Concurrent creation can expose a collection before its local shards
        // are readable. Retry only readback 5xx responses, never a failed create
        // or an initial lookup, and still require a compatible final response.
        for attempt in 0..=3 {
            let response = self
                .client
                .get(url.clone())
                .header("api-key", &self.api_key)
                .send()
                .await
                .map_err(|_| AppError::Qdrant("Chunk collection lookup transport failed".into()))?;
            if after_create
                && matches!(response.status().as_u16(), 500 | 502 | 503 | 504)
                && attempt < 3
            {
                tokio::time::sleep(Duration::from_millis(100 * (1 << attempt))).await;
                continue;
            }
            if response.status() == StatusCode::NOT_FOUND {
                return Ok(None);
            }
            if !response.status().is_success() {
                return Err(AppError::Qdrant(format!(
                    "Chunk collection lookup returned {}",
                    response.status()
                )));
            }
            return response.json().await.map(Some).map_err(|_| {
                AppError::Qdrant("Malformed chunk collection lookup response".into())
            });
        }
        unreachable!("the final readback attempt always returns")
    }
}

#[cfg(test)]
mod tests;

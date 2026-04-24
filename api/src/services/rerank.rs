//! Cross-Encoder Reranking Service using TEI
//!
//! Uses the already-deployed TEI Reranker container on port 8082.

use anyhow::Result;
use reqwest::Client;
use serde::{Deserialize, Serialize};

/// TEI Reranker client
pub struct RerankerService {
    client: Client,
    url: String,
}

#[derive(Debug, Serialize)]
struct RerankRequest {
    query: String,
    texts: Vec<String>,
    truncate: bool,
}

#[derive(Debug, Deserialize)]
struct RerankResponse(Vec<RerankScore>);

#[derive(Debug, Deserialize)]
struct RerankScore {
    index: usize,
    score: f32,
}

impl RerankerService {
    pub fn new(url: Option<String>) -> Self {
        let default_url = "http://localhost:8082".to_string();
        let url = url.unwrap_or(default_url);
        Self {
            client: Client::new(),
            url,
        }
    }

    /// Default URL from environment or localhost:8082
    pub fn from_env() -> Self {
        let url = std::env::var("TEI_RERANKER_URL")
            .unwrap_or_else(|_| "http://localhost:8082".to_string());
        Self::new(Some(url))
    }

    /// Health check - verify reranker is accessible
    pub async fn health_check(&self) -> Result<()> {
        self.client
            .get(format!("{}/health", self.url))
            .send()
            .await?;
        Ok(())
    }

    /// Rerank texts by relevance to query
    ///
    /// # Arguments
    /// * `query` - The search query
    /// * `texts` - List of texts to rerank
    ///
    /// # Returns
    /// Indices sorted by relevance score (highest first)
    pub async fn rerank(&self, query: &str, texts: Vec<String>) -> Result<Vec<usize>> {
        if texts.is_empty() {
            return Ok(vec![]);
        }

        let request = RerankRequest {
            query: query.to_string(),
            texts,
            truncate: true,
        };

        let response = self.client
            .post(format!("{}/rerank", self.url))
            .json(&request)
            .send()
            .await?
            .json::<RerankResponse>()
            .await?;

        // Sort by score descending and return indices
        let mut scores = response.0;
        scores.sort_by(|a, b| b.score.total_cmp(&a.score));

        Ok(scores.into_iter().map(|s| s.index).collect())
    }

    /// Rerank search results
    pub async fn rerank_results<T: AsRef<str>>(
        &self,
        query: &str,
        results: Vec<(T, T)>,  // (id, text) pairs
    ) -> Result<Vec<String>> {
        if results.is_empty() {
            return Ok(vec![]);
        }

        let texts: Vec<String> = results.iter().map(|(_, t)| t.as_ref().to_string()).collect();
        let ids: Vec<String> = results.iter().map(|(id, _)| id.as_ref().to_string()).collect();

        let reranked_indices = self.rerank(query, texts).await?;

        Ok(reranked_indices.into_iter().map(|i| ids[i].clone()).collect())
    }

    /// Health check
    pub async fn health(&self) -> Result<bool> {
        let response = self.client
            .get(format!("{}/health", self.url))
            .send()
            .await?;
        Ok(response.status().is_success())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    #[ignore] // Requires running TEI reranker
    async fn test_rerank() {
        let service = RerankerService::from_env();

        let texts = vec![
            "The quick brown fox".to_string(),
            "A lazy dog sleeps".to_string(),
            "The fox jumps over".to_string(),
        ];

        let result = service.rerank("fox jumping", texts).await.unwrap();
        assert_eq!(result.len(), 3);
        // "The fox jumps over" should be ranked highest
        assert_eq!(result[0], 2);
    }
}

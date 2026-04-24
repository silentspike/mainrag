//! Quality Tiers System
//!
//! Two-tier search quality:
//! - fast: Hybrid search (FTS + Vector) without reranking, <100ms
//! - balanced: Hybrid search + BGE Reranking, 100-300ms

use serde::{Deserialize, Serialize};

/// Quality tier for search requests
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize, Default)]
#[serde(rename_all = "lowercase")]
pub enum QualityTier {
    /// Fast: Hybrid search only, no reranking, <100ms
    Fast,
    /// Balanced: Hybrid + BGE Reranking, 100-300ms (default)
    #[default]
    Balanced,
}

impl QualityTier {
    /// Parse tier from query string
    pub fn parse(tier_str: Option<&str>) -> Self {
        match tier_str {
            Some("fast") => QualityTier::Fast,
            Some("balanced") | None => QualityTier::Balanced,
            _ => QualityTier::Balanced,
        }
    }

    /// Whether this tier should use reranking
    pub fn should_rerank(&self) -> bool {
        matches!(self, QualityTier::Balanced)
    }

    /// Expected latency budget in ms
    pub fn latency_budget_ms(&self) -> u64 {
        match self {
            QualityTier::Fast => 100,
            QualityTier::Balanced => 300,
        }
    }

    /// Convert to string
    pub fn as_str(&self) -> &'static str {
        match self {
            QualityTier::Fast => "fast",
            QualityTier::Balanced => "balanced",
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_tier_parsing() {
        assert_eq!(QualityTier::parse(Some("fast")), QualityTier::Fast);
        assert_eq!(QualityTier::parse(Some("balanced")), QualityTier::Balanced);
        assert_eq!(QualityTier::parse(None), QualityTier::Balanced);
        assert_eq!(QualityTier::parse(Some("invalid")), QualityTier::Balanced);
        // Legacy tiers fall back to balanced
        assert_eq!(QualityTier::parse(Some("deep")), QualityTier::Balanced);
        assert_eq!(QualityTier::parse(Some("verified")), QualityTier::Balanced);
    }

    #[test]
    fn test_should_rerank() {
        assert!(!QualityTier::Fast.should_rerank());
        assert!(QualityTier::Balanced.should_rerank());
    }

    #[test]
    fn test_latency_budgets() {
        assert_eq!(QualityTier::Fast.latency_budget_ms(), 100);
        assert_eq!(QualityTier::Balanced.latency_budget_ms(), 300);
    }
}

//! Query Expansion E2E Tests
//!
//! Tests the full search pipeline with query expansion via HTTP API.
//!
//! Run with: cargo test --test query_expansion_e2e -- --ignored
//!
//! Prerequisites:
//! - MainRAG API running on localhost:3001
//! - Services (PostgreSQL, Qdrant, TEI) running
//! - QUERY_EXPANSION_ENABLED=true

use serde::{Deserialize, Serialize};

const API_BASE: &str = "http://localhost:3001";

/// Check if API is available
fn api_available() -> bool {
    std::process::Command::new("curl")
        .args([
            "-s",
            "-o",
            "/dev/null",
            "-w",
            "%{http_code}",
            &format!("{}/health", API_BASE),
        ])
        .output()
        .map(|o| o.stdout.starts_with(b"200"))
        .unwrap_or(false)
}

#[derive(Debug, Serialize)]
struct SearchRequest {
    query: String,
    limit: u32,
    #[serde(skip_serializing_if = "Option::is_none")]
    quality: Option<String>,
}

#[derive(Debug, Deserialize)]
struct SearchResponse {
    results: Vec<SearchResult>,
    total: u64,
    took_ms: u64,
    quality_tier: String,
    reranked: bool,
}

#[derive(Debug, Deserialize)]
#[allow(dead_code)]
struct SearchResult {
    chunk_id: i64,
    file_path: String,
    content: String,
    snippet: String,
    score: f32,
    source_name: String,
}

#[tokio::test]
#[ignore = "requires running MainRAG API"]
async fn test_e2e_german_query_finds_english_content() {
    if !api_available() {
        eprintln!("Skipping: API not available at {}", API_BASE);
        return;
    }

    let client = reqwest::Client::new();

    // Search with German term - should find English content via expansion
    let response = client
        .post(format!("{}/api/v1/search", API_BASE))
        .json(&SearchRequest {
            query: "fehler".to_string(),
            limit: 10,
            quality: None,
        })
        .send()
        .await
        .expect("request failed");

    assert!(response.status().is_success(), "API returned error");

    let search_result: SearchResponse = response.json().await.expect("parse failed");

    println!(
        "E2E: 'fehler' search returned {} results in {}ms",
        search_result.total, search_result.took_ms
    );
    println!(
        "Quality tier: {}, Reranked: {}",
        search_result.quality_tier, search_result.reranked
    );

    // Should have results
    assert!(search_result.total > 0, "Expected results for 'fehler'");

    // Print first few results for manual inspection
    for (i, result) in search_result.results.iter().take(3).enumerate() {
        println!(
            "  [{}] score={:.3} source={} path={}",
            i + 1,
            result.score,
            result.source_name,
            result.file_path.chars().take(60).collect::<String>()
        );
    }
}

#[tokio::test]
#[ignore = "requires running MainRAG API"]
async fn test_e2e_english_query_finds_german_content() {
    if !api_available() {
        eprintln!("Skipping: API not available at {}", API_BASE);
        return;
    }

    let client = reqwest::Client::new();

    // Search with English term - should find German content via expansion
    let response = client
        .post(format!("{}/api/v1/search", API_BASE))
        .json(&SearchRequest {
            query: "error handling".to_string(),
            limit: 10,
            quality: None,
        })
        .send()
        .await
        .expect("request failed");

    assert!(response.status().is_success(), "API returned error");

    let search_result: SearchResponse = response.json().await.expect("parse failed");

    println!(
        "E2E: 'error handling' search returned {} results in {}ms",
        search_result.total, search_result.took_ms
    );

    assert!(
        search_result.total > 0,
        "Expected results for 'error handling'"
    );
}

#[tokio::test]
#[ignore = "requires running MainRAG API"]
async fn test_e2e_code_specific_terms() {
    if !api_available() {
        eprintln!("Skipping: API not available at {}", API_BASE);
        return;
    }

    let client = reqwest::Client::new();

    // Test various code-specific terms that should benefit from expansion
    let test_queries = vec![
        ("async", "async programming patterns"),
        ("mutex", "thread synchronization"),
        ("iterator", "collection traversal"),
        ("trait", "Rust type system"),
    ];

    for (query, description) in test_queries {
        let response = client
            .post(format!("{}/api/v1/search", API_BASE))
            .json(&SearchRequest {
                query: query.to_string(),
                limit: 5,
                quality: None,
            })
            .send()
            .await
            .expect("request failed");

        let search_result: SearchResponse = response.json().await.expect("parse failed");

        println!(
            "E2E: '{}' ({}) -> {} results in {}ms",
            query, description, search_result.total, search_result.took_ms
        );
    }
}

#[tokio::test]
#[ignore = "requires running MainRAG API"]
async fn test_e2e_search_performance() {
    if !api_available() {
        eprintln!("Skipping: API not available at {}", API_BASE);
        return;
    }

    let client = reqwest::Client::new();

    // Run multiple searches and measure performance
    let queries = vec!["function", "database", "error", "config", "test"];

    let mut total_time_ms = 0u64;
    let mut total_results = 0u64;

    for query in &queries {
        let start = std::time::Instant::now();

        let response = client
            .post(format!("{}/api/v1/search", API_BASE))
            .json(&SearchRequest {
                query: query.to_string(),
                limit: 20,
                quality: None,
            })
            .send()
            .await
            .expect("request failed");

        let search_result: SearchResponse = response.json().await.expect("parse failed");

        let elapsed = start.elapsed().as_millis() as u64;
        total_time_ms += elapsed;
        total_results += search_result.total;

        println!(
            "  '{}': {} results, {}ms (server: {}ms)",
            query, search_result.total, elapsed, search_result.took_ms
        );
    }

    let avg_time = total_time_ms / queries.len() as u64;
    println!(
        "\nE2E Performance: {} queries, avg {}ms, total {} results",
        queries.len(),
        avg_time,
        total_results
    );

    // Performance assertion: average should be under 3 seconds
    assert!(
        avg_time < 3000,
        "Search too slow: {}ms avg (expected <3000ms)",
        avg_time
    );
}

#[tokio::test]
#[ignore = "requires running MainRAG API"]
async fn test_e2e_quality_tiers() {
    if !api_available() {
        eprintln!("Skipping: API not available at {}", API_BASE);
        return;
    }

    let client = reqwest::Client::new();

    // Test both quality tiers
    for tier in &["fast", "balanced"] {
        let response = client
            .post(format!("{}/api/v1/search", API_BASE))
            .json(&SearchRequest {
                query: "function".to_string(),
                limit: 10,
                quality: Some(tier.to_string()),
            })
            .send()
            .await
            .expect("request failed");

        let search_result: SearchResponse = response.json().await.expect("parse failed");

        println!(
            "E2E: quality='{}' -> tier='{}', reranked={}, {}ms",
            tier, search_result.quality_tier, search_result.reranked, search_result.took_ms
        );

        assert_eq!(search_result.quality_tier, *tier, "Quality tier mismatch");
    }
}

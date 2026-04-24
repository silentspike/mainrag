//! Query Expansion Performance Benchmark
//!
//! Measures the overhead of query expansion on search latency.
//!
//! Run with: cargo bench --bench query_expansion_bench
//!
//! Prerequisites:
//! - MainRAG API running on localhost:3001
//! - QUERY_EXPANSION_ENABLED=true

use criterion::{black_box, criterion_group, criterion_main, BenchmarkId, Criterion};
use serde::{Deserialize, Serialize};
use std::time::Duration;

const API_BASE: &str = "http://localhost:3001";

#[derive(Debug, Serialize)]
struct SearchRequest {
    query: String,
    limit: u32,
    #[serde(skip_serializing_if = "Option::is_none")]
    quality: Option<String>,
}

#[derive(Debug, Deserialize)]
#[allow(dead_code)] // fields populated by serde::Deserialize; bench only cares about shape
struct SearchResponse {
    results: Vec<serde_json::Value>,
    total: u64,
    took_ms: u64,
    quality_tier: String,
}

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

fn bench_search_query(c: &mut Criterion) {
    if !api_available() {
        eprintln!("Warning: MainRAG API not available, skipping benchmark");
        return;
    }

    // Create runtime for async
    let rt = tokio::runtime::Runtime::new().unwrap();
    let client = reqwest::Client::new();

    let test_queries = vec![
        ("simple_en", "function"),
        ("simple_de", "fehler"),
        ("compound_en", "error handling"),
        ("compound_de", "fehlerbehandlung"),
        ("code_term", "async await"),
    ];

    let mut group = c.benchmark_group("query_expansion");
    group.sample_size(20); // Reduce sample size for integration benchmarks
    group.measurement_time(Duration::from_secs(30));

    for (name, query) in test_queries {
        group.bench_with_input(BenchmarkId::new("search", name), &query, |b, query| {
            b.to_async(&rt).iter(|| async {
                let response = client
                    .post(format!("{}/api/v1/search", API_BASE))
                    .json(&SearchRequest {
                        query: query.to_string(),
                        limit: 10,
                        quality: Some("balanced".to_string()),
                    })
                    .send()
                    .await
                    .expect("request failed");

                let result: SearchResponse = response.json().await.expect("parse failed");
                black_box(result)
            });
        });
    }

    group.finish();
}

fn bench_fast_vs_balanced(c: &mut Criterion) {
    if !api_available() {
        return;
    }

    let rt = tokio::runtime::Runtime::new().unwrap();
    let client = reqwest::Client::new();

    let mut group = c.benchmark_group("quality_tiers");
    group.sample_size(20);
    group.measurement_time(Duration::from_secs(30));

    for tier in &["fast", "balanced"] {
        group.bench_with_input(BenchmarkId::new("tier", tier), tier, |b, tier| {
            b.to_async(&rt).iter(|| async {
                let response = client
                    .post(format!("{}/api/v1/search", API_BASE))
                    .json(&SearchRequest {
                        query: "error".to_string(),
                        limit: 20,
                        quality: Some(tier.to_string()),
                    })
                    .send()
                    .await
                    .expect("request failed");

                let result: SearchResponse = response.json().await.expect("parse failed");
                black_box(result)
            });
        });
    }

    group.finish();
}

criterion_group!(benches, bench_search_query, bench_fast_vs_balanced);
criterion_main!(benches);

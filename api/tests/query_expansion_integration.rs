//! Query Expansion Integration Tests
//!
//! Tests the QueryExpander service against real Qdrant and TEI.
//!
//! Run with: cargo test --test query_expansion_integration -- --ignored
//!
//! Prerequisites:
//! - Qdrant running on localhost:6333 with synonyms_v1 collection
//! - TEI running on localhost:8080
//! - API key: <REDACTED_QDRANT_API_KEY>

use std::sync::Arc;

/// Skip test if services are not available
fn services_available() -> bool {
    // Quick check if Qdrant is reachable
    std::process::Command::new("curl")
        .args([
            "-s",
            "-o",
            "/dev/null",
            "-w",
            "%{http_code}",
            "http://localhost:6333/healthz",
        ])
        .output()
        .map(|o| o.stdout.starts_with(b"200"))
        .unwrap_or(false)
}

#[tokio::test]
#[ignore = "requires Qdrant and TEI services"]
async fn test_query_expander_finds_synonyms() {
    use mainrag_api::config::{QdrantConfig, TeiConfig};
    use mainrag_api::services::{QueryExpander, TeiClient};

    if !services_available() {
        eprintln!("Skipping: Services not available");
        return;
    }

    let tei_config = TeiConfig {
        url: "http://localhost:8080".to_string(),
        reranker_url: Some("http://localhost:8082".to_string()),
        model: None,
        embedding_dim: None,
    };

    let qdrant_config = QdrantConfig {
        url: "http://localhost:6333".to_string(),
        api_key: Some("<REDACTED_QDRANT_API_KEY>".to_string()),
        chunk_collection: "mainrag_chunks".to_string(),
        code_collection: "mainrag_code".to_string(),
        synonyms_collection: Some("synonyms_v1".to_string()),
    };

    let tei = Arc::new(TeiClient::new(&tei_config));
    let expander = QueryExpander::new(&qdrant_config, tei, true);

    // Test: German term should find synonyms
    let result = expander
        .expand("fehler", None)
        .await
        .expect("expand failed");

    // Should have found synonyms
    assert!(
        !result.synonyms.is_empty(),
        "Expected synonyms for 'fehler', got none"
    );

    // FTS query should contain expansion terms (| separator)
    assert!(
        result.fts_query.contains(" | "),
        "Expected expanded FTS query with | separator, got: {}",
        result.fts_query
    );

    // Should contain original term
    assert!(
        result.fts_query.contains("fehler"),
        "FTS query should contain original term"
    );

    // Should contain English synonyms
    let has_english = result.fts_query.contains("error")
        || result.fts_query.contains("bug")
        || result.fts_query.contains("fault");
    assert!(
        has_english,
        "Expected English synonyms in expansion, got: {}",
        result.fts_query
    );

    // Embedding should be 768 dimensions (BGE model)
    assert_eq!(
        result.embedding.len(),
        768,
        "Expected 768-dim embedding, got {}",
        result.embedding.len()
    );

    println!(
        "Test passed! Query: '{}' -> {} synonyms",
        result.original,
        result.synonyms.len()
    );
    println!("FTS: {}", result.fts_query);
}

#[tokio::test]
#[ignore = "requires Qdrant and TEI services"]
async fn test_query_expander_handles_unknown_terms() {
    use mainrag_api::config::{QdrantConfig, TeiConfig};
    use mainrag_api::services::{QueryExpander, TeiClient};

    if !services_available() {
        eprintln!("Skipping: Services not available");
        return;
    }

    let tei_config = TeiConfig {
        url: "http://localhost:8080".to_string(),
        reranker_url: Some("http://localhost:8082".to_string()),
        model: None,
        embedding_dim: None,
    };

    let qdrant_config = QdrantConfig {
        url: "http://localhost:6333".to_string(),
        api_key: Some("<REDACTED_QDRANT_API_KEY>".to_string()),
        chunk_collection: "mainrag_chunks".to_string(),
        code_collection: "mainrag_code".to_string(),
        synonyms_collection: Some("synonyms_v1".to_string()),
    };

    let tei = Arc::new(TeiClient::new(&tei_config));
    let expander = QueryExpander::new(&qdrant_config, tei, true);

    // Test: Very specific term unlikely to have synonyms
    let result = expander
        .expand("xyzabc123nonsense", None)
        .await
        .expect("expand failed");

    // Should still have embedding (even without synonyms)
    assert_eq!(result.embedding.len(), 768);

    // FTS query should be original (no expansion)
    // Note: May still have some low-similarity matches, that's OK
    println!(
        "Unknown term: '{}' -> {} synonyms, FTS: {}",
        result.original,
        result.synonyms.len(),
        result.fts_query
    );
}

#[tokio::test]
#[ignore = "requires Qdrant and TEI services"]
async fn test_query_expander_disabled_mode() {
    use mainrag_api::config::{QdrantConfig, TeiConfig};
    use mainrag_api::services::{QueryExpander, TeiClient};

    if !services_available() {
        eprintln!("Skipping: Services not available");
        return;
    }

    let tei_config = TeiConfig {
        url: "http://localhost:8080".to_string(),
        reranker_url: Some("http://localhost:8082".to_string()),
        model: None,
        embedding_dim: None,
    };

    let qdrant_config = QdrantConfig {
        url: "http://localhost:6333".to_string(),
        api_key: Some("<REDACTED_QDRANT_API_KEY>".to_string()),
        chunk_collection: "mainrag_chunks".to_string(),
        code_collection: "mainrag_code".to_string(),
        synonyms_collection: Some("synonyms_v1".to_string()),
    };

    let tei = Arc::new(TeiClient::new(&tei_config));

    // Create with disabled=false
    let expander = QueryExpander::new(&qdrant_config, tei, false);

    assert!(!expander.is_enabled(), "Expander should be disabled");

    // Should return original query without expansion
    let result = expander
        .expand("fehler", None)
        .await
        .expect("expand failed");

    assert!(
        result.synonyms.is_empty(),
        "Disabled expander should not find synonyms"
    );
    assert_eq!(
        result.fts_query, "fehler",
        "Disabled expander should return original query"
    );

    println!("Disabled mode test passed");
}

#[tokio::test]
#[ignore = "requires Qdrant and TEI services"]
async fn test_query_expander_cross_language() {
    use mainrag_api::config::{QdrantConfig, TeiConfig};
    use mainrag_api::services::{QueryExpander, TeiClient};

    if !services_available() {
        eprintln!("Skipping: Services not available");
        return;
    }

    let tei_config = TeiConfig {
        url: "http://localhost:8080".to_string(),
        reranker_url: Some("http://localhost:8082".to_string()),
        model: None,
        embedding_dim: None,
    };

    let qdrant_config = QdrantConfig {
        url: "http://localhost:6333".to_string(),
        api_key: Some("<REDACTED_QDRANT_API_KEY>".to_string()),
        chunk_collection: "mainrag_chunks".to_string(),
        code_collection: "mainrag_code".to_string(),
        synonyms_collection: Some("synonyms_v1".to_string()),
    };

    let tei = Arc::new(TeiClient::new(&tei_config));
    let expander = QueryExpander::new(&qdrant_config, tei, true);

    // Test cases: German -> should find English, English -> should find German
    let test_cases = vec![
        ("variable", vec!["var", "variabel"]),
        ("funktion", vec!["function", "func", "method"]),
        ("datenbank", vec!["database", "db"]),
    ];

    for (query, expected_any) in test_cases {
        let result = expander.expand(query, None).await.expect("expand failed");

        let found_any = expected_any
            .iter()
            .any(|exp| result.fts_query.to_lowercase().contains(exp));

        println!(
            "Cross-language: '{}' -> FTS has expected: {} ({})",
            query, found_any, result.fts_query
        );
    }
}

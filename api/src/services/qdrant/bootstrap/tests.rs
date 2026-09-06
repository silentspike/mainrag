use super::*;
use axum::{body::Body, extract::State, http::Request, response::IntoResponse, Router};
use std::collections::VecDeque;
use std::sync::Arc;
use tokio::sync::Mutex;

fn configured(url: String, name: &str) -> QdrantClient {
    QdrantClient::new(&QdrantConfig {
        url,
        api_key: Some("fixture-only".into()),
        chunk_collection: name.into(),
        code_collection: "unused".into(),
        synonyms_collection: None,
    })
}

fn compatible(dimension: usize) -> Value {
    json!({"result": {"config": {"params": {"vectors": {"size": dimension, "distance": "Cosine"}}}}})
}

type Reply = (&'static str, StatusCode, Value);

#[derive(Default)]
struct Script {
    replies: VecDeque<Reply>,
    invalid_request: bool,
}

async fn scripted(
    State(script): State<Arc<Mutex<Script>>>,
    request: Request<Body>,
) -> impl IntoResponse {
    let mut script = script.lock().await;
    if request.uri().path() != "/collections/fixture"
        || request
            .headers()
            .get("api-key")
            .and_then(|v| v.to_str().ok())
            != Some("fixture-only")
    {
        script.invalid_request = true;
    }
    match script.replies.pop_front() {
        Some((method, status, body)) => {
            if request.method().as_str() != method {
                script.invalid_request = true;
            }
            (status, axum::Json(body))
        }
        None => {
            script.invalid_request = true;
            (StatusCode::INTERNAL_SERVER_ERROR, axum::Json(json!(null)))
        }
    }
}

async fn scenario(replies: Vec<Reply>, cpu_mode: bool, dimension: usize, success: bool) {
    let script = Arc::new(Mutex::new(Script {
        replies: replies.into(),
        ..Default::default()
    }));
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let client = configured(
        format!("http://{}", listener.local_addr().unwrap()),
        "fixture",
    );
    let router = Router::new().fallback(scripted).with_state(script.clone());
    let task = tokio::spawn(async move { axum::serve(listener, router).await });
    let result = tokio::time::timeout(
        Duration::from_secs(5),
        client.ensure_chunk_collection(cpu_mode, dimension),
    )
    .await;
    task.abort();
    let _ = task.await;
    assert_eq!(
        result.expect("bootstrap must remain bounded").is_ok(),
        success
    );
    let script = script.lock().await;
    assert!(
        !script.invalid_request,
        "unexpected method, endpoint, or authentication"
    );
    assert!(
        script.replies.is_empty(),
        "expected requests did not execute"
    );
}

#[tokio::test]
async fn bootstrap_creates_only_after_absence_and_preserves_existing() {
    scenario(
        vec![
            ("GET", StatusCode::NOT_FOUND, json!(null)),
            ("PUT", StatusCode::OK, json!({"result": true})),
            ("GET", StatusCode::OK, compatible(768)),
        ],
        false,
        768,
        true,
    )
    .await;
    scenario(
        vec![("GET", StatusCode::OK, compatible(768))],
        false,
        768,
        true,
    )
    .await;
}

#[tokio::test]
async fn bootstrap_failed_lookup_never_creates() {
    for status in [
        StatusCode::UNAUTHORIZED,
        StatusCode::FORBIDDEN,
        StatusCode::INTERNAL_SERVER_ERROR,
    ] {
        scenario(vec![("GET", status, json!(null))], false, 768, false).await;
    }
    scenario(
        vec![("GET", StatusCode::OK, json!({"result": false}))],
        false,
        768,
        false,
    )
    .await;
}

#[tokio::test]
async fn bootstrap_only_conflict_allows_compatible_readback() {
    for dimension in [768, 1024] {
        scenario(
            vec![
                ("GET", StatusCode::NOT_FOUND, json!(null)),
                ("PUT", StatusCode::CONFLICT, json!(null)),
                ("GET", StatusCode::OK, compatible(dimension)),
            ],
            false,
            768,
            dimension == 768,
        )
        .await;
    }
    for status in [
        StatusCode::BAD_REQUEST,
        StatusCode::FORBIDDEN,
        StatusCode::INTERNAL_SERVER_ERROR,
    ] {
        scenario(
            vec![
                ("GET", StatusCode::NOT_FOUND, json!(null)),
                ("PUT", status, json!(null)),
            ],
            false,
            768,
            false,
        )
        .await;
    }
    scenario(
        vec![
            ("GET", StatusCode::NOT_FOUND, json!(null)),
            ("PUT", StatusCode::OK, json!({"result": true})),
            ("GET", StatusCode::NOT_FOUND, json!(null)),
        ],
        false,
        768,
        false,
    )
    .await;
}

#[tokio::test]
async fn bootstrap_cpu_mode_and_invalid_dimensions_make_no_requests() {
    scenario(vec![], true, 768, true).await;
    scenario(vec![], false, 0, false).await;
}

#[test]
fn bootstrap_rejects_incompatible_or_malformed_vectors() {
    assert!(validate_collection(&compatible(768), 768).is_ok());
    assert!(validate_collection(&compatible(1024), 768).is_err());
    for vectors in [
        json!(null),
        json!({"dense": {"size": 768, "distance": "Cosine"}}),
        json!({"size": 768, "distance": "Dot"}),
        json!({"size": "768", "distance": "Cosine"}),
    ] {
        assert!(validate_collection(
            &json!({"result":{"config":{"params":{"vectors":vectors}}}}),
            768
        )
        .is_err());
    }
}

#[tokio::test]
#[ignore = "requires explicit ephemeral Qdrant fixture; executed by CI"]
async fn bootstrap_real_qdrant_preserves_points_and_search() -> anyhow::Result<()> {
    use anyhow::ensure;
    let url = std::env::var("MAINRAG_QDRANT_TEST_URL")?;
    ensure!(std::env::var("MAINRAG_QDRANT_FIXTURE_ACK")?.as_str() == "ephemeral-only");
    let name = format!("issue9_{}", Uuid::new_v4().simple());
    let client = configured(url, &name);
    let result = async {
        // Two real startup attempts may race; either create or validated readback
        // is acceptable, never reset/recreate existing state.
        let (a, b) = tokio::join!(client.ensure_chunk_collection(false, 768), client.ensure_chunk_collection(false, 768));
        a?;
        b?;
        let details: Value = client.client.get(format!("{}/collections/{name}", client.base_url))
            .header("api-key", &client.api_key).send().await?.error_for_status()?.json().await?;
        ensure!(details["result"]["config"]["hnsw_config"]["m"] == 16);
        ensure!(details["result"]["config"]["hnsw_config"]["ef_construct"] == 200);
        ensure!(details["result"]["config"]["quantization_config"]["scalar"]["type"] == "int8");
        ensure!(details["result"]["config"]["quantization_config"]["scalar"]["always_ram"] == false);
        let mut vector = vec![0.0f32; 768];
        vector[0] = 1.0;
        let payload = json!({"chunk_id": 7, "source_id": 1, "public_fixture": "first-boot"});
        client.client.put(format!("{}/collections/{name}/points?wait=true", client.base_url))
            .header("api-key", &client.api_key)
            .json(&json!({"points":[{"id":7,"vector":vector,"payload":payload}]}))
            .send().await?.error_for_status()?;
        let point_url = format!("{}/collections/{name}/points/7", client.base_url);
        let before: Value = client.client.get(&point_url).header("api-key", &client.api_key)
            .send().await?.error_for_status()?.json().await?;
        for _ in 0..3 { client.ensure_chunk_collection(false, 768).await?; }
        ensure!(client.ensure_chunk_collection(false, 1024).await.is_err());
        let after: Value = client.client.get(&point_url).header("api-key", &client.api_key)
            .send().await?.error_for_status()?.json().await?;
        ensure!(before["result"] == after["result"]);
        ensure!(after["result"]["id"] == 7 && after["result"]["payload"] == payload);
        let results = client.search_chunks_with_tenant(vector, 10, &TenantContext::Admin, Some(1)).await?;
        ensure!(results.len() == 1 && results[0].0 == 7);
        ensure!(client.count_by_source(1).await? == 1);
        println!("issue9: real Qdrant create/concurrent startup/repeated startup/mismatch rejection/point preservation/vector search PASS");
        Ok::<(), anyhow::Error>(())
    }.await;
    // Only this test's generated collection is removed. A crash is cleaned by
    // the ephemeral CI service lifetime; no existing collection is targeted.
    let cleanup = client
        .client
        .delete(format!("{}/collections/{name}", client.base_url))
        .header("api-key", &client.api_key)
        .send()
        .await?
        .error_for_status();
    result?;
    cleanup?;
    Ok(())
}

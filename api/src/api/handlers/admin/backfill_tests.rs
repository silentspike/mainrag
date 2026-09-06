//! Real-route regression coverage for maintenance in CPU mode.
//! No environment mutation, database connection, or external service is needed.

use super::*;
use axum::{body::Body, http::Request, response::IntoResponse, Router};
use serde_json::{json, Value};
use std::net::TcpListener;
use std::time::Duration;
use tower::ServiceExt;

fn isolated_state(cpu_mode: bool, listener: &TcpListener) -> Arc<AppState> {
    let address = listener.local_addr().unwrap();
    let url = format!("http://{address}");
    let config: crate::config::Config = serde_json::from_value(json!({
        "server": {
            "host": "127.0.0.1", "port": 0,
            "cors_origins": [], "api_key_pepper": "fixture-only-backfill-pepper",
            "http_connect_timeout_secs": 1, "http_request_timeout_secs": 1,
            "db_pool_wait_timeout_secs": 1, "qdrant_backfill_active": false,
            "cpu_mode": cpu_mode, "backfill_oversampling_factor": 3
        },
        "database": {
            "host": "127.0.0.1", "port": address.port(), "name": "fixture",
            "user": "fixture", "password": "fixture-only", "max_connections": 1,
            "tls_mode": "disable"
        },
        "qdrant": {
            "url": url, "chunk_collection": "fixture_chunks",
            "code_collection": "fixture_code"
        },
        "tei": { "url": url, "reranker_url": url },
        "jwt": { "secret": "fixture-only-backfill-jwt-secret-32-characters", "expiry_hours": 1 },
        "ocr": {}, "storage_v2_pack_root": "unused-fixture-packs",
        "storage_v2_pack_io_buffer_bytes": 4096
    }))
    .unwrap();
    let db = crate::db::postgres::create_pool(&config.database).unwrap();
    // Any awaited maintenance DB acquisition fails immediately. Reaching it
    // therefore produces 503, never the expected CPU-mode 400.
    db.close();
    let tei = Arc::new(crate::services::TeiClient::new(&config.tei));
    let qdrant = Arc::new(crate::services::QdrantClient::new(&config.qdrant));
    let search = crate::services::SearchService::new(
        db.clone(),
        tei.clone(),
        qdrant.clone(),
        Arc::new(crate::services::RerankerService::new(Some(url))),
        Arc::new(crate::services::QueryExpander::new(
            &config.qdrant,
            tei.clone(),
            false,
        )),
        false,
        3,
        cpu_mode,
        Default::default(),
    );
    Arc::new(AppState {
        instance_id: uuid::Uuid::new_v4(),
        rls_client: crate::db::RlsClient::new(db.clone()),
        health_pool: crate::db::HealthPool::new(db.clone()),
        intelligence: crate::services::intelligence::IntelligenceService::new(db.clone()).unwrap(),
        db,
        tei,
        qdrant,
        search,
        config,
        revoked_tokens: moka::sync::Cache::new(10),
        sse_active_streams: Arc::new(std::sync::atomic::AtomicUsize::new(0)),
        domain_registry: None,
    })
}

fn dependency_trap() -> TcpListener {
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    listener.set_nonblocking(true).unwrap();
    listener
}

fn assert_no_dependency_connection(listener: &TcpListener) {
    assert_eq!(
        listener.accept().unwrap_err().kind(),
        std::io::ErrorKind::WouldBlock,
        "maintenance must not connect to TEI, Qdrant, or PostgreSQL"
    );
}

async fn post(app: &Router, path: &str, token: Option<&str>) -> axum::response::Response {
    let mut request = Request::builder()
        .method("POST")
        .uri(path)
        .header("Content-Type", "application/json");
    if let Some(token) = token {
        request = request.header("Authorization", format!("Bearer {token}"));
    }
    tokio::time::timeout(
        Duration::from_secs(2),
        app.clone().oneshot(request.body(Body::from("{}")).unwrap()),
    )
    .await
    .expect("mode and authentication rejection must not wait on services")
    .unwrap()
}

#[tokio::test]
async fn cpu_backfill_routes_reject_without_io_and_preserve_auth_and_intelligence() {
    let listener = dependency_trap();
    let state = isolated_state(true, &listener);
    let admin = crate::auth::Claims::new(
        "00000000-0000-0000-0000-000000000052",
        "fixture@example.invalid",
        true,
        1,
    );
    let token = crate::auth::jwt::create_token(&admin, &state.config.jwt.secret).unwrap();
    let mut user = admin.clone();
    user.is_admin = false;
    user.role = "user".to_string();
    let user_token = crate::auth::jwt::create_token(&user, &state.config.jwt.secret).unwrap();
    // Build the production router once: its metrics recorder is process-global.
    let app = crate::api::create_router(state.clone());

    for (path, dependency) in [
        ("/api/v1/admin/backfill/orphaned", "embedding service"),
        ("/api/v1/admin/backfill/qdrant-user-ids", "Qdrant"),
    ] {
        assert_eq!(
            post(&app, path, None).await.status(),
            StatusCode::UNAUTHORIZED
        );
        assert_eq!(
            post(&app, path, Some(&user_token)).await.status(),
            StatusCode::FORBIDDEN
        );
        for _ in 0..2 {
            let response = post(&app, path, Some(&token)).await;
            assert_eq!(response.status(), StatusCode::BAD_REQUEST);
            assert_eq!(response.headers()["content-type"], "application/json");
            let bytes = axum::body::to_bytes(response.into_body(), 4096)
                .await
                .unwrap();
            let body: Value = serde_json::from_slice(&bytes).unwrap();
            assert_eq!(body.as_object().unwrap().len(), 2);
            assert_eq!(body["status"], 400);
            let message = body["error"].as_str().unwrap();
            assert!(message.contains(dependency));
            assert!(message.contains("CPU mode"));
            assert!(message.contains("mainrag --gpu"));
            assert!(message.contains("retry"));
        }
    }

    // Intelligence is PostgreSQL-backed and must pass the CPU-mode boundary.
    // The deliberately closed pool proves reachability, not backfill success.
    assert_eq!(
        post(&app, "/api/v1/admin/backfill/intelligence", Some(&token))
            .await
            .status(),
        StatusCode::SERVICE_UNAVAILABLE
    );
    state.revoked_tokens.insert(admin.jti, ());
    for path in [
        "/api/v1/admin/backfill/orphaned",
        "/api/v1/admin/backfill/qdrant-user-ids",
    ] {
        assert_eq!(
            post(&app, path, Some(&token)).await.status(),
            StatusCode::UNAUTHORIZED
        );
    }
    assert_no_dependency_connection(&listener);
}

#[tokio::test]
async fn full_mode_orphaned_backfill_reaches_existing_database_path() {
    let listener = dependency_trap();
    let state = isolated_state(false, &listener);
    let claims = Arc::new(crate::auth::Claims::new(
        "00000000-0000-0000-0000-000000000052",
        "fixture@example.invalid",
        true,
        1,
    ));
    let error = admin_backfill_orphaned(State(state), Extension(claims))
        .await
        .unwrap_err();
    assert!(matches!(&error, AppError::Pool(_)));
    assert_eq!(
        error.into_response().status(),
        StatusCode::SERVICE_UNAVAILABLE
    );
    assert_no_dependency_connection(&listener);
}

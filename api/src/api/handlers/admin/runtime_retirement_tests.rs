//! Exercise authenticated production routes against the final retirement boundary.

use super::*;
use axum::{body::Body, http::Request};
use serde_json::json;
use std::net::TcpListener;
use tower::ServiceExt;

#[tokio::test]
async fn runtime_retirement_blocks_bootstrap_preserves_auth_and_native_routes() {
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    listener.set_nonblocking(true).unwrap();
    let mut state = super::backfill_tests::isolated_state(false, &listener);
    let config = &mut Arc::get_mut(&mut state).unwrap().config.server;
    config.storage_v2_default_read_manifest_sha256 = Some("a".repeat(64));
    config.storage_v2_active_ingest_commit_sha = Some("b".repeat(40));
    // Activation alone must still allow the old-hit coverage bootstrap.
    assert!(super::super::legacy_hits::require_legacy_bootstrap(config).is_ok());
    config.storage_v2_legacy_retired_manifest_sha256 = Some("c".repeat(64));
    config.validate_storage_v2_runtime().unwrap();
    let admin = crate::auth::Claims::new(
        "00000000-0000-4000-8000-000000000068",
        "fixture@example.invalid",
        true,
        1,
    );
    let token = crate::auth::jwt::create_token(&admin, &state.config.jwt.secret).unwrap();
    let mut user = admin.clone();
    user.is_admin = false;
    user.role = "user".into();
    let user_token = crate::auth::jwt::create_token(&user, &state.config.jwt.secret).unwrap();
    let app = crate::api::create_router(state.clone());
    let retired = [
        (
            "/api/v1/admin/sources/1/storage-v2-release-candidate-build",
            json!({"commit_sha":"b".repeat(40)}),
        ),
        (
            "/api/v1/admin/sources/1/storage-v2-candidate-query-evidence",
            json!({"generation_id":1,"commit_sha":"b".repeat(40),"query":"fixture",
                "candidate_occurrence_ids":[],"current_chunk_ids":[]}),
        ),
        (
            "/api/v1/admin/sources/1/storage-v2-legacy-hit-inventory",
            json!({"generation_id":1,"after_file_id":0}),
        ),
        (
            "/api/v1/admin/sources/1/storage-v2-legacy-hit-producer",
            json!({"generation_id":1,"file_id":1,"expected_file_sha256":"d".repeat(64),
                "expected_file_revision":1,"expected_legacy_epoch":1,"after_hit_id":0,
                "source_spool_budget_bytes":4096}),
        ),
    ];
    for (path, payload) in &retired {
        for (authorization, expected) in [
            (None, StatusCode::UNAUTHORIZED),
            (Some(&user_token), StatusCode::FORBIDDEN),
            (Some(&token), StatusCode::CONFLICT),
        ] {
            let mut request = Request::builder()
                .method("POST")
                .uri(*path)
                .header("Content-Type", "application/json");
            if let Some(token) = authorization {
                request = request.header("Authorization", format!("Bearer {token}"));
            }
            let response = tokio::time::timeout(
                std::time::Duration::from_secs(2),
                app.clone()
                    .oneshot(request.body(Body::from(payload.to_string())).unwrap()),
            )
            .await
            .expect("retirement must reject before resource or database I/O")
            .unwrap();
            assert_eq!(response.status(), expected, "{path}");
            if expected == StatusCode::CONFLICT {
                let bytes = axum::body::to_bytes(response.into_body(), 4096)
                    .await
                    .unwrap();
                let value: serde_json::Value = serde_json::from_slice(&bytes).unwrap();
                assert!(value["error"]
                    .as_str()
                    .unwrap()
                    .contains("legacy bootstrap is retired"));
            }
        }
    }
    // A closed pool proves these native paths still pass retirement, not their
    // successful database behavior. The persisted native fixture covers that.
    for (path, payload) in [
        (
            "/api/v1/legacy-hits/resolve",
            json!({"source_id":1,"old_hit_id":"chunk:1","generation":"1"}),
        ),
        (
            "/api/v1/admin/sources/1/storage-v2-legacy-hit-progress",
            json!({"generation_id":1,"file_id":1}),
        ),
        (
            "/api/v1/admin/sources/1/storage-v2-release-candidate-verify",
            json!({"generation_id":1}),
        ),
    ] {
        let response = app
            .clone()
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri(path)
                    .header("Content-Type", "application/json")
                    .header("Authorization", format!("Bearer {token}"))
                    .body(Body::from(payload.to_string()))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(response.status(), StatusCode::SERVICE_UNAVAILABLE, "{path}");
    }
    assert_eq!(
        listener.accept().unwrap_err().kind(),
        std::io::ErrorKind::WouldBlock
    );
}

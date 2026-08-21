use axum::http::{header::HeaderName, HeaderValue};
use axum::{
    extract::State,
    middleware,
    routing::{delete, get, patch, post},
    Extension, Router,
};
use metrics_exporter_prometheus::PrometheusHandle;
use std::sync::Arc;
use std::time::Duration;
use tower_http::{
    compression::CompressionLayer,
    cors::{AllowOrigin, Any, CorsLayer},
    limit::RequestBodyLimitLayer,
    set_header::SetResponseHeaderLayer,
    timeout::TimeoutLayer,
    trace::TraceLayer,
};

use crate::api::handlers;
use crate::auth::middleware::{admin_middleware, auth_middleware, AuthLayer};
use crate::metrics::{metrics_handler, metrics_middleware, setup_metrics};
use crate::ratelimit::{create_keyed_rate_limiter, keyed_rate_limit_middleware, KeyedRateLimiter};
use crate::AppState;

pub fn create_router(state: Arc<AppState>) -> Router {
    // Setup metrics
    let metrics_handle = setup_metrics();

    // W2: Keyed rate limiter for auth routes (10/min per IP to prevent brute force)
    let rate_limiter = create_keyed_rate_limiter(10);

    // Setup auth layer (Sprint 2.8: includes revoked_tokens cache for jti check)
    let auth_layer = AuthLayer::new(
        &state.config,
        state.revoked_tokens.clone(),
        state.db.clone(),
    );

    let cors = {
        // H6: Fail-fast on invalid CORS origins (don't silently skip)
        let mut origins: Vec<HeaderValue> = Vec::new();
        for origin_str in &state.config.server.cors_origins {
            match origin_str.parse::<HeaderValue>() {
                Ok(val) => origins.push(val),
                Err(e) => {
                    tracing::error!(origin = %origin_str, error = %e,
                        "Invalid CORS origin — fix CORS_ORIGINS env var");
                    // Don't silently skip — this is a config error
                    panic!(
                        "Invalid CORS origin '{}': {}. Fix CORS_ORIGINS.",
                        origin_str, e
                    );
                }
            }
        }
        if origins.is_empty() {
            tracing::info!("CORS disabled (CORS_ORIGINS empty/unset) — no Access-Control headers");
            // Fail-closed: no allowed origins = browser will block cross-origin requests
            CorsLayer::new()
        } else {
            tracing::info!("CORS enabled for {} origins", origins.len());
            CorsLayer::new()
                .allow_origin(AllowOrigin::list(origins))
                .allow_methods(Any)
                .allow_headers(Any)
                .expose_headers([
                    HeaderName::from_static("x-search-mode"),
                    HeaderName::from_static("x-request-id"),
                ])
        }
    };

    // Sprint 6.2: Security headers — API-only (no frontend)
    let nosniff = SetResponseHeaderLayer::overriding(
        HeaderName::from_static("x-content-type-options"),
        HeaderValue::from_static("nosniff"),
    );
    let frame_deny = SetResponseHeaderLayer::overriding(
        HeaderName::from_static("x-frame-options"),
        HeaderValue::from_static("DENY"),
    );
    let csp = SetResponseHeaderLayer::overriding(
        HeaderName::from_static("content-security-policy"),
        HeaderValue::from_static("default-src 'none'; frame-ancestors 'none'"),
    );
    // L1: Prevent caching of API responses (especially auth tokens)
    let no_cache = SetResponseHeaderLayer::overriding(
        HeaderName::from_static("cache-control"),
        HeaderValue::from_static("no-cache, no-store, must-revalidate"),
    );

    // W4: SSE route WITHOUT TimeoutLayer (SSE streams must not be killed after 30s)
    // Still protected by auth + admin middleware
    let sse_routes = Router::new()
        .route(
            "/api/v1/admin/processes/stream",
            get(handlers::admin_process_stats_stream),
        )
        .layer(middleware::from_fn(admin_middleware))
        .layer(middleware::from_fn({
            let auth = auth_layer.clone();
            move |req, next| {
                let auth = Extension(auth.clone());
                async move { auth_middleware(auth, req, next).await }
            }
        }));

    // Long-running admin routes with a 24h timeout. Source sync and resumable
    // storage-v2 candidate construction are intentionally source-bounded but
    // can exceed ten minutes for multi-gigabyte sources.
    let long_running_routes = Router::new()
        .route(
            "/api/v1/admin/sources/:id/sync",
            post(handlers::admin_sync_source),
        )
        .route(
            "/api/v1/admin/sources/:id/sync-files",
            post(handlers::admin_sync_files),
        )
        .route(
            "/api/v1/admin/backfill/orphaned",
            post(handlers::admin_backfill_orphaned),
        )
        .route(
            "/api/v1/admin/backfill/qdrant-user-ids",
            post(handlers::admin_backfill_qdrant_user_ids),
        );
    #[cfg(feature = "storage-v2-retrieval")]
    let long_running_routes = long_running_routes.route(
        "/api/v1/admin/sources/:id/storage-v2-release-candidate-build",
        post(handlers::admin_build_release_candidate),
    );
    let long_running_routes = long_running_routes
        .layer(middleware::from_fn(admin_middleware))
        .layer(middleware::from_fn({
            let auth = auth_layer.clone();
            move |req, next| {
                let auth = Extension(auth.clone());
                async move { auth_middleware(auth, req, next).await }
            }
        }))
        .layer(TimeoutLayer::with_status_code(
            axum::http::StatusCode::REQUEST_TIMEOUT,
            Duration::from_secs(24 * 3600),
        ));

    // Timed routes: everything except SSE and long-running, with 30s timeout
    let timed_routes = Router::new()
        // H7: Liveness probes public (for load balancers), detail health behind auth
        .route("/healthz", get(handlers::liveness))
        .route("/readyz", get(handlers::liveness))
        // Model information endpoint (Phase 14: Model Upgrades)
        .route("/models", get(handlers::model_info))
        // Metrics endpoint (no auth for Prometheus scraping)
        .route("/metrics", get(metrics_endpoint))
        // API v1 (with rate limiting) — SSE route excluded, handled separately above
        .nest("/api/v1", api_v1_routes(rate_limiter, auth_layer))
        // Sprint 6.1: 120s request timeout — search on 800k+ chunks needs time
        // (FTS + Qdrant vector search + TEI reranker = can exceed 30s)
        .layer(TimeoutLayer::with_status_code(
            axum::http::StatusCode::REQUEST_TIMEOUT,
            Duration::from_secs(120),
        ));

    // Merge: specific routes first, then timed routes (catch-all)
    Router::new()
        .merge(sse_routes)
        .merge(long_running_routes)
        .merge(timed_routes)
        // Global middleware (applied to both SSE and timed routes)
        .layer(middleware::from_fn(metrics_middleware))
        .layer(CompressionLayer::new())
        .layer(TraceLayer::new_for_http())
        .layer(cors)
        // Sprint 6.2: Security headers
        .layer(nosniff)
        .layer(frame_deny)
        .layer(csp)
        .layer(no_cache)
        .layer(Extension(metrics_handle))
        // State
        .with_state(state)
}

fn api_v1_routes(rate_limiter: KeyedRateLimiter, auth_layer: AuthLayer) -> Router<Arc<AppState>> {
    Router::new()
        // Auth routes (public, rate limited — Sprint 2.1: ONLY auth gets rate limiting)
        // Sprint 6.1: 64KB body limit for auth payloads
        .nest(
            "/auth",
            auth_routes(rate_limiter, auth_layer.clone())
                .layer(RequestBodyLimitLayer::new(64 * 1024)),
        )
        // Authenticated routes (API-Key or JWT required, NO rate limit on search hot-path)
        // Sprint 6.1: 1MB body limit for search/MCP queries
        .nest(
            "/",
            authenticated_routes(auth_layer.clone()).layer(RequestBodyLimitLayer::new(1024 * 1024)),
        )
        // Admin routes (auth + admin role required, NO rate limit)
        // Sprint 6.1: 10MB body limit for admin upload/sync payloads
        .nest(
            "/admin",
            admin_routes(auth_layer).layer(RequestBodyLimitLayer::new(10 * 1024 * 1024)),
        )
}

/// Routes that require authentication (API-Key for agents, JWT for admin).
/// Moved from public_routes() to enforce auth on all data endpoints.
fn authenticated_routes(auth_layer: AuthLayer) -> Router<Arc<AppState>> {
    let routes = Router::new()
        // H7: Detailed health check requires auth (exposes service status)
        .route("/health", get(handlers::health_check))
        // Search
        .route("/search", post(handlers::hybrid_search))
        .route("/search/keyword", post(handlers::keyword_search))
        // Code Intelligence (Phase 10)
        .route("/intelligence/symbols", get(handlers::search_symbols))
        .route(
            "/intelligence/symbols/:id/callgraph",
            get(handlers::get_symbol_callgraph),
        )
        .route(
            "/intelligence/files/:file_id/symbols",
            get(handlers::list_file_symbols),
        )
        .route("/intelligence/callers", get(handlers::find_callers_by_name))
        .route("/intelligence/callees", get(handlers::find_callees_by_name))
        .route("/intelligence/call-chain", get(handlers::find_call_chain))
        // Intelligence Layer: Symbol Cards, Path Explanation, Negative Evidence
        .route("/intelligence/cards", get(handlers::browse_symbol_cards))
        .route("/intelligence/cards/:id", get(handlers::get_symbol_card))
        .route("/intelligence/explain_path", post(handlers::explain_path))
        .route(
            "/intelligence/negative_evidence",
            post(handlers::create_negative_evidence).get(handlers::search_negative_evidence),
        )
        .route("/intelligence/ownership", get(handlers::get_ownership))
        .route("/intelligence/explore", post(handlers::explore))
        // MCP Server (Phase 11b) - Claude/LLM integration
        .route("/mcp/tools", get(handlers::list_mcp_tools))
        .route("/mcp/tools/execute", post(handlers::execute_mcp_tool))
        .route("/mcp/protocol", get(handlers::get_mcp_protocol_info))
        // Sources (read-only)
        .route("/sources", get(handlers::list_sources))
        .route("/sources/:id", get(handlers::get_source));
    #[cfg(feature = "storage-v2-intelligence")]
    let routes = routes.route(
        "/intelligence/shadow",
        get(handlers::shadow_intelligence_command),
    );
    #[cfg(feature = "storage-v2-retrieval")]
    let routes = routes.route(
        "/sources/:id/shadow-state",
        get(handlers::shadow_source_state),
    );
    routes
        // Auth middleware (validates JWT or API-Key and adds Claims extension)
        .layer(middleware::from_fn(move |req, next| {
            let auth = Extension(auth_layer.clone());
            async move { auth_middleware(auth, req, next).await }
        }))
}

fn auth_routes(rate_limiter: KeyedRateLimiter, auth_layer: AuthLayer) -> Router<Arc<AppState>> {
    // Protected auth routes (require JWT)
    let protected_routes = Router::new()
        .route("/me", get(handlers::get_profile))
        .route("/me", patch(handlers::update_profile))
        .route("/change-password", post(handlers::change_password))
        .route("/logout", post(handlers::logout))
        // Auth middleware (validates JWT and adds Claims extension)
        .layer(middleware::from_fn(move |req, next| {
            let auth = Extension(auth_layer.clone());
            async move { auth_middleware(auth, req, next).await }
        }));

    // Public auth routes (no JWT required)
    // Sprint 4.3: Registration endpoint REMOVED — agents use API-Keys, admin created via init-admin.sh
    let public_routes = Router::new().route("/login", post(handlers::login));

    // Merge both route sets
    Router::new()
        .merge(protected_routes)
        .merge(public_routes)
        // W2: Keyed rate limiting on auth routes only (10/min per IP to prevent brute force)
        .layer(middleware::from_fn(move |req, next| {
            let limiter = rate_limiter.clone();
            async move { keyed_rate_limit_middleware(limiter, req, next).await }
        }))
}

fn admin_routes(auth_layer: AuthLayer) -> Router<Arc<AppState>> {
    let routes = Router::new()
        // Source management
        .route("/sources", get(handlers::admin_list_sources))
        .route("/sources", post(handlers::admin_create_source))
        .route("/sources/:id", patch(handlers::admin_update_source))
        .route("/sources/:id", delete(handlers::admin_delete_source))
        .route("/sources/:id/stats", get(handlers::admin_source_stats))
        // sync + sync-files moved to long_running_routes (10min timeout)
        // Watch mode (Phase 11a) - monitor files and auto-index
        .route("/watch/status", get(handlers::get_watch_status_all))
        .route("/watch/status/:source_id", get(handlers::get_watch_status))
        .route("/watch/toggle/:source_id", patch(handlers::toggle_watch))
        .route("/watch/stats", get(handlers::get_watch_stats))
        // User management
        .route("/users", get(handlers::admin_list_users))
        .route("/users/:id", get(handlers::admin_get_user))
        .route("/users/:id", patch(handlers::admin_update_user))
        .route("/users/:id", delete(handlers::admin_delete_user))
        // Process monitoring (moved from public to admin)
        // W4: /processes/stream moved to top-level SSE routes (no TimeoutLayer)
        .route("/processes", get(handlers::admin_process_stats))
        // System stats
        .route("/stats", get(handlers::admin_system_stats))
        // Agent management (Sprint 2.7: API-Key provisioning)
        .route("/agents", post(handlers::admin_create_agent))
        .route("/agents", get(handlers::admin_list_agents))
        .route("/agents/:id", delete(handlers::admin_revoke_agent))
        .route("/agents/:id/rotate", post(handlers::admin_rotate_agent_key));
    #[cfg(feature = "storage-v2-retrieval")]
    let routes = routes
        .route(
            "/sources/:id/storage-v2-shadow-slice",
            post(handlers::admin_run_shadow_slice),
        )
        .route(
            "/sources/:id/storage-v2-dual-read",
            post(handlers::admin_record_dual_read),
        )
        .route(
            "/sources/:id/storage-v2-release-candidate-qualify",
            post(handlers::admin_qualify_release_candidate),
        )
        .route(
            "/sources/:id/storage-v2-shadow-runs/:run_id/cleanup",
            post(handlers::admin_cleanup_shadow_slice),
        );
    routes
        // backfill endpoints moved to long_running_routes (10min timeout)
        // Admin middleware (checks is_admin claim)
        .layer(middleware::from_fn(admin_middleware))
        // Auth middleware (validates JWT or API-Key)
        .layer(middleware::from_fn(move |req, next| {
            let auth = Extension(auth_layer.clone());
            async move { auth_middleware(auth, req, next).await }
        }))
    // Sprint 2.1: NO rate limiting on admin routes (only auth routes get rate limits)
}

async fn metrics_endpoint(
    State(state): State<Arc<AppState>>,
    headers: axum::http::HeaderMap,
    Extension(handle): Extension<PrometheusHandle>,
) -> axum::response::Response {
    use axum::http::StatusCode;
    use axum::response::IntoResponse;

    // S3: Only accept Bearer token in Authorization header (no query param — leaks to logs)
    if let Some(ref expected_token) = state.config.server.metrics_token {
        let bearer_ok = headers
            .get("authorization")
            .and_then(|v| v.to_str().ok())
            .map(|v: &str| {
                let token_part = v.strip_prefix("Bearer ").unwrap_or(v);
                token_part == expected_token.as_str()
            })
            .unwrap_or(false);

        if !bearer_ok {
            return StatusCode::UNAUTHORIZED.into_response();
        }
    }

    metrics_handler(handle).await.into_response()
}

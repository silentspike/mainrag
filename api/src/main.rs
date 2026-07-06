use std::net::SocketAddr;
use std::sync::Arc;

use clap::{Parser, ValueEnum};
use tracing_subscriber::{layer::SubscriberExt, util::SubscriberInitExt};

mod api;
mod auth;
mod config;
mod db;
mod error;
mod metrics;
mod plugins;
mod ratelimit;
mod services;
mod utils;

use config::Config;
use db::postgres;
use services::domain_profile::DomainProfileRegistry;
use services::intelligence::IntelligenceService;
use services::{OutboxWorker, QdrantClient, RerankerService, SearchService, TeiClient};

pub struct AppState {
    /// Raw pool — ONLY for service construction (IndexService, OutboxWorker, AuthLayer).
    /// Handlers MUST NOT use this directly. Use `rls_client` instead.
    pub db: db::PostgresPool,
    /// K3: Transaction-scoped RLS client. All handler DB access goes through this.
    pub rls_client: db::RlsClient,
    /// K3-FIX1: Restricted pool for health checks and monitoring only.
    pub health_pool: db::HealthPool,
    pub tei: Arc<TeiClient>,
    pub qdrant: Arc<QdrantClient>,
    pub search: SearchService,
    pub config: Config,
    /// Sprint 2.8: Revoked JWT IDs (jti) cache for O(1) lookup
    pub revoked_tokens: moka::sync::Cache<String, ()>,
    /// Sprint 4.7: Active SSE stream counter for concurrency limiting (max 5)
    pub sse_active_streams: Arc<std::sync::atomic::AtomicUsize>,
    /// Intelligence Layer: Symbol Cards, Path Explanation, Annotations
    pub intelligence: IntelligenceService,
    /// Domain Profile Registry (loaded from data/domain_profiles/*.toml)
    pub domain_registry: Option<DomainProfileRegistry>,
}

// ============================================================================
// CLI Arguments
// ============================================================================

#[derive(Parser, Debug)]
#[command(name = "mainrag-api")]
#[command(about = "MAINRAG API Server - RAG/Search infrastructure")]
struct Cli {
    /// Run mode
    #[arg(short, long, default_value = "api")]
    mode: RunMode,
}

#[derive(Debug, Clone, Copy, ValueEnum)]
enum RunMode {
    /// Full API server (default)
    Api,
}

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    // Parse CLI arguments
    let cli = Cli::parse();

    // Initialize tracing
    tracing_subscriber::registry()
        .with(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| "mainrag_api=debug,tower_http=debug".into()),
        )
        .with(tracing_subscriber::fmt::layer())
        .init();

    tracing::info!("Starting mainrag-api in {:?} mode", cli.mode);

    // Load configuration
    let config = Config::from_env()?;
    tracing::info!("Configuration loaded");

    // Log PDF backend info
    plugins::pdf::log_backend_info();

    // Create database pool
    let db_pool = postgres::create_pool(&config.database)?;
    postgres::test_connection(&db_pool).await?;
    tracing::info!("PostgreSQL connection established");

    // Validate DEFAULT_USER_ID at startup - HARD FAIL if invalid!
    // Without a valid admin user, RLS context cannot be set for system tasks.
    postgres::validate_default_user(&db_pool).await?;

    match cli.mode {
        RunMode::Api => run_api_server(db_pool, config).await,
    }
}

// ============================================================================
// Run Modes
// ============================================================================

/// Full API server with background event processor
async fn run_api_server(db_pool: db::PostgresPool, config: Config) -> anyhow::Result<()> {
    let cpu_mode = config.server.cpu_mode;
    if cpu_mode {
        tracing::warn!(
            "MAINRAG CPU MODE — vector search/rerank/expansion disabled, FTS + intelligence only"
        );
    }

    // Create Qdrant client
    let qdrant = Arc::new(QdrantClient::new(&config.qdrant));
    if cpu_mode {
        tracing::warn!("CPU mode: skipping Qdrant startup health check");
    } else {
        qdrant.health_check().await?;
        tracing::info!("Qdrant connection established (on_disk mode)");
    }

    // K4-FIX4: Auto-create user_id payload index for tenant isolation (idempotent)
    if cpu_mode {
        tracing::warn!("CPU mode: skipping Qdrant user_id payload index creation");
    } else {
        match qdrant.create_payload_index("user_id", "keyword").await {
            Ok(()) => tracing::info!("Qdrant user_id payload index ensured"),
            Err(e) => tracing::warn!(
                "Could not create Qdrant user_id index (may already exist): {}",
                e
            ),
        }
    }

    // Create TEI client
    let tei = Arc::new(TeiClient::new(&config.tei));
    if cpu_mode {
        tracing::warn!("CPU mode: skipping TEI startup health check");
    } else {
        tei.health_check().await?;
        tracing::info!("TEI connection established");
    }

    // Create Reranker service (BGE reranker-base on port 8082)
    let reranker = Arc::new(RerankerService::new(config.tei.reranker_url.clone()));
    match reranker.health_check().await {
        Ok(_) => tracing::info!("Reranker connection established"),
        Err(e) => tracing::warn!(
            "Reranker health check failed (will degrade search quality): {}",
            e
        ),
    }

    // Create QueryExpander for synonym-based query expansion
    // Feature flag: QUERY_EXPANSION_ENABLED (default: true)
    let query_expansion_enabled_from_env = std::env::var("QUERY_EXPANSION_ENABLED")
        .map(|v| v == "true" || v == "1")
        .unwrap_or(true);
    let query_expansion_enabled = !cpu_mode && query_expansion_enabled_from_env;
    let query_expander = Arc::new(services::QueryExpander::new(
        &config.qdrant,
        tei.clone(),
        query_expansion_enabled,
    ));
    if cpu_mode {
        tracing::warn!("CPU mode: query expansion forced disabled");
    } else if query_expansion_enabled {
        tracing::info!("Query expansion enabled (synonyms_v1 collection)");
    } else {
        tracing::info!("Query expansion disabled");
    }

    // Domain Profile Registry (must be loaded before SearchService for domain-scoped ranking)
    let profiles_dir = services::domain_profile::default_profiles_dir();
    let domain_registry = match DomainProfileRegistry::load_from_dir(&profiles_dir) {
        Ok(registry) => {
            let count = registry.profiles().len();
            if count > 0 {
                tracing::info!("Loaded {} domain profile(s) from {:?}", count, profiles_dir);
            }
            Some(registry)
        }
        Err(e) => {
            tracing::warn!(
                "Failed to load domain profiles from {:?}: {}",
                profiles_dir,
                e
            );
            None
        }
    };

    // Extract domain source names for search ranking boost
    let domain_source_names: std::collections::HashSet<String> = domain_registry
        .as_ref()
        .map(|reg| {
            reg.profiles()
                .iter()
                .flat_map(|p| p.code_sources.iter().chain(p.support_sources.iter()))
                .cloned()
                .collect()
        })
        .unwrap_or_default();

    // Create search service (Qdrant + PostgreSQL FTS hybrid + reranking + query expansion)
    let search = SearchService::new(
        db_pool.clone(),
        tei.clone(),
        qdrant.clone(),
        reranker.clone(),
        query_expander,
        config.server.qdrant_backfill_active,
        config.server.backfill_oversampling_factor,
        config.server.cpu_mode,
        domain_source_names,
    );

    // Sprint 2.8: Create revoked-tokens cache (max 10k entries, TTL = JWT expiry)
    let revoked_tokens = moka::sync::Cache::builder()
        .max_capacity(10_000)
        .time_to_live(std::time::Duration::from_secs(
            config.jwt.expiry_hours * 3600,
        ))
        .build();

    // Sprint 2.8 Startup-Gate: Load all revoked JTIs from DB into cache
    // HTTP listener MUST NOT start until this is complete
    {
        let client = db_pool
            .get()
            .await
            .expect("FATAL: Cannot connect to DB for revoked-token warmup");
        let rows = client
            .query(
                "SELECT jti FROM revoked_tokens WHERE expires_at > NOW()",
                &[],
            )
            .await;
        match rows {
            Ok(rows) => {
                let count = rows.len();
                for row in rows {
                    let jti: String = row.get("jti");
                    revoked_tokens.insert(jti, ());
                }
                tracing::info!("Startup gate: loaded {} revoked JTIs into cache", count);
            }
            Err(e) => {
                // Table might not exist yet — warn but don't fail startup
                tracing::warn!("Could not load revoked tokens (table may not exist): {}", e);
            }
        }
    }

    // K4-FIX3: Log and meter backfill status
    if config.server.qdrant_backfill_active {
        tracing::warn!(
            "QDRANT_BACKFILL_ACTIVE=true — PG-RLS post-filter + oversampling enabled for search"
        );
        ::metrics::gauge!("mainrag_qdrant_backfill_active").set(1.0);
    } else {
        tracing::info!("Qdrant backfill inactive (tenant isolation via Qdrant filter only)");
        ::metrics::gauge!("mainrag_qdrant_backfill_active").set(0.0);
    }

    // K3: Create RLS client wrapping the same pool (transaction-scoped RLS)
    let rls_client = db::RlsClient::new(db_pool.clone());

    // K3-FIX1: HealthPool for non-RLS health checks
    let health_pool = db::HealthPool::new(db_pool.clone());

    // Intelligence Layer
    let intelligence =
        IntelligenceService::new(db_pool.clone()).expect("Failed to create IntelligenceService");

    // Create app state
    let state = Arc::new(AppState {
        db: db_pool,
        rls_client,
        health_pool,
        tei,
        qdrant,
        search,
        config: config.clone(),
        revoked_tokens,
        sse_active_streams: Arc::new(std::sync::atomic::AtomicUsize::new(0)),
        intelligence,
        domain_registry,
    });
    tracing::info!("App state initialized with quality tiers (fast/balanced)");

    if cpu_mode {
        tracing::warn!(
            "CPU mode: outbox worker, outbox purge task, and Qdrant health task disabled"
        );
        ::metrics::gauge!("qdrant_health_status").set(0.0);
    } else {
        // Start outbox worker for async Qdrant synchronization
        let outbox_db = state.db.clone();
        let outbox_qdrant = state.qdrant.clone();
        let outbox_worker = Arc::new(OutboxWorker::new(outbox_db, outbox_qdrant));

        // Spawn main processing loop
        let worker_main = outbox_worker.clone();
        tokio::spawn(async move {
            worker_main.run().await;
        });

        // Spawn purge task (cleanup old done/failed entries every 1h)
        let worker_purge = outbox_worker.clone();
        tokio::spawn(async move {
            worker_purge.run_purge_task().await;
        });

        // Spawn Qdrant health check task (updates qdrant_health_status gauge every 1min)
        let qdrant_health = state.qdrant.clone();
        tokio::spawn(async move {
            use tokio::time::{interval, Duration};
            let mut ticker = interval(Duration::from_secs(60)); // 1min
            let mut consecutive_failures: u32 = 0;

            // Set initial health status
            ::metrics::gauge!("qdrant_health_status").set(1.0);

            loop {
                ticker.tick().await;
                match qdrant_health.health_check().await {
                    Ok(true) => {
                        ::metrics::gauge!("qdrant_health_status").set(1.0);
                        if consecutive_failures > 0 {
                            tracing::info!(
                                "Qdrant health recovered after {} failures",
                                consecutive_failures
                            );
                            consecutive_failures = 0;
                        }
                    }
                    Ok(false) => {
                        // Qdrant returned non-success HTTP status
                        ::metrics::gauge!("qdrant_health_status").set(0.0);
                        consecutive_failures += 1;
                        if consecutive_failures == 1 || consecutive_failures.is_multiple_of(10) {
                            tracing::warn!(
                                "Qdrant unhealthy (HTTP non-success, {} consecutive)",
                                consecutive_failures
                            );
                        }
                    }
                    Err(e) => {
                        ::metrics::gauge!("qdrant_health_status").set(0.0);
                        consecutive_failures += 1;
                        // Log every Nth failure to avoid spam
                        if consecutive_failures == 1 || consecutive_failures.is_multiple_of(10) {
                            tracing::warn!(
                                "Qdrant health check failed ({} consecutive): {}",
                                consecutive_failures,
                                e
                            );
                        }
                    }
                }
            }
        });
    }

    // Create router
    let app = api::create_router(state);

    // Start server
    let addr = SocketAddr::from(([0, 0, 0, 0], config.server.port));
    tracing::info!("Starting server on {}", addr);

    let listener = tokio::net::TcpListener::bind(addr).await?;

    // Sprint 6.4: Graceful shutdown with SIGTERM/SIGINT handler
    // B8 fix: After signal, allow 10s for in-flight requests, then force exit.
    let (shutdown_tx, shutdown_rx) = tokio::sync::oneshot::channel::<()>();

    // Spawn drain-timeout watchdog: after shutdown signal + 10s → force exit
    tokio::spawn(async move {
        shutdown_signal_wait().await;
        tracing::info!("Shutdown signal received, draining connections (10s timeout)...");
        let _ = shutdown_tx.send(());
        tokio::time::sleep(std::time::Duration::from_secs(10)).await;
        tracing::warn!("Drain timeout exceeded, forcing shutdown");
        std::process::exit(0);
    });

    axum::serve(listener, app)
        .with_graceful_shutdown(async {
            shutdown_rx.await.ok();
        })
        .await?;

    tracing::info!("Server shutdown complete (all requests drained)");
    Ok(())
}

/// Sprint 6.4: Listen for shutdown signals (SIGTERM, SIGINT/Ctrl+C)
async fn shutdown_signal_wait() {
    use tokio::signal;

    let ctrl_c = async {
        signal::ctrl_c()
            .await
            .expect("failed to install Ctrl+C handler");
    };

    #[cfg(unix)]
    let terminate = async {
        signal::unix::signal(signal::unix::SignalKind::terminate())
            .expect("failed to install SIGTERM handler")
            .recv()
            .await;
    };

    #[cfg(not(unix))]
    let terminate = std::future::pending::<()>();

    tokio::select! {
        _ = ctrl_c => {},
        _ = terminate => {},
    }
}

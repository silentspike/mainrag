use deadpool_postgres::{Config, Pool, Runtime, Client};
use std::sync::{Arc, LazyLock};
use std::time::Duration;
use tokio_postgres::NoTls;
use tokio_postgres_rustls::MakeRustlsConnect;
use tracing::error;
use uuid::Uuid;

use crate::config::DatabaseConfig;
use crate::error::{AppError, Result};

pub type PostgresPool = Pool;

/// Default admin user ID for system operations.
/// Can be overridden via MAINRAG_DEFAULT_USER_ID env var.
/// MUST match an existing user in the database with admin privileges.
/// UUID format is validated at initialization time to avoid DB cast errors.
pub static DEFAULT_USER_ID: LazyLock<Uuid> = LazyLock::new(|| {
    let fallback = "db8e73cc-f562-40c5-b3ca-70e6a042ef89";
    match std::env::var("MAINRAG_DEFAULT_USER_ID") {
        Ok(raw) => {
            // Validate UUID format in Rust (prevents DB cast errors)
            Uuid::parse_str(&raw).unwrap_or_else(|e| {
                error!("MAINRAG_DEFAULT_USER_ID '{}' is not a valid UUID: {}. Using fallback.", raw, e);
                Uuid::parse_str(fallback).expect("Fallback UUID is invalid")
            })
        }
        Err(_) => {
            // Sprint 4.6: Warn when using hardcoded fallback UUID
            tracing::warn!(
                "MAINRAG_DEFAULT_USER_ID not set — using hardcoded fallback {}. \
                 Set this env var to a valid admin UUID in production.",
                fallback
            );
            Uuid::parse_str(fallback).expect("Fallback UUID is invalid")
        }
    }
});

/// Validate DEFAULT_USER_ID exists in database with admin privileges (call at startup)
///
/// CRITICAL: This function returns Err if the default user is invalid.
/// The caller (main.rs) MUST propagate this error to fail startup.
/// Without a valid default user, RLS context cannot be set for system tasks.
pub async fn validate_default_user(pool: &Pool) -> anyhow::Result<()> {
    let client = pool.get().await?;
    let user_id = *DEFAULT_USER_ID;

    let exists: bool = client.query_one(
        "SELECT EXISTS(SELECT 1 FROM users WHERE id = $1 AND is_admin = true)",
        &[&user_id]
    ).await?.get(0);

    if !exists {
        // HARD-FAIL: Without a valid default user, system tasks cannot work!
        anyhow::bail!(
            "DEFAULT_USER_ID {} not found or not admin! \
             Set MAINRAG_DEFAULT_USER_ID environment variable to a valid admin UUID. \
             This is required for RLS context in system tasks.",
            user_id
        );
    }

    tracing::info!("DEFAULT_USER_ID {} validated as admin", user_id);
    Ok(())
}

/// Get a database client with RLS context applied within a transaction.
///
/// DEPRECATED for handler use: Use `RlsClient::with_rls()` or `RlsClient::with_system()` instead.
/// This function is still used by service layers (IndexService, SearchService) that manage
/// their own DB connections. Handler code MUST NOT call this directly.
///
/// Uses `set_config(..., true)` = SET LOCAL which is transaction-scoped.
/// NOTE: SET LOCAL without an explicit transaction only lasts for the single statement.
/// For full RLS protection across multiple queries, use RlsClient's closure-based API.
pub async fn get_client_with_rls(pool: &Pool, user_id: Option<Uuid>) -> Result<Client> {
    let client = pool.get().await?;

    let uid = user_id.unwrap_or(*DEFAULT_USER_ID);

    // Use `false` = session-scoped: persists across statements on this connection.
    // Safe with connection pooling because every caller re-sets via get_client_with_rls().
    // `true` (SET LOCAL) only lasted for the set_config statement itself in autocommit mode,
    // causing subsequent queries to fail with empty app.user_id.
    client
        .execute(
            "SELECT set_config('app.user_id', $1::text, false)",
            &[&uid.to_string()],
        )
        .await
        .map_err(|e| AppError::Internal(format!("Failed to set RLS context: {}", e)))?;

    Ok(client)
}

pub fn create_pool(config: &DatabaseConfig) -> Result<PostgresPool> {
    let mut cfg = Config::new();
    cfg.host = Some(config.host.clone());
    cfg.port = Some(config.port);
    cfg.dbname = Some(config.name.clone());
    cfg.user = Some(config.user.clone());
    cfg.password = Some(config.password.clone());

    cfg.pool = Some(deadpool_postgres::PoolConfig {
        max_size: config.max_connections,
        timeouts: deadpool_postgres::Timeouts {
            wait: Some(Duration::from_secs(5)),
            ..Default::default()
        },
        ..Default::default()
    });

    match config.tls_mode.as_str() {
        "require" | "prefer" => {
            let tls_config = build_rustls_config()
                .map_err(|e| AppError::Internal(format!("Failed to build TLS config: {}", e)))?;
            let tls = MakeRustlsConnect::new(tls_config);
            tracing::info!(mode = %config.tls_mode, "PostgreSQL TLS enabled");
            cfg.create_pool(Some(Runtime::Tokio1), tls)
                .map_err(|e| AppError::Internal(format!("Failed to create TLS pool: {}", e)))
        }
        _ => {
            if config.tls_mode != "disable" {
                tracing::warn!(
                    mode = %config.tls_mode,
                    "Unknown POSTGRES_TLS mode, defaulting to disable"
                );
            }
            tracing::info!("PostgreSQL TLS disabled (NoTls)");
            cfg.create_pool(Some(Runtime::Tokio1), NoTls)
                .map_err(|e| AppError::Internal(format!("Failed to create pool: {}", e)))
        }
    }
}

/// Build rustls ClientConfig with system root certificates
fn build_rustls_config() -> std::result::Result<rustls::ClientConfig, Box<dyn std::error::Error>> {
    let mut root_store = rustls::RootCertStore::empty();
    let cert_result = rustls_native_certs::load_native_certs();
    for cert in cert_result.certs {
        root_store.add(cert)?;
    }

    let config = rustls::ClientConfig::builder()
        .with_root_certificates(Arc::new(root_store))
        .with_no_client_auth();

    Ok(config)
}

pub async fn test_connection(pool: &PostgresPool) -> Result<()> {
    let client = pool.get().await?;
    let row = client.query_one("SELECT 1 as test", &[]).await?;
    let _: i32 = row.get("test");
    Ok(())
}

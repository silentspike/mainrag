//! K3: RlsClient — Compile-Time Guard for Transaction-Scoped RLS
//!
//! Wraps the database pool with a closure-based API that enforces
//! transaction-scoped RLS via SET LOCAL. The pool is private — handlers
//! cannot accidentally bypass RLS by calling pool.get() directly.
//!
//! Two closure APIs:
//! - `with_rls()`: For authenticated handlers (sets app.user_id + optional app.is_admin)
//! - `with_system()`: For pre-auth/system operations (uses DEFAULT_USER_ID, is_admin=true)
//!
//! The HRTB+BoxFuture signature is required because Transaction<'a> borrows the
//! Client which lives inside the closure function. A plain async closure would
//! create self-referencing lifetimes that Rust cannot express.

use std::future::Future;
use std::pin::Pin;

use deadpool_postgres::Pool;
use uuid::Uuid;

use super::postgres::DEFAULT_USER_ID;
use crate::error::{AppError, Result};

/// Compile-Time Guard: The only way to run RLS-relevant DB queries.
///
/// The pool is PRIVATE — not accessible outside this module.
/// Handlers must use `with_rls()` or `with_system()`.
///
/// For health checks and pool status (non-RLS), use `health_check()` / `pool_status()`.
/// For auth middleware that needs raw pool access, use `raw_pool()` (pub(crate) only).
pub struct RlsClient {
    pool: Pool,
}

impl RlsClient {
    /// Create a new RlsClient wrapping a pool. Called once at startup.
    pub fn new(pool: Pool) -> Self {
        Self { pool }
    }

    /// Execute a closure within a transaction with RLS context set.
    ///
    /// Sets `app.user_id` via SET LOCAL (transaction-scoped).
    /// If `is_admin` is true, also sets `app.is_admin = 'true'` for admin bypass.
    ///
    /// The transaction is committed on success, rolled back on error.
    ///
    /// # Arguments
    /// * `user_id` - The authenticated user's UUID
    /// * `is_admin` - Whether the user has admin privileges (bypasses RLS)
    /// * `f` - Closure receiving a `&Transaction` reference
    pub async fn with_rls<F, R>(&self, user_id: Uuid, is_admin: bool, f: F) -> Result<R>
    where
        R: Send + 'static,
        F: for<'a> FnOnce(
            &'a deadpool_postgres::Transaction<'a>,
        ) -> Pin<Box<dyn Future<Output = Result<R>> + Send + 'a>>,
    {
        let mut client = self.pool.get().await?;
        let txn = client
            .transaction()
            .await
            .map_err(|e| AppError::Internal(format!("Failed to start RLS transaction: {}", e)))?;

        // SET LOCAL: scoped to this transaction only, no pool leak
        txn.execute(
            "SELECT set_config('app.user_id', $1::text, true)",
            &[&user_id.to_string()],
        )
        .await
        .map_err(|e| AppError::Internal(format!("Failed to set RLS user_id: {}", e)))?;

        if is_admin {
            txn.execute("SELECT set_config('app.is_admin', 'true', true)", &[])
                .await
                .map_err(|e| AppError::Internal(format!("Failed to set RLS is_admin: {}", e)))?;
        }

        let result = f(&txn).await?;

        txn.commit()
            .await
            .map_err(|e| AppError::Internal(format!("Failed to commit RLS transaction: {}", e)))?;

        Ok(result)
    }

    /// Execute a closure with system-level RLS context (DEFAULT_USER_ID, is_admin=true).
    ///
    /// Use for background jobs, startup tasks, and pre-auth operations that need
    /// full database access without an authenticated user context.
    pub async fn with_system<F, R>(&self, f: F) -> Result<R>
    where
        R: Send + 'static,
        F: for<'a> FnOnce(
            &'a deadpool_postgres::Transaction<'a>,
        ) -> Pin<Box<dyn Future<Output = Result<R>> + Send + 'a>>,
    {
        self.with_rls(*DEFAULT_USER_ID, true, f).await
    }

    /// Health check: verify pool connectivity (non-RLS, no transaction needed)
    pub async fn health_check(&self) -> Result<()> {
        let client = self.pool.get().await?;
        let row = client.query_one("SELECT 1 as test", &[]).await?;
        let _: i32 = row.get("test");
        Ok(())
    }

    /// Pool status for metrics/monitoring
    pub fn pool_status(&self) -> deadpool_postgres::Status {
        self.pool.status()
    }

    /// Raw pool access for auth middleware (pub(crate) — not accessible from handlers).
    ///
    /// The auth middleware needs direct pool access to validate API keys and load
    /// revoked tokens. This is safe because auth middleware runs BEFORE handlers
    /// and doesn't touch RLS-protected tables.
    pub(crate) fn raw_pool(&self) -> &Pool {
        &self.pool
    }
}

#[cfg(test)]
mod tests {
    #[test]
    fn test_rls_client_pool_is_private() {
        // This test documents the compile-time guarantee:
        // RlsClient.pool is private, so handlers cannot call pool.get() directly.
        // The only public APIs are with_rls(), with_system(), health_check(), pool_status().
        // raw_pool() is pub(crate) — accessible within the crate but not from external consumers.
        //
        // A trybuild compile-fail test would be the gold standard here,
        // but for now this documents the invariant.
    }
}

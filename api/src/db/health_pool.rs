//! K3-FIX1: HealthPool Newtype — restricted pool access for health checks only.
//!
//! Wraps the database pool and exposes ONLY health_check() + pool_status().
//! Handlers use this for non-RLS health monitoring instead of the raw pool.

use deadpool_postgres::Pool;

use crate::error::Result;

/// Restricted pool wrapper for health checks and monitoring only.
///
/// Unlike RlsClient (which provides transaction-scoped RLS access),
/// HealthPool only allows connectivity checks and pool status queries.
/// No data queries are possible through this type.
pub struct HealthPool {
    pool: Pool,
}

impl HealthPool {
    /// Create a new HealthPool wrapping the same pool as RlsClient.
    pub fn new(pool: Pool) -> Self {
        Self { pool }
    }

    /// Health check: verify pool connectivity (SELECT 1)
    pub async fn health_check(&self) -> Result<()> {
        let client = self.pool.get().await?;
        let row = client.query_one("SELECT 1 as test", &[]).await?;
        let _: i32 = row.get("test");
        Ok(())
    }

    /// Pool status for Prometheus metrics / monitoring dashboards
    pub fn pool_status(&self) -> deadpool_postgres::Status {
        self.pool.status()
    }
}

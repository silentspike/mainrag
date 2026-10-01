//! K3-FIX1: HealthPool Newtype — restricted pool access for health checks only.
//!
//! Exposes fixed connectivity/active-set checks and pool status only.
//! Handlers use this for non-RLS health monitoring instead of the raw pool.

use deadpool_postgres::Pool;

use crate::error::Result;

/// Restricted pool wrapper for health checks and monitoring only.
///
/// Unlike RlsClient (which provides transaction-scoped RLS access),
/// HealthPool only allows fixed health checks and pool status queries.
/// Arbitrary data queries are not exposed through this type.
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

    /// Check the configured active-set receipt without exposing source data.
    pub async fn active_set_health_check(&self, manifest_sha256: &str) -> Result<()> {
        let client = self.pool.get().await?;
        client
            .query_one(
                "SELECT storage_v2_require_complete_active_set($1)",
                &[&manifest_sha256],
            )
            .await?;
        Ok(())
    }

    /// Pool status for Prometheus metrics / monitoring dashboards
    pub fn pool_status(&self) -> deadpool_postgres::Status {
        self.pool.status()
    }
}

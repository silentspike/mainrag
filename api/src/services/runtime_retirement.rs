//! Reject startup with a missing legacy schema and an incomplete native binding.

use anyhow::{ensure, Context, Result};
use tokio_postgres::GenericClient;

/// A configured retirement boundary may be installed before the approved drop.
/// Removing it afterwards cannot silently re-enable legacy bootstrap or workers.
/// This catalog read proves schema presence only, never cleanup acceptance.
pub async fn check_legacy_runtime_state<C>(client: &C, retirement_configured: bool) -> Result<()>
where
    C: GenericClient + Sync,
{
    let legacy_present: bool = client
        .query_one(
            "SELECT to_regclass('public.files') IS NOT NULL \
         AND to_regclass('public.chunks') IS NOT NULL \
         AND to_regclass('public.indexing_outbox') IS NOT NULL",
            &[],
        )
        .await
        .context("legacy runtime schema presence check failed")?
        .get(0);
    ensure!(
        legacy_present || retirement_configured,
        "legacy schema is incomplete; restore the exact native retirement manifest binding before startup"
    );
    Ok(())
}

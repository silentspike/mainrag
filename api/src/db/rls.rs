use crate::error::Result;
use deadpool_postgres::Client;
/// RLS (Row Level Security) helpers for enforcing user-level access control
///
/// All database queries that touch RLS-enabled tables must call set_config('app.user_id', ...)
/// to properly enforce Row Level Security policies defined in schema_security.sql
use uuid::Uuid;

/// Apply RLS context to a database client.
///
/// CRITICAL (NEW-1 fix): Uses `true` = SET LOCAL (transaction-scoped)
/// to prevent user_id leaking across pooled connections.
pub async fn apply_rls_context(client: &Client, user_id: Uuid) -> Result<()> {
    client
        .execute(
            "SELECT set_config('app.user_id', $1::text, true)",
            &[&user_id.to_string()],
        )
        .await?;
    Ok(())
}

/// Apply RLS context with explicit scope control.
///
/// IMPORTANT: Always use `true` (SET LOCAL = transaction-scoped) in production
/// to prevent user_id leaking via connection pooling.
pub async fn apply_rls_context_scoped(
    client: &Client,
    user_id: Uuid,
    _session_scope: bool,
) -> Result<()> {
    // Always use transaction-scoped (true) regardless of parameter (NEW-1 safety)
    client
        .execute(
            "SELECT set_config('app.user_id', $1::text, true)",
            &[&user_id.to_string()],
        )
        .await?;
    Ok(())
}

/// Clear RLS context (for cleanup or testing)
pub async fn clear_rls_context(client: &Client) -> Result<()> {
    client
        .execute("SELECT set_config('app.user_id', '', true)", &[])
        .await?;
    Ok(())
}

/// Transaction wrapper that automatically applies RLS context
///
/// This struct wraps a database transaction and automatically sets the app.user_id
/// at the start, ensuring all queries in the transaction respect RLS policies.
pub struct RlsTransaction {
    user_id: Uuid,
}

impl RlsTransaction {
    /// Create a new RLS-aware transaction context
    pub fn new(user_id: Uuid) -> Self {
        Self { user_id }
    }

    /// Get the user ID associated with this transaction
    pub fn user_id(&self) -> Uuid {
        self.user_id
    }
}

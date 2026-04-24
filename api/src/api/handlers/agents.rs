use axum::{
    extract::{Path, State},
    http::StatusCode,
    response::IntoResponse,
    Json,
};
use serde::{Deserialize, Serialize};
use std::sync::Arc;
use uuid::Uuid;

use crate::{error::AppError, AppState};

// ===================================================================
// Request/Response Types
// ===================================================================

#[derive(Debug, Deserialize)]
pub struct CreateAgentRequest {
    pub name: String,
}

#[derive(Debug, Serialize)]
pub struct CreateAgentResponse {
    pub agent_id: String,
    pub name: String,
    pub api_key: String,
    pub key_prefix: String,
    pub message: String,
}

#[derive(Debug, Serialize)]
pub struct AgentResponse {
    pub id: String,
    pub agent_id: String,
    pub name: String,
    pub key_prefix: String,
    pub status: String,
    pub created_at: String,
    pub last_used_at: Option<String>,
}

#[derive(Debug, Serialize)]
pub struct AgentListResponse {
    pub agents: Vec<AgentResponse>,
}

#[derive(Debug, Serialize)]
pub struct RotateKeyResponse {
    pub api_key: String,
    pub key_prefix: String,
    pub message: String,
}

// ===================================================================
// Handlers (Admin-only — behind admin_middleware)
// ===================================================================

/// POST /api/v1/admin/agents — Create a new agent with API key
pub async fn admin_create_agent(
    State(state): State<Arc<AppState>>,
    Json(req): Json<CreateAgentRequest>,
) -> Result<impl IntoResponse, AppError> {
    // Validate agent name: alphanumeric + hyphen, max 32 chars
    if req.name.is_empty() || req.name.len() > 32 {
        return Err(AppError::BadRequest(
            "Agent name must be 1-32 characters".to_string(),
        ));
    }
    if !req.name.chars().all(|c| c.is_alphanumeric() || c == '-') {
        return Err(AppError::BadRequest(
            "Agent name must be alphanumeric with hyphens only".to_string(),
        ));
    }

    // Pre-compute key material outside the DB transaction
    let agent_id = Uuid::new_v4();
    let raw_key_bytes: [u8; 32] = rand::random::<[u8; 32]>();
    let raw_key = base64_encode(&raw_key_bytes);
    let key_prefix = raw_key[..8].to_string();
    let key_hash = hmac_sha256_hash(&state.config, raw_key.as_bytes());
    let agent_username = format!("agent:{}", req.name);
    let display_name = format!("Agent: {}", req.name);
    let key_id = Uuid::new_v4();
    let name = req.name;

    // Clone values needed both inside the closure and after it
    let name_for_db = name.clone();
    let key_prefix_for_db = key_prefix.clone();

    // K3: All DB operations in a single transaction via RlsClient
    state.rls_client.with_system(|txn| Box::pin(async move {
        // Find or create user record for this agent (required for RLS FK)
        let user_row = txn
            .query_opt(
                "SELECT id FROM users WHERE username = $1",
                &[&agent_username],
            )
            .await?;

        let user_id = if let Some(row) = user_row {
            row.get::<_, Uuid>("id")
        } else {
            let row = txn.query_one(
                r#"INSERT INTO users (username, password_hash, display_name, is_active, is_admin)
                   VALUES ($1, 'api_key_only', $2, true, false)
                   RETURNING id"#,
                &[&agent_username, &display_name],
            ).await.map_err(|_| {
                AppError::BadRequest("Could not create agent".to_string())
            })?;
            row.get::<_, Uuid>("id")
        };

        // Insert API key record
        txn.execute(
            r#"INSERT INTO api_keys (id, agent_id, user_id, key_hash, key_prefix, agent_name, status)
               VALUES ($1, $2, $3, $4, $5, $6, 'active')"#,
            &[&key_id, &agent_id, &user_id, &key_hash.as_slice(), &key_prefix_for_db, &name_for_db],
        ).await?;

        Ok(())
    })).await?;

    tracing::info!(agent_name = %name, key_prefix = %key_prefix, "Agent created with API key");

    Ok((
        StatusCode::CREATED,
        Json(CreateAgentResponse {
            agent_id: agent_id.to_string(),
            name,
            api_key: raw_key,
            key_prefix: key_prefix.to_string(),
            message: "API key shown once — save it now!".to_string(),
        }),
    ))
}

/// GET /api/v1/admin/agents — List all agents
pub async fn admin_list_agents(
    State(state): State<Arc<AppState>>,
) -> Result<Json<AgentListResponse>, AppError> {
    state
        .rls_client
        .with_system(|txn| {
            Box::pin(async move {
                let rows = txn.query(
            r#"SELECT id, agent_id, agent_name, key_prefix, status, created_at, last_used_at
               FROM api_keys
               ORDER BY created_at DESC"#,
            &[],
        ).await?;

                let agents = rows
                    .iter()
                    .map(|row| AgentResponse {
                        id: row.get::<_, Uuid>("id").to_string(),
                        agent_id: row.get::<_, Uuid>("agent_id").to_string(),
                        name: row.get("agent_name"),
                        key_prefix: row.get("key_prefix"),
                        status: row.get("status"),
                        created_at: row
                            .get::<_, chrono::DateTime<chrono::Utc>>("created_at")
                            .to_rfc3339(),
                        last_used_at: row
                            .get::<_, Option<chrono::DateTime<chrono::Utc>>>("last_used_at")
                            .map(|t| t.to_rfc3339()),
                    })
                    .collect();

                Ok(Json(AgentListResponse { agents }))
            })
        })
        .await
}

/// DELETE /api/v1/admin/agents/:id — Revoke an agent's API key
pub async fn admin_revoke_agent(
    State(state): State<Arc<AppState>>,
    Path(id): Path<Uuid>,
) -> Result<StatusCode, AppError> {
    let result = state.rls_client.with_system(|txn| Box::pin(async move {
        let result = txn.execute(
            "UPDATE api_keys SET status = 'revoked', revoked_at = NOW() WHERE agent_id = $1 AND status IN ('active', 'rotating')",
            &[&id],
        ).await?;
        Ok(result)
    })).await?;

    if result == 0 {
        return Err(AppError::NotFound(
            "Agent not found or already revoked".to_string(),
        ));
    }

    tracing::info!(agent_id = %id, "Agent API key(s) revoked");
    Ok(StatusCode::NO_CONTENT)
}

/// POST /api/v1/admin/agents/:id/rotate — Rotate an agent's API key
pub async fn admin_rotate_agent_key(
    State(state): State<Arc<AppState>>,
    Path(agent_id): Path<Uuid>,
) -> Result<Json<RotateKeyResponse>, AppError> {
    // Pre-compute new key material outside the DB transaction
    let raw_key_bytes: [u8; 32] = rand::random::<[u8; 32]>();
    let raw_key = base64_encode(&raw_key_bytes);
    let key_prefix = raw_key[..8].to_string();
    let key_hash = hmac_sha256_hash(&state.config, raw_key.as_bytes());
    let new_key_id = Uuid::new_v4();

    let key_prefix_for_db = key_prefix.clone();

    let agent_name = state.rls_client.with_system(|txn| Box::pin(async move {
        // Find the current active key
        let current_key = txn.query_opt(
            "SELECT id, user_id, agent_name FROM api_keys WHERE agent_id = $1 AND status = 'active'",
            &[&agent_id],
        ).await?
        .ok_or_else(|| AppError::NotFound("No active key found for this agent".to_string()))?;

        let user_id: Uuid = current_key.get("user_id");
        let agent_name: String = current_key.get("agent_name");
        let current_key_id: Uuid = current_key.get("id");

        // Mark old key as rotating (1h grace period)
        txn.execute(
            "UPDATE api_keys SET status = 'rotating', rotated_at = NOW(), expires_at = NOW() + INTERVAL '1 hour' WHERE id = $1",
            &[&current_key_id],
        ).await?;

        // Insert new active key
        txn.execute(
            r#"INSERT INTO api_keys (id, agent_id, user_id, key_hash, key_prefix, agent_name, status)
               VALUES ($1, $2, $3, $4, $5, $6, 'active')"#,
            &[&new_key_id, &agent_id, &user_id, &key_hash.as_slice(), &key_prefix_for_db, &agent_name],
        ).await?;

        Ok(agent_name)
    })).await?;

    tracing::info!(agent_name = %agent_name, key_prefix = %key_prefix, "Agent API key rotated (old key valid for 1h)");

    Ok(Json(RotateKeyResponse {
        api_key: raw_key,
        key_prefix,
        message: "New key active. Old key valid for 1 hour.".to_string(),
    }))
}

// ===================================================================
// Helpers
// ===================================================================

fn base64_encode(bytes: &[u8]) -> String {
    use base64::Engine;
    base64::engine::general_purpose::STANDARD.encode(bytes)
}

/// HMAC-SHA256 hash of raw_key using API_KEY_PEPPER from config
fn hmac_sha256_hash(_config: &crate::config::Config, raw_key: &[u8]) -> Vec<u8> {
    use hmac::{Hmac, Mac};
    use sha2::Sha256;

    type HmacSha256 = Hmac<Sha256>;

    let pepper = std::env::var("API_KEY_PEPPER").unwrap_or_else(|_| {
        tracing::warn!("API_KEY_PEPPER not set — using fallback (INSECURE for production!)");
        "mainrag_default_pepper_change_me".to_string()
    });

    let mut mac =
        HmacSha256::new_from_slice(pepper.as_bytes()).expect("HMAC can take key of any size");
    mac.update(raw_key);
    mac.finalize().into_bytes().to_vec()
}

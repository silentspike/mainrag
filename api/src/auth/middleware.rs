use axum::{
    extract::Request,
    http::{header::AUTHORIZATION, StatusCode},
    middleware::Next,
    response::Response,
    Extension,
};
use hmac::{Hmac, Mac};
use sha2::Sha256;
use std::sync::Arc;
use std::time::Duration;

use crate::auth::jwt::{validate_token, Claims};
use crate::config::Config;
use crate::db::PostgresPool;

type HmacSha256 = Hmac<Sha256>;

/// Cached agent identity from a validated API-Key lookup.
#[derive(Clone, Debug)]
pub struct AgentInfo {
    pub user_id: String,
    pub agent_name: String,
}

#[derive(Clone)]
pub struct AuthLayer {
    pub jwt_secret: String,
    /// Sprint 4.3: Previous JWT secret for dual-key rotation
    pub jwt_secret_previous: Option<String>,
    /// Sprint 2.8: Revoked JWT IDs (jti) cache for O(1) lookup
    pub revoked_tokens: moka::sync::Cache<String, ()>,
    /// Sprint 2.7: API-Key HMAC hash -> AgentInfo cache (max 1000, TTL 5min)
    pub api_key_cache: moka::sync::Cache<Vec<u8>, AgentInfo>,
    /// Database pool for API-Key lookups
    pub db: PostgresPool,
    /// HMAC pepper for API-Key hashing
    pub api_key_pepper: String,
    /// Previous pepper for zero-downtime rotation
    pub api_key_pepper_previous: Option<String>,
}

impl AuthLayer {
    pub fn new(
        config: &Config,
        revoked_tokens: moka::sync::Cache<String, ()>,
        db: PostgresPool,
    ) -> Self {
        let api_key_cache = moka::sync::Cache::builder()
            .max_capacity(1_000)
            .time_to_live(Duration::from_secs(300)) // 5 minutes
            .build();

        Self {
            jwt_secret: config.jwt.secret.clone(),
            jwt_secret_previous: config.jwt.secret_previous.clone(),
            revoked_tokens,
            api_key_cache,
            db,
            api_key_pepper: config.server.api_key_pepper.clone(),
            api_key_pepper_previous: config.server.api_key_pepper_previous.clone(),
        }
    }
}

/// Compute HMAC-SHA256(pepper, api_key) and return raw 32-byte hash.
fn compute_key_hash(pepper: &str, api_key: &str) -> Vec<u8> {
    let mut mac =
        HmacSha256::new_from_slice(pepper.as_bytes()).expect("HMAC accepts any key length");
    mac.update(api_key.as_bytes());
    mac.finalize().into_bytes().to_vec()
}

/// Dual-Auth middleware: checks API-Key first (for agents), then JWT (for admin).
/// API-Key auth produces Claims with role="agent", is_admin=false.
/// JWT auth produces Claims from the token (typically role="admin", is_admin=true).
pub async fn auth_middleware(
    Extension(auth): Extension<AuthLayer>,
    mut request: Request,
    next: Next,
) -> Result<Response, StatusCode> {
    // 1. Check API-Key header (primary auth for agents)
    if let Some(api_key) = request
        .headers()
        .get("X-API-Key")
        .and_then(|h| h.to_str().ok())
    {
        // FIX-7: Reduce logged prefix from 8 to 4 chars, UTF-8 safe
        let key_prefix: String = api_key.chars().take(4).collect();

        // Compute HMAC-SHA256 hash with current pepper
        let key_hash = compute_key_hash(&auth.api_key_pepper, api_key);

        // Cache lookup
        if let Some(agent_info) = auth.api_key_cache.get(&key_hash) {
            tracing::debug!(
                key_prefix = %key_prefix,
                agent = %agent_info.agent_name,
                "API-Key auth from cache"
            );
            let claims = Claims::for_agent(&agent_info.user_id, &agent_info.agent_name);
            request.extensions_mut().insert(Arc::new(claims));
            return Ok(next.run(request).await);
        }

        // Cache miss -> DB lookup with current hash
        if let Some(agent_info) = db_lookup_api_key(&auth, &key_hash).await {
            tracing::info!(
                key_prefix = %key_prefix,
                agent = %agent_info.agent_name,
                "API-Key auth via DB lookup"
            );
            let claims = Claims::for_agent(&agent_info.user_id, &agent_info.agent_name);
            auth.api_key_cache.insert(key_hash, agent_info);
            request.extensions_mut().insert(Arc::new(claims));
            return Ok(next.run(request).await);
        }

        // Try previous pepper if configured (for rotation)
        if let Some(ref prev_pepper) = auth.api_key_pepper_previous {
            let old_hash = compute_key_hash(prev_pepper, api_key);

            if let Some(agent_info) = db_lookup_api_key(&auth, &old_hash).await {
                tracing::info!(
                    key_prefix = %key_prefix,
                    agent = %agent_info.agent_name,
                    "API-Key matched with previous pepper, rehashing"
                );

                // Rehash with new pepper and update DB
                let new_hash = key_hash; // already computed above
                if let Err(e) = db_rehash_api_key(&auth.db, &old_hash, &new_hash).await {
                    tracing::warn!(
                        key_prefix = %key_prefix,
                        error = %e,
                        "Failed to rehash API-Key in DB (will retry next request)"
                    );
                }

                let claims = Claims::for_agent(&agent_info.user_id, &agent_info.agent_name);
                auth.api_key_cache.insert(new_hash, agent_info);
                request.extensions_mut().insert(Arc::new(claims));
                return Ok(next.run(request).await);
            }
        }

        // FIX-7: API-Key provided but invalid - return 401 immediately.
        // NEVER fall through to JWT when X-API-Key header is present.
        // Without this, an attacker could send invalid API-Key + valid admin JWT
        // to escalate privileges from agent to admin.
        tracing::warn!(key_prefix = %key_prefix, "API-Key not found or expired");
        return Err(StatusCode::UNAUTHORIZED);
    }

    // 2. Check JWT (Bearer token) - for admin via CLI/API
    let auth_header = request
        .headers()
        .get(AUTHORIZATION)
        .and_then(|h| h.to_str().ok());

    let token = match auth_header {
        Some(h) if h.starts_with("Bearer ") => &h[7..],
        _ => return Err(StatusCode::UNAUTHORIZED),
    };

    // Sprint 4.3: Try current secret first, then previous (dual-key rotation)
    let claims = validate_token(token, &auth.jwt_secret)
        .or_else(|_| {
            auth.jwt_secret_previous
                .as_ref()
                .ok_or_else(|| crate::error::AppError::Auth("Invalid token".to_string()))
                .and_then(|prev| validate_token(token, prev))
        })
        .map_err(|_| StatusCode::UNAUTHORIZED)?;

    // Sprint 2.8: Check if token's jti has been revoked
    if auth.revoked_tokens.contains_key(&claims.jti) {
        tracing::warn!(jti = %claims.jti, "Rejected revoked JWT token");
        return Err(StatusCode::UNAUTHORIZED);
    }

    // Add claims to request extensions for handlers to access
    request.extensions_mut().insert(Arc::new(claims));

    Ok(next.run(request).await)
}

/// Lookup an API-Key hash in the database.
/// Returns AgentInfo if the key is active/rotating and not expired.
async fn db_lookup_api_key(auth: &AuthLayer, key_hash: &[u8]) -> Option<AgentInfo> {
    let client = match auth.db.get().await {
        Ok(c) => c,
        Err(e) => {
            tracing::error!(error = %e, "Failed to get DB connection for API-Key lookup");
            return None;
        }
    };

    let row = match client
        .query_opt(
            "SELECT agent_id::text, user_id::text, name \
             FROM api_keys \
             WHERE key_hash = $1 \
               AND status IN ('active', 'rotating') \
               AND (expires_at IS NULL OR expires_at > NOW())",
            &[&key_hash],
        )
        .await
    {
        Ok(r) => r,
        Err(e) => {
            tracing::error!(error = %e, "API-Key DB lookup query failed");
            return None;
        }
    };

    row.map(|r| AgentInfo {
        user_id: r.get("user_id"),
        agent_name: r.get("name"),
    })
}

/// Rehash an API-Key in the database from old_hash to new_hash (pepper rotation).
async fn db_rehash_api_key(
    db: &PostgresPool,
    old_hash: &[u8],
    new_hash: &[u8],
) -> Result<(), Box<dyn std::error::Error + Send + Sync>> {
    let client = db.get().await?;
    client
        .execute(
            "UPDATE api_keys SET key_hash = $1 WHERE key_hash = $2",
            &[&new_hash, &old_hash],
        )
        .await?;
    tracing::info!("API-Key rehashed successfully (pepper rotation)");
    Ok(())
}

pub async fn admin_middleware(
    Extension(claims): Extension<Arc<Claims>>,
    request: Request,
    next: Next,
) -> Result<Response, StatusCode> {
    // Explicit role-gate: must be admin role AND is_admin flag
    if !claims.is_admin || claims.role != "admin" {
        return Err(StatusCode::FORBIDDEN);
    }
    Ok(next.run(request).await)
}

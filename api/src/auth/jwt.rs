use chrono::{Duration, Utc};
use jsonwebtoken::{decode, encode, DecodingKey, EncodingKey, Header, Validation};
use serde::{Deserialize, Serialize};
use uuid::Uuid;

use crate::error::{AppError, Result};

#[derive(Debug, Serialize, Deserialize, Clone)]
pub struct Claims {
    pub sub: String, // User ID (UUID)
    pub email: String,
    pub is_admin: bool,
    pub exp: i64, // Expiration time
    pub iat: i64, // Issued at
    /// JWT ID for token revocation (Sprint 2.8)
    #[serde(default = "default_jti")]
    pub jti: String,
    /// Role: "admin" or "agent" (default: "admin" for backward compat with existing JWTs)
    #[serde(default = "default_role")]
    pub role: String,
    /// Agent name (only set for API-Key authenticated agents)
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub agent_name: Option<String>,
}

fn default_jti() -> String {
    Uuid::new_v4().to_string()
}

fn default_role() -> String {
    "admin".to_string()
}

impl Claims {
    pub fn new(user_id: &str, email: &str, is_admin: bool, expiry_hours: u64) -> Self {
        let now = Utc::now();
        Self {
            sub: user_id.to_string(),
            email: email.to_string(),
            is_admin,
            iat: now.timestamp(),
            exp: (now + Duration::hours(expiry_hours as i64)).timestamp(),
            jti: Uuid::new_v4().to_string(),
            role: if is_admin {
                "admin".to_string()
            } else {
                "user".to_string()
            },
            agent_name: None,
        }
    }

    /// Create claims for an API-Key authenticated agent
    #[allow(dead_code)]
    pub fn for_agent(user_id: &str, agent_name: &str) -> Self {
        Self {
            sub: user_id.to_string(),
            email: String::new(),
            is_admin: false,
            iat: Utc::now().timestamp(),
            exp: i64::MAX, // API-Key claims don't expire (key validity is checked separately)
            jti: Uuid::new_v4().to_string(),
            role: "agent".to_string(),
            agent_name: Some(agent_name.to_string()),
        }
    }
}

pub fn create_token(claims: &Claims, secret: &str) -> Result<String> {
    encode(
        &Header::default(),
        claims,
        &EncodingKey::from_secret(secret.as_bytes()),
    )
    .map_err(|e| AppError::Auth(format!("Failed to create token: {}", e)))
}

pub fn validate_token(token: &str, secret: &str) -> Result<Claims> {
    let token_data = decode::<Claims>(
        token,
        &DecodingKey::from_secret(secret.as_bytes()),
        &Validation::default(),
    )
    .map_err(|e| AppError::Auth(format!("Invalid token: {}", e)))?;

    Ok(token_data.claims)
}

/// Generate a new JWT token
pub fn generate_token(
    user_id: &str,
    email: &str,
    is_admin: bool,
    expiry_hours: u64,
    secret: &str,
) -> Result<String> {
    let claims = Claims::new(user_id, email, is_admin, expiry_hours);
    create_token(&claims, secret)
}

use axum::{
    extract::{Path, State},
    http::StatusCode,
    Extension, Json,
};
use serde::{Deserialize, Serialize};
use std::sync::Arc;
use uuid::Uuid;

use crate::api::JsonBody;
use crate::{auth::Claims, error::AppError, AppState};

// ===================================================================
// Request/Response Types
// ===================================================================

// RegisterRequest removed (Sprint 4.3): Registration endpoint deleted.

#[derive(Debug, Deserialize)]
pub struct LoginRequest {
    pub username: String, // Can be username or email
    pub password: String,
}

#[derive(Debug, Serialize)]
pub struct AuthResponse {
    pub token: String,
    pub token_type: String,
    pub expires_in: u64,
    pub user: UserResponse,
}

#[derive(Debug, Serialize)]
pub struct UserResponse {
    pub id: String,
    pub username: String,
    pub email: Option<String>,
    pub display_name: Option<String>,
    pub is_admin: bool,
    pub is_active: bool,
    pub created_at: String,
}

#[derive(Debug, Deserialize)]
pub struct UpdateProfileRequest {
    pub display_name: Option<String>,
    pub email: Option<String>,
}

#[derive(Debug, Deserialize)]
pub struct ChangePasswordRequest {
    pub current_password: String,
    pub new_password: String,
}

// ===================================================================
// Handlers
// ===================================================================

// Registration endpoint removed (Sprint 4.3): Agents use API-Keys, Admin created via init-script.

/// POST /api/v1/auth/login
pub async fn login(
    State(state): State<Arc<AppState>>,
    JsonBody(req): JsonBody<LoginRequest>,
) -> Result<Json<AuthResponse>, AppError> {
    let login_username = req.username;
    let login_password = req.password;

    // K3: Fetch user data in transaction-scoped context
    let login_username_q = login_username.clone();
    let user_data = state
        .rls_client
        .with_system(|txn| {
            Box::pin(async move {
                let row = txn
                    .query_opt(
                        r#"
                SELECT id, username, email, password_hash, display_name, is_admin, is_active,
                       created_at, failed_login_count, locked_until
                FROM users
                WHERE username = $1 OR email = $1
                "#,
                        &[&login_username_q],
                    )
                    .await?
                    .ok_or_else(|| AppError::Unauthorized("Invalid credentials".to_string()))?;

                Ok((
                    row.get::<_, Uuid>("id"),
                    row.get::<_, String>("password_hash"),
                    row.get::<_, bool>("is_admin"),
                    row.get::<_, bool>("is_active"),
                    row.get::<_, String>("username"),
                    row.get::<_, Option<String>>("email"),
                    row.get::<_, Option<String>>("display_name"),
                    row.get::<_, chrono::DateTime<chrono::Utc>>("created_at"),
                    row.get::<_, Option<i32>>("failed_login_count"),
                    row.get::<_, Option<chrono::DateTime<chrono::Utc>>>("locked_until"),
                ))
            })
        })
        .await?;

    let (
        user_id,
        password_hash,
        is_admin,
        is_active,
        username,
        email,
        display_name,
        created_at,
        failed_login_count,
        locked_until,
    ) = user_data;

    // Check account lockout (Sprint 2.3: 5 failures → 15min lock)
    if let Some(until) = locked_until {
        if until > chrono::Utc::now() {
            tracing::warn!(username = %login_username, locked_until = %until, "Login attempt on locked account");
            return Err(AppError::Unauthorized("Invalid credentials".to_string()));
        }
    }

    // Check if user is active
    if !is_active {
        // Generic message to prevent enumeration
        return Err(AppError::Unauthorized("Invalid credentials".to_string()));
    }

    // Verify password (CPU-intensive, runs in blocking thread to avoid starving Tokio workers)
    let password_valid = {
        let pw = login_password.clone();
        let hash = password_hash.clone();
        tokio::task::spawn_blocking(move || bcrypt::verify(&pw, &hash))
            .await
            .map_err(|e| AppError::Internal(format!("Blocking task join error: {e}")))?
            .map_err(|e| AppError::Internal(format!("Password verification failed: {e}")))?
    };

    if !password_valid {
        // Increment failed login counter and lock if threshold reached
        let failed_count = failed_login_count.unwrap_or(0) + 1;
        let login_username_log = login_username.clone();
        state.rls_client.with_system(|txn| Box::pin(async move {
            if failed_count >= 5 {
                // Lock account for 15 minutes
                txn.execute(
                    "UPDATE users SET failed_login_count = $1, locked_until = NOW() + INTERVAL '15 minutes' WHERE id = $2",
                    &[&failed_count, &user_id],
                ).await?;
                tracing::warn!(username = %login_username_log, "Account locked after {} failed attempts", failed_count);
            } else {
                txn.execute(
                    "UPDATE users SET failed_login_count = $1 WHERE id = $2",
                    &[&failed_count, &user_id],
                ).await?;
            }
            Ok(())
        })).await?;
        return Err(AppError::Unauthorized("Invalid credentials".to_string()));
    }

    // K3: Successful login — reset failed counter and update stats
    state.rls_client.with_system(|txn| Box::pin(async move {
        txn.execute(
            "UPDATE users SET last_login = NOW(), login_count = COALESCE(login_count, 0) + 1, failed_login_count = 0, locked_until = NULL WHERE id = $1",
            &[&user_id],
        ).await?;
        Ok(())
    })).await?;

    let user = UserResponse {
        id: user_id.to_string(),
        username: username.clone(),
        email: email.clone(),
        display_name,
        is_admin,
        is_active,
        created_at: created_at.to_rfc3339(),
    };

    // Generate JWT token
    let email_for_token = email.unwrap_or_else(|| username.clone());
    let token = crate::auth::generate_token(
        &user.id,
        &email_for_token,
        is_admin,
        state.config.jwt.expiry_hours,
        &state.config.jwt.secret,
    )?;

    Ok(Json(AuthResponse {
        token,
        token_type: "Bearer".to_string(),
        expires_in: state.config.jwt.expiry_hours * 3600,
        user,
    }))
}

/// GET /api/v1/auth/me
pub async fn get_profile(
    State(state): State<Arc<AppState>>,
    Extension(claims): Extension<Arc<Claims>>,
) -> Result<Json<UserResponse>, AppError> {
    let user_id = Uuid::parse_str(&claims.sub)
        .map_err(|_| AppError::Internal("Invalid user ID in token".to_string()))?;

    // K3: All DB queries via RlsClient
    state
        .rls_client
        .with_system(|txn| {
            Box::pin(async move {
                let row = txn
                    .query_opt(
                        r#"
                SELECT id, username, email, display_name, is_admin, is_active, created_at
                FROM users
                WHERE id = $1
                "#,
                        &[&user_id],
                    )
                    .await?
                    .ok_or_else(|| AppError::NotFound("User not found".to_string()))?;

                Ok(Json(UserResponse {
                    id: row.get::<_, Uuid>("id").to_string(),
                    username: row.get("username"),
                    email: row.get("email"),
                    display_name: row.get("display_name"),
                    is_admin: row.get("is_admin"),
                    is_active: row.get("is_active"),
                    created_at: row
                        .get::<_, chrono::DateTime<chrono::Utc>>("created_at")
                        .to_rfc3339(),
                }))
            })
        })
        .await
}

/// PATCH /api/v1/auth/me
pub async fn update_profile(
    State(state): State<Arc<AppState>>,
    Extension(claims): Extension<Arc<Claims>>,
    JsonBody(req): JsonBody<UpdateProfileRequest>,
) -> Result<Json<UserResponse>, AppError> {
    let user_id = Uuid::parse_str(&claims.sub)
        .map_err(|_| AppError::Internal("Invalid user ID in token".to_string()))?;

    // Validate before entering transaction
    if let Some(ref email) = req.email {
        if !email.contains('@') {
            return Err(AppError::BadRequest("Invalid email format".to_string()));
        }
    }
    if req.display_name.is_none() && req.email.is_none() {
        return Err(AppError::BadRequest("No fields to update".to_string()));
    }

    // K3: Dynamic SQL built and executed inside transaction
    state.rls_client.with_system(|txn| Box::pin(async move {
        let mut updates = Vec::new();
        let mut params: Vec<&(dyn tokio_postgres::types::ToSql + Sync)> = vec![&user_id];
        let mut param_idx = 2;

        if let Some(ref display_name) = req.display_name {
            updates.push(format!("display_name = ${}", param_idx));
            params.push(display_name);
            param_idx += 1;
        }

        if let Some(ref email) = req.email {
            updates.push(format!("email = ${}", param_idx));
            params.push(email);
        }

        let _ = param_idx; // suppress unused assignment warning

        updates.push("updated_at = NOW()".to_string());

        let query = format!(
            "UPDATE users SET {} WHERE id = $1 RETURNING id, username, email, display_name, is_admin, is_active, created_at",
            updates.join(", ")
        );

        let row = txn.query_one(&query, &params).await?;

        Ok(Json(UserResponse {
            id: row.get::<_, Uuid>("id").to_string(),
            username: row.get("username"),
            email: row.get("email"),
            display_name: row.get("display_name"),
            is_admin: row.get("is_admin"),
            is_active: row.get("is_active"),
            created_at: row.get::<_, chrono::DateTime<chrono::Utc>>("created_at").to_rfc3339(),
        }))
    })).await
}

/// POST /api/v1/auth/change-password
pub async fn change_password(
    State(state): State<Arc<AppState>>,
    Extension(claims): Extension<Arc<Claims>>,
    JsonBody(req): JsonBody<ChangePasswordRequest>,
) -> Result<Json<serde_json::Value>, AppError> {
    // Sprint 2.2: Strong password policy for Admin
    validate_password_strength(&req.new_password)?;

    let user_id = Uuid::parse_str(&claims.sub)
        .map_err(|_| AppError::Internal("Invalid user ID in token".to_string()))?;

    // K3: Fetch current password hash
    let current_hash: String = state
        .rls_client
        .with_system(|txn| {
            Box::pin(async move {
                let row = txn
                    .query_opt("SELECT password_hash FROM users WHERE id = $1", &[&user_id])
                    .await?
                    .ok_or_else(|| AppError::NotFound("User not found".to_string()))?;
                Ok(row.get("password_hash"))
            })
        })
        .await?;

    // Verify current password (CPU-intensive, runs in blocking thread)
    let password_valid = {
        let pw = req.current_password.clone();
        let hash = current_hash.clone();
        tokio::task::spawn_blocking(move || bcrypt::verify(&pw, &hash))
            .await
            .map_err(|e| AppError::Internal(format!("Blocking task join error: {e}")))?
            .map_err(|e| AppError::Internal(format!("Password verification failed: {e}")))?
    };

    if !password_valid {
        return Err(AppError::Unauthorized(
            "Current password is incorrect".to_string(),
        ));
    }

    // Hash new password (CPU-intensive, runs in blocking thread)
    let new_hash = {
        let pw = req.new_password.clone();
        tokio::task::spawn_blocking(move || bcrypt::hash(&pw, bcrypt::DEFAULT_COST))
            .await
            .map_err(|e| AppError::Internal(format!("Blocking task join error: {e}")))?
            .map_err(|e| AppError::Internal(format!("Password hashing failed: {e}")))?
    };

    // K3: Update password in transaction
    state
        .rls_client
        .with_system(|txn| {
            Box::pin(async move {
                txn.execute(
                    "UPDATE users SET password_hash = $1, updated_at = NOW() WHERE id = $2",
                    &[&new_hash, &user_id],
                )
                .await?;
                Ok(())
            })
        })
        .await?;

    Ok(Json(serde_json::json!({
        "message": "Password changed successfully"
    })))
}

/// POST /api/v1/auth/logout — Revokes the JWT token (Sprint 2.8)
pub async fn logout(
    State(state): State<Arc<AppState>>,
    Extension(claims): Extension<Arc<Claims>>,
) -> Result<Json<serde_json::Value>, AppError> {
    let user_id = Uuid::parse_str(&claims.sub)
        .map_err(|_| AppError::Internal("Invalid user ID in token".to_string()))?;

    // Sprint 2.8: Add jti to revoked-tokens cache (immediate invalidation)
    state.revoked_tokens.insert(claims.jti.clone(), ());

    // K3: Persist revoked token to DB for restart-safety
    let jti_uuid = Uuid::parse_str(&claims.jti).unwrap_or_else(|_| Uuid::new_v4());
    let expires_at = chrono::DateTime::from_timestamp(claims.exp, 0)
        .unwrap_or_else(|| chrono::Utc::now() + chrono::Duration::hours(24));

    state.rls_client.with_system(|txn| Box::pin(async move {
        txn.execute(
            "INSERT INTO revoked_tokens (jti, user_id, expires_at) VALUES ($1, $2, $3) ON CONFLICT DO NOTHING",
            &[&jti_uuid, &user_id, &expires_at],
        ).await?;
        Ok(())
    })).await?;

    // Best-effort audit log (separate transaction, ignore failure)
    let _ = state
        .rls_client
        .with_system(|txn| {
            Box::pin(async move {
                txn.execute(
                    r#"
            INSERT INTO audit_log (user_id, action, resource_type, details, created_at)
            VALUES ($1, 'logout', 'session', '{}', NOW())
            "#,
                    &[&user_id],
                )
                .await?;
                Ok(())
            })
        })
        .await;

    tracing::info!(user_id = %user_id, jti = %claims.jti, "Token revoked via logout");

    Ok(Json(serde_json::json!({
        "message": "Logged out successfully"
    })))
}

// ===================================================================
// Admin User Management
// ===================================================================

#[derive(Debug, Serialize)]
pub struct UserListResponse {
    pub users: Vec<UserResponse>,
    pub total: i64,
}

/// GET /api/v1/admin/users
pub async fn admin_list_users(
    State(state): State<Arc<AppState>>,
) -> Result<Json<UserListResponse>, AppError> {
    // K3: All DB queries via RlsClient (users table has no RLS, use with_system)
    state
        .rls_client
        .with_system(|txn| {
            Box::pin(async move {
                let rows = txn
                    .query(
                        r#"
                SELECT id, username, email, display_name, is_admin, is_active, created_at
                FROM users
                ORDER BY created_at DESC
                LIMIT 100
                "#,
                        &[],
                    )
                    .await?;

                let count_row = txn
                    .query_one("SELECT COUNT(*) as total FROM users", &[])
                    .await?;
                let total: i64 = count_row.get("total");

                let users = rows
                    .iter()
                    .map(|row| UserResponse {
                        id: row.get::<_, Uuid>("id").to_string(),
                        username: row.get("username"),
                        email: row.get("email"),
                        display_name: row.get("display_name"),
                        is_admin: row.get("is_admin"),
                        is_active: row.get("is_active"),
                        created_at: row
                            .get::<_, chrono::DateTime<chrono::Utc>>("created_at")
                            .to_rfc3339(),
                    })
                    .collect();

                Ok(Json(UserListResponse { users, total }))
            })
        })
        .await
}

/// GET /api/v1/admin/users/:id
pub async fn admin_get_user(
    State(state): State<Arc<AppState>>,
    Path(id): Path<Uuid>,
) -> Result<Json<UserResponse>, AppError> {
    state
        .rls_client
        .with_system(|txn| {
            Box::pin(async move {
                let row = txn
                    .query_opt(
                        r#"
                SELECT id, username, email, display_name, is_admin, is_active, created_at
                FROM users
                WHERE id = $1
                "#,
                        &[&id],
                    )
                    .await?
                    .ok_or_else(|| AppError::NotFound("User not found".to_string()))?;

                Ok(Json(UserResponse {
                    id: row.get::<_, Uuid>("id").to_string(),
                    username: row.get("username"),
                    email: row.get("email"),
                    display_name: row.get("display_name"),
                    is_admin: row.get("is_admin"),
                    is_active: row.get("is_active"),
                    created_at: row
                        .get::<_, chrono::DateTime<chrono::Utc>>("created_at")
                        .to_rfc3339(),
                }))
            })
        })
        .await
}

#[derive(Debug, Deserialize)]
pub struct AdminUpdateUserRequest {
    pub is_admin: Option<bool>,
    pub is_active: Option<bool>,
    pub display_name: Option<String>,
}

/// PATCH /api/v1/admin/users/:id
pub async fn admin_update_user(
    State(state): State<Arc<AppState>>,
    Path(id): Path<Uuid>,
    JsonBody(req): JsonBody<AdminUpdateUserRequest>,
) -> Result<Json<UserResponse>, AppError> {
    // Validate before entering transaction
    if req.is_admin.is_none() && req.is_active.is_none() && req.display_name.is_none() {
        return Err(AppError::BadRequest("No fields to update".to_string()));
    }

    // K3: Dynamic SQL built and executed inside transaction
    state.rls_client.with_system(|txn| Box::pin(async move {
        let mut updates = Vec::new();
        let mut params: Vec<&(dyn tokio_postgres::types::ToSql + Sync)> = vec![&id];
        let mut param_idx = 2;

        if let Some(ref is_admin) = req.is_admin {
            updates.push(format!("is_admin = ${}", param_idx));
            params.push(is_admin);
            param_idx += 1;
        }

        if let Some(ref is_active) = req.is_active {
            updates.push(format!("is_active = ${}", param_idx));
            params.push(is_active);
            param_idx += 1;
        }

        if let Some(ref display_name) = req.display_name {
            updates.push(format!("display_name = ${}", param_idx));
            params.push(display_name);
        }

        let _ = param_idx; // suppress unused assignment warning

        updates.push("updated_at = NOW()".to_string());

        let query = format!(
            "UPDATE users SET {} WHERE id = $1 RETURNING id, username, email, display_name, is_admin, is_active, created_at",
            updates.join(", ")
        );

        let row = txn.query_one(&query, &params).await?;

        Ok(Json(UserResponse {
            id: row.get::<_, Uuid>("id").to_string(),
            username: row.get("username"),
            email: row.get("email"),
            display_name: row.get("display_name"),
            is_admin: row.get("is_admin"),
            is_active: row.get("is_active"),
            created_at: row.get::<_, chrono::DateTime<chrono::Utc>>("created_at").to_rfc3339(),
        }))
    })).await
}

// ===================================================================
// Password Validation (Sprint 2.2)
// ===================================================================

/// Validate password strength: min 8 chars, 1 upper, 1 lower, 1 digit, 1 special
fn validate_password_strength(password: &str) -> Result<(), AppError> {
    if password.len() < 8 {
        return Err(AppError::BadRequest(
            "Password must be at least 8 characters".to_string(),
        ));
    }
    if !password.chars().any(|c| c.is_uppercase()) {
        return Err(AppError::BadRequest(
            "Password must contain at least one uppercase letter".to_string(),
        ));
    }
    if !password.chars().any(|c| c.is_lowercase()) {
        return Err(AppError::BadRequest(
            "Password must contain at least one lowercase letter".to_string(),
        ));
    }
    if !password.chars().any(|c| c.is_ascii_digit()) {
        return Err(AppError::BadRequest(
            "Password must contain at least one digit".to_string(),
        ));
    }
    if !password.chars().any(|c| !c.is_alphanumeric()) {
        return Err(AppError::BadRequest(
            "Password must contain at least one special character".to_string(),
        ));
    }
    Ok(())
}

/// DELETE /api/v1/admin/users/:id
pub async fn admin_delete_user(
    State(state): State<Arc<AppState>>,
    Extension(claims): Extension<Arc<Claims>>,
    Path(id): Path<Uuid>,
) -> Result<StatusCode, AppError> {
    // Prevent self-deletion (check outside transaction)
    if claims.sub == id.to_string() {
        return Err(AppError::BadRequest(
            "Cannot delete your own account".to_string(),
        ));
    }

    // K3: Delete user in transaction
    let result = state
        .rls_client
        .with_system(|txn| {
            Box::pin(async move {
                // H10: Prevent deleting the last admin — would lock out the system
                let target_is_admin: bool = txn
                    .query_one("SELECT is_admin FROM users WHERE id = $1", &[&id])
                    .await
                    .map(|row| row.get::<_, bool>("is_admin"))
                    .unwrap_or(false);

                if target_is_admin {
                    let admin_count: i64 = txn
                        .query_one("SELECT COUNT(*) FROM users WHERE is_admin = true", &[])
                        .await?
                        .get(0);
                    if admin_count <= 1 {
                        return Err(crate::error::AppError::BadRequest(
                            "Cannot delete the last admin user".to_string(),
                        ));
                    }
                }

                let result = txn
                    .execute("DELETE FROM users WHERE id = $1", &[&id])
                    .await?;
                Ok(result)
            })
        })
        .await?;

    if result == 0 {
        return Err(AppError::NotFound("User not found".to_string()));
    }

    Ok(StatusCode::NO_CONTENT)
}

use axum::{
    http::StatusCode,
    response::{IntoResponse, Response},
    Json,
};
use serde_json::json;

#[derive(Debug, thiserror::Error)]
pub enum AppError {
    #[error("Database error: {0}")]
    Database(#[from] tokio_postgres::Error),

    #[error("Pool error: {0}")]
    Pool(#[from] deadpool_postgres::PoolError),

    #[error("IO error: {0}")]
    Io(#[from] std::io::Error),

    #[error("Qdrant error: {0}")]
    Qdrant(String),

    #[error("TEI error: {0}")]
    Tei(String),

    #[error("Authentication error: {0}")]
    Auth(String),

    #[error("Unauthorized: {0}")]
    Unauthorized(String),

    #[error("Forbidden: {0}")]
    Forbidden(String),

    #[error("Not found: {0}")]
    NotFound(String),

    #[error("Bad request: {0}")]
    BadRequest(String),

    #[error("Conflict: {0}")]
    Conflict(String),

    #[error("Internal error: {0}")]
    Internal(String),

    #[error("Rate limited")]
    RateLimited,
}

impl IntoResponse for AppError {
    fn into_response(self) -> Response {
        let (status, message) = match &self {
            AppError::Database(e) => {
                tracing::error!("Database error: {:?}", e);
                (StatusCode::INTERNAL_SERVER_ERROR, "Database error")
            }
            AppError::Pool(e) => {
                tracing::error!("Pool error: {:?}", e);
                (StatusCode::SERVICE_UNAVAILABLE, "Database pool exhausted")
            }
            AppError::Io(e) => {
                tracing::error!("IO error: {:?}", e);
                (StatusCode::INTERNAL_SERVER_ERROR, "IO error")
            }
            AppError::Qdrant(msg) => {
                tracing::error!("Qdrant error: {}", msg);
                (StatusCode::INTERNAL_SERVER_ERROR, "Vector store error")
            }
            AppError::Tei(msg) => {
                tracing::error!("TEI error: {}", msg);
                (StatusCode::INTERNAL_SERVER_ERROR, "Embedding service error")
            }
            AppError::Auth(msg) => {
                // Sprint 2.5: Log internal details, return generic message to client
                tracing::warn!("Auth error: {}", msg);
                (StatusCode::UNAUTHORIZED, "Authentication failed")
            }
            AppError::Unauthorized(msg) => {
                tracing::warn!("Unauthorized: {}", msg);
                (StatusCode::UNAUTHORIZED, "Invalid credentials")
            }
            AppError::Forbidden(msg) => {
                tracing::warn!("Forbidden: {}", msg);
                (StatusCode::FORBIDDEN, "Access denied")
            }
            AppError::NotFound(msg) => {
                (StatusCode::NOT_FOUND, msg.as_str())
            }
            AppError::BadRequest(msg) => {
                (StatusCode::BAD_REQUEST, msg.as_str())
            }
            AppError::Conflict(msg) => {
                (StatusCode::CONFLICT, msg.as_str())
            }
            AppError::Internal(msg) => {
                tracing::error!("Internal error: {}", msg);
                (StatusCode::INTERNAL_SERVER_ERROR, "Internal server error")
            }
            AppError::RateLimited => {
                (StatusCode::TOO_MANY_REQUESTS, "Rate limit exceeded")
            }
        };

        let body = Json(json!({
            "error": message,
            "status": status.as_u16()
        }));

        (status, body).into_response()
    }
}

pub type Result<T> = std::result::Result<T, AppError>;

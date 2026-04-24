//! Custom extractors with improved error handling

use axum::{
    async_trait,
    extract::{FromRequest, Request, rejection::JsonRejection},
    http::StatusCode,
    response::{IntoResponse, Response},
    Json,
};
use serde::de::DeserializeOwned;
use serde_json::json;

/// Custom JSON extractor that returns JSON error responses
pub struct JsonBody<T>(pub T);

#[derive(Debug)]
pub struct JsonBodyRejection(JsonRejection);

impl IntoResponse for JsonBodyRejection {
    fn into_response(self) -> Response {
        // M8: Generic error messages to client — detail only in server logs
        let (public_message, log_detail) = match &self.0 {
            JsonRejection::JsonDataError(e) => {
                ("Invalid JSON data".to_string(), e.body_text().to_string())
            }
            JsonRejection::JsonSyntaxError(e) => {
                ("JSON syntax error".to_string(), e.body_text().to_string())
            }
            JsonRejection::MissingJsonContentType(_) => {
                ("Content-Type must be application/json".to_string(), String::new())
            }
            JsonRejection::BytesRejection(_) => {
                ("Failed to read request body".to_string(), String::new())
            }
            _ => ("Invalid JSON request".to_string(), String::new()),
        };

        if !log_detail.is_empty() {
            tracing::debug!(detail = %log_detail, "JSON rejection detail (not sent to client)");
        }

        let body = Json(json!({
            "error": public_message,
            "status": 400
        }));

        (StatusCode::BAD_REQUEST, body).into_response()
    }
}

#[async_trait]
impl<S, T> FromRequest<S> for JsonBody<T>
where
    T: DeserializeOwned,
    S: Send + Sync,
{
    type Rejection = JsonBodyRejection;

    async fn from_request(req: Request, state: &S) -> Result<Self, Self::Rejection> {
        match Json::<T>::from_request(req, state).await {
            Ok(Json(value)) => Ok(JsonBody(value)),
            Err(rejection) => Err(JsonBodyRejection(rejection)),
        }
    }
}

// Re-export for convenience - allows using JsonBody like Json
impl<T> std::ops::Deref for JsonBody<T> {
    type Target = T;
    fn deref(&self) -> &Self::Target {
        &self.0
    }
}

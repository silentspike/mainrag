use axum::{body::Body, extract::Request, http::StatusCode, middleware::Next, response::Response};
use governor::{clock::DefaultClock, state::keyed::DefaultKeyedStateStore, Quota, RateLimiter};
use http_body_util::BodyExt;
use std::num::NonZeroU32;
use std::sync::Arc;

// W2: KeyedRateLimiter — per IP+Username rate limiting on auth routes only
pub type KeyedRateLimiter = Arc<RateLimiter<String, DefaultKeyedStateStore<String>, DefaultClock>>;

pub fn create_keyed_rate_limiter(requests_per_minute: u32) -> KeyedRateLimiter {
    let rpm = requests_per_minute.max(1);
    let quota = Quota::per_minute(
        NonZeroU32::new(rpm).expect("requests_per_minute guaranteed non-zero by max(1)"),
    );
    Arc::new(RateLimiter::keyed(quota))
}

pub async fn keyed_rate_limit_middleware(
    limiter: KeyedRateLimiter,
    request: Request,
    next: Next,
) -> Result<Response, StatusCode> {
    // Extract IP from x-forwarded-for or fallback to "unknown"
    let ip = request
        .headers()
        .get("x-forwarded-for")
        .and_then(|h| h.to_str().ok())
        .unwrap_or("unknown")
        .to_string();

    // Sprint 2.1: Extract username from JSON body for IP+Username keying
    // Buffer the body, parse username, then reconstruct the request
    let (parts, body) = request.into_parts();
    let bytes = body
        .collect()
        .await
        .map_err(|_| StatusCode::BAD_REQUEST)?
        .to_bytes();

    let username = serde_json::from_slice::<serde_json::Value>(&bytes)
        .ok()
        .and_then(|v| v.get("username").and_then(|u| u.as_str()).map(String::from))
        .unwrap_or_default();

    // S4: Key = IP + Username. If username is empty/missing, use IP-only key
    // (prevents bypass via missing username field)
    let key = if username.is_empty() {
        format!("ip:{}", ip)
    } else {
        format!("{}:{}", ip, username.to_lowercase())
    };

    // Reconstruct the request with the buffered body
    let request = Request::from_parts(parts, Body::from(bytes));

    match limiter.check_key(&key) {
        Ok(_) => Ok(next.run(request).await),
        Err(_) => {
            metrics::counter!("mainrag_rate_limit_hits", "route" => "auth").increment(1);
            // M5: Truncate username to prevent log injection
            let safe_user: String = username
                .chars()
                .take(64)
                .filter(|c| !c.is_control())
                .collect();
            tracing::warn!(ip = %ip, username = %safe_user, "Rate limit exceeded on auth route");
            Err(StatusCode::TOO_MANY_REQUESTS)
        }
    }
}

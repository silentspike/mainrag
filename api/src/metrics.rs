use axum::{
    extract::Request,
    middleware::Next,
    response::Response,
};
use metrics::{counter, histogram};
use metrics_exporter_prometheus::{PrometheusBuilder, PrometheusHandle};
use std::time::Instant;

pub fn setup_metrics() -> PrometheusHandle {
    PrometheusBuilder::new()
        .install_recorder()
        .expect("Failed to install Prometheus recorder")
}

pub async fn metrics_middleware(request: Request, next: Next) -> Response {
    let start = Instant::now();
    let method = request.method().to_string();
    let path = request.uri().path().to_string();

    let response = next.run(request).await;

    let duration = start.elapsed().as_secs_f64();
    let status = response.status().as_u16().to_string();

    // Record metrics
    counter!("http_requests_total", "method" => method.clone(), "path" => path.clone(), "status" => status.clone()).increment(1);
    histogram!("http_request_duration_seconds", "method" => method, "path" => path, "status" => status).record(duration);

    response
}

pub async fn metrics_handler(handle: PrometheusHandle) -> String {
    handle.render()
}

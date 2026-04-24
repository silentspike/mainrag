use axum::{
    extract::State,
    http::StatusCode,
    response::{sse::{Event, Sse}, IntoResponse},
    Json,
};
use serde::Serialize;
use sysinfo::{System, Pid};
use std::sync::Arc;
use std::sync::atomic::Ordering;
use tokio::time::{sleep, Duration};
use async_stream::stream;

use crate::{error::Result, AppState};

// Sprint 4.7: RAII Guard for SSE stream concurrency limiting.
// Ensures counter is always decremented even on abnormal disconnect.
const MAX_SSE_STREAMS: usize = 5;
const SSE_TIMEOUT_SECS: u64 = 30 * 60; // 30 minutes

struct SseGuard {
    counter: Arc<std::sync::atomic::AtomicUsize>,
}

impl SseGuard {
    fn try_acquire(counter: Arc<std::sync::atomic::AtomicUsize>) -> Option<Self> {
        let prev = counter.fetch_add(1, Ordering::Relaxed);
        if prev >= MAX_SSE_STREAMS {
            // Rollback — we exceeded the limit
            counter.fetch_sub(1, Ordering::Relaxed);
            None
        } else {
            Some(Self { counter })
        }
    }
}

impl Drop for SseGuard {
    fn drop(&mut self) {
        self.counter.fetch_sub(1, Ordering::Relaxed);
    }
}

// ============================================================
// Types
// ============================================================

#[derive(Debug, Clone, Serialize)]
pub struct ProcessStats {
    pub name: String,
    pub pid: u32,
    pub cpu_percent: f64,      // CPU percentage (0-100+)
    pub memory_mb: u64,        // Memory in MB
    pub uptime_sec: u64,       // Seconds since system boot
    pub status: String,        // "Running", "Stopped", etc
}

#[derive(Debug, Serialize)]
pub struct ProcessMonitorResponse {
    pub processes: Vec<ProcessStats>,
    pub timestamp_ms: u64,
}

// ============================================================
// Process Detection Functions
// ============================================================

async fn detect_mainrag_api_pid() -> Option<u32> {
    // Try pgrep first (more reliable)
    let output = tokio::process::Command::new("pgrep")
        .arg("-f")
        .arg("mainrag-api")
        .output()
        .await
        .ok()?;

    String::from_utf8_lossy(&output.stdout)
        .trim()
        .lines()
        .next()?
        .parse::<u32>()
        .ok()
}

async fn detect_postgresql_pid() -> Option<u32> {
    // Try systemctl first
    let output = tokio::process::Command::new("systemctl")
        .arg("show")
        .arg("--property=MainPID")
        .arg("postgresql")
        .output()
        .await
        .ok()?;

    let pid_str = String::from_utf8_lossy(&output.stdout);
    if let Some(pid) = pid_str.strip_prefix("MainPID=") {
        if let Ok(pid) = pid.trim().parse::<u32>() {
            if pid > 0 {
                return Some(pid);
            }
        }
    }

    // Fallback: pgrep
    let output = tokio::process::Command::new("pgrep")
        .arg("-f")
        .arg("postgres")
        .output()
        .await
        .ok()?;

    String::from_utf8_lossy(&output.stdout)
        .trim()
        .lines()
        .next()?
        .parse::<u32>()
        .ok()
}

async fn detect_qdrant_pid() -> Option<u32> {
    // Try systemctl first
    let output = tokio::process::Command::new("systemctl")
        .arg("show")
        .arg("--property=MainPID")
        .arg("qdrant")
        .output()
        .await
        .ok()?;

    let pid_str = String::from_utf8_lossy(&output.stdout);
    if let Some(pid) = pid_str.strip_prefix("MainPID=") {
        if let Ok(pid) = pid.trim().parse::<u32>() {
            if pid > 0 {
                return Some(pid);
            }
        }
    }

    // Fallback: docker ps
    let output = tokio::process::Command::new("docker")
        .arg("ps")
        .arg("--filter")
        .arg("name=qdrant")
        .arg("--format")
        .arg("{{.Pid}}")
        .output()
        .await
        .ok()?;

    String::from_utf8_lossy(&output.stdout)
        .trim()
        .parse::<u32>()
        .ok()
}

async fn detect_tei_pid() -> Option<u32> {
    // TEI runs in Docker
    let output = tokio::process::Command::new("docker")
        .arg("ps")
        .arg("--filter")
        .arg("name=mainrag-tei")
        .arg("--format")
        .arg("{{.Pid}}")
        .output()
        .await
        .ok()?;

    let pid_str = String::from_utf8_lossy(&output.stdout).trim().to_string();
    if !pid_str.is_empty() {
        return pid_str.parse::<u32>().ok();
    }

    None
}

// ============================================================
// Stats Collection
// ============================================================

pub async fn get_all_processes() -> Vec<ProcessStats> {
    let mut sys = System::new_all();
    sys.refresh_all();

    let mut stats = Vec::new();

    // Detect PIDs for 4 processes
    let (api_pid, pg_pid, qdrant_pid, tei_pid) = tokio::join!(
        detect_mainrag_api_pid(),
        detect_postgresql_pid(),
        detect_qdrant_pid(),
        detect_tei_pid(),
    );

    let processes = [
        ("mainrag-api", api_pid),
        ("postgresql", pg_pid),
        ("qdrant", qdrant_pid),
        ("tei", tei_pid),
    ];

    let uptime = System::uptime();

    for (name, opt_pid) in &processes {
        if let Some(pid) = opt_pid {
            if let Some(process) = sys.process(Pid::from(*pid as usize)) {
                stats.push(ProcessStats {
                    name: name.to_string(),
                    pid: *pid,
                    cpu_percent: process.cpu_usage() as f64,
                    memory_mb: process.memory() / 1024 / 1024,  // sysinfo returns Bytes, convert to MB
                    uptime_sec: uptime,
                    status: format!("{:?}", process.status()).replace("Running", "Running"),
                });
            } else {
                // Process not found in /proc (stopped)
                stats.push(ProcessStats {
                    name: name.to_string(),
                    pid: *pid,
                    cpu_percent: 0.0,
                    memory_mb: 0,
                    uptime_sec: 0,
                    status: "Stopped".to_string(),
                });
            }
        } else {
            // PID detection failed (not running)
            stats.push(ProcessStats {
                name: name.to_string(),
                pid: 0,
                cpu_percent: 0.0,
                memory_mb: 0,
                uptime_sec: 0,
                status: "Stopped".to_string(),
            });
        }
    }

    stats
}

// ============================================================
// HTTP Handlers
// ============================================================

pub async fn admin_process_stats(
    State(_state): State<Arc<AppState>>,
) -> Result<Json<ProcessMonitorResponse>> {
    let processes = get_all_processes().await;

    Ok(Json(ProcessMonitorResponse {
        processes,
        timestamp_ms: chrono::Utc::now().timestamp_millis() as u64,
    }))
}

pub async fn admin_process_stats_stream(
    State(state): State<Arc<AppState>>,
) -> impl IntoResponse {
    // Sprint 4.7: RAII concurrency guard — max 5 concurrent SSE streams
    let guard = match SseGuard::try_acquire(state.sse_active_streams.clone()) {
        Some(g) => g,
        None => {
            tracing::warn!("SSE stream rejected: max {} concurrent streams reached", MAX_SSE_STREAMS);
            return Err(StatusCode::SERVICE_UNAVAILABLE);
        }
    };

    let stream = stream! {
        // Move guard into stream so it lives as long as the stream
        let _guard = guard;

        // Sprint 4.7: 30 minute timeout via tokio::time::timeout
        let deadline = tokio::time::Instant::now() + Duration::from_secs(SSE_TIMEOUT_SECS);

        loop {
            if tokio::time::Instant::now() >= deadline {
                tracing::info!("SSE stream timeout after {}min", SSE_TIMEOUT_SECS / 60);
                break;
            }

            let stats = get_all_processes().await;
            let response = ProcessMonitorResponse {
                processes: stats,
                timestamp_ms: chrono::Utc::now().timestamp_millis() as u64,
            };

            match serde_json::to_string(&response) {
                Ok(json) => {
                    let event: std::result::Result<Event, crate::error::AppError> = Ok(Event::default().data(json));
                    yield event;
                }
                Err(e) => {
                    tracing::error!("Failed to serialize process stats: {}", e);
                }
            }

            sleep(Duration::from_millis(1000)).await;
        }
    };

    Ok(Sse::new(stream))
}

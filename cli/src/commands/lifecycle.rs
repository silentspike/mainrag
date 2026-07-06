use crate::client::api::HealthResponse;
use crate::client::ApiClient;
use anyhow::{anyhow, bail, Context, Result};
use colored::Colorize;
use serde::Serialize;
use serde_json::{json, Value};
use std::io::Write;
#[cfg(unix)]
use std::os::unix::process::ExitStatusExt;
use std::path::Path;
use std::process::{Command, ExitStatus, Output, Stdio};
use std::thread;
use std::time::{Duration, Instant};

const COMPOSE_FILE: &str = "/work/mainrag/docker-compose.yml";
const COMPOSE_ENV_FILE: &str = "/etc/mainrag/mainrag.env";
const VERIFY_QUERY: &str = "mainrag";

const APP_UNITS: &[&str] = &[
    "mainrag-watcher.service",
    "mainrag-watcher-codex.service",
    "mainrag-watcher-gemini.service",
    "mainrag-svelte.service",
    "mainrag-api.service",
];

const WATCHER_UNITS: &[&str] = &[
    "mainrag-watcher.service",
    "mainrag-watcher-codex.service",
    "mainrag-watcher-gemini.service",
];

const LEGACY_GPU_UNITS: &[&str] = &[
    "mainrag-tei.service",
    "mainrag-tei-gte.service",
    "mainrag-tei-reranker.service",
    "qdrant.service",
];

const GPU_CONTAINERS: &[&str] = &[
    "mainrag-tei-gte",
    "mainrag-tei-reranker",
    "mainrag-tei-embeddings",
    "qdrant-mainrag",
];

const CPU_TIMERS: &[&str] = &[
    "pgbackrest-mainrag.timer",
    "pgbackrest-mainrag-full.timer",
    "mainrag-pbs-backup.timer",
];

const GPU_TIMERS: &[&str] = &[
    "mainrag-qdrant-snapshot.timer",
    "pgbackrest-mainrag.timer",
    "pgbackrest-mainrag-full.timer",
    "mainrag-pbs-backup.timer",
];

const STOP_TIMERS: &[&str] = &[
    "mainrag-qdrant-snapshot.timer",
    "pgbackrest-mainrag.timer",
    "pgbackrest-mainrag-full.timer",
    "mainrag-pbs-backup.timer",
    "finanzioso-collector.timer",
    "finanzioso-finalizer.timer",
    "finanzioso-retention.timer",
];

const CPU_INACTIVE_TIMERS: &[&str] = &[
    "mainrag-qdrant-snapshot.timer",
    "finanzioso-collector.timer",
    "finanzioso-finalizer.timer",
    "finanzioso-retention.timer",
];

const FAILED_RESET_UNITS: &[&str] = &[
    "postgresql.service",
    "mainrag-api.service",
    "mainrag-svelte.service",
    "mainrag-watcher.service",
    "mainrag-watcher-codex.service",
    "mainrag-watcher-gemini.service",
    "mainrag-tei.service",
    "mainrag-tei-gte.service",
    "mainrag-tei-reranker.service",
    "qdrant.service",
    "finanzioso-collector.service",
    "finanzioso-finalizer.service",
    "finanzioso-retention.service",
    "mainrag-qdrant-snapshot.service",
    "mainrag-pbs-backup.service",
    "pgbackrest-mainrag.service",
    "pgbackrest-mainrag-full.service",
    "mainrag-qdrant-snapshot.timer",
    "pgbackrest-mainrag.timer",
    "pgbackrest-mainrag-full.timer",
    "mainrag-pbs-backup.timer",
    "finanzioso-collector.timer",
    "finanzioso-finalizer.timer",
    "finanzioso-retention.timer",
];

const CPU_ACTIVE_UNITS: &[&str] = &[
    "postgresql.service",
    "mainrag-api.service",
    "mainrag-svelte.service",
    "mainrag-watcher.service",
    "mainrag-watcher-codex.service",
    "mainrag-watcher-gemini.service",
];

const STOP_VERIFY_UNITS: &[&str] = &[
    "postgresql.service",
    "mainrag-api.service",
    "mainrag-svelte.service",
    "mainrag-watcher.service",
    "mainrag-watcher-codex.service",
    "mainrag-watcher-gemini.service",
    "mainrag-tei.service",
    "mainrag-tei-gte.service",
    "mainrag-tei-reranker.service",
    "qdrant.service",
];

const WATCHER_INTERVAL_DROPIN: &str = "cpu-watch-interval.conf";
const API_CPU_DROPIN: &str = "/etc/systemd/system/mainrag-api.service.d/cpu-mode.conf";

#[derive(Debug, Clone, Copy, Serialize)]
#[serde(rename_all = "lowercase")]
enum CheckState {
    Pass,
    Fail,
    Skip,
}

#[derive(Debug, Clone, Serialize)]
struct Check {
    name: String,
    state: CheckState,
    detail: String,
}

impl Check {
    fn pass(name: impl Into<String>, detail: impl Into<String>) -> Self {
        Self {
            name: name.into(),
            state: CheckState::Pass,
            detail: detail.into(),
        }
    }

    fn fail(name: impl Into<String>, detail: impl Into<String>) -> Self {
        Self {
            name: name.into(),
            state: CheckState::Fail,
            detail: detail.into(),
        }
    }

    fn skip(name: impl Into<String>, detail: impl Into<String>) -> Self {
        Self {
            name: name.into(),
            state: CheckState::Skip,
            detail: detail.into(),
        }
    }

    fn failed(&self) -> bool {
        matches!(self.state, CheckState::Fail)
    }
}

enum AuthProbe<T> {
    Available(T),
    Skipped(String),
}

pub async fn stop(json_output: bool) -> Result<()> {
    if !json_output {
        println!("{}", "Stopping MainRAG lifecycle services...".bold());
    }

    systemctl_tolerant("stop", APP_UNITS);
    systemctl_tolerant("stop", LEGACY_GPU_UNITS);
    compose_tolerant(&["down"]);

    for container in GPU_CONTAINERS {
        docker_stop_remove(container);
    }

    systemctl_tolerant("stop", STOP_TIMERS);
    stop_postgres_if_dedicated()?;
    reset_failed_units();
    let checks = verify_stopped();
    print_checks("mainrag --stop", &checks, json_output)?;
    Ok(())
}

pub async fn start_cpu(api_url: &str, json_output: bool) -> Result<()> {
    if !json_output {
        println!("{}", "Starting MainRAG in CPU-only mode...".bold());
    }

    let mut checks = cpu_preflight_checks();
    if checks.iter().any(Check::failed) {
        print_checks("CPU preflight", &checks, json_output)?;
    }

    systemctl_tolerant("stop", APP_UNITS);
    systemctl_tolerant("stop", STOP_TIMERS);
    write_cpu_dropins()?;
    run_checked("sudo", &["systemctl", "daemon-reload"])?;

    run_checked("sudo", &["systemctl", "start", "postgresql.service"])?;
    wait_for_pg()?;

    systemctl_tolerant("stop", LEGACY_GPU_UNITS);
    compose_tolerant(&["down"]);
    for container in GPU_CONTAINERS {
        docker_stop_remove(container);
    }

    run_checked("sudo", &["systemctl", "start", "mainrag-api.service"])?;
    wait_for_http_ok(
        &format!("{}/healthz", trim_url(api_url)),
        Duration::from_secs(60),
    )
    .await?;

    checks.push(journal_contains_check(
        "CPU mode banner",
        "mainrag-api.service",
        "MAINRAG CPU MODE",
    ));
    checks.extend(authenticated_health_checks(api_url, "cpu").await?);
    checks.push(watcher_token_check(api_url).await);
    if checks.iter().any(Check::failed) {
        print_checks("mainrag --cpu", &checks, json_output)?;
    }

    systemctl_checked(
        "start",
        &[
            "mainrag-svelte.service",
            "mainrag-watcher.service",
            "mainrag-watcher-codex.service",
            "mainrag-watcher-gemini.service",
        ],
    )?;
    systemctl_checked("start", CPU_TIMERS)?;

    checks.extend(active_unit_checks(CPU_ACTIVE_UNITS));
    checks.extend(active_unit_checks(CPU_TIMERS));
    checks.extend(inactive_unit_checks(CPU_INACTIVE_TIMERS));
    checks.extend(no_gpu_container_checks());
    checks.extend(authenticated_search_checks(api_url, SearchExpectation::Cpu).await?);

    print_checks("mainrag --cpu", &checks, json_output)?;
    Ok(())
}

pub async fn start_gpu(api_url: &str, json_output: bool) -> Result<()> {
    if !json_output {
        println!("{}", "Starting MainRAG in full GPU mode...".bold());
    }

    run_checked("nvidia-smi", &["-L"]).context("nvidia-smi did not list a GPU")?;
    systemctl_tolerant("stop", APP_UNITS);
    systemctl_tolerant("stop", STOP_TIMERS);

    remove_cpu_dropins()?;
    run_checked("sudo", &["systemctl", "daemon-reload"])?;

    run_checked("sudo", &["systemctl", "start", "postgresql.service"])?;
    wait_for_pg()?;

    if let Err(err) = compose_up_and_wait().await {
        rollback_gpu_start_failure();
        bail!(
            "GPU stack did not become healthy: {}. Use `mainrag --cpu` while the GPU stack is unavailable.",
            err
        );
    }

    run_checked("sudo", &["systemctl", "start", "mainrag-api.service"])?;
    wait_for_http_ok(
        &format!("{}/healthz", trim_url(api_url)),
        Duration::from_secs(60),
    )
    .await?;

    let mut checks = authenticated_health_checks(api_url, "full").await?;
    checks.push(watcher_token_check(api_url).await);
    if checks.iter().any(Check::failed) {
        print_checks("mainrag --gpu", &checks, json_output)?;
    }

    systemctl_checked(
        "start",
        &[
            "mainrag-svelte.service",
            "mainrag-watcher.service",
            "mainrag-watcher-codex.service",
            "mainrag-watcher-gemini.service",
        ],
    )?;
    systemctl_checked("start", GPU_TIMERS)?;

    checks.extend(active_unit_checks(CPU_ACTIVE_UNITS));
    checks.extend(active_unit_checks(GPU_TIMERS));
    checks.extend(container_health_checks());
    checks.extend(authenticated_search_checks(api_url, SearchExpectation::Full).await?);

    print_checks("mainrag --gpu", &checks, json_output)?;
    Ok(())
}

fn cpu_preflight_checks() -> Vec<Check> {
    let mut checks = Vec::new();
    let wants = run_tolerant("systemctl", &["show", "mainrag-api.service", "-p", "Wants"]);
    let wants_text = output_text(&wants);
    if wants.status.success()
        && !wants_text.contains("qdrant")
        && !wants_text.contains("mainrag-tei")
    {
        checks.push(Check::pass("mainrag-api Wants", wants_text));
    } else {
        checks.push(Check::fail(
            "mainrag-api Wants",
            format!(
                "{}; run C1 cleanup first: remove qdrant/tei Wants from mainrag-api.service",
                if wants_text.is_empty() {
                    "unable to inspect Wants".to_string()
                } else {
                    wants_text
                }
            ),
        ));
    }

    for unit in LEGACY_GPU_UNITS {
        let status = systemctl_is_enabled(unit);
        if status == "masked" {
            checks.push(Check::pass(format!("{unit} masked"), status));
        } else {
            checks.push(Check::fail(
                format!("{unit} masked"),
                format!("is-enabled={status}; run C1 cleanup first"),
            ));
        }
    }
    checks
}

fn write_cpu_dropins() -> Result<()> {
    write_root_file(
        API_CPU_DROPIN,
        "[Service]\nEnvironment=MAINRAG_CPU_MODE=true\nCPUQuota=600%\nCPUWeight=40\nIOWeight=40\n",
    )?;

    for unit in WATCHER_UNITS {
        write_root_file(
            &watcher_dropin_path(unit),
            "[Service]\nEnvironment=MAINRAG_WATCH_MIN_SYNC_SECS=600\n",
        )?;
    }
    Ok(())
}

fn remove_cpu_dropins() -> Result<()> {
    let mut paths = vec![API_CPU_DROPIN.to_string()];
    for unit in WATCHER_UNITS {
        paths.push(watcher_dropin_path(unit));
    }
    let args: Vec<&str> = std::iter::once("rm")
        .chain(std::iter::once("-f"))
        .chain(paths.iter().map(String::as_str))
        .collect();
    run_checked("sudo", &args)?;
    Ok(())
}

fn watcher_dropin_path(unit: &str) -> String {
    format!("/etc/systemd/system/{unit}.d/{WATCHER_INTERVAL_DROPIN}")
}

fn stop_postgres_if_dedicated() -> Result<()> {
    let active = systemctl_is_active("postgresql.service") == "active";
    if active {
        assert_postgres_dedicated()?;
    }
    run_tolerant("sudo", &["systemctl", "stop", "postgresql.service"]);
    Ok(())
}

fn assert_postgres_dedicated() -> Result<()> {
    let output = run_checked("sudo", &["-u", "postgres", "psql", "-lqt"])
        .context("Failed to list PostgreSQL databases before stop")?;
    let dbs = parse_psql_database_names(&String::from_utf8_lossy(&output.stdout));
    let unknown: Vec<String> = dbs
        .into_iter()
        .filter(|name| !is_allowed_local_database(name))
        .collect();

    if !unknown.is_empty() {
        bail!(
            "Refusing to stop PostgreSQL: unknown databases found: {}",
            unknown.join(", ")
        );
    }
    Ok(())
}

fn is_allowed_local_database(name: &str) -> bool {
    matches!(
        name,
        "mainrag" | "coderag" | "multillm" | "postgres" | "template0" | "template1"
    ) || name.starts_with("finanzioso")
}

fn parse_psql_database_names(output: &str) -> Vec<String> {
    output
        .lines()
        .filter_map(|line| line.split('|').next())
        .map(str::trim)
        .filter(|name| !name.is_empty())
        .map(ToOwned::to_owned)
        .collect()
}

fn verify_stopped() -> Vec<Check> {
    let mut checks = Vec::new();
    for unit in STOP_VERIFY_UNITS.iter().chain(STOP_TIMERS.iter()) {
        let status = systemctl_is_active(unit);
        if status == "active" || status == "activating" {
            checks.push(Check::fail(format!("{unit} inactive"), status));
        } else {
            checks.push(Check::pass(format!("{unit} inactive"), status));
        }
    }
    checks.extend(failed_state_checks(
        STOP_VERIFY_UNITS
            .iter()
            .chain(STOP_TIMERS.iter())
            .copied()
            .chain([
                "finanzioso-collector.service",
                "finanzioso-finalizer.service",
                "finanzioso-retention.service",
            ]),
    ));

    checks.extend(no_gpu_container_checks());

    for port in [3001_u16, 3002, 5432, 6333, 6334, 8082, 8090, 8091] {
        if port_is_free(port) {
            checks.push(Check::pass(format!("port {port} free"), "not listening"));
        } else {
            checks.push(Check::fail(format!("port {port} free"), "still listening"));
        }
    }

    let pgrep = run_tolerant("pgrep", &["-f", "mainrag-api|mainrag watch"]);
    if pgrep.status.success() {
        checks.push(Check::fail(
            "mainrag processes stopped",
            String::from_utf8_lossy(&pgrep.stdout).trim().to_string(),
        ));
    } else {
        checks.push(Check::pass("mainrag processes stopped", "no pgrep match"));
    }

    checks
}

fn reset_failed_units() {
    let args: Vec<&str> = std::iter::once("systemctl")
        .chain(std::iter::once("reset-failed"))
        .chain(FAILED_RESET_UNITS.iter().copied())
        .collect();
    run_tolerant("sudo", &args);
}

fn failed_state_checks<'a>(units: impl IntoIterator<Item = &'a str>) -> Vec<Check> {
    units
        .into_iter()
        .map(|unit| {
            let status = systemctl_is_failed(unit);
            if status == "failed" {
                Check::fail(format!("{unit} not failed"), status)
            } else {
                Check::pass(format!("{unit} not failed"), status)
            }
        })
        .collect()
}

fn active_unit_checks(units: &[&str]) -> Vec<Check> {
    units
        .iter()
        .map(|unit| {
            let status = systemctl_is_active(unit);
            if status == "active" {
                Check::pass(format!("{unit} active"), status)
            } else {
                Check::fail(format!("{unit} active"), status)
            }
        })
        .collect()
}

fn inactive_unit_checks(units: &[&str]) -> Vec<Check> {
    units
        .iter()
        .map(|unit| {
            let status = systemctl_is_active(unit);
            if status == "active" || status == "activating" {
                Check::fail(format!("{unit} inactive"), status)
            } else {
                Check::pass(format!("{unit} inactive"), status)
            }
        })
        .collect()
}

fn no_gpu_container_checks() -> Vec<Check> {
    GPU_CONTAINERS
        .iter()
        .map(|name| {
            if docker_running(name) {
                Check::fail(format!("{name} stopped"), "running")
            } else {
                Check::pass(format!("{name} stopped"), "not running")
            }
        })
        .collect()
}

fn container_health_checks() -> Vec<Check> {
    ["mainrag-tei-gte", "mainrag-tei-reranker", "qdrant-mainrag"]
        .iter()
        .map(|name| {
            let status = docker_health_status(name);
            if status == "healthy" {
                Check::pass(format!("{name} healthy"), status)
            } else {
                Check::fail(format!("{name} healthy"), status)
            }
        })
        .collect()
}

async fn authenticated_health_checks(api_url: &str, expected_mode: &str) -> Result<Vec<Check>> {
    let mut checks = Vec::new();
    match authenticated_health(api_url).await? {
        AuthProbe::Skipped(reason) => {
            checks.push(Check::skip("authenticated /api/v1/health", reason));
        }
        AuthProbe::Available(health) => {
            let mode = health.mode.as_deref().unwrap_or("unknown");
            let mode_ok = mode == expected_mode;
            let status_ok = health.status == "healthy";
            let service_ok = if expected_mode == "cpu" {
                health.services.postgres && !health.services.qdrant && !health.services.tei
            } else {
                health.services.postgres && health.services.qdrant && health.services.tei
            };
            if mode_ok && status_ok && service_ok {
                checks.push(Check::pass(
                    "authenticated /api/v1/health",
                    format!(
                        "status={}, mode={}, pg={}, qdrant={}, tei={}",
                        health.status,
                        mode,
                        health.services.postgres,
                        health.services.qdrant,
                        health.services.tei
                    ),
                ));
            } else {
                checks.push(Check::fail(
                    "authenticated /api/v1/health",
                    format!(
                        "status={}, mode={}, pg={}, qdrant={}, tei={}",
                        health.status,
                        mode,
                        health.services.postgres,
                        health.services.qdrant,
                        health.services.tei
                    ),
                ));
            }
        }
    }
    Ok(checks)
}

async fn authenticated_health(api_url: &str) -> Result<AuthProbe<HealthResponse>> {
    let Some(token) = load_token() else {
        return Ok(AuthProbe::Skipped(
            "no stored token; run `mainrag auth login` for authenticated checks".to_string(),
        ));
    };

    let mut client = ApiClient::new(api_url)?;
    client.set_token(token);
    match client.health().await {
        Ok(health) => Ok(AuthProbe::Available(health)),
        Err(err) if err.to_string().contains("Unauthorized") => Ok(AuthProbe::Skipped(
            "stored token was rejected; run `mainrag auth login`".to_string(),
        )),
        Err(err) => Err(err),
    }
}

async fn watcher_token_check(api_url: &str) -> Check {
    let token_output = run_tolerant(
        "sudo",
        &[
            "-u",
            "mainrag",
            "cat",
            "/var/lib/mainrag/.config/mainrag/token",
        ],
    );
    if !token_output.status.success() {
        return Check::fail(
            "watcher token /var/lib/mainrag",
            format!(
                "{}; run `sudo -u mainrag HOME=/var/lib/mainrag /opt/mainrag/bin/mainrag auth login`",
                output_text(&token_output)
            ),
        );
    }

    let token = String::from_utf8_lossy(&token_output.stdout)
        .trim()
        .to_string();
    if token.is_empty() {
        return Check::fail(
            "watcher token /var/lib/mainrag",
            "token file is empty; run `sudo -u mainrag HOME=/var/lib/mainrag /opt/mainrag/bin/mainrag auth login`",
        );
    }

    let client = match reqwest::Client::builder()
        .timeout(Duration::from_secs(10))
        .build()
    {
        Ok(client) => client,
        Err(err) => return Check::fail("watcher token /api/v1/sources", err.to_string()),
    };

    let url = format!("{}/api/v1/sources", trim_url(api_url));
    match client.get(url).bearer_auth(token).send().await {
        Ok(response) if response.status().is_success() => Check::pass(
            "watcher token /api/v1/sources",
            "valid for HOME=/var/lib/mainrag",
        ),
        Ok(response) if response.status() == reqwest::StatusCode::UNAUTHORIZED => Check::fail(
            "watcher token /api/v1/sources",
            "stored watcher token was rejected; run `sudo -u mainrag HOME=/var/lib/mainrag /opt/mainrag/bin/mainrag auth login`",
        ),
        Ok(response) => Check::fail(
            "watcher token /api/v1/sources",
            format!("HTTP {}", response.status()),
        ),
        Err(err) => Check::fail("watcher token /api/v1/sources", err.to_string()),
    }
}

#[derive(Clone, Copy)]
enum SearchExpectation {
    Cpu,
    Full,
}

async fn authenticated_search_checks(
    api_url: &str,
    expectation: SearchExpectation,
) -> Result<Vec<Check>> {
    let Some(token) = load_token() else {
        return Ok(vec![Check::skip(
            "authenticated search probes",
            "no stored token; run `mainrag auth login`",
        )]);
    };

    let mut checks = Vec::new();
    match post_search_probe(api_url, &token, "search/keyword").await {
        Ok(probe) => {
            if probe.total > 0 {
                checks.push(Check::pass(
                    "POST /search/keyword",
                    format!("200 total={}", probe.total),
                ));
            } else {
                checks.push(Check::fail("POST /search/keyword", "200 but total=0"));
            }
        }
        Err(err) if err.to_string().contains("Unauthorized") => checks.push(Check::skip(
            "POST /search/keyword",
            "stored token was rejected; run `mainrag auth login`",
        )),
        Err(err) => checks.push(Check::fail("POST /search/keyword", err.to_string())),
    }

    match post_search_probe(api_url, &token, "search").await {
        Ok(probe) => {
            let expected = match expectation {
                SearchExpectation::Cpu => "degraded-fts-only",
                SearchExpectation::Full => "full",
            };
            if probe.search_mode.as_deref() == Some(expected) && probe.total > 0 {
                checks.push(Check::pass(
                    "POST /search",
                    format!(
                        "200 x-search-mode={} total={}",
                        probe.search_mode.unwrap_or_default(),
                        probe.total
                    ),
                ));
            } else {
                checks.push(Check::fail(
                    "POST /search",
                    format!(
                        "x-search-mode={:?}, total={}, expected={}",
                        probe.search_mode, probe.total, expected
                    ),
                ));
            }
        }
        Err(err) if err.to_string().contains("Unauthorized") => checks.push(Check::skip(
            "POST /search",
            "stored token was rejected; run `mainrag auth login`",
        )),
        Err(err) => checks.push(Check::fail("POST /search", err.to_string())),
    }

    Ok(checks)
}

struct SearchProbe {
    total: u64,
    search_mode: Option<String>,
}

async fn post_search_probe(api_url: &str, token: &str, endpoint: &str) -> Result<SearchProbe> {
    let client = reqwest::Client::builder()
        .timeout(Duration::from_secs(30))
        .build()?;
    let url = format!("{}/api/v1/{endpoint}", trim_url(api_url));
    let response = client
        .post(url)
        .bearer_auth(token)
        .json(&json!({
            "query": VERIFY_QUERY,
            "limit": 3
        }))
        .send()
        .await
        .context("search probe request failed")?;

    if response.status() == reqwest::StatusCode::UNAUTHORIZED {
        bail!("Unauthorized");
    }
    if !response.status().is_success() {
        let status = response.status();
        let body = response.text().await.unwrap_or_default();
        bail!("HTTP {status}: {body}");
    }

    let search_mode = response
        .headers()
        .get("x-search-mode")
        .and_then(|value| value.to_str().ok())
        .map(ToOwned::to_owned);
    let body: Value = response
        .json()
        .await
        .context("parse search probe response")?;
    let total = body.get("total").and_then(Value::as_u64).unwrap_or(0);
    Ok(SearchProbe { total, search_mode })
}

async fn compose_up_and_wait() -> Result<()> {
    run_checked(
        "docker",
        &[
            "compose",
            "-f",
            COMPOSE_FILE,
            "--env-file",
            COMPOSE_ENV_FILE,
            "up",
            "-d",
        ],
    )?;

    let expected = ["mainrag-tei-gte", "mainrag-tei-reranker", "qdrant-mainrag"];
    let start = Instant::now();
    let timeout = Duration::from_secs(300);
    loop {
        let statuses: Vec<String> = expected
            .iter()
            .map(|name| format!("{name}={}", docker_health_status(name)))
            .collect();
        if statuses.iter().all(|status| status.ends_with("=healthy")) {
            return Ok(());
        }
        if start.elapsed() >= timeout {
            bail!("container health timeout: {}", statuses.join(", "));
        }
        thread::sleep(Duration::from_secs(5));
    }
}

fn rollback_gpu_start_failure() {
    compose_tolerant(&["down"]);
    systemctl_tolerant("stop", APP_UNITS);
    systemctl_tolerant("stop", GPU_TIMERS);
    let _ = stop_postgres_if_dedicated();
    reset_failed_units();
}

async fn wait_for_http_ok(url: &str, timeout: Duration) -> Result<()> {
    let client = reqwest::Client::builder()
        .timeout(Duration::from_secs(5))
        .build()?;
    let start = Instant::now();
    loop {
        match client.get(url).send().await {
            Ok(response) if response.status().is_success() => return Ok(()),
            _ if start.elapsed() >= timeout => bail!("Timed out waiting for {url}"),
            _ => thread::sleep(Duration::from_secs(1)),
        }
    }
}

fn wait_for_pg() -> Result<()> {
    wait_for("PostgreSQL readiness", Duration::from_secs(30), || {
        run_tolerant("pg_isready", &[]).status.success()
    })
}

fn wait_for<F>(description: &str, timeout: Duration, mut poll: F) -> Result<()>
where
    F: FnMut() -> bool,
{
    let start = Instant::now();
    loop {
        if poll() {
            return Ok(());
        }
        if start.elapsed() >= timeout {
            bail!("Timed out waiting for {description}");
        }
        thread::sleep(Duration::from_secs(1));
    }
}

fn journal_contains_check(name: &str, unit: &str, needle: &str) -> Check {
    let output = run_tolerant(
        "sudo",
        &["journalctl", "-u", unit, "-n", "200", "--no-pager"],
    );
    let text = output_text(&output);
    if output.status.success() && text.contains(needle) {
        Check::pass(name, format!("journal contains `{needle}`"))
    } else {
        Check::fail(name, format!("journal did not contain `{needle}`"))
    }
}

fn print_checks(title: &str, checks: &[Check], json_output: bool) -> Result<()> {
    let failed = checks.iter().any(Check::failed);
    if json_output {
        println!(
            "{}",
            serde_json::to_string_pretty(&json!({
                "operation": title,
                "ok": !failed,
                "checks": checks,
            }))?
        );
    } else {
        println!();
        println!("{}", title.bold());
        for check in checks {
            let label = match check.state {
                CheckState::Pass => "[ok]".green(),
                CheckState::Fail => "[fail]".red(),
                CheckState::Skip => "[skip]".yellow(),
            };
            println!("  {} {} - {}", label, check.name.cyan(), check.detail);
        }
    }

    if failed {
        bail!("{title} verification failed");
    }
    Ok(())
}

fn systemctl_checked(action: &str, units: &[&str]) -> Result<()> {
    let args: Vec<&str> = std::iter::once("systemctl")
        .chain(std::iter::once(action))
        .chain(units.iter().copied())
        .collect();
    run_checked("sudo", &args)?;
    Ok(())
}

fn systemctl_tolerant(action: &str, units: &[&str]) {
    let args: Vec<&str> = std::iter::once("systemctl")
        .chain(std::iter::once(action))
        .chain(units.iter().copied())
        .collect();
    run_tolerant("sudo", &args);
}

fn compose_tolerant(args: &[&str]) {
    let full_args: Vec<&str> = [
        "compose",
        "-f",
        COMPOSE_FILE,
        "--env-file",
        COMPOSE_ENV_FILE,
    ]
    .into_iter()
    .chain(args.iter().copied())
    .collect();
    run_tolerant("docker", &full_args);
}

fn docker_stop_remove(container: &str) {
    run_tolerant("docker", &["stop", container]);
    run_tolerant("docker", &["rm", container]);
}

fn docker_running(name: &str) -> bool {
    let output = run_tolerant("docker", &["ps", "--format", "{{.Names}}"]);
    String::from_utf8_lossy(&output.stdout)
        .lines()
        .any(|line| line.trim() == name)
}

fn docker_health_status(name: &str) -> String {
    let output = run_tolerant(
        "docker",
        &[
            "inspect",
            "--format",
            "{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}",
            name,
        ],
    );
    if output.status.success() {
        String::from_utf8_lossy(&output.stdout).trim().to_string()
    } else {
        output_text(&output)
    }
}

fn port_is_free(port: u16) -> bool {
    let port_filter = format!(":{port}");
    let output = run_tolerant("ss", &["-H", "-tln", "sport", "=", &port_filter]);
    String::from_utf8_lossy(&output.stdout).trim().is_empty()
}

fn systemctl_is_active(unit: &str) -> String {
    let output = run_tolerant("systemctl", &["is-active", unit]);
    String::from_utf8_lossy(&output.stdout).trim().to_string()
}

fn systemctl_is_enabled(unit: &str) -> String {
    let output = run_tolerant("systemctl", &["is-enabled", unit]);
    String::from_utf8_lossy(&output.stdout).trim().to_string()
}

fn systemctl_is_failed(unit: &str) -> String {
    let output = run_tolerant("systemctl", &["is-failed", unit]);
    String::from_utf8_lossy(&output.stdout).trim().to_string()
}

fn write_root_file(path: &str, contents: &str) -> Result<()> {
    if let Some(parent) = Path::new(path).parent() {
        let parent = parent
            .to_str()
            .ok_or_else(|| anyhow!("non-UTF8 path: {}", parent.display()))?;
        run_checked("sudo", &["install", "-d", "-m", "755", parent])?;
    }

    let mut child = Command::new("sudo")
        .arg("tee")
        .arg(path)
        .stdin(Stdio::piped())
        .stdout(Stdio::null())
        .spawn()
        .with_context(|| format!("failed to start sudo tee for {path}"))?;

    child
        .stdin
        .as_mut()
        .ok_or_else(|| anyhow!("failed to open stdin for sudo tee"))?
        .write_all(contents.as_bytes())?;

    let status = child.wait()?;
    if !status.success() {
        bail!("failed to write {path}");
    }
    Ok(())
}

fn run_checked(program: &str, args: &[&str]) -> Result<Output> {
    let output = Command::new(program)
        .args(args)
        .output()
        .with_context(|| format!("failed to run {}", command_text(program, args)))?;
    if !output.status.success() {
        bail!(
            "{} failed: {}",
            command_text(program, args),
            output_text(&output)
        );
    }
    Ok(output)
}

fn run_tolerant(program: &str, args: &[&str]) -> Output {
    Command::new(program)
        .args(args)
        .output()
        .unwrap_or_else(|err| {
            failed_output(&format!(
                "failed to run {}: {err}",
                command_text(program, args)
            ))
        })
}

#[cfg(unix)]
fn failed_output(message: &str) -> Output {
    Output {
        status: ExitStatus::from_raw(127 << 8),
        stdout: Vec::new(),
        stderr: message.as_bytes().to_vec(),
    }
}

#[cfg(not(unix))]
fn failed_output(message: &str) -> Output {
    let _ = message;
    panic!("failed to run command")
}

fn output_text(output: &Output) -> String {
    let stdout = String::from_utf8_lossy(&output.stdout).trim().to_string();
    let stderr = String::from_utf8_lossy(&output.stderr).trim().to_string();
    match (stdout.is_empty(), stderr.is_empty()) {
        (true, true) => String::new(),
        (false, true) => stdout,
        (true, false) => stderr,
        (false, false) => format!("{stdout}; {stderr}"),
    }
}

fn command_text(program: &str, args: &[&str]) -> String {
    std::iter::once(program)
        .chain(args.iter().copied())
        .collect::<Vec<_>>()
        .join(" ")
}

fn load_token() -> Option<String> {
    ApiClient::load_token_from_file().filter(|token| !token.trim().is_empty())
}

fn trim_url(url: &str) -> &str {
    url.trim_end_matches('/')
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_psql_database_names() {
        let names = parse_psql_database_names(
            " mainrag | postgres | UTF8\n template0 | postgres | UTF8\n finanzioso | postgres | UTF8\n",
        );
        assert_eq!(names, vec!["mainrag", "template0", "finanzioso"]);
    }

    #[test]
    fn local_database_allowlist_is_explicit() {
        for name in [
            "mainrag",
            "coderag",
            "multillm",
            "postgres",
            "template0",
            "template1",
            "finanzioso",
            "finanzioso_test",
        ] {
            assert!(is_allowed_local_database(name), "{name}");
        }

        for name in ["production", "customer_app", "mainrag_backup", "codex"] {
            assert!(!is_allowed_local_database(name), "{name}");
        }
    }

    #[test]
    fn watcher_dropin_uses_full_unit_name() {
        assert_eq!(
            watcher_dropin_path("mainrag-watcher.service"),
            "/etc/systemd/system/mainrag-watcher.service.d/cpu-watch-interval.conf"
        );
    }

    #[test]
    fn lifecycle_timers_cover_pbs_and_snapshot_modes() {
        assert!(CPU_TIMERS.contains(&"mainrag-pbs-backup.timer"));
        assert!(!CPU_TIMERS.contains(&"mainrag-qdrant-snapshot.timer"));
        assert!(GPU_TIMERS.contains(&"mainrag-pbs-backup.timer"));
        assert!(GPU_TIMERS.contains(&"mainrag-qdrant-snapshot.timer"));
        assert!(STOP_TIMERS.contains(&"mainrag-pbs-backup.timer"));
        assert!(STOP_TIMERS.contains(&"finanzioso-finalizer.timer"));
        assert!(CPU_INACTIVE_TIMERS.contains(&"mainrag-qdrant-snapshot.timer"));
    }

    #[test]
    fn failed_reset_covers_mainrag_and_zombie_units() {
        for unit in [
            "qdrant.service",
            "finanzioso-finalizer.service",
            "mainrag-api.service",
            "postgresql.service",
            "mainrag-pbs-backup.timer",
        ] {
            assert!(FAILED_RESET_UNITS.contains(&unit), "{unit}");
        }
    }
}

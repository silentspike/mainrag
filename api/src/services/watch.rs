//! File System Watch Service for automatic incremental re-indexing
//!
//! Enterprise-grade watch service with:
//! - In-flight tracking (prevents re-queuing files during processing)
//! - Per-file rate limiting (15s cooldown)
//! - Signature cache (mtime + size check before expensive operations)
//! - Adaptive debounce (flush on idle OR max wait)
//! - Prometheus metrics
//! - Integration with IndexService pipeline

use anyhow::Result;
use metrics::{counter, gauge, histogram};
use moka::sync::Cache;
use notify::RecursiveMode;
use notify_debouncer_mini::{new_debouncer, DebouncedEvent};
use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::{Duration, Instant, SystemTime};
use tokio::sync::mpsc;
use tracing::{debug, error, info, warn};

/// Watch service configuration
#[derive(Clone)]
pub struct WatchConfig {
    /// Debounce duration in milliseconds (notify-level coalescing)
    pub debounce_ms: u64,
    /// Minimum interval between syncs for the same file (rate limiting)
    pub min_sync_interval: Duration,
    /// Flush pending files after this idle duration
    pub idle_flush: Duration,
    /// Maximum wait before forcing a flush
    pub max_wait: Duration,
    /// File extensions to watch (empty = all supported)
    pub extensions: Vec<String>,
    /// Directories to ignore
    pub ignore_dirs: Vec<String>,
}

impl Default for WatchConfig {
    fn default() -> Self {
        // Configurable via environment variables
        let debounce_ms = std::env::var("MAINRAG_WATCH_DEBOUNCE_MS")
            .ok()
            .and_then(|v| v.parse().ok())
            .unwrap_or(300);

        let min_sync_secs = std::env::var("MAINRAG_WATCH_MIN_SYNC_SECS")
            .ok()
            .and_then(|v| v.parse().ok())
            .unwrap_or(15);

        Self {
            debounce_ms,
            min_sync_interval: Duration::from_secs(min_sync_secs),
            idle_flush: Duration::from_millis(300),
            max_wait: Duration::from_millis(1200),
            extensions: vec![], // Empty = use SUPPORTED_EXTENSIONS
            ignore_dirs: vec![
                ".git".to_string(),
                ".hg".to_string(),
                ".svn".to_string(),
                ".idea".to_string(),
                ".vscode".to_string(),
                ".vs".to_string(),
                "node_modules".to_string(),
                "__pycache__".to_string(),
                ".pytest_cache".to_string(),
                ".mypy_cache".to_string(),
                ".tox".to_string(),
                ".venv".to_string(),
                "venv".to_string(),
                ".eggs".to_string(),
                "target".to_string(),
                ".cargo".to_string(),
                "vendor".to_string(),
                "build".to_string(),
                "dist".to_string(),
                "out".to_string(),
                ".gradle".to_string(),
                "bin".to_string(),
                "obj".to_string(),
                "Pods".to_string(),
                ".cache".to_string(),
                ".nx".to_string(),
                ".turbo".to_string(),
            ],
        }
    }
}

/// Supported file extensions (must match IndexService)
const SUPPORTED_EXTENSIONS: &[&str] = &[
    // Programming languages
    "rs", "py", "pyw", "pyi", "js", "jsx", "mjs", "cjs", "ts", "tsx", "mts", "cts", "c", "cpp",
    "cc", "cxx", "h", "hpp", "hxx", "hh", "cs", "go", "zig", "lua", "java", "rb", "rake",
    "gemspec", "php", "phtml", "sh", "bash", "zsh", // Config/Data/Markup
    "html", "htm", "css", "scss", "sass", "less", "xml", "xsl", "xsd", "svg", "yaml", "yml",
    "json", "jsonc", "jsonl", "toml", "sql", "md", "markdown", "scm", "ss",
    "rkt", // Documents
    "txt", "pdf",
];

/// Lightweight file signature to avoid expensive content hashing
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct FileSignature {
    pub modified: SystemTime,
    pub len: u64,
}

impl FileSignature {
    pub fn from_path(path: &Path) -> Option<Self> {
        let meta = std::fs::metadata(path).ok()?;
        let modified = meta.modified().ok()?;
        Some(Self {
            modified,
            len: meta.len(),
        })
    }
}

/// Watched source entry
#[derive(Debug, Clone)]
pub struct WatchedSource {
    pub source_id: i64,
    pub name: String,
    pub path: PathBuf,
}

/// File change event with rate limiting info
#[derive(Debug, Clone)]
pub struct FileChangeEvent {
    pub source_id: i64,
    pub source_name: String,
    pub path: PathBuf,
    pub kind: FileChangeKind,
}

#[derive(Debug, Clone, Copy)]
pub enum FileChangeKind {
    Created,
    Modified,
    Deleted,
}

/// Batch of files ready for sync
#[derive(Debug, Clone)]
pub struct SyncBatch {
    pub source_id: i64,
    pub source_name: String,
    pub files: Vec<PathBuf>,
}

/// Watch service metrics
struct WatchMetrics;

impl WatchMetrics {
    fn record_event_received() {
        counter!("mainrag_watch_events_received_total").increment(1);
    }

    fn record_event_filtered(reason: &str) {
        counter!("mainrag_watch_events_filtered_total", "reason" => reason.to_string())
            .increment(1);
    }

    fn record_batch_dispatched(file_count: usize) {
        counter!("mainrag_watch_batches_dispatched_total").increment(1);
        histogram!("mainrag_watch_batch_size").record(file_count as f64);
    }

    fn set_pending_files(count: usize) {
        gauge!("mainrag_watch_pending_files").set(count as f64);
    }

    fn set_in_flight_files(count: usize) {
        gauge!("mainrag_watch_in_flight_files").set(count as f64);
    }

    fn set_watched_sources(count: usize) {
        gauge!("mainrag_watch_sources_total").set(count as f64);
    }
}

/// Enterprise-grade watch service with optimizations
pub struct WatchService {
    config: WatchConfig,
    sources: HashMap<i64, WatchedSource>,
    /// Signature cache: mtime + size to detect real changes (moka: max 100k, TTL 1h)
    signature_cache: Arc<Cache<PathBuf, FileSignature>>,
    /// Last sync time per file (rate limiting) (moka: max 100k, TTL 1h)
    last_sync_time: Arc<Cache<PathBuf, Instant>>,
    /// Files currently being processed (in-flight tracking) (moka: max 100k, TTL 1h)
    in_flight: Arc<Cache<PathBuf, ()>>,
    /// Channel to send batches for processing
    batch_tx: mpsc::Sender<SyncBatch>,
}

impl WatchService {
    /// Create watch service with batch receiver
    pub fn new(config: WatchConfig) -> (Self, mpsc::Receiver<SyncBatch>) {
        let (batch_tx, batch_rx) = mpsc::channel(100);

        let cache_ttl = Duration::from_secs(3600); // 1 hour
        let max_entries: u64 = 100_000;

        (
            Self {
                config,
                sources: HashMap::new(),
                signature_cache: Arc::new(
                    Cache::builder()
                        .max_capacity(max_entries)
                        .time_to_live(cache_ttl)
                        .build(),
                ),
                last_sync_time: Arc::new(
                    Cache::builder()
                        .max_capacity(max_entries)
                        .time_to_live(cache_ttl)
                        .build(),
                ),
                in_flight: Arc::new(
                    Cache::builder()
                        .max_capacity(max_entries)
                        .time_to_live(cache_ttl)
                        .build(),
                ),
                batch_tx,
            },
            batch_rx,
        )
    }

    /// Add a source to watch
    pub fn add_source(&mut self, source: WatchedSource) -> Result<()> {
        info!(
            "Adding source to watch: {} ({})",
            source.name,
            source.path.display()
        );
        self.sources.insert(source.source_id, source);
        WatchMetrics::set_watched_sources(self.sources.len());
        Ok(())
    }

    /// Remove a source from watch
    pub fn remove_source(&mut self, source_id: i64) -> Result<()> {
        if let Some(source) = self.sources.remove(&source_id) {
            info!("Removed source from watch: {}", source.name);
            WatchMetrics::set_watched_sources(self.sources.len());
        }
        Ok(())
    }

    /// Mark files as completed (call from IndexService after sync)
    pub fn mark_completed(&self, files: &[PathBuf]) {
        let now = Instant::now();
        for path in files {
            self.in_flight.invalidate(path);
            self.last_sync_time.insert(path.clone(), now);
        }
        WatchMetrics::set_in_flight_files(self.in_flight.entry_count() as usize);
    }

    /// Mark files as failed (re-queue for retry)
    pub fn mark_failed(&self, files: &[PathBuf]) {
        for path in files {
            self.in_flight.invalidate(path);
            // Don't update last_sync_time - allow immediate retry
        }
        WatchMetrics::set_in_flight_files(self.in_flight.entry_count() as usize);
    }

    /// Get current stats
    pub fn stats(&self) -> WatchStats {
        WatchStats {
            sources: self.sources.len(),
            signature_cache_size: self.signature_cache.entry_count() as usize,
            last_sync_cache_size: self.last_sync_time.entry_count() as usize,
            in_flight: self.in_flight.entry_count() as usize,
        }
    }

    /// Start watching all sources (blocking - run in spawn_blocking)
    pub fn start_blocking(&self) -> Result<()> {
        if self.sources.is_empty() {
            warn!("No sources to watch");
            return Ok(());
        }

        let (tx, rx) = std::sync::mpsc::channel();

        let mut debouncer = new_debouncer(Duration::from_millis(self.config.debounce_ms), tx)?;

        // Watch all source paths
        for source in self.sources.values() {
            info!("Watching: {} at {}", source.name, source.path.display());
            debouncer
                .watcher()
                .watch(&source.path, RecursiveMode::Recursive)?;
        }

        info!(
            "Watch service started, monitoring {} sources",
            self.sources.len()
        );

        // Pending files per source
        let mut pending: HashMap<i64, Vec<PathBuf>> = HashMap::new();
        let mut last_event_time = Instant::now();
        let mut last_batch_time = Instant::now();

        // Event loop with adaptive debounce
        loop {
            match rx.recv_timeout(Duration::from_millis(100)) {
                Ok(Ok(events)) => {
                    let now = Instant::now();

                    for event in events {
                        WatchMetrics::record_event_received();

                        if let Some((source, filtered_path)) = self.filter_event(&event) {
                            pending
                                .entry(source.source_id)
                                .or_default()
                                .push(filtered_path);
                            last_event_time = now;
                        }
                    }
                }
                Ok(Err(err)) => {
                    error!("Watch error: {:?}", err);
                }
                Err(std::sync::mpsc::RecvTimeoutError::Timeout) => {
                    // Check for adaptive flush
                }
                Err(std::sync::mpsc::RecvTimeoutError::Disconnected) => {
                    info!("Watcher disconnected");
                    break;
                }
            }

            // Adaptive batch flush: idle timeout OR max wait
            let now = Instant::now();
            let idle = now.duration_since(last_event_time) >= self.config.idle_flush;
            let waited_too_long = now.duration_since(last_batch_time) >= self.config.max_wait;

            let total_pending: usize = pending.values().map(|v| v.len()).sum();
            WatchMetrics::set_pending_files(total_pending);

            if total_pending > 0 && (idle || waited_too_long) {
                // Dispatch batches per source
                for (source_id, files) in pending.drain() {
                    if files.is_empty() {
                        continue;
                    }

                    if let Some(source) = self.sources.get(&source_id) {
                        // Mark files as in-flight BEFORE dispatching
                        for path in &files {
                            self.in_flight.insert(path.clone(), ());
                        }
                        WatchMetrics::set_in_flight_files(self.in_flight.entry_count() as usize);

                        let batch = SyncBatch {
                            source_id,
                            source_name: source.name.clone(),
                            files: files.clone(),
                        };

                        WatchMetrics::record_batch_dispatched(batch.files.len());

                        info!(
                            "[{}] Dispatching {} file(s) for source '{}'",
                            chrono::Local::now().format("%H:%M:%S"),
                            batch.files.len(),
                            source.name
                        );

                        for path in &batch.files {
                            let rel = path.strip_prefix(&source.path).unwrap_or(path);
                            debug!("  - {}", rel.display());
                        }

                        // Non-blocking send (drop if receiver is full)
                        if self.batch_tx.try_send(batch).is_err() {
                            warn!(
                                "Batch channel full, dropping batch for source {}",
                                source_id
                            );
                            // Remove from in-flight since we couldn't dispatch
                            for path in &files {
                                self.in_flight.invalidate(path);
                            }
                        }
                    }
                }

                last_batch_time = now;
            }
        }

        Ok(())
    }

    /// Filter an event and return (source, path) if it should be processed
    fn filter_event(&self, event: &DebouncedEvent) -> Option<(WatchedSource, PathBuf)> {
        let path = &event.path;

        // Find which source this belongs to
        let source = self
            .sources
            .values()
            .find(|s| path.starts_with(&s.path))?
            .clone();

        // Check if file has supported extension
        if !self.is_supported_file(path) {
            WatchMetrics::record_event_filtered("unsupported_extension");
            return None;
        }

        // Check if in excluded directory
        if self.is_excluded_path(path, &source.path) {
            WatchMetrics::record_event_filtered("excluded_dir");
            return None;
        }

        // Rate limiting: skip if synced recently
        let now = Instant::now();
        if let Some(last_time) = self.last_sync_time.get(path) {
            if now.duration_since(last_time) < self.config.min_sync_interval {
                WatchMetrics::record_event_filtered("rate_limited");
                debug!("Rate limited: {}", path.display());
                return None;
            }
        }

        // In-flight check: skip if currently being processed
        if self.in_flight.contains_key(path) {
            WatchMetrics::record_event_filtered("in_flight");
            debug!("In-flight, skipping: {}", path.display());
            return None;
        }

        // Signature check: skip if mtime+size unchanged
        if path.exists() {
            if let Some(sig) = FileSignature::from_path(path) {
                if let Some(prev) = self.signature_cache.get(path) {
                    if prev == sig {
                        WatchMetrics::record_event_filtered("signature_unchanged");
                        debug!("Signature unchanged: {}", path.display());
                        return None;
                    }
                }
                self.signature_cache.insert(path.clone(), sig);
            }
        } else {
            // File was removed; drop cached signature
            self.signature_cache.invalidate(path);
        }

        Some((source, path.clone()))
    }

    /// Check if file has a supported extension
    fn is_supported_file(&self, path: &Path) -> bool {
        // Special case: Dockerfile
        if let Some(filename) = path.file_name().and_then(|n| n.to_str()) {
            if filename == "Dockerfile" || filename.starts_with("Dockerfile.") {
                return true;
            }
        }

        // Check extension against config or defaults
        let extensions = if self.config.extensions.is_empty() {
            SUPPORTED_EXTENSIONS
        } else {
            // This is a bit awkward but necessary for the borrow checker
            return path
                .extension()
                .and_then(|ext| ext.to_str())
                .map(|ext| self.config.extensions.iter().any(|e| e == ext))
                .unwrap_or(false);
        };

        path.extension()
            .and_then(|ext| ext.to_str())
            .map(|ext| extensions.contains(&ext))
            .unwrap_or(false)
    }

    /// Check if path is in an excluded directory
    fn is_excluded_path(&self, path: &Path, base_path: &Path) -> bool {
        let rel_path = path.strip_prefix(base_path).unwrap_or(path);

        for component in rel_path.components() {
            if let std::path::Component::Normal(name) = component {
                let name_str = name.to_string_lossy();
                if self
                    .config
                    .ignore_dirs
                    .iter()
                    .any(|d| d == name_str.as_ref())
                {
                    return true;
                }
            }
        }

        false
    }
}

/// Watch service statistics
#[derive(Debug, Clone)]
pub struct WatchStats {
    pub sources: usize,
    pub signature_cache_size: usize,
    pub last_sync_cache_size: usize,
    pub in_flight: usize,
}

/// Start watch service as async task with callback
pub async fn start_watch_service(
    sources: Vec<WatchedSource>,
    config: WatchConfig,
) -> Result<(Arc<WatchServiceHandle>, mpsc::Receiver<SyncBatch>)> {
    let (mut service, batch_rx) = WatchService::new(config);

    // Add sources
    for source in sources {
        service.add_source(source)?;
    }

    // Create handle for external control
    let signature_cache = service.signature_cache.clone();
    let last_sync_time = service.last_sync_time.clone();
    let in_flight = service.in_flight.clone();

    let handle = Arc::new(WatchServiceHandle {
        signature_cache,
        last_sync_time,
        in_flight,
    });

    // Spawn blocking watcher
    tokio::task::spawn_blocking(move || {
        if let Err(e) = service.start_blocking() {
            error!("Watch service error: {}", e);
        }
    });

    Ok((handle, batch_rx))
}

/// Handle for external control of watch service
pub struct WatchServiceHandle {
    signature_cache: Arc<Cache<PathBuf, FileSignature>>,
    last_sync_time: Arc<Cache<PathBuf, Instant>>,
    in_flight: Arc<Cache<PathBuf, ()>>,
}

impl WatchServiceHandle {
    /// Mark files as completed (call after successful sync)
    pub fn mark_completed(&self, files: &[PathBuf]) {
        let now = Instant::now();
        for path in files {
            self.in_flight.invalidate(path);
            self.last_sync_time.insert(path.clone(), now);
        }
        WatchMetrics::set_in_flight_files(self.in_flight.entry_count() as usize);
    }

    /// Mark files as failed (allows retry)
    pub fn mark_failed(&self, files: &[PathBuf]) {
        for path in files {
            self.in_flight.invalidate(path);
        }
        WatchMetrics::set_in_flight_files(self.in_flight.entry_count() as usize);
    }

    /// Get current stats
    pub fn stats(&self) -> WatchStats {
        WatchStats {
            sources: 0, // Not tracked in handle
            signature_cache_size: self.signature_cache.entry_count() as usize,
            last_sync_cache_size: self.last_sync_time.entry_count() as usize,
            in_flight: self.in_flight.entry_count() as usize,
        }
    }
}

// Environment Variable Documentation:
// MAINRAG_WATCH_DEBOUNCE_MS - Notify-level debounce in milliseconds
//   - Default: 300
//   - Coalesces rapid filesystem events before processing
//
// MAINRAG_WATCH_MIN_SYNC_SECS - Per-file rate limit in seconds
//   - Default: 15
//   - Prevents re-syncing the same file repeatedly
//   - Set higher for sources with frequent writes (e.g., JSONL logs)

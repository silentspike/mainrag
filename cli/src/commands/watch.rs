//! Watch command - monitor sources for changes with incremental sync
//!
//! Enterprise-grade watch with:
//! - In-flight tracking (prevents re-queuing files during processing)
//! - Per-file rate limiting (15s cooldown)
//! - Signature cache (mtime + size check)
//! - Adaptive debounce (flush on idle OR max wait)
//! - Incremental sync (only changed files, not full source)

use anyhow::Result;
use notify_debouncer_mini::new_debouncer;
use notify::RecursiveMode;
use std::collections::{HashMap, HashSet};
use std::path::PathBuf;
use std::time::{Duration, Instant, SystemTime};
use crate::client::ApiClient;
use colored::Colorize;

/// Minimum interval between syncs for the same file (rate limiting)
const MIN_SYNC_INTERVAL: Duration = Duration::from_secs(15);

/// Flush pending files after this idle duration
const IDLE_FLUSH: Duration = Duration::from_millis(300);

/// Maximum wait before forcing a flush
const MAX_WAIT: Duration = Duration::from_millis(1200);

/// Debounce window for notify
const DEBOUNCE_MS: u64 = 300;

/// Supported file extensions
const SUPPORTED_EXTENSIONS: &[&str] = &[
    "rs", "py", "pyw", "pyi", "js", "jsx", "mjs", "cjs", "ts", "tsx", "mts", "cts",
    "c", "cpp", "cc", "cxx", "h", "hpp", "hxx", "hh", "cs", "go", "zig", "lua",
    "java", "rb", "rake", "gemspec", "php", "phtml", "sh", "bash", "zsh",
    "html", "htm", "css", "scss", "sass", "less", "xml", "xsl", "xsd", "svg",
    "yaml", "yml", "json", "jsonc", "jsonl", "toml", "sql", "md", "markdown",
    "scm", "ss", "rkt", "txt", "pdf",
];

/// Directories to ignore
const IGNORE_DIRS: &[&str] = &[
    ".git", ".hg", ".svn", ".idea", ".vscode", ".vs",
    "node_modules", "__pycache__", ".pytest_cache", ".mypy_cache", ".tox",
    ".venv", "venv", ".eggs", "target", ".cargo", "vendor",
    "build", "dist", "out", ".gradle", "bin", "obj", "Pods",
    ".cache", ".nx", ".turbo",
];

/// File signature for cheap change detection
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
struct FileSignature {
    modified: SystemTime,
    len: u64,
}

impl FileSignature {
    fn from_path(path: &std::path::Path) -> Option<Self> {
        let meta = std::fs::metadata(path).ok()?;
        let modified = meta.modified().ok()?;
        Some(Self { modified, len: meta.len() })
    }
}

/// Watch sources for changes and trigger incremental re-sync
pub async fn watch(
    client: &ApiClient,
    source_name: Option<&str>,
    daemon: bool,
) -> Result<()> {
    // Get sources to watch
    let response = client.list_sources().await?;
    let sources = &response.sources;

    let sources_to_watch: Vec<(i64, String, PathBuf)> = if let Some(name) = source_name {
        // Watch specific source
        let source = sources.iter()
            .find(|s| s.name == name)
            .ok_or_else(|| anyhow::anyhow!("Source not found: {}", name))?;

        vec![(
            source.id,
            source.name.clone(),
            PathBuf::from(&source.path),
        )]
    } else {
        // Watch all fs sources
        sources.iter()
            .filter(|s| s.source_type == "fs")
            .filter_map(|s| {
                let path = PathBuf::from(&s.path);
                if path.exists() {
                    Some((s.id, s.name.clone(), path))
                } else {
                    None
                }
            })
            .collect()
    };

    if sources_to_watch.is_empty() {
        println!("{}", "No sources to watch".yellow());
        return Ok(());
    }

    println!("{}", format!("Watching {} sources:", sources_to_watch.len()).cyan());
    for (_, name, path) in &sources_to_watch {
        println!("  {} {}", format!("[{}]", name).cyan(), path.display());
    }
    println!();

    if daemon {
        println!("{}", "Running in daemon mode (Ctrl+C to stop)...".dimmed());
    } else {
        println!("{}", "Watching for changes (Ctrl+C to stop)...".dimmed());
    }

    // Set up debounced watcher
    let (tx, rx) = std::sync::mpsc::channel();
    let mut debouncer = new_debouncer(Duration::from_millis(DEBOUNCE_MS), tx)?;

    // Watch all paths (gracefully skip sources with permission errors)
    let mut watched_count = 0;
    for (_, name, path) in &sources_to_watch {
        match debouncer.watcher().watch(path, RecursiveMode::Recursive) {
            Ok(()) => {
                watched_count += 1;
            }
            Err(e) => {
                eprintln!(
                    "  {} Skipping source '{}': {} ({})",
                    "⚠".yellow(),
                    name,
                    e,
                    path.display()
                );
            }
        }
    }

    if watched_count == 0 {
        anyhow::bail!("No sources could be watched (all had permission errors)");
    }

    println!(
        "{}",
        format!("Successfully watching {}/{} sources", watched_count, sources_to_watch.len()).green()
    );

    // State for enterprise features
    let mut signature_cache: HashMap<PathBuf, FileSignature> = HashMap::new();
    let mut last_sync_time: HashMap<PathBuf, Instant> = HashMap::new();
    let mut in_flight: HashSet<PathBuf> = HashSet::new();
    let mut pending: HashMap<String, Vec<PathBuf>> = HashMap::new(); // source_name -> files
    let mut last_event_time = Instant::now();
    let mut last_batch_time = Instant::now();

    // Event loop with adaptive debounce
    loop {
        match rx.recv_timeout(Duration::from_millis(100)) {
            Ok(Ok(events)) => {
                let now = Instant::now();

                for event in events {
                    let path = &event.path;

                    // Check if should ignore
                    if should_ignore(path) {
                        continue;
                    }

                    // Check if supported file
                    if !is_supported_file(path) {
                        continue;
                    }

                    // Find source for this path
                    let source = sources_to_watch.iter()
                        .find(|(_, _, p)| path.starts_with(p));

                    if let Some((_id, name, _base_path)) = source {
                        // Rate limiting: skip if synced recently
                        if let Some(last_time) = last_sync_time.get(path) {
                            if now.duration_since(*last_time) < MIN_SYNC_INTERVAL {
                                continue;
                            }
                        }

                        // In-flight check: skip if currently being processed
                        if in_flight.contains(path) {
                            continue;
                        }

                        // Signature check: skip if mtime+size unchanged
                        if path.exists() {
                            if let Some(sig) = FileSignature::from_path(path) {
                                if let Some(prev) = signature_cache.get(path) {
                                    if *prev == sig {
                                        continue;
                                    }
                                }
                                signature_cache.insert(path.clone(), sig);
                            }
                        } else {
                            signature_cache.remove(path);
                        }

                        // Add to pending
                        pending
                            .entry(name.clone())
                            .or_default()
                            .push(path.clone());
                        last_event_time = now;
                    }
                }
            }
            Ok(Err(e)) => {
                eprintln!("{} Watch error: {:?}", "Error:".red(), e);
            }
            Err(std::sync::mpsc::RecvTimeoutError::Timeout) => {
                // Check if we should flush
            }
            Err(std::sync::mpsc::RecvTimeoutError::Disconnected) => {
                eprintln!("{} Channel error", "Error:".red());
                break;
            }
        }

        // Adaptive batch flush: idle timeout OR max wait
        let now = Instant::now();
        let idle = now.duration_since(last_event_time) >= IDLE_FLUSH;
        let waited_too_long = now.duration_since(last_batch_time) >= MAX_WAIT;

        let total_pending: usize = pending.values().map(|v| v.len()).sum();

        if total_pending > 0 && (idle || waited_too_long) {
            // Process each source's pending files
            for (source_name, files) in pending.drain() {
                if files.is_empty() {
                    continue;
                }

                // Mark files as in-flight BEFORE dispatching
                for path in &files {
                    in_flight.insert(path.clone());
                }

                // Find base path for relative display
                let base_path = sources_to_watch.iter()
                    .find(|(_, name, _)| name == &source_name)
                    .map(|(_, _, p)| p.clone())
                    .unwrap_or_default();

                println!(
                    "\n{} Syncing {} file(s) for '{}':",
                    format!("[{}]", chrono::Local::now().format("%H:%M:%S")).dimmed(),
                    files.len(),
                    source_name.cyan()
                );
                for path in &files {
                    let rel = path.strip_prefix(&base_path).unwrap_or(path);
                    println!("  - {}", rel.display());
                }

                // Trigger incremental sync via API
                match client.sync_files(&source_name, &files).await {
                    Ok(result) => {
                        println!(
                            "  {} +{} ~{} skip:{} chunks:{} embed:{}",
                            "✓".green(),
                            result.stats.files_processed,
                            result.stats.files_processed, // Updated
                            0i64, // Skipped not in response
                            result.stats.chunks_created,
                            result.stats.embeddings_generated
                        );

                        // Mark completed
                        let completed_time = Instant::now();
                        for path in &files {
                            in_flight.remove(path);
                            last_sync_time.insert(path.clone(), completed_time);
                        }
                    }
                    Err(e) => {
                        println!("  {} Sync failed: {}", "✗".red(), e);
                        // Mark failed (allow retry)
                        for path in &files {
                            in_flight.remove(path);
                        }
                    }
                }
            }

            last_batch_time = now;
        }
    }

    #[allow(unreachable_code)]
    Ok(())
}

/// Check if path should be ignored (in excluded directory)
fn should_ignore(path: &std::path::Path) -> bool {
    path.components().any(|c| {
        if let std::path::Component::Normal(name) = c {
            IGNORE_DIRS.contains(&name.to_str().unwrap_or(""))
        } else {
            false
        }
    })
}

/// Check if file has a supported extension
fn is_supported_file(path: &std::path::Path) -> bool {
    // Special case: Dockerfile
    if let Some(filename) = path.file_name().and_then(|n| n.to_str()) {
        if filename == "Dockerfile" || filename.starts_with("Dockerfile.") {
            return true;
        }
    }

    path.extension()
        .and_then(|ext| ext.to_str())
        .map(|ext| SUPPORTED_EXTENSIONS.contains(&ext))
        .unwrap_or(false)
}

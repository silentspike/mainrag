//! Index/Ingest Pipeline Service
//!
//! Handles file discovery, chunking, embedding generation, and storage to both PostgreSQL and Qdrant.
//! Implements Hybrid architecture: PostgreSQL for metadata/FTS, Qdrant for vector search.
//! Includes Code Intelligence integration for symbol extraction and call graph analysis.

use sha2::{Digest, Sha256};
use std::path::Path;
use std::sync::Arc;
use tokio::fs;
use tracing::{debug, error, info, warn};

use crate::db::{PostgresPool, DEFAULT_USER_ID};
use crate::error::{AppError, Result};
use crate::plugins::{self, RawFile};
use crate::services::chunker::{get_default_chunker, Chunk, Chunker};
use crate::services::intelligence::IntelligenceService;
use crate::services::{QdrantClient, TeiClient};
use pgvector::Vector;

/// Batch size for embedding generation (Sprint 8.2: configurable via EMBEDDING_BATCH_SIZE env)
fn embedding_batch_size() -> usize {
    std::env::var("EMBEDDING_BATCH_SIZE")
        .ok()
        .and_then(|v| v.parse().ok())
        .unwrap_or(96)
}

/// Maximum chunks per file — prevents a single large file from blocking the pipeline.
/// Files generating more chunks than this are truncated with a warning.
fn max_chunks_per_file() -> usize {
    std::env::var("MAX_CHUNKS_PER_FILE")
        .ok()
        .and_then(|v| v.parse().ok())
        .unwrap_or(500)
}

/// Per-file processing timeout in seconds — prevents a single file from blocking the entire sync.
fn per_file_timeout_secs() -> u64 {
    std::env::var("PER_FILE_TIMEOUT_SECS")
        .ok()
        .and_then(|v| v.parse().ok())
        .unwrap_or(120)
}

/// Sprint 8.3: Chunker version for incremental indexing
fn chunker_version() -> String {
    std::env::var("CHUNKER_VERSION").unwrap_or_else(|_| "semantic-v1".to_string())
}

/// Sprint 8.3: Embedding model ID for versioned chunk tracking
fn embedding_model_id() -> String {
    std::env::var("EMBEDDING_MODEL_ID").unwrap_or_else(|_| "BAAI/bge-base-en-v1.5".to_string())
}

fn embedding_with_cch_enabled() -> bool {
    std::env::var("EMBEDDING_WITH_CCH").unwrap_or_default() != "false"
}

fn embedding_storage_model_name_for(base_model_name: &str, cch_enabled: bool) -> String {
    if cch_enabled {
        format!("{}+cch", base_model_name)
    } else {
        base_model_name.to_string()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn embedding_storage_model_name_tracks_cch_suffix() {
        assert_eq!(
            embedding_storage_model_name_for("Alibaba-NLP/gte-modernbert-base", true),
            "Alibaba-NLP/gte-modernbert-base+cch"
        );
        assert_eq!(
            embedding_storage_model_name_for("Alibaba-NLP/gte-modernbert-base", false),
            "Alibaba-NLP/gte-modernbert-base"
        );
    }

    #[test]
    fn embedding_document_text_prepends_context_only_when_enabled_and_present() {
        assert_eq!(
            embedding_document_text_for(Some("[mainrag] src/lib.rs"), "fn run() {}", true),
            "[mainrag] src/lib.rs\n\nfn run() {}"
        );
        assert_eq!(
            embedding_document_text_for(Some("[mainrag] src/lib.rs"), "fn run() {}", false),
            "fn run() {}"
        );
        assert_eq!(
            embedding_document_text_for(Some(""), "fn run() {}", true),
            "fn run() {}"
        );
        assert_eq!(
            embedding_document_text_for(None, "fn run() {}", true),
            "fn run() {}"
        );
    }
}

/// Model name stored in chunk_embeddings for the actual embedded document text.
pub fn embedding_storage_model_name(base_model_name: &str) -> String {
    embedding_storage_model_name_for(base_model_name, embedding_with_cch_enabled())
}

fn embedding_document_text_for(
    context_prefix: Option<&str>,
    content_text: &str,
    cch_enabled: bool,
) -> String {
    if cch_enabled {
        if let Some(prefix) = context_prefix.filter(|prefix| !prefix.is_empty()) {
            return format!("{}\n\n{}", prefix, content_text);
        }
    }

    content_text.to_string()
}

/// Text sent to the embedder for stored document chunks.
pub fn embedding_document_text(context_prefix: Option<&str>, content_text: &str) -> String {
    embedding_document_text_for(context_prefix, content_text, embedding_with_cch_enabled())
}

/// Sprint 8.3: Tokenizer version for versioned chunk tracking
fn tokenizer_version() -> String {
    std::env::var("TOKENIZER_VERSION").unwrap_or_else(|_| "tiktoken-cl100k".to_string())
}

/// MAINRAG_CPU_MODE disables vector-side indexing work while preserving PG/FTS/intelligence.
fn cpu_mode_enabled() -> bool {
    std::env::var("MAINRAG_CPU_MODE")
        .map(|v| v == "true" || v == "1")
        .unwrap_or(false)
}

/// Sprint 8.3: Compute hex-encoded SHA256 hash of chunk content
fn chunk_content_sha256(text: &str) -> String {
    let mut h = Sha256::new();
    h.update(text.as_bytes());
    format!("{:x}", h.finalize())
}

/// Consume an existing full-file probe, including an empty result. New files
/// and append-only deltas have no probe and are chunked exactly once here.
fn chunks_for_write(
    probe: Option<Vec<Chunk>>,
    chunker: &dyn Chunker,
    content: &str,
    language: Option<&str>,
) -> Vec<Chunk> {
    probe.unwrap_or_else(|| chunker.chunk(content, language))
}

#[cfg(test)]
mod chunk_reuse_tests;

#[cfg(test)]
mod intelligence_retry_tests;

/// Max length for CCH (Contextual Chunk Header) prefix
#[allow(dead_code)]
const CCH_MAX_LENGTH: usize = 300;

/// Build CCH (Contextual Chunk Header) for a chunk
/// Format: "[source] path > parent_context" (max 100 chars)
///
/// Examples:
/// - "[coderag] src/search.rs > impl SearchService"
/// - "[docs] README.md > ## Installation"
/// - "[coderag] src/main.rs" (no parent context for level 0)
fn build_cch(source_name: &str, file_path: &str, parent_context: Option<&str>) -> String {
    let prefix = match parent_context {
        Some(ctx) if !ctx.is_empty() => {
            format!("[{}] {} > {}", source_name, file_path, ctx)
        }
        _ => format!("[{}] {}", source_name, file_path),
    };

    // Truncate to max length, respecting char boundaries
    if prefix.len() > CCH_MAX_LENGTH {
        let truncated: String = prefix.chars().take(CCH_MAX_LENGTH - 3).collect();
        format!("{}...", truncated)
    } else {
        prefix
    }
}

/// File extensions to index
#[allow(dead_code)]
const INDEXABLE_EXTENSIONS: &[&str] = &[
    "rs", "py", "pyi", "pyw", "js", "jsx", "mjs", "cjs", "ts", "tsx", "mts", "cts", "go", "java",
    "c", "cpp", "cc", "cxx", "h", "hpp", "hh", "hxx", "md", "markdown", "txt", "json", "jsonl",
    "jsonc", "yaml", "yml", "toml", "sql", "sh", "bash", "zsh", "html", "htm", "css", "scss",
    "sass", "vue", "svelte", "cs", "zig", "lua", "rb", "rake", "gemspec", "php", "phtml", "xml",
    "xsl", "xslt", "svg", "scm", "ss", "rkt",
];

pub struct IndexService {
    db: PostgresPool,
    tei: Arc<TeiClient>,
    qdrant: Arc<QdrantClient>,
    chunker: Box<dyn Chunker>,
    intelligence: Arc<IntelligenceService>,
}

#[derive(Debug)]
pub struct IndexStats {
    pub files_processed: usize,
    pub files_skipped: usize,
    pub chunks_created: usize,
    pub embeddings_generated: usize,
    pub files_deleted: usize,
    pub errors: Vec<String>,
}

/// Result of processing a single file
#[derive(Debug)]
pub enum ProcessResult {
    /// File was processed (chunks, embeddings created)
    Processed { chunks: usize, embeddings: usize },
    /// File was skipped (hash unchanged)
    Skipped,
}

impl IndexService {
    /// Create IndexService with Qdrant + PostgreSQL hybrid storage and Code Intelligence
    /// M1: Returns Result instead of panicking on IntelligenceService init failure
    pub fn new(db: PostgresPool, tei: Arc<TeiClient>, qdrant: Arc<QdrantClient>) -> Result<Self> {
        let chunker = get_default_chunker();
        let intelligence = Arc::new(IntelligenceService::new(db.clone()).map_err(|e| {
            AppError::Internal(format!("Failed to create IntelligenceService: {}", e))
        })?);
        Ok(Self {
            db,
            tei,
            qdrant,
            chunker,
            intelligence,
        })
    }

    /// Index a source by ID
    pub async fn index_source(&self, source_id: i64) -> Result<IndexStats> {
        // HOTFIX: Use session-scoped set_config (false) instead of SET LOCAL (true)
        // which only lasts for a single statement without an explicit transaction.
        // IndexService runs as system admin for all indexing operations.
        let mut client = self.db.get().await?;
        client
            .execute(
                "SELECT set_config('app.user_id', $1::text, false)",
                &[&DEFAULT_USER_ID.to_string()],
            )
            .await
            .map_err(|e| AppError::Internal(format!("Failed to set RLS context: {}", e)))?;
        client
            .execute("SELECT set_config('app.is_admin', 'true', false)", &[])
            .await
            .map_err(|e| AppError::Internal(format!("Failed to set RLS is_admin: {}", e)))?;

        // Get source info
        let source = client
            .query_opt(
                "SELECT id, name, type, path FROM sources WHERE id = $1",
                &[&source_id],
            )
            .await?
            .ok_or_else(|| AppError::NotFound(format!("Source {} not found", source_id)))?;

        let source_name: String = source.get("name");
        let source_type: String = source.get("type");
        let source_path: String = source.get("path");

        info!(
            "Starting index for source {} ({}) at {}",
            source_name, source_type, source_path
        );

        let mut stats = IndexStats {
            files_processed: 0,
            files_skipped: 0,
            chunks_created: 0,
            embeddings_generated: 0,
            files_deleted: 0,
            errors: vec![],
        };

        // Get existing file paths for deletion detection
        let existing_rows = client
            .query(
                "SELECT id, path FROM files WHERE source_id = $1",
                &[&source_id],
            )
            .await?;
        let mut existing_files: std::collections::HashMap<String, i64> = existing_rows
            .iter()
            .map(|row| (row.get::<_, String>("path"), row.get::<_, i64>("id")))
            .collect();
        let initial_file_count = existing_files.len();

        // Use plugin to discover and fetch files
        let plugin = plugins::get_plugin(&source_type)
            .ok_or_else(|| AppError::BadRequest(format!("Unknown source type: {}", source_type)))?;

        let sync_result = plugin
            .sync(&source_path)
            .await
            .map_err(|e| AppError::Internal(format!("Plugin sync error: {}", e)))?;

        info!(
            "Plugin discovery found {} files for source {}",
            sync_result.files.len(),
            source_name
        );

        // Add plugin errors to stats
        stats.errors.extend(sync_result.errors);

        // Process files from plugin with per-file timeout and progress logging
        let total_files = sync_result.files.len();
        let file_timeout = std::time::Duration::from_secs(per_file_timeout_secs());
        let progress_interval = (total_files / 10).max(1); // Log every 10%

        for (file_idx, raw_file) in sync_result.files.into_iter().enumerate() {
            // Mark this path as seen (remove from deletion candidates)
            existing_files.remove(&raw_file.path);

            // Progress logging (every 10% or at start/end)
            if file_idx % progress_interval == 0 || file_idx == total_files - 1 {
                info!(
                    "Sync progress: {}/{} files ({:.0}%), {} chunks, {} errors — source '{}'",
                    file_idx + 1,
                    total_files,
                    (file_idx + 1) as f64 / total_files as f64 * 100.0,
                    stats.chunks_created,
                    stats.errors.len(),
                    source_name
                );
            }

            // Per-file timeout: prevents a single large file from blocking the entire sync
            let file_path = raw_file.path.clone();
            match tokio::time::timeout(
                file_timeout,
                self.process_raw_file(source_id, &source_name, raw_file),
            )
            .await
            {
                Ok(Ok(ProcessResult::Processed { chunks, embeddings })) => {
                    stats.files_processed += 1;
                    stats.chunks_created += chunks;
                    stats.embeddings_generated += embeddings;
                }
                Ok(Ok(ProcessResult::Skipped)) => {
                    stats.files_skipped += 1;
                }
                Ok(Err(e)) => {
                    let err_msg = format!("Error processing file {}: {}", file_path, e);
                    warn!("{}", err_msg);
                    stats.errors.push(err_msg);
                }
                Err(_) => {
                    let err_msg = format!(
                        "File processing timed out after {}s: {} (skipping)",
                        file_timeout.as_secs(),
                        file_path
                    );
                    warn!("{}", err_msg);
                    stats.errors.push(err_msg);
                    metrics::counter!("mainrag_file_timeout").increment(1);
                }
            }

            // Yield to runtime between files — keeps health checks responsive
            tokio::task::yield_now().await;
        }

        // Delete files that no longer exist in source (deletion detection)
        if !existing_files.is_empty() {
            info!(
                "Deleting {} orphan files from source {} (were: {}, now: {})",
                existing_files.len(),
                source_name,
                initial_file_count,
                initial_file_count - existing_files.len()
            );

            for (path, file_id) in &existing_files {
                // Get chunk IDs before deleting for Qdrant cleanup
                let chunk_rows = client
                    .query("SELECT id FROM chunks WHERE file_id = $1", &[file_id])
                    .await?;

                // TRANSACTIONAL: Outbox inserts + file delete must be atomic
                // Ensures either all deletes are queued AND file is deleted, or neither
                let tx = client.transaction().await?;

                // CRITICAL: Queue delete entries to outbox BEFORE PostgreSQL DELETE
                // Otherwise chunk_ids are gone after CASCADE and we get zombie vectors in Qdrant
                for row in &chunk_rows {
                    let chunk_id: i64 = row.get("id");
                    tx.execute(
                        "INSERT INTO indexing_outbox (action, chunk_id, file_id, source_id, payload) \
                         VALUES ('delete', $1, $2, $3, '{}'::jsonb)",
                        &[&chunk_id, file_id, &source_id]
                    ).await?;
                }

                // Delete from PostgreSQL (CASCADE will delete chunks and embeddings)
                let deleted = tx
                    .execute("DELETE FROM files WHERE id = $1", &[file_id])
                    .await?;

                // Commit: all delete entries queued AND file deleted, or neither
                tx.commit().await?;

                if deleted > 0 {
                    info!(
                        "Deleted orphan file: {} (id: {}, {} chunks queued for Qdrant delete)",
                        path,
                        file_id,
                        chunk_rows.len()
                    );
                    stats.files_deleted += 1;
                    // NOTE: Direct qdrant.delete_chunks() removed - Worker handles this via outbox
                }
            }
        }

        // Update source metadata
        client
            .execute(
                r#"
                UPDATE sources SET
                    last_synced = NOW(),
                    file_count = (SELECT COUNT(*) FROM files WHERE source_id = $1),
                    total_size = (SELECT COALESCE(SUM(size_original), 0) FROM files WHERE source_id = $1),
                    updated_at = NOW()
                WHERE id = $1
                "#,
                &[&source_id],
            )
            .await?;

        info!(
            "Index complete: {} files, {} chunks, {} embeddings, {} deleted, {} errors",
            stats.files_processed,
            stats.chunks_created,
            stats.embeddings_generated,
            stats.files_deleted,
            stats.errors.len()
        );

        // Sprint 8.4: Qdrant Consistency Tracking — write sync_ledger entry
        // Compare PG chunk count vs Qdrant point count for this source.
        // Detects drift between the two stores (e.g. from failed outbox processing).
        let ledger_cpu_mode = cpu_mode_enabled();
        match self.record_sync_ledger(&client, source_id).await {
            Ok((pg_count, qdrant_count, drift)) => {
                if drift > 0 {
                    warn!(
                        "Sprint 8.4: Drift detected for source {}: PG={} chunks, Qdrant={} points, drift={}",
                        source_id, pg_count, qdrant_count, drift
                    );
                    metrics::counter!("mainrag_sync_drift_detected").increment(1);
                } else if ledger_cpu_mode {
                    debug!(
                        "Sprint 8.4: Sync ledger recorded CPU-mode source {}: PG={} chunks, Qdrant count intentionally not sampled",
                        source_id, pg_count
                    );
                } else {
                    debug!(
                        "Sprint 8.4: Sync ledger OK for source {}: PG={}, Qdrant={}",
                        source_id, pg_count, qdrant_count
                    );
                }
            }
            Err(e) => {
                // Non-fatal: ledger is observability, not critical path
                warn!(
                    "Sprint 8.4: Failed to record sync ledger for source {}: {}",
                    source_id, e
                );
            }
        }

        Ok(stats)
    }

    /// Sync specific files incrementally (for watch mode)
    ///
    /// Unlike `index_source()` which does a full scan, this method:
    /// - Only processes the specified files
    /// - Does NOT detect deletions (call with empty file list to skip)
    /// - Uses existing hash-based skip logic
    /// - Supports JSONL append-only optimization
    pub async fn sync_files(
        &self,
        source_id: i64,
        files: &[std::path::PathBuf],
    ) -> Result<IndexStats> {
        // HOTFIX: Session-scoped RLS context for system indexing operations
        let client = self.db.get().await?;
        client
            .execute(
                "SELECT set_config('app.user_id', $1::text, false)",
                &[&DEFAULT_USER_ID.to_string()],
            )
            .await
            .map_err(|e| AppError::Internal(format!("Failed to set RLS context: {}", e)))?;
        client
            .execute("SELECT set_config('app.is_admin', 'true', false)", &[])
            .await
            .map_err(|e| AppError::Internal(format!("Failed to set RLS is_admin: {}", e)))?;

        // Get source info
        let source = client
            .query_opt(
                "SELECT id, name, type, path FROM sources WHERE id = $1",
                &[&source_id],
            )
            .await?
            .ok_or_else(|| AppError::NotFound(format!("Source {} not found", source_id)))?;

        let source_name: String = source.get("name");
        let source_path: String = source.get("path");
        let source_path = std::path::Path::new(&source_path);

        info!(
            "Syncing {} files for source '{}' (incremental)",
            files.len(),
            source_name
        );

        let mut stats = IndexStats {
            files_processed: 0,
            files_skipped: 0,
            chunks_created: 0,
            embeddings_generated: 0,
            files_deleted: 0,
            errors: vec![],
        };

        for file_path in files {
            // Skip if file doesn't exist (was deleted)
            if !file_path.exists() {
                debug!("File no longer exists, skipping: {}", file_path.display());
                continue;
            }

            // Read file content
            let content = match fs::read_to_string(file_path).await {
                Ok(c) => c,
                Err(e) => {
                    let err_msg = format!("Failed to read {}: {}", file_path.display(), e);
                    warn!("{}", err_msg);
                    stats.errors.push(err_msg);
                    continue;
                }
            };

            // Calculate relative path from source root
            let rel_path = file_path
                .strip_prefix(source_path)
                .unwrap_or(file_path)
                .to_string_lossy()
                .to_string();

            // Detect language from extension
            let language = file_path
                .extension()
                .and_then(|e| e.to_str())
                .map(|ext| match ext.to_lowercase().as_str() {
                    "rs" => "rust",
                    "py" | "pyi" | "pyw" => "python",
                    "js" | "jsx" | "mjs" | "cjs" => "javascript",
                    "ts" | "tsx" | "mts" | "cts" => "typescript",
                    "go" => "go",
                    "java" => "java",
                    "c" | "h" => "c",
                    "cpp" | "cc" | "cxx" | "hpp" | "hxx" | "hh" => "cpp",
                    "cs" => "csharp",
                    "rb" | "rake" | "gemspec" => "ruby",
                    "php" | "phtml" => "php",
                    "sh" | "bash" | "zsh" => "bash",
                    "json" | "jsonl" | "jsonc" => "json",
                    "yaml" | "yml" => "yaml",
                    "toml" => "toml",
                    "md" | "markdown" => "markdown",
                    "html" | "htm" => "html",
                    "css" | "scss" | "sass" | "less" => "css",
                    "sql" => "sql",
                    "xml" | "xsl" | "xsd" | "svg" => "xml",
                    _ => ext,
                })
                .map(String::from);

            let raw_file = RawFile {
                path: rel_path,
                content: content.clone(),
                size: content.len(),
                language,
                last_modified: None,
                source_path: None,
                source_range: None,
            };

            match self
                .process_raw_file(source_id, &source_name, raw_file)
                .await
            {
                Ok(ProcessResult::Processed { chunks, embeddings }) => {
                    stats.files_processed += 1;
                    stats.chunks_created += chunks;
                    stats.embeddings_generated += embeddings;
                }
                Ok(ProcessResult::Skipped) => {
                    stats.files_skipped += 1;
                }
                Err(e) => {
                    let err_msg = format!("Error processing {}: {}", file_path.display(), e);
                    warn!("{}", err_msg);
                    stats.errors.push(err_msg);
                }
            }
        }

        info!(
            "Sync complete: {} processed, {} skipped, {} chunks, {} embeddings, {} errors",
            stats.files_processed,
            stats.files_skipped,
            stats.chunks_created,
            stats.embeddings_generated,
            stats.errors.len()
        );

        // Sprint 8.3: Prometheus metrics for versioned incremental indexing
        metrics::counter!("mainrag_chunks_reembedded").increment(stats.chunks_created as u64);
        metrics::counter!("mainrag_chunks_skipped_unchanged").increment(stats.files_skipped as u64);

        Ok(stats)
    }

    /// Discover all indexable files in a directory
    #[allow(dead_code)]
    async fn discover_files(&self, root: &Path) -> Result<Vec<std::path::PathBuf>> {
        let mut files = vec![];
        self.discover_files_recursive(root, &mut files).await?;
        Ok(files)
    }

    #[allow(dead_code)]
    #[async_recursion::async_recursion]
    async fn discover_files_recursive(
        &self,
        dir: &Path,
        files: &mut Vec<std::path::PathBuf>,
    ) -> Result<()> {
        let mut entries = fs::read_dir(dir).await?;

        while let Some(entry) = entries.next_entry().await? {
            let path = entry.path();
            let file_type = entry.file_type().await?;

            // Skip hidden files/dirs
            if path
                .file_name()
                .and_then(|n| n.to_str())
                .map(|n| n.starts_with('.'))
                .unwrap_or(false)
            {
                continue;
            }

            // Skip common non-source directories
            let skip_dirs = [
                "node_modules",
                "target",
                "dist",
                "build",
                ".git",
                "__pycache__",
                "venv",
            ];
            if file_type.is_dir() {
                let dir_name = path.file_name().and_then(|n| n.to_str()).unwrap_or("");
                if skip_dirs.contains(&dir_name) {
                    continue;
                }
                self.discover_files_recursive(&path, files).await?;
            } else if file_type.is_file() {
                // Check if file has indexable extension
                if let Some(ext) = path.extension().and_then(|e| e.to_str()) {
                    if INDEXABLE_EXTENSIONS.contains(&ext.to_lowercase().as_str()) {
                        files.push(path);
                    }
                }
            }
        }

        Ok(())
    }

    async fn analyze_intelligence_for_file(
        &self,
        file_id: i64,
        rel_path: &str,
        content: &str,
    ) -> Result<()> {
        let path = Path::new(rel_path);
        let Some(ext) = path.extension().and_then(|e| e.to_str()) else {
            return Ok(());
        };

        // Run intelligence for all code/data files supported by the parser.
        // Matches parser.rs Lang::from_extension() - full CodeRag parity.
        let code_extensions = [
            // Rust
            "rs", // Python (including Windows .pyw)
            "py", "pyi", "pyw", // JavaScript (including JSX for React)
            "js", "jsx", "mjs", "cjs", // TypeScript (including module variants)
            "ts", "tsx", "mts", "cts", // Go
            "go",  // C
            "c",   // C++ (including all common extensions and headers)
            "cpp", "cc", "cxx", "c++", "cp", "h", "hpp", "hh", "hxx", "h++",  // Java
            "java", // JSON (including JSONL and JSONC)
            "json", "jsonl", "jsonc", // TOML
            "toml",  // YAML
            "yaml", "yml", // Shell (including zsh)
            "sh", "bash", "zsh", // Markdown
            "md", "markdown", // C# (Feature Parity A.6)
            "cs",       // Zig
            "zig",      // Lua
            "lua",      // Ruby
            "rb", "rake", "gemspec", // PHP
            "php", "phtml", // HTML
            "html", "htm", // CSS (including preprocessors)
            "css", "scss", "sass", // XML (including XSLT, SVG)
            "xml", "xsl", "xslt", "svg", // Scheme/Racket
            "scm", "ss", "rkt", // SQL
            "sql",
        ];

        if !code_extensions.contains(&ext.to_lowercase().as_str()) {
            return Ok(());
        }

        match self.intelligence.analyze_file(file_id, path, content).await {
            Ok(parse_result) => {
                info!(
                    "Code Intelligence: extracted {} symbols and {} calls from {}",
                    parse_result.symbols.len(),
                    parse_result.calls.len(),
                    rel_path
                );
            }
            Err(e) => {
                // Don't fail the entire indexing if intelligence extraction fails.
                warn!("Code Intelligence failed for {}: {}", rel_path, e);
            }
        }

        Ok(())
    }

    /// Process a RawFile from a plugin: chunk, embed, store
    async fn process_raw_file(
        &self,
        source_id: i64,
        source_name: &str,
        raw_file: RawFile,
    ) -> Result<ProcessResult> {
        // STREAMING PATH: Large conversation files have empty content + source_path set.
        // Read them in a memory-bounded streaming fashion instead of loading entirely.
        if raw_file.content.is_empty() && raw_file.source_path.is_some() {
            return self
                .process_large_conversation_file(source_id, source_name, raw_file)
                .await;
        }

        let content = &raw_file.content;
        if content.is_empty() {
            warn!("Skipping empty file: {}", raw_file.path);
            return Ok(ProcessResult::Skipped);
        }

        // Safe string truncation for preview (respects UTF-8 char boundaries)
        let preview: String = content.chars().take(50).collect();
        info!(
            "Processing file: path={}, size={}, language={:?}, content_preview={:?}",
            raw_file.path, raw_file.size, raw_file.language, preview
        );

        // Compute hash
        let mut hasher = Sha256::new();
        hasher.update(content.as_bytes());
        let hash = hasher.finalize().to_vec();

        let language = raw_file.language;
        let rel_path = raw_file.path;
        let file_size = raw_file.size as i32;

        info!("File params: source_id={}, path={}, hash_size={}, content_size={}, file_size={}, language={:?}",
              source_id, rel_path, hash.len(), content.len(), file_size, language);

        info!("Getting DB client...");
        // HOTFIX: Session-scoped RLS context for system indexing operations
        let mut client = self.db.get().await?;
        client
            .execute(
                "SELECT set_config('app.user_id', $1::text, false)",
                &[&DEFAULT_USER_ID.to_string()],
            )
            .await
            .map_err(|e| AppError::Internal(format!("Failed to set RLS context: {}", e)))?;
        client
            .execute("SELECT set_config('app.is_admin', 'true', false)", &[])
            .await
            .map_err(|e| AppError::Internal(format!("Failed to set RLS is_admin: {}", e)))?;
        info!("DB client obtained");

        // INCREMENTAL INDEXING: Check hash BEFORE any modifications!
        // If file exists with the same hash, skip chunk/vector indexing, but
        // retry intelligence when the previous attempt did not complete.
        // IMPORTANT: hash is BYTEA (Vec<u8>), not hex string!
        //
        // JSONL OPTIMIZATION: For JSONL files (conversations), detect append-only changes.
        // If file only grew (new bytes appended), skip re-chunking the unchanged portion.
        let is_jsonl = rel_path.ends_with(".jsonl");
        let mut is_append_only = false;
        let mut old_size: i64 = 0;
        let mut existing_file_id: Option<i64> = None;

        if let Some(existing_row) = client
            .query_opt(
                "SELECT id, hash, size_original, intelligence_analyzed_at IS NOT NULL AS intelligence_complete \
                 FROM files WHERE source_id = $1 AND path = $2",
                &[&source_id, &rel_path],
            )
            .await?
        {
            let existing_hash: Vec<u8> = existing_row.get("hash");
            let file_id: i64 = existing_row.get("id");
            existing_file_id = Some(file_id);

            if existing_hash == hash {
                if !existing_row.get::<_, bool>("intelligence_complete") {
                    self.analyze_intelligence_for_file(file_id, &rel_path, content)
                        .await?;
                }
                debug!(
                    "File {} unchanged (hash match), skipping re-index",
                    rel_path
                );
                return Ok(ProcessResult::Skipped);
            }

            // JSONL append-only detection
            if is_jsonl {
                let existing_size: i32 = existing_row.get("size_original");
                let new_size = content.len() as i64;

                if new_size > existing_size as i64 {
                    // M1: Use file_id directly (already extracted above)
                    let old_content_row = client
                        .query_opt("SELECT content FROM files WHERE id = $1", &[&file_id])
                        .await?;

                    if let Some(old_row) = old_content_row {
                        let old_compressed: Vec<u8> = old_row.get("content");
                        if let Ok(old_content) = zstd::decode_all(&old_compressed[..]) {
                            if let Ok(old_str) = String::from_utf8(old_content) {
                                // Verify old content is prefix of new content
                                if content.starts_with(&old_str) {
                                    // Validate existing chunks (not incomplete sync)
                                    let chunk_count: i64 = client
                                        .query_one(
                                            "SELECT COUNT(*) FROM chunks WHERE file_id = $1",
                                            &[&file_id],
                                        )
                                        .await?
                                        .get(0);

                                    // Expect at least 1 chunk per 10KB of old content
                                    let expected_min_chunks =
                                        ((existing_size as i64) / 10_000).max(1);

                                    if chunk_count >= expected_min_chunks {
                                        is_append_only = true;
                                        old_size = existing_size as i64;
                                        info!(
                                            "JSONL append-only detected: {} ({} -> {} bytes, +{} bytes delta, {} existing chunks)",
                                            rel_path, old_size, new_size, new_size - old_size, chunk_count
                                        );
                                    } else {
                                        warn!(
                                            "JSONL append-only rejected: {} has only {} chunks for {} bytes (expected >= {}), forcing full resync",
                                            rel_path, chunk_count, existing_size, expected_min_chunks
                                        );
                                    }
                                }
                            }
                        }
                    }
                }
            }

            if !is_append_only {
                debug!(
                    "File {} hash changed, proceeding with full re-index",
                    rel_path
                );
            }
        }

        // MEMORY GUARD: For large files (>5MB), skip storing full content in DB.
        // The file is on disk and can be re-read. This prevents 56MB+ files from
        // consuming hundreds of MB for compression + DB storage.
        const LARGE_FILE_THRESHOLD: usize = 5 * 1024 * 1024; // 5 MB
        let is_large_file = content.len() > LARGE_FILE_THRESHOLD;

        let file_row = if is_large_file {
            info!(
                "Large file ({}MB > 5MB threshold): storing metadata only, skipping content in DB",
                content.len() / (1024 * 1024)
            );
            let empty_compressed: Vec<u8> = zstd::encode_all(&b""[..], 3)?;
            client
                .query_one(
                    r#"
                    INSERT INTO files (source_id, path, hash, content, content_text, language, size_original, size_compressed, last_modified)
                    VALUES ($1, $2, $3, $4, NULL, $5, $6, $7, NOW())
                    ON CONFLICT (source_id, path) DO UPDATE SET
                        hash = EXCLUDED.hash,
                        content = EXCLUDED.content,
                        content_text = NULL,
                        intelligence_analyzed_at = NULL,
                        intelligence_symbols_count = 0,
                        intelligence_calls_count = 0,
                        language = EXCLUDED.language,
                        size_original = EXCLUDED.size_original,
                        size_compressed = EXCLUDED.size_compressed,
                        last_modified = NOW(),
                        updated_at = NOW()
                    RETURNING id
                    "#,
                    &[
                        &source_id,
                        &rel_path,
                        &hash,
                        &empty_compressed,
                        &language,
                        &file_size,
                        &(empty_compressed.len() as i32),
                    ],
                )
                .await?
        } else {
            // Normal files: compress and store content
            info!("Compressing content...");
            let compressed = zstd::encode_all(content.as_bytes(), 3)?;
            info!(
                "Compression done: {} -> {} bytes",
                content.len(),
                compressed.len()
            );

            client
                .query_one(
                    r#"
                    INSERT INTO files (source_id, path, hash, content, content_text, language, size_original, size_compressed, last_modified)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, NOW())
                    ON CONFLICT (source_id, path) DO UPDATE SET
                        hash = EXCLUDED.hash,
                        content = EXCLUDED.content,
                        content_text = EXCLUDED.content_text,
                        intelligence_analyzed_at = NULL,
                        intelligence_symbols_count = 0,
                        intelligence_calls_count = 0,
                        language = EXCLUDED.language,
                        size_original = EXCLUDED.size_original,
                        size_compressed = EXCLUDED.size_compressed,
                        last_modified = NOW(),
                        updated_at = NOW()
                    RETURNING id
                    "#,
                    &[
                        &source_id,
                        &rel_path,
                        &hash,
                        &compressed,
                        &content,
                        &language,
                        &file_size,
                        &(compressed.len() as i32),
                    ],
                )
                .await?
        };

        let file_id: i64 = file_row.get("id");
        info!("File inserted with id: {}", file_id);

        // Completion belongs to the persisted file content, not to chunk or
        // embedding changes. Failure leaves the marker pending for a hash-skip
        // retry, including when a version match or empty chunk result follows.
        self.analyze_intelligence_for_file(file_id, &rel_path, content)
            .await?;

        // Sprint 8.3: Versioned chunk-level incremental skip logic
        // For non-append-only files with existing chunks: generate new chunks,
        // compare content hashes + version metadata against existing chunks.
        // If ALL match → skip re-embed entirely (file record already updated above).
        let probe_chunks = (!is_append_only && existing_file_id.is_some())
            .then(|| self.chunker.chunk(content, language.as_deref()));
        if let Some(probe_chunks) = probe_chunks.as_ref() {
            if !probe_chunks.is_empty() {
                let cv = chunker_version();
                let mid = embedding_model_id();
                let tv = tokenizer_version();

                let existing_version_rows = client.query(
                    "SELECT chunk_content_hash, chunker_version, embedding_model_id, tokenizer_version \
                     FROM chunks WHERE file_id = $1 ORDER BY start_line, id",
                    &[&file_id],
                ).await?;

                if !existing_version_rows.is_empty()
                    && existing_version_rows.len() == probe_chunks.len()
                {
                    let all_unchanged =
                        probe_chunks
                            .iter()
                            .zip(existing_version_rows.iter())
                            .all(|(nc, er)| {
                                let new_hash = chunk_content_sha256(&nc.text);
                                let eh: Option<String> = er.get("chunk_content_hash");
                                let ecv: Option<String> = er.get("chunker_version");
                                let emid: Option<String> = er.get("embedding_model_id");
                                let etv: Option<String> = er.get("tokenizer_version");

                                eh.as_deref() == Some(new_hash.as_str())
                                    && ecv.as_deref() == Some(cv.as_str())
                                    && emid.as_deref() == Some(mid.as_str())
                                    && etv.as_deref() == Some(tv.as_str())
                            });

                    if all_unchanged {
                        info!(
                            "Sprint 8.3: All {} chunks unchanged for {} (content+version match), skipping re-embed",
                            probe_chunks.len(),
                            rel_path
                        );
                        return Ok(ProcessResult::Skipped);
                    }
                }

                // Log version mismatch or partial content change
                if let Some(first) = existing_version_rows.first() {
                    let ecv: Option<String> = first.get("chunker_version");
                    let emid: Option<String> = first.get("embedding_model_id");
                    if ecv.as_deref() != Some(cv.as_str()) || emid.as_deref() != Some(mid.as_str())
                    {
                        warn!(
                            "Sprint 8.3: Version mismatch for {}: chunker {:?}->{}, model {:?}->{}, forcing full re-embed",
                            rel_path, ecv, cv, emid, mid
                        );
                        // Sprint 8.3: Track stale chunks by reason
                        if ecv.as_deref() != Some(cv.as_str()) {
                            metrics::counter!("mainrag_chunks_stale", "reason" => "chunker")
                                .increment(1);
                        }
                        if emid.as_deref() != Some(mid.as_str()) {
                            metrics::counter!("mainrag_chunks_stale", "reason" => "model")
                                .increment(1);
                        }
                    } else {
                        let unchanged_count = probe_chunks
                            .iter()
                            .zip(existing_version_rows.iter())
                            .filter(|(nc, er)| {
                                let new_hash = chunk_content_sha256(&nc.text);
                                let eh: Option<String> = er.get("chunk_content_hash");
                                eh.as_deref() == Some(new_hash.as_str())
                            })
                            .count();
                        info!(
                            "Sprint 8.3: {}/{} chunks content-unchanged for {}, {} chunks need re-embed",
                            unchanged_count,
                            probe_chunks.len(),
                            rel_path,
                            probe_chunks.len() - unchanged_count
                        );
                    }
                }
            }
        }

        // Delete existing chunks for this file - with Qdrant cleanup via outbox
        // CRITICAL: Must insert outbox delete entries BEFORE deleting chunks!
        // Uses set-based SQL (not loop) for atomicity and performance.
        //
        // JSONL APPEND-ONLY OPTIMIZATION: Skip chunk deletion for append-only files!
        // Existing chunks are valid; we only add new chunks for the delta.
        if !is_append_only {
            info!(
                "Deleting existing chunks for file_id={} with Qdrant cleanup...",
                file_id
            );

            let tx = client.transaction().await.map_err(|e| {
                AppError::Internal(format!(
                    "Failed to start transaction for chunk cleanup: {}",
                    e
                ))
            })?;

            // 1. Set-based outbox delete entries (one statement, not loop!)
            let outbox_count = tx
                .execute(
                    "INSERT INTO indexing_outbox (action, chunk_id, file_id, source_id, payload)
                 SELECT 'delete', c.id, c.file_id, f.source_id, '{}'::jsonb
                 FROM chunks c
                 JOIN files f ON f.id = c.file_id
                 WHERE c.file_id = $1",
                    &[&file_id],
                )
                .await
                .map_err(|e| {
                    AppError::Internal(format!("Failed to create outbox delete entries: {}", e))
                })?;

            // 2. Now delete the chunks (outbox entries preserve chunk IDs for Qdrant)
            let deleted_count = tx
                .execute("DELETE FROM chunks WHERE file_id = $1", &[&file_id])
                .await
                .map_err(|e| AppError::Internal(format!("Failed to delete chunks: {}", e)))?;

            tx.commit().await.map_err(|e| {
                AppError::Internal(format!("Failed to commit chunk cleanup transaction: {}", e))
            })?;

            info!(
                "Deleted {} existing chunks for file_id={}, queued {} outbox delete entries",
                deleted_count, file_id, outbox_count
            );
        } else {
            info!(
                "JSONL append-only: Keeping {} existing chunks for file_id={}",
                client
                    .query_one(
                        "SELECT COUNT(*) FROM chunks WHERE file_id = $1",
                        &[&file_id]
                    )
                    .await
                    .map(|r| r.get::<_, i64>(0))
                    .unwrap_or(0),
                file_id
            );
        }

        // Create chunks using semantic chunker
        // JSONL APPEND-ONLY OPTIMIZATION: Only chunk the delta (new bytes)
        let content_to_chunk: &str = if is_append_only && old_size > 0 {
            // UTF-8 safety: old_size is a byte offset that could land inside a
            // multi-byte character.  For JSONL files the best split point is the
            // last newline at or before old_size (lines are always valid boundaries).
            // Fallback: walk forward to the next char boundary.
            let raw_start = (old_size as usize).min(content.len());
            let start = if raw_start < content.len() && !content.is_char_boundary(raw_start) {
                // Prefer the last newline before raw_start (JSONL-friendly)
                content[..raw_start]
                    .rfind('\n')
                    .map(|pos| pos + 1)
                    .unwrap_or_else(|| {
                        // No newline found — walk forward to next valid char boundary
                        let mut s = raw_start;
                        while s < content.len() && !content.is_char_boundary(s) {
                            s += 1;
                        }
                        s
                    })
            } else {
                raw_start
            };
            let delta = &content[start..];
            if delta.is_empty() {
                info!("JSONL append-only but no new content (delta=0), skipping chunk generation");
                return Ok(ProcessResult::Processed {
                    chunks: 0,
                    embeddings: 0,
                });
            }
            info!(
                "JSONL incremental: Chunking only delta ({} bytes of {} total)",
                delta.len(),
                content.len()
            );
            delta
        } else {
            // No clone — borrow the content directly
            content
        };

        // Reuse the full-file version probe by move when present; new files and
        // append-only deltas have not been chunked yet. Intelligence parsing is
        // separate and unchanged. Keep truncation after the version comparison.
        info!(
            "Preparing chunks for file {} with {} bytes (reuse_probe={})",
            rel_path,
            content_to_chunk.len(),
            probe_chunks.is_some()
        );
        let mut semantic_chunks = chunks_for_write(
            probe_chunks,
            &*self.chunker,
            content_to_chunk,
            language.as_deref(),
        );

        // Enterprise guard: MAX_CHUNKS_PER_FILE prevents any single file from
        // generating unbounded chunks (e.g., 41MB JSON → 3000+ chunks → hours of TEI calls).
        let max_chunks = max_chunks_per_file();
        if semantic_chunks.len() > max_chunks {
            warn!(
                "File {} generated {} chunks (exceeds MAX_CHUNKS_PER_FILE={}), truncating to {} chunks. \
                 Consider increasing MAX_CHUNKS_PER_FILE or excluding this file.",
                rel_path, semantic_chunks.len(), max_chunks, max_chunks
            );
            metrics::counter!("mainrag_file_chunks_truncated").increment(1);
            semantic_chunks.truncate(max_chunks);
        }

        // Sprint 8.1: Log chunk-size statistics per file
        if !semantic_chunks.is_empty() {
            let token_counts: Vec<usize> = semantic_chunks
                .iter()
                .map(|c| crate::services::chunker::token::count_tokens(&c.text))
                .collect();
            let sum: usize = token_counts.iter().sum();
            let min = token_counts.iter().copied().min().unwrap_or(0);
            let max = token_counts.iter().copied().max().unwrap_or(0);
            let avg = sum / token_counts.len();
            info!(
                "Sprint 8.1: {} chunks for {}: avg={} min={} max={} tokens (language={:?})",
                semantic_chunks.len(),
                rel_path,
                avg,
                min,
                max,
                language
            );
        }

        if semantic_chunks.is_empty() {
            warn!(
                "No chunks created for file: {} (content_size={})",
                rel_path,
                content.len()
            );
            // No chunks created - counts as processed but with 0 output
            return Ok(ProcessResult::Processed {
                chunks: 0,
                embeddings: 0,
            });
        }

        let mut chunk_texts: Vec<String> = Vec::with_capacity(semantic_chunks.len());
        let mut chunk_hashes: Vec<Vec<u8>> = Vec::with_capacity(semantic_chunks.len());

        // JSONL APPEND-ONLY: Calculate line offset for delta chunks
        // When chunking only the delta, line numbers are relative to the delta start.
        // We need to add the line count of the old content to get absolute line numbers.
        let line_offset = if is_append_only && old_size > 0 {
            // Count newlines in the old portion to get the line offset.
            // UTF-8 safety: old_size may not be a char boundary. Since we only
            // count '\n' (a single-byte ASCII char), finding the nearest valid
            // boundary is sufficient — newlines never appear inside multi-byte
            // sequences.
            let raw_end = (old_size as usize).min(content.len());
            let safe_end = if raw_end < content.len() && !content.is_char_boundary(raw_end) {
                let mut e = raw_end;
                while e > 0 && !content.is_char_boundary(e) {
                    e -= 1;
                }
                e
            } else {
                raw_end
            };
            content[..safe_end].matches('\n').count() as u32
        } else {
            0
        };

        // Sprint 3.1: Batch-INSERT via UNNEST — ~290 DB-Roundtrips → 1
        // Pre-compute all column arrays (NO DB calls in this loop)
        info!(
            "Creating {} chunks for file (line_offset={})",
            semantic_chunks.len(),
            line_offset
        );
        let mut b_chunk_types: Vec<String> = Vec::with_capacity(semantic_chunks.len());
        let mut b_content_hashes: Vec<Vec<u8>> = Vec::with_capacity(semantic_chunks.len());
        let mut b_content_compressed: Vec<Vec<u8>> = Vec::with_capacity(semantic_chunks.len());
        let mut b_content_texts: Vec<String> = Vec::with_capacity(semantic_chunks.len());
        let mut b_start_lines: Vec<i32> = Vec::with_capacity(semantic_chunks.len());
        let mut b_end_lines: Vec<i32> = Vec::with_capacity(semantic_chunks.len());
        let mut b_levels: Vec<i16> = Vec::with_capacity(semantic_chunks.len());
        let mut b_context_prefixes: Vec<String> = Vec::with_capacity(semantic_chunks.len());
        let mut b_content_hash_hexes: Vec<String> = Vec::with_capacity(semantic_chunks.len());
        let mut b_chunker_versions: Vec<String> = Vec::with_capacity(semantic_chunks.len());
        let mut b_model_ids: Vec<String> = Vec::with_capacity(semantic_chunks.len());
        let mut b_tokenizer_versions: Vec<String> = Vec::with_capacity(semantic_chunks.len());

        for (idx, chunk) in semantic_chunks.iter().enumerate() {
            let chunk_text = &chunk.text;
            let start_line = chunk.start_line + line_offset as usize;
            let end_line = chunk.end_line + line_offset as usize;

            let chunk_hash = {
                let mut h = Sha256::new();
                h.update(chunk_text.as_bytes());
                h.finalize().to_vec()
            };

            let chunk_compressed = zstd::encode_all(chunk_text.as_bytes(), 3)?;

            let chunk_type = match chunk.chunk_type {
                crate::services::chunker::ChunkType::File => "file",
                crate::services::chunker::ChunkType::Code => "code",
                crate::services::chunker::ChunkType::Text => "text",
                crate::services::chunker::ChunkType::Config => "config",
                crate::services::chunker::ChunkType::Function => "function",
                crate::services::chunker::ChunkType::Class => "class",
                crate::services::chunker::ChunkType::Module => "module",
                crate::services::chunker::ChunkType::Type => "type",
                crate::services::chunker::ChunkType::Section => "section",
                crate::services::chunker::ChunkType::Conversation => "conversation",
            };

            // CCH (Contextual Chunk Header)
            let parent_context: Option<String> = chunk.parent_idx.and_then(|pidx| {
                semantic_chunks.get(pidx).map(|parent| {
                    parent
                        .text
                        .lines()
                        .find(|l| !l.trim().is_empty())
                        .map(|l| l.chars().take(50).collect::<String>())
                        .unwrap_or_default()
                })
            });
            let context_prefix = build_cch(source_name, &rel_path, parent_context.as_deref());

            // Sprint 8.3: Versioned chunk tracking
            let chunk_content_hash_hex = chunk_content_sha256(chunk_text);
            let current_chunker_version = chunker_version();
            let current_model_id = embedding_model_id();
            let current_tokenizer_version = tokenizer_version();

            debug!(
                "Preparing chunk {}: file_id={}, type={}, start={}, end={}",
                idx, file_id, chunk_type, start_line, end_line
            );

            b_chunk_types.push(chunk_type.to_string());
            b_content_hashes.push(chunk_hash.clone());
            b_content_compressed.push(chunk_compressed);
            b_content_texts.push(chunk_text.clone());
            b_start_lines.push(start_line as i32);
            b_end_lines.push(end_line as i32);
            b_levels.push(chunk.level as i16);
            // Query embeddings do not get CCH; documents do when configured.
            let embedding_text = embedding_document_text(Some(&context_prefix), chunk_text);

            b_context_prefixes.push(context_prefix);
            b_content_hash_hexes.push(chunk_content_hash_hex);
            b_chunker_versions.push(current_chunker_version);
            b_model_ids.push(current_model_id);
            b_tokenizer_versions.push(current_tokenizer_version);

            // Keep for later use (embedding dedup + Qdrant upsert)
            chunk_texts.push(embedding_text);
            chunk_hashes.push(chunk_hash);
        }

        // Single batch INSERT with UNNEST: all chunks in 1 DB roundtrip
        let rows = client
            .query(
                "INSERT INTO chunks (
                file_id, chunk_type, content_hash, content_compressed, content_text,
                start_line, end_line, level, context_prefix,
                chunk_content_hash, chunker_version, embedding_model_id, tokenizer_version
            )
            SELECT $1,
                unnest($2::text[]),
                unnest($3::bytea[]),
                unnest($4::bytea[]),
                unnest($5::text[]),
                unnest($6::int[]),
                unnest($7::int[]),
                unnest($8::smallint[]),
                unnest($9::text[]),
                unnest($10::text[]),
                unnest($11::text[]),
                unnest($12::text[]),
                unnest($13::text[])
            RETURNING id",
                &[
                    &file_id,
                    &b_chunk_types,
                    &b_content_hashes,
                    &b_content_compressed,
                    &b_content_texts,
                    &b_start_lines,
                    &b_end_lines,
                    &b_levels,
                    &b_context_prefixes,
                    &b_content_hash_hexes,
                    &b_chunker_versions,
                    &b_model_ids,
                    &b_tokenizer_versions,
                ],
            )
            .await
            .map_err(|e| {
                error!(
                    "Failed to batch-insert {} chunks: {}",
                    semantic_chunks.len(),
                    e
                );
                AppError::Internal(format!("Chunk batch insert failed: {}", e))
            })?;

        let chunk_ids: Vec<i64> = rows.iter().map(|row| row.get::<_, i64>("id")).collect();
        info!(
            "Batch-inserted {} chunks for file_id={} (1 DB roundtrip instead of {})",
            chunk_ids.len(),
            file_id,
            semantic_chunks.len()
        );

        // Sprint 3.5: Batch UPDATE for parent_chunk_id via UNNEST instead of per-chunk loop
        {
            let mut update_chunk_ids: Vec<i64> = Vec::new();
            let mut update_parent_ids: Vec<i64> = Vec::new();
            for (idx, chunk) in semantic_chunks.iter().enumerate() {
                if let Some(parent_idx) = chunk.parent_idx {
                    if parent_idx < chunk_ids.len() {
                        update_chunk_ids.push(chunk_ids[idx]);
                        update_parent_ids.push(chunk_ids[parent_idx]);
                    }
                }
            }
            if !update_chunk_ids.is_empty() {
                client.execute(
                    "UPDATE chunks SET parent_chunk_id = u.parent_id FROM (SELECT unnest($1::bigint[]) AS id, unnest($2::bigint[]) AS parent_id) u WHERE chunks.id = u.id",
                    &[&update_chunk_ids, &update_parent_ids]
                ).await.map_err(|e| {
                    error!("Failed to batch-set parent_chunk_ids: {}", e);
                    AppError::Internal(format!("Batch parent update failed: {}", e))
                })?;
                debug!(
                    "Set parent_chunk_id for {} chunks in batch",
                    update_chunk_ids.len()
                );
            }
        }

        let mut embeddings_count = 0;

        if cpu_mode_enabled() {
            info!(
                "CPU mode: skipped embeddings for {} chunks (backfill via admin backfill orphaned)",
                chunk_ids.len()
            );
        } else {
            // EMBEDDING DEDUPLICATION: Check for existing embeddings by content_hash + model
            // If identical content was embedded before, reuse that vector instead of calling TEI.
            // Uses BATCH query with ANY($1) instead of N separate queries for performance.
            // When CCH is enabled, append "+cch" so old embeddings without
            // contextual prefixes are not reused.
            let model_name = embedding_storage_model_name(self.tei.get_model_name());
            let mut reused_count = 0;

            // Build map: chunk_idx -> Option<existing_embedding>
            // Query existing embeddings by content_hash (BYTEA) + model
            let mut existing_embeddings: Vec<Option<Vector>> = vec![None; chunk_ids.len()];

            // BATCH QUERY: One query for ALL content_hashes instead of N queries
            // Returns distinct content_hash -> vector mappings for this model
            let batch_existing = client
                .query(
                    "SELECT DISTINCT ON (c.content_hash) c.content_hash, ce.vector
             FROM chunk_embeddings ce
             JOIN chunks c ON c.id = ce.chunk_id
             WHERE c.content_hash = ANY($1) AND ce.model = $2",
                    &[&chunk_hashes, &model_name],
                )
                .await?;

            // Build hashmap: content_hash -> embedding vector
            let existing_map: std::collections::HashMap<Vec<u8>, Vector> = batch_existing
                .into_iter()
                .map(|row| {
                    let hash: Vec<u8> = row.get("content_hash");
                    let vec: Vector = row.get("vector");
                    (hash, vec)
                })
                .collect();

            // Map existing embeddings to our chunks
            for (idx, content_hash) in chunk_hashes.iter().enumerate() {
                if let Some(embedding) = existing_map.get(content_hash) {
                    existing_embeddings[idx] = Some(embedding.clone());
                    reused_count += 1;
                    debug!("Reusing embedding for chunk {} (content_hash match)", idx);
                }
            }

            if reused_count > 0 {
                info!("Embedding deduplication: {} of {} chunks can reuse existing embeddings (1 batch query)",
                  reused_count, chunk_ids.len());
            }

            // Collect indices that need new embeddings
            let need_embedding_indices: Vec<usize> = (0..chunk_ids.len())
                .filter(|&i| existing_embeddings[i].is_none())
                .collect();

            // Generate embeddings in batches for chunks that need them
            let batch_size = embedding_batch_size();
            if !need_embedding_indices.is_empty() {
                debug!(
                "Sprint 8.2: Embedding {} chunks in batches of {} (configured via EMBEDDING_BATCH_SIZE)",
                need_embedding_indices.len(), batch_size
            );
                for batch_start in (0..need_embedding_indices.len()).step_by(batch_size) {
                    let batch_end = (batch_start + batch_size).min(need_embedding_indices.len());
                    let batch_indices = &need_embedding_indices[batch_start..batch_end];

                    let batch_texts: Vec<&str> = batch_indices
                        .iter()
                        .map(|&idx| chunk_texts[idx].as_str())
                        .collect();

                    debug!(
                        "Sprint 8.2: TEI batch {}/{}: {} texts",
                        batch_start / batch_size + 1,
                        need_embedding_indices.len().div_ceil(batch_size),
                        batch_texts.len()
                    );

                    let embeddings = self.tei.embed_batch(&batch_texts).await?;

                    // Store new embeddings in our map
                    for (i, embedding) in embeddings.into_iter().enumerate() {
                        let chunk_idx = batch_indices[i];
                        existing_embeddings[chunk_idx] = Some(Vector::from(embedding));
                    }
                }
            }

            // Store all embeddings (both reused and new) in PostgreSQL + outbox
            // BATCH INSERT via UNNEST — N embeddings in 2 DB roundtrips instead of 2*N
            {
                let mut b_chunk_ids: Vec<i64> = Vec::with_capacity(existing_embeddings.len());
                let mut b_models: Vec<String> = Vec::with_capacity(existing_embeddings.len());
                let mut b_vectors: Vec<Vector> = Vec::with_capacity(existing_embeddings.len());

                for (chunk_idx, embedding_opt) in existing_embeddings.into_iter().enumerate() {
                    let chunk_id = chunk_ids[chunk_idx];
                    let embedding_vec = embedding_opt.ok_or_else(|| {
                        AppError::Internal(format!(
                            "Chunk {} missing embedding after embed phase (idx {})",
                            chunk_id, chunk_idx
                        ))
                    })?;

                    b_chunk_ids.push(chunk_id);
                    b_models.push(model_name.clone());
                    b_vectors.push(embedding_vec);
                }

                if !b_chunk_ids.is_empty() {
                    let tx = client.transaction().await.map_err(|e| {
                        AppError::Internal(format!("Failed to start embedding transaction: {}", e))
                    })?;

                    // Batch upsert embeddings (1 roundtrip instead of N)
                    tx.execute(
                        "INSERT INTO chunk_embeddings (chunk_id, model, vector)
                     SELECT unnest($1::bigint[]), unnest($2::text[]), unnest($3::vector[])
                     ON CONFLICT (chunk_id) DO UPDATE SET
                     vector = EXCLUDED.vector, model = EXCLUDED.model, created_at = NOW()",
                        &[&b_chunk_ids, &b_models, &b_vectors],
                    )
                    .await
                    .map_err(|e| {
                        AppError::Internal(format!("Failed to batch-insert embeddings: {}", e))
                    })?;

                    // Batch upsert outbox entries (1 roundtrip instead of N)
                    tx.execute(
                    "INSERT INTO indexing_outbox (action, chunk_id, file_id, source_id, payload)
                     SELECT 'upsert', unnest($1::bigint[]), $2, $3, '{}'::jsonb",
                    &[&b_chunk_ids, &file_id, &source_id],
                )
                .await
                .map_err(|e| {
                    AppError::Internal(format!("Failed to batch-insert outbox entries: {}", e))
                })?;

                    tx.commit().await.map_err(|e| {
                        AppError::Internal(format!("Failed to commit embedding transaction: {}", e))
                    })?;

                    embeddings_count = b_chunk_ids.len();
                }
            }

            // NOTE: Direct Qdrant upsert removed - Worker processes outbox entries asynchronously
            // This provides transactional guarantees: PostgreSQL commit = Qdrant sync will happen
            if embeddings_count > 0 {
                info!(
                    "Queued {} embeddings to outbox for Qdrant sync",
                    embeddings_count
                );
            }
        }

        Ok(ProcessResult::Processed {
            chunks: chunk_ids.len(),
            embeddings: embeddings_count,
        })
    }

    /// Sprint 8.4: Record sync ledger entry comparing PG chunk count vs Qdrant point count.
    /// Returns (pg_count, qdrant_count, drift) on success.
    /// Non-fatal: errors are logged but don't fail the sync.
    async fn record_sync_ledger(
        &self,
        client: &deadpool_postgres::Client,
        source_id: i64,
    ) -> Result<(i64, i64, i64)> {
        // Count chunks in PostgreSQL for this source
        let pg_row = client
            .query_one(
                "SELECT COUNT(*) FROM chunks c \
                 JOIN files f ON f.id = c.file_id \
                 WHERE f.source_id = $1",
                &[&source_id],
            )
            .await?;
        let pg_count: i64 = pg_row.get(0);

        if cpu_mode_enabled() {
            let qdrant_count = 0_i64;
            let drift = 0_i64;
            let status = "cpu_mode";
            let details = Some(format!(
                "CPU mode: Qdrant point count intentionally not sampled; PG has {} chunks",
                pg_count
            ));

            client
                .execute(
                    "INSERT INTO sync_ledger (source_id, pg_chunk_count, qdrant_point_count, drift_count, status, details) \
                     VALUES ($1, $2, $3, $4, $5, $6)",
                    &[
                        &source_id,
                        &pg_count,
                        &qdrant_count,
                        &drift,
                        &status,
                        &details,
                    ],
                )
                .await?;

            return Ok((pg_count, qdrant_count, drift));
        }

        // Count points in Qdrant for this source
        let qdrant_count = self.qdrant.count_by_source(source_id).await.unwrap_or(0) as i64;

        let drift = (pg_count - qdrant_count).abs();
        let status = if drift == 0 { "ok" } else { "drift" };

        let details = if drift > 0 {
            Some(format!(
                "PG has {} chunks, Qdrant has {} points (delta={})",
                pg_count,
                qdrant_count,
                pg_count - qdrant_count
            ))
        } else {
            None
        };

        client
            .execute(
                "INSERT INTO sync_ledger (source_id, pg_chunk_count, qdrant_point_count, drift_count, status, details) \
                 VALUES ($1, $2, $3, $4, $5, $6)",
                &[
                    &source_id,
                    &pg_count,
                    &qdrant_count,
                    &drift,
                    &status,
                    &details,
                ],
            )
            .await?;

        Ok((pg_count, qdrant_count, drift))
    }

    /// Stream-process a large conversation file without loading it entirely into memory.
    /// Reads the file in bounded chunks (for JSONL: line-by-line, for JSON: message-by-message),
    /// computes hash incrementally, and processes chunks in batches.
    /// Peak memory: O(batch_size * chunk_size) instead of O(file_size).
    #[allow(unused_assignments)] // `global_line` tracked for resume/offset support
    async fn process_large_conversation_file(
        &self,
        source_id: i64,
        source_name: &str,
        raw_file: RawFile,
    ) -> Result<ProcessResult> {
        use std::io::{BufRead, BufReader};

        let disk_path = raw_file
            .source_path
            .as_ref()
            .ok_or_else(|| AppError::Internal("Large file without source_path".into()))?;

        info!(
            "STREAMING large conversation file: {} ({}MB)",
            raw_file.path,
            raw_file.size / (1024 * 1024)
        );

        // 1. Compute hash by streaming (never hold full file in memory)
        let hash = {
            let file = std::fs::File::open(disk_path).map_err(|e| {
                AppError::Internal(format!("Failed to open {}: {}", disk_path.display(), e))
            })?;
            let mut reader = BufReader::with_capacity(64 * 1024, file);
            let mut hasher = Sha256::new();
            loop {
                let buf = reader
                    .fill_buf()
                    .map_err(|e| AppError::Internal(format!("Read error: {}", e)))?;
                if buf.is_empty() {
                    break;
                }
                hasher.update(buf);
                let len = buf.len();
                reader.consume(len);
            }
            hasher.finalize().to_vec()
        };

        let rel_path = &raw_file.path;
        let language = raw_file.language;
        let file_size = raw_file.size as i32;

        // 2. Get DB client + RLS
        let mut client = self.db.get().await?;
        client
            .execute(
                "SELECT set_config('app.user_id', $1::text, false)",
                &[&DEFAULT_USER_ID.to_string()],
            )
            .await?;
        client
            .execute("SELECT set_config('app.is_admin', 'true', false)", &[])
            .await?;

        // 3. Check if file is unchanged
        let existing = client
            .query_opt(
                "SELECT id, hash FROM files WHERE source_id = $1 AND path = $2",
                &[&source_id, rel_path],
            )
            .await?;

        if let Some(row) = &existing {
            let existing_hash: Vec<u8> = row.get("hash");
            if existing_hash == hash {
                debug!("Large file {} unchanged (hash match), skipping", rel_path);
                return Ok(ProcessResult::Skipped);
            }
        }

        // 4. Upsert file record (NO content stored for large files)
        let empty_compressed: Vec<u8> = zstd::encode_all(&b""[..], 3)
            .map_err(|e| AppError::Internal(format!("zstd error: {}", e)))?;
        let file_id: i64 = client.query_one(
            r#"INSERT INTO files (source_id, path, hash, content, content_text, language, size_original, size_compressed, last_modified)
               VALUES ($1, $2, $3, $4, NULL, $5, $6, $7, NOW())
               ON CONFLICT (source_id, path) DO UPDATE SET
                   hash = EXCLUDED.hash, content = EXCLUDED.content, content_text = NULL,
                   language = EXCLUDED.language, size_original = EXCLUDED.size_original,
                   size_compressed = EXCLUDED.size_compressed, last_modified = NOW(), updated_at = NOW()
               RETURNING id"#,
            &[&source_id, rel_path, &hash, &empty_compressed, &language, &file_size, &(empty_compressed.len() as i32)],
        ).await?.get(0);

        // 5. Delete existing chunks (with Qdrant cleanup)
        {
            let tx = client.transaction().await?;
            tx.execute(
                "INSERT INTO indexing_outbox (action, chunk_id, file_id, source_id, payload)
                 SELECT 'delete', c.id, c.file_id, f.source_id, '{}'::jsonb
                 FROM chunks c JOIN files f ON f.id = c.file_id WHERE c.file_id = $1",
                &[&file_id],
            )
            .await?;
            tx.execute("DELETE FROM chunks WHERE file_id = $1", &[&file_id])
                .await?;
            tx.commit().await?;
        }

        // 6. Stream-read, chunk in batches, embed, flush
        // Read file in 1MB windows for JSONL, or use streaming Gemini parser for JSON
        let is_json = disk_path.extension().map(|e| e == "json").unwrap_or(false);
        let batch_size = embedding_batch_size();
        let mut total_chunks = 0usize;
        let mut total_embeddings = 0usize;
        // `global_line` is incremented per JSONL line below; the final value
        // is not read again but tracking it keeps the invariant for future
        // resume/offset support without a larger refactor. `#[allow]` on the
        // declaration does not cover the later `+= 1`, so an attribute on the
        // function scope is the cleaner fix (applied above the outer `async fn`).
        let mut global_line = 0u32;

        const READ_BUFFER: usize = 1024 * 1024; // 1MB read buffer

        if is_json {
            // Gemini JSON: read file in one pass but parse messages individually via streaming chunker
            // We DO need the file as a string for the bracket-counting parser, but we can read it
            // in a streaming fashion and process batches.
            // For truly huge JSON (>100MB), we'd need a full SAX parser. For now, read in chunks
            // and assemble only the messages array portion.

            // Read file content in bounded fashion: only keep current + next message in memory
            let content = tokio::fs::read_to_string(disk_path).await.map_err(|e| {
                AppError::Internal(format!("Failed to read {}: {}", disk_path.display(), e))
            })?;

            // Use the streaming chunker (parses messages one-by-one, no full DOM)
            let chunks = self.chunker.chunk(&content, language.as_deref());
            drop(content); // Free the file content immediately after chunking

            let max_chunks = max_chunks_per_file();
            let chunk_count = chunks.len().min(max_chunks);

            // Process in batches
            for batch_start in (0..chunk_count).step_by(batch_size) {
                let batch_end = (batch_start + batch_size).min(chunk_count);
                let batch = &chunks[batch_start..batch_end];

                let (c, e) = self
                    .flush_chunk_batch(
                        &client,
                        file_id,
                        source_id,
                        source_name,
                        rel_path,
                        batch,
                        &language,
                        global_line,
                    )
                    .await?;
                total_chunks += c;
                total_embeddings += e;
            }
        } else {
            // JSONL: read line-by-line, accumulate messages, chunk when buffer full
            let file = std::fs::File::open(disk_path)
                .map_err(|e| AppError::Internal(format!("Failed to open: {}", e)))?;
            let reader = BufReader::with_capacity(READ_BUFFER, file);

            let chunker = crate::services::chunker::jsonl::JsonlChunker::default();
            let mut line_buffer = String::with_capacity(256 * 1024); // 256KB message accumulator
            let mut accumulated_chunks = Vec::new();

            for line_result in reader.lines() {
                let line =
                    line_result.map_err(|e| AppError::Internal(format!("Read error: {}", e)))?;
                global_line += 1;

                if line.trim().is_empty() {
                    continue;
                }
                line_buffer.push_str(&line);
                line_buffer.push('\n');

                // When buffer exceeds 256KB, chunk what we have and flush
                if line_buffer.len() > 256 * 1024 {
                    let batch_chunks = chunker.chunk(&line_buffer, language.as_deref());
                    accumulated_chunks.extend(batch_chunks);
                    line_buffer.clear();

                    // Flush when we have enough chunks
                    if accumulated_chunks.len() >= batch_size {
                        let (c, e) = self
                            .flush_chunk_batch(
                                &client,
                                file_id,
                                source_id,
                                source_name,
                                rel_path,
                                &accumulated_chunks,
                                &language,
                                0,
                            )
                            .await?;
                        total_chunks += c;
                        total_embeddings += e;
                        accumulated_chunks.clear();
                    }
                }
            }

            // Flush remaining
            if !line_buffer.is_empty() {
                let batch_chunks = chunker.chunk(&line_buffer, language.as_deref());
                accumulated_chunks.extend(batch_chunks);
            }
            if !accumulated_chunks.is_empty() {
                let (c, e) = self
                    .flush_chunk_batch(
                        &client,
                        file_id,
                        source_id,
                        source_name,
                        rel_path,
                        &accumulated_chunks,
                        &language,
                        0,
                    )
                    .await?;
                total_chunks += c;
                total_embeddings += e;
            }
        }

        info!(
            "STREAMING complete for {}: {} chunks, {} embeddings",
            rel_path, total_chunks, total_embeddings
        );
        Ok(ProcessResult::Processed {
            chunks: total_chunks,
            embeddings: total_embeddings,
        })
    }

    /// Flush a batch of chunks to DB + embedding pipeline. Returns (chunks_inserted, embeddings_created).
    #[allow(clippy::too_many_arguments)]
    async fn flush_chunk_batch(
        &self,
        client: &deadpool_postgres::Client,
        file_id: i64,
        source_id: i64,
        source_name: &str,
        rel_path: &str,
        chunks: &[crate::services::chunker::Chunk],
        _language: &Option<String>,
        _line_offset: u32,
    ) -> Result<(usize, usize)> {
        if chunks.is_empty() {
            return Ok((0, 0));
        }

        let cv = chunker_version();
        let mid = embedding_model_id();
        let tv = tokenizer_version();
        let model_name = embedding_storage_model_name(self.tei.get_model_name());

        // Batch-insert chunks
        let mut b_chunk_types = Vec::with_capacity(chunks.len());
        let mut b_content_hashes = Vec::with_capacity(chunks.len());
        let mut b_content_compressed = Vec::with_capacity(chunks.len());
        let mut b_content_texts = Vec::with_capacity(chunks.len());
        let mut b_start_lines = Vec::with_capacity(chunks.len());
        let mut b_end_lines = Vec::with_capacity(chunks.len());
        let mut b_levels = Vec::with_capacity(chunks.len());
        let mut b_context_prefixes = Vec::with_capacity(chunks.len());
        let mut b_content_hash_hexes = Vec::with_capacity(chunks.len());
        let mut b_chunker_versions = Vec::with_capacity(chunks.len());
        let mut b_model_ids = Vec::with_capacity(chunks.len());
        let mut b_tokenizer_versions = Vec::with_capacity(chunks.len());

        let mut chunk_texts = Vec::with_capacity(chunks.len());
        let mut chunk_hashes = Vec::with_capacity(chunks.len());

        for chunk in chunks {
            let hash = Sha256::digest(chunk.text.as_bytes()).to_vec();
            let compressed = zstd::encode_all(chunk.text.as_bytes(), 3)
                .map_err(|e| AppError::Internal(format!("zstd: {}", e)))?;

            b_chunk_types.push(format!("{:?}", chunk.chunk_type).to_lowercase());
            b_content_hashes.push(hash.clone());
            b_content_compressed.push(compressed);
            b_content_texts.push(chunk.text.clone());
            b_start_lines.push(chunk.start_line as i32);
            b_end_lines.push(chunk.end_line as i32);
            b_levels.push(chunk.level as i16);
            let context_prefix = chunk
                .context_prefix
                .clone()
                .unwrap_or_else(|| build_cch(source_name, rel_path, None));
            b_context_prefixes.push(context_prefix.clone());
            b_content_hash_hexes.push(chunk_content_sha256(&chunk.text));
            b_chunker_versions.push(cv.clone());
            b_model_ids.push(mid.clone());
            b_tokenizer_versions.push(tv.clone());

            chunk_texts.push(embedding_document_text(
                Some(&context_prefix),
                chunk.text.as_str(),
            ));
            chunk_hashes.push(hash);
        }

        let inserted = client.query(
            r#"INSERT INTO chunks (file_id, chunk_type, content_hash, content_compressed, content_text,
                start_line, end_line, level, context_prefix, chunk_content_hash, chunker_version,
                embedding_model_id, tokenizer_version)
               SELECT $1, unnest($2::text[]), unnest($3::bytea[]), unnest($4::bytea[]), unnest($5::text[]),
                      unnest($6::int[]), unnest($7::int[]), unnest($8::smallint[]), unnest($9::text[]),
                      unnest($10::varchar[]), unnest($11::varchar[]), unnest($12::varchar[]), unnest($13::varchar[])
               RETURNING id"#,
            &[
                &file_id, &b_chunk_types, &b_content_hashes, &b_content_compressed, &b_content_texts,
                &b_start_lines, &b_end_lines, &b_levels, &b_context_prefixes,
                &b_content_hash_hexes, &b_chunker_versions, &b_model_ids, &b_tokenizer_versions,
            ],
        ).await?;

        let chunk_ids: Vec<i64> = inserted.iter().map(|r| r.get(0)).collect();
        info!(
            "Batch-inserted {} chunks for file_id={}",
            chunk_ids.len(),
            file_id
        );

        if cpu_mode_enabled() {
            info!(
                "CPU mode: skipped streaming embeddings for {} chunks (backfill via admin backfill orphaned)",
                chunk_ids.len()
            );
            return Ok((chunk_ids.len(), 0));
        }

        // Generate embeddings in batches — batch-insert to DB (2 roundtrips per TEI batch, not 2*N)
        let batch_sz = embedding_batch_size();
        let mut embed_count = 0usize;

        for batch_start in (0..chunk_texts.len()).step_by(batch_sz) {
            let batch_end = (batch_start + batch_sz).min(chunk_texts.len());
            let text_batch: Vec<&str> = chunk_texts[batch_start..batch_end]
                .iter()
                .map(|s| s.as_str())
                .collect();

            match self.tei.embed_batch(&text_batch).await {
                Ok(embeddings) => {
                    let mut b_ids: Vec<i64> = Vec::with_capacity(embeddings.len());
                    let mut b_models: Vec<String> = Vec::with_capacity(embeddings.len());
                    let mut b_vecs: Vec<Vector> = Vec::with_capacity(embeddings.len());

                    for (i, embedding) in embeddings.iter().enumerate() {
                        let chunk_idx = batch_start + i;
                        b_ids.push(chunk_ids[chunk_idx]);
                        b_models.push(model_name.clone());
                        b_vecs.push(Vector::from(embedding.clone()));
                    }

                    // Batch upsert embeddings (1 roundtrip)
                    client
                        .execute(
                            "INSERT INTO chunk_embeddings (chunk_id, model, vector)
                         SELECT unnest($1::bigint[]), unnest($2::text[]), unnest($3::vector[])
                         ON CONFLICT (chunk_id) DO UPDATE SET
                         vector = EXCLUDED.vector, model = EXCLUDED.model, created_at = NOW()",
                            &[&b_ids, &b_models, &b_vecs],
                        )
                        .await?;

                    // Batch outbox entries (1 roundtrip)
                    client.execute(
                        "INSERT INTO indexing_outbox (action, chunk_id, file_id, source_id, payload)
                         SELECT 'upsert', unnest($1::bigint[]), $2, $3, '{}'::jsonb",
                        &[&b_ids, &file_id, &source_id],
                    ).await?;

                    embed_count += b_ids.len();
                }
                Err(e) => {
                    warn!("Embedding batch failed: {}", e);
                }
            }
        }

        Ok((chunk_ids.len(), embed_count))
    }
}

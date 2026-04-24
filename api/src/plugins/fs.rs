//! Filesystem plugin
//!
//! Discovers and reads files from a local filesystem directory
//! Uses `ignore` crate WalkBuilder for proper .gitignore support

use async_trait::async_trait;
use ignore::WalkBuilder;
use std::path::{Path, PathBuf};
use tokio::fs;
use tokio::task::spawn_blocking;
use tracing::warn;

use super::{RawFile, SourcePlugin, SyncResult};

/// Maximum file size to index — configurable via MAX_FILE_SIZE_MB env var.
/// Files larger than this are skipped entirely. Default: 50 MB.
/// For 100GB+ files, a streaming pipeline (not yet implemented) would be needed.
fn max_file_size() -> u64 {
    std::env::var("MAX_FILE_SIZE_MB")
        .ok()
        .and_then(|v| v.parse::<u64>().ok())
        .unwrap_or(50)
        * 1024
        * 1024
}

// Binary file signatures to skip
const BINARY_SIGNATURES: &[&[u8]] = &[
    b"\xFF\xFE",         // UTF-16 LE BOM
    b"\xFE\xFF",         // UTF-16 BE BOM
    b"\x7FELF",          // ELF binary
    b"MZ",               // Windows PE
    b"\xCA\xFE\xBA\xBE", // Java class
    b"\x89PNG",          // PNG
    b"\xFF\xD8\xFF",     // JPEG
    b"GIF87a",           // GIF87
    b"GIF89a",           // GIF89
    b"%PDF",             // PDF
];

pub struct FilesystemPlugin;

impl Default for FilesystemPlugin {
    fn default() -> Self {
        Self::new()
    }
}

impl FilesystemPlugin {
    pub fn new() -> Self {
        Self
    }
}

/// Check if a file is binary by examining its magic bytes
/// Sprint 5.1: Uses tokio::fs for async I/O (avoids blocking the runtime)
async fn check_if_binary(path: &Path) -> anyhow::Result<bool> {
    use tokio::io::AsyncReadExt;

    let mut file = tokio::fs::File::open(path).await?;
    let mut buffer = [0u8; 512]; // Read first 512 bytes
    let bytes_read = file.read(&mut buffer).await?;

    if bytes_read == 0 {
        return Ok(false); // Empty files are text
    }

    // Check for known binary signatures
    let header = &buffer[..bytes_read];
    for &sig in BINARY_SIGNATURES {
        if header.starts_with(sig) {
            return Ok(true);
        }
    }

    // If no BOM, check for null bytes (common in binary files)
    if header.contains(&0) {
        return Ok(true);
    }

    // Check for mostly non-printable characters
    let non_printable_count = header
        .iter()
        .filter(|&&b| b < 0x20 && b != b'\n' && b != b'\r' && b != b'\t')
        .count();

    if non_printable_count as f32 / bytes_read as f32 > 0.3 {
        return Ok(true);
    }

    Ok(false)
}

#[async_trait]
impl SourcePlugin for FilesystemPlugin {
    async fn sync(&self, source_path: &str) -> anyhow::Result<SyncResult> {
        let path = Path::new(source_path);

        if !path.exists() {
            return Err(anyhow::anyhow!("Path does not exist: {}", source_path));
        }

        if !path.is_dir() {
            return Err(anyhow::anyhow!("Path is not a directory: {}", source_path));
        }

        let mut files = vec![];
        let mut errors = vec![];

        self.collect_files(path, Path::new(source_path), &mut files, &mut errors)
            .await?;

        Ok(SyncResult { files, errors })
    }

    fn source_type(&self) -> &'static str {
        "fs"
    }
}

impl FilesystemPlugin {
    /// Collect files using WalkBuilder for proper .gitignore support
    /// WalkBuilder handles:
    /// - Nested .gitignore files in subdirectories
    /// - Parent directory .gitignore
    /// - Global gitignore (~/.gitignore)
    /// - .git/info/exclude
    async fn collect_files(
        &self,
        root_path: &Path,
        _current_path: &Path, // Not used with WalkBuilder (kept for API compat)
        files: &mut Vec<RawFile>,
        errors: &mut Vec<String>,
    ) -> anyhow::Result<()> {
        const INDEXABLE_EXTENSIONS: &[&str] = &[
            "rs", "py", "js", "ts", "tsx", "go", "java", "c", "cpp", "h", "hpp", "md", "txt",
            "json", "jsonl", "yaml", "yml", "toml", "sql", "sh", "bash", "html", "css", "scss",
            "vue", "svelte",
        ];

        let root = root_path.to_path_buf();

        // WalkBuilder is sync but very fast - run in blocking thread
        let paths: Vec<PathBuf> = spawn_blocking(move || {
            WalkBuilder::new(&root)
                .hidden(false) // Include hidden files/dirs (needed for .claude source)
                .git_ignore(true) // Respect .gitignore (NESTED!)
                .git_global(false) // Don't use ~/.gitignore (may have broad patterns)
                .git_exclude(true) // Respect .git/info/exclude
                .parents(false) // Don't check parent directories for ignore
                .ignore(true) // Respect .ignore files
                .build()
                .filter_map(|entry| entry.ok())
                .filter(|entry| entry.file_type().is_some_and(|ft| ft.is_file()))
                .map(|entry| entry.path().to_path_buf())
                .collect()
        })
        .await?;

        // Process collected paths (extension/size/binary checks)
        // DEBUG: Count extensions found
        let mut ext_counts: std::collections::HashMap<String, usize> =
            std::collections::HashMap::new();
        for path in &paths {
            if let Some(ext) = path.extension().and_then(|e| e.to_str()) {
                *ext_counts.entry(ext.to_lowercase()).or_insert(0) += 1;
            }
        }
        tracing::warn!(
            "FsPlugin discovered {} files: {:?}",
            paths.len(),
            ext_counts
        );

        for path in paths {
            // Extension check
            let ext = match path.extension().and_then(|e| e.to_str()) {
                Some(e) => e.to_lowercase(),
                None => continue,
            };
            if !INDEXABLE_EXTENSIONS.contains(&ext.as_str()) {
                tracing::debug!(
                    "Skipping file with unsupported extension: {}",
                    path.display()
                );
                continue;
            }

            // DEBUG: Log JSONL files that pass extension check
            if ext == "jsonl" {
                tracing::warn!("JSONL file passed extension check: {}", path.display());
            }

            // Size check
            let metadata = match path.metadata() {
                Ok(m) => m,
                Err(e) => {
                    let err = format!("Failed to read metadata for {}: {}", path.display(), e);
                    warn!("{}", err);
                    errors.push(err);
                    continue;
                }
            };

            let file_size = metadata.len();
            let max_size = max_file_size();
            if file_size > max_size {
                let err = format!(
                    "File too large ({}MB > {}MB limit): {} — adjust MAX_FILE_SIZE_MB to increase",
                    file_size / (1024 * 1024),
                    max_size / (1024 * 1024),
                    path.display()
                );
                warn!("{}", err);
                errors.push(err);
                continue;
            }

            // Binary check
            match check_if_binary(&path).await {
                Ok(true) => continue, // Skip binary silently
                Err(e) => {
                    let err = format!("Failed to check binary status {}: {}", path.display(), e);
                    warn!("{}", err);
                    errors.push(err);
                    continue;
                }
                Ok(false) => {} // Continue processing
            }

            let relative_path = path
                .strip_prefix(root_path)
                .unwrap_or(&path)
                .to_string_lossy()
                .to_string();

            // MEMORY GUARD: Large conversation files (>5MB) are NOT loaded into memory.
            // The index service will stream them from disk.
            let is_conversation_ext = ext == "jsonl" || ext == "json";
            if is_conversation_ext && file_size > super::LARGE_FILE_THRESHOLD as u64 {
                tracing::info!(
                    "Large conversation file ({}MB): will stream from disk: {}",
                    file_size / (1024 * 1024),
                    relative_path
                );
                files.push(RawFile {
                    path: relative_path,
                    content: String::new(), // empty — stream from source_path
                    size: file_size as usize,
                    language: Some(ext),
                    last_modified: None,
                    source_path: Some(path.clone()),
                });
                continue;
            }

            // Read content for normal-sized files
            match fs::read_to_string(&path).await {
                Ok(content) => {
                    // DEBUG: Log JSONL files being added
                    if ext == "jsonl" {
                        tracing::warn!(
                            "JSONL file added to sync: {} ({} bytes)",
                            relative_path,
                            content.len()
                        );
                    }

                    files.push(RawFile {
                        path: relative_path,
                        content,
                        size: file_size as usize,
                        language: Some(ext),
                        last_modified: None,
                        source_path: None,
                    });
                }
                Err(e) => {
                    let error_msg = if e.kind() == std::io::ErrorKind::InvalidData {
                        format!("Invalid UTF-8 in file {}: {}", path.display(), e)
                    } else {
                        format!("Failed to read {}: {}", path.display(), e)
                    };
                    warn!("{}", error_msg);
                    errors.push(error_msg);
                }
            }
        }

        Ok(())
    }
}

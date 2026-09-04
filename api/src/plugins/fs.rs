//! Filesystem plugin
//!
//! Discovers and reads files from a local filesystem directory
//! Uses `ignore` crate WalkBuilder for proper .gitignore support

use async_trait::async_trait;
use ignore::WalkBuilder;
use std::io::{Read, Seek, SeekFrom};
use std::path::{Path, PathBuf};
use tokio::fs;
use tokio::task::spawn_blocking;
use tracing::warn;

use super::{RawFile, RawFileRange, SourcePlugin, SyncResult};

const STORAGE_V2_FRAGMENT_BYTES: u64 = 1024 * 1024;
const STORAGE_V2_NEWLINE_WINDOW_BYTES: u64 = 64 * 1024;

/// Maximum file size for the legacy eager/streaming sync path, configurable via
/// MAX_FILE_SIZE_MB. Storage v2 applies its separate bounded fragmentation
/// contract instead of dropping oversized text. Default: 50 MB.
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

        self.collect_files(path, Path::new(source_path), &mut files, &mut errors, true)
            .await?;

        Ok(SyncResult { files, errors })
    }

    async fn sync_for_storage_v2(&self, source_path: &str) -> anyhow::Result<SyncResult> {
        let path = Path::new(source_path);

        if !path.exists() {
            return Err(anyhow::anyhow!("Path does not exist: {}", source_path));
        }
        if !path.is_dir() {
            return Err(anyhow::anyhow!("Path is not a directory: {}", source_path));
        }

        let mut files = vec![];
        let mut errors = vec![];
        self.collect_files(path, Path::new(source_path), &mut files, &mut errors, false)
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
        load_content: bool,
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

            // Metadata and binary checks happen before the size policy. A
            // storage-v2 scan decomposes large accepted text files instead of
            // dropping bytes or materializing the complete file in memory.
            let metadata = match path.metadata() {
                Ok(m) => m,
                Err(e) => {
                    let err = format!("Failed to read metadata for {}: {}", path.display(), e);
                    warn!("{}", err);
                    errors.push(err);
                    continue;
                }
            };

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

            let file_size = metadata.len();
            let max_size = max_file_size();
            if load_content && file_size > max_size {
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

            let relative_path = path
                .strip_prefix(root_path)
                .unwrap_or(&path)
                .to_string_lossy()
                .to_string();

            if !load_content {
                if file_size > STORAGE_V2_FRAGMENT_BYTES {
                    for source_range in storage_v2_fragment_ranges(&path, file_size)? {
                        files.push(RawFile {
                            path: relative_path.clone(),
                            content: String::new(),
                            size: usize::try_from(source_range.end - source_range.start)?,
                            language: Some(ext.clone()),
                            last_modified: None,
                            source_path: Some(path.clone()),
                            source_range: Some(source_range),
                        });
                    }
                } else {
                    files.push(RawFile {
                        path: relative_path,
                        content: String::new(),
                        size: file_size as usize,
                        language: Some(ext),
                        last_modified: None,
                        source_path: Some(path),
                        source_range: None,
                    });
                }
                continue;
            }

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
                    source_range: None,
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
                        source_range: None,
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

fn storage_v2_fragment_ranges(path: &Path, length: u64) -> anyhow::Result<Vec<RawFileRange>> {
    if length == 0 {
        return Ok(vec![RawFileRange { start: 0, end: 0 }]);
    }
    let mut file = std::fs::File::open(path)?;
    let mut ranges = Vec::new();
    let mut start = 0_u64;
    while start < length {
        let target = start.saturating_add(STORAGE_V2_FRAGMENT_BYTES).min(length);
        let end = if target == length {
            length
        } else {
            let newline_window_start = target
                .saturating_sub(STORAGE_V2_NEWLINE_WINDOW_BYTES)
                .max(start);
            file.seek(SeekFrom::Start(newline_window_start))?;
            let mut newline_window = vec![0_u8; usize::try_from(target - newline_window_start)?];
            file.read_exact(&mut newline_window)?;
            if let Some(offset) = newline_window.iter().rposition(|byte| *byte == b'\n') {
                newline_window_start + u64::try_from(offset)? + 1
            } else {
                file.seek(SeekFrom::Start(target))?;
                let mut boundary = [0_u8; 4];
                let read = file.read(&mut boundary)?;
                let offset = boundary[..read]
                    .iter()
                    .position(|byte| byte & 0b1100_0000 != 0b1000_0000)
                    .ok_or_else(|| {
                        anyhow::anyhow!("large text file has no valid UTF-8 fragment boundary")
                    })?;
                target + u64::try_from(offset)?
            }
        };
        if end <= start {
            anyhow::bail!("large text file produced an invalid fragment range");
        }
        ranges.push(RawFileRange { start, end });
        start = end;
    }
    Ok(ranges)
}

#[cfg(test)]
mod tests {
    use super::*;

    struct TestDirectory(PathBuf);

    impl Drop for TestDirectory {
        fn drop(&mut self) {
            let _ = std::fs::remove_dir_all(&self.0);
        }
    }

    #[tokio::test]
    async fn storage_v2_discovery_defers_file_content() {
        let directory = TestDirectory(
            std::env::temp_dir().join(format!("mainrag-storage-v2-fs-{}", uuid::Uuid::new_v4())),
        );
        std::fs::create_dir_all(&directory.0).expect("create test directory");
        let source_file = directory.0.join("sample.rs");
        std::fs::write(&source_file, "fn bounded() {}\n").expect("write test source");

        let result = FilesystemPlugin::new()
            .sync_for_storage_v2(directory.0.to_str().expect("UTF-8 test path"))
            .await
            .expect("discover storage-v2 source");

        assert!(result.errors.is_empty());
        assert_eq!(result.files.len(), 1);
        assert_eq!(result.files[0].path, "sample.rs");
        assert!(result.files[0].content.is_empty());
        assert_eq!(
            result.files[0].source_path.as_deref(),
            Some(source_file.as_path())
        );
        assert_eq!(result.files[0].source_range, None);
    }

    #[tokio::test]
    async fn storage_v2_discovery_fragments_oversized_text_without_dropping_bytes() {
        let directory = TestDirectory(
            std::env::temp_dir().join(format!("mainrag-storage-v2-fs-{}", uuid::Uuid::new_v4())),
        );
        std::fs::create_dir_all(&directory.0).expect("create test directory");
        let source_file = directory.0.join("large.jsonl");
        let mut content = vec![b'a'; STORAGE_V2_FRAGMENT_BYTES as usize - 17];
        content.extend_from_slice(" €\n".as_bytes());
        content.extend_from_slice(&[b'b'; 100]);
        std::fs::write(&source_file, &content).expect("write test source");
        let mut files = Vec::new();
        let mut errors = Vec::new();

        FilesystemPlugin::new()
            .collect_files(&directory.0, &directory.0, &mut files, &mut errors, false)
            .await
            .expect("discover fragmented storage-v2 source");

        assert!(errors.is_empty());
        assert_eq!(files.len(), 2);
        assert!(files
            .iter()
            .all(|file| file.source_path.as_deref() == Some(source_file.as_path())));
        let ranges = files
            .iter()
            .map(|file| file.source_range.expect("fragment range"))
            .collect::<Vec<_>>();
        assert_eq!(ranges[0].start, 0);
        assert_eq!(ranges[1].start, ranges[0].end);
        assert_eq!(ranges[1].end, content.len() as u64);
        assert_eq!(content[ranges[0].end as usize - 1], b'\n');
        assert_eq!(
            files.iter().map(|file| file.size).sum::<usize>(),
            content.len()
        );
    }

    #[test]
    fn large_file_fragment_ranges_are_contiguous_and_utf8_aligned() {
        let directory = TestDirectory(
            std::env::temp_dir().join(format!("mainrag-storage-v2-range-{}", uuid::Uuid::new_v4())),
        );
        std::fs::create_dir_all(&directory.0).expect("create test directory");
        let source_file = directory.0.join("large.txt");
        let mut content = vec![b'a'; STORAGE_V2_FRAGMENT_BYTES as usize - 1];
        content.extend_from_slice("€".as_bytes());
        content.extend_from_slice(&vec![b'b'; STORAGE_V2_FRAGMENT_BYTES as usize]);
        std::fs::write(&source_file, &content).expect("write fragmented source");

        let ranges = storage_v2_fragment_ranges(&source_file, content.len() as u64)
            .expect("derive fragment ranges");

        assert_eq!(ranges.first().map(|range| range.start), Some(0));
        assert_eq!(
            ranges.last().map(|range| range.end),
            Some(content.len() as u64)
        );
        assert!(ranges.windows(2).all(|pair| pair[0].end == pair[1].start));
        for range in ranges {
            assert!(range.end - range.start <= STORAGE_V2_FRAGMENT_BYTES + 3);
            std::str::from_utf8(&content[range.start as usize..range.end as usize])
                .expect("each range is valid UTF-8");
        }
    }
}

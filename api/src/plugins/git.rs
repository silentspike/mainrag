//! Git repository plugin
//!
//! Clones/pulls git repositories and extracts files

use async_trait::async_trait;
use git2::{build::CheckoutBuilder, Repository, StatusOptions};
use std::path::{Path, PathBuf};
use std::process::Stdio;
use std::sync::{Arc, LazyLock};
use std::time::Duration;
use tokio::fs;
use tokio::process::{Child, Command};
use tracing::{info, warn};

use super::{RawFile, SourcePlugin, SyncResult};

const INDEXABLE_EXTENSIONS: &[&str] = &[
    "rs", "py", "js", "ts", "tsx", "go", "java", "c", "cpp", "h", "hpp", "md", "txt", "json",
    "yaml", "yml", "toml", "sql", "sh", "bash", "html", "css", "scss", "vue", "svelte",
];

const GIT_CACHE_DIR: &str = "/data/mainrag/git-cache";
const MAX_FILE_SIZE: u64 = 50 * 1024 * 1024; // 50 MB
const GIT_TRANSPORT_TIMEOUT: Duration = Duration::from_secs(600);
static GIT_SYNC_LOCK: LazyLock<Arc<tokio::sync::Mutex<()>>> =
    LazyLock::new(|| Arc::new(tokio::sync::Mutex::new(())));

struct CloneStaging(PathBuf);

impl Drop for CloneStaging {
    fn drop(&mut self) {
        // This unique staging directory is owned by this clone attempt only.
        let _ = std::fs::remove_dir_all(&self.0);
    }
}

struct GitProcess {
    child: Child,
    group: Option<u32>,
}

impl Drop for GitProcess {
    fn drop(&mut self) {
        #[cfg(unix)]
        if let Some(group) = self.group {
            // The child creates its own process group. Kill transport/index-pack
            // descendants as well when a deadline or caller cancellation fires.
            unsafe { libc::kill(-(group as i32), libc::SIGKILL) };
        }
    }
}

async fn run_git(command: &mut Command, deadline: Duration) -> anyhow::Result<()> {
    command
        .env("GIT_TERMINAL_PROMPT", "0")
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .kill_on_drop(true);
    #[cfg(unix)]
    {
        use std::os::unix::process::CommandExt;
        command.as_std_mut().process_group(0);
    }
    let child = command.spawn()?;
    let mut process = GitProcess {
        group: child.id(),
        child,
    };
    let status = tokio::time::timeout(deadline, process.child.wait())
        .await
        .map_err(|_| anyhow::anyhow!("git transport deadline exceeded"))??;
    process.group = None;
    if !status.success() {
        anyhow::bail!("git transport failed");
    }
    Ok(())
}

fn cached_identity(cache: &Path, source: &str) -> anyhow::Result<(String, git2::Oid)> {
    let repo = Repository::open(cache)?;
    let mut options = StatusOptions::new();
    options.include_untracked(true).recurse_untracked_dirs(true);
    if !repo.statuses(Some(&mut options))?.is_empty() {
        anyhow::bail!("git source cache contains local changes");
    }
    let head = repo.head()?;
    if !head.is_branch() {
        anyhow::bail!("git source cache has no checked-out branch");
    }
    let branch = head
        .shorthand()
        .ok_or_else(|| anyhow::anyhow!("git source branch name is invalid"))?
        .to_string();
    if !matches!(branch.as_str(), "main" | "master") {
        anyhow::bail!("git source cache branch is unsupported");
    }
    if repo.find_remote("origin")?.url() != Some(source) {
        anyhow::bail!("git source cache origin differs from registered source");
    }
    let oid = head
        .target()
        .ok_or_else(|| anyhow::anyhow!("git source branch has no commit"))?;
    Ok((branch, oid))
}

fn publish_clone(staging: &Path, cache: &Path) -> anyhow::Result<()> {
    #[cfg(target_os = "linux")]
    {
        use std::ffi::CString;
        use std::os::unix::ffi::OsStrExt;
        let staging = CString::new(staging.as_os_str().as_bytes())?;
        let cache = CString::new(cache.as_os_str().as_bytes())?;
        // Publish the owned staging directory atomically without replacing even
        // an empty directory or dangling symlink created by another process.
        let result = unsafe {
            libc::renameat2(
                libc::AT_FDCWD,
                staging.as_ptr(),
                libc::AT_FDCWD,
                cache.as_ptr(),
                libc::RENAME_NOREPLACE,
            )
        };
        if result != 0 {
            return Err(std::io::Error::last_os_error().into());
        }
    }
    #[cfg(not(target_os = "linux"))]
    {
        if cache.symlink_metadata().is_ok() {
            anyhow::bail!("git source cache appeared during clone");
        }
        std::fs::rename(staging, cache)?;
    }
    Ok(())
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

/// Check if a file is binary by examining its magic bytes
fn check_if_binary(path: &Path) -> anyhow::Result<bool> {
    use std::io::Read;

    let mut file = std::fs::File::open(path)?;
    let mut buffer = [0u8; 512]; // Read first 512 bytes
    let bytes_read = file.read(&mut buffer)?;

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

pub struct GitPlugin {
    cache_dir: PathBuf,
}

impl Default for GitPlugin {
    fn default() -> Self {
        Self::new()
    }
}

impl GitPlugin {
    pub fn new() -> Self {
        Self {
            cache_dir: PathBuf::from(GIT_CACHE_DIR),
        }
    }

    /// Ensure cache directory exists
    async fn ensure_cache_dir(&self) -> anyhow::Result<()> {
        fs::create_dir_all(&self.cache_dir).await?;
        Ok(())
    }

    /// Get local cache path for repo
    fn get_cache_path(&self, source_name: &str) -> PathBuf {
        self.cache_dir.join(source_name)
    }

    /// Clone or update repository
    async fn sync_repo(&self, source_path: &str, source_name: &str) -> anyhow::Result<PathBuf> {
        if source_name.is_empty()
            || source_name == "."
            || source_name == ".."
            || source_name.contains(['/', '\\'])
        {
            anyhow::bail!("git source cache name is invalid");
        }
        let lease = GIT_SYNC_LOCK.clone().lock_owned().await;
        self.ensure_cache_dir().await?;

        let cache_path = self.get_cache_path(source_name);

        // If repo exists, pull updates
        if cache_path.exists() {
            info!("Updating existing repo: {}", source_name);
            let cache = cache_path.clone();
            let source = source_path.to_string();
            let (branch, old_oid) =
                tokio::task::spawn_blocking(move || cached_identity(&cache, &source)).await??;
            let refspec = format!("+refs/heads/{branch}:refs/remotes/origin/{branch}");
            run_git(
                Command::new("git").arg("-C").arg(&cache_path).args([
                    "fetch",
                    "--no-tags",
                    "--no-recurse-submodules",
                    "origin",
                    &refspec,
                ]),
                GIT_TRANSPORT_TIMEOUT,
            )
            .await?;
            let cache = cache_path.clone();
            let source = source_path.to_string();
            tokio::task::spawn_blocking(move || -> anyhow::Result<()> {
                let _lease = lease;
                if cached_identity(&cache, &source)? != (branch.clone(), old_oid) {
                    anyhow::bail!("git source cache changed during fetch");
                }
                let repo = Repository::open(&cache)?;
                let remote_oid = repo
                    .find_reference(&format!("refs/remotes/origin/{branch}"))?
                    .target()
                    .ok_or_else(|| anyhow::anyhow!("git source remote branch has no commit"))?;
                if remote_oid != old_oid {
                    if !repo.graph_descendant_of(remote_oid, old_oid)? {
                        anyhow::bail!("git source history changed without fast-forward");
                    }
                    let target = repo.find_object(remote_oid, None)?;
                    repo.checkout_tree(&target, Some(CheckoutBuilder::new().safe()))?;
                    repo.head()?
                        .set_target(remote_oid, "Advance verified git source cache")?;
                }
                Ok(())
            })
            .await??;
            return Ok(cache_path);
        }

        info!("Cloning repo: {}", source_name);
        let staging =
            self.cache_dir
                .join(format!(".{}-clone-{}", source_name, uuid::Uuid::new_v4()));
        let staging_owner = CloneStaging(staging.clone());
        let result = run_git(
            Command::new("git")
                .args([
                    "clone",
                    "--recurse-submodules",
                    "--single-branch",
                    "--no-tags",
                    "--",
                ])
                .arg(source_path)
                .arg(&staging),
            GIT_TRANSPORT_TIMEOUT,
        )
        .await;
        if let Err(error) = result {
            let _ = fs::remove_dir_all(&staging).await;
            return Err(error);
        }
        let cache = cache_path.clone();
        let source = source_path.to_string();
        tokio::task::spawn_blocking(move || -> anyhow::Result<()> {
            let _lease = lease;
            let _staging_owner = staging_owner;
            cached_identity(&staging, &source)?;
            if cache.try_exists()? {
                anyhow::bail!("git source cache appeared during clone");
            }
            publish_clone(&staging, &cache)?;
            Ok(())
        })
        .await??;
        Ok(cache_path)
    }

    /// Walk directory and collect files
    async fn collect_files(&self, root_path: &Path) -> anyhow::Result<Vec<RawFile>> {
        let mut files = vec![];

        // Use walkdir for recursive traversal
        let walker = ignore::WalkBuilder::new(root_path)
            .hidden(true)
            .git_ignore(true)
            .build();

        for entry in walker {
            let entry = match entry {
                Ok(e) => e,
                Err(e) => {
                    warn!("Error walking dir: {}", e);
                    continue;
                }
            };

            let path = entry.path();

            // Skip directories
            if path.is_dir() {
                continue;
            }

            // Skip .git
            if path.components().any(|c| c.as_os_str() == ".git") {
                continue;
            }

            // Check extension
            let ext = path.extension().and_then(|e| e.to_str()).unwrap_or("");

            if !INDEXABLE_EXTENSIONS.contains(&ext) {
                continue;
            }

            // Check file size
            if let Ok(metadata) = path.metadata() {
                if metadata.len() > MAX_FILE_SIZE {
                    warn!(
                        "File too large ({}MB > {}MB): {}",
                        metadata.len() / (1024 * 1024),
                        MAX_FILE_SIZE / (1024 * 1024),
                        path.display()
                    );
                    continue;
                }
            }

            // Check for binary files
            match check_if_binary(path) {
                Ok(true) => {
                    warn!("Skipping binary file: {}", path.display());
                    continue;
                }
                Ok(false) => {} // Continue to read
                Err(e) => {
                    warn!("Failed to check binary status {}: {}", path.display(), e);
                    continue;
                }
            }

            // Read file
            match tokio::fs::read_to_string(path).await {
                Ok(content) => {
                    let relative_path = path
                        .strip_prefix(root_path)
                        .unwrap_or(path)
                        .to_string_lossy()
                        .to_string();

                    files.push(RawFile {
                        path: relative_path,
                        size: content.len(),
                        content,
                        language: Some(ext.to_string()),
                        last_modified: None,
                        source_path: None,
                        source_range: None,
                    });
                }
                Err(e) => {
                    // Distinguish between different error types
                    let error_msg = if e.kind() == std::io::ErrorKind::InvalidData {
                        format!("Invalid UTF-8 in file {}: {}", path.display(), e)
                    } else {
                        format!("Failed to read {}: {}", path.display(), e)
                    };
                    warn!("{}", error_msg);
                }
            }
        }

        Ok(files)
    }
}

#[async_trait]
impl SourcePlugin for GitPlugin {
    async fn sync(&self, source_path: &str) -> anyhow::Result<SyncResult> {
        // Extract repo name from URL
        let source_name = source_path
            .split('/')
            .next_back()
            .unwrap_or("repo")
            .trim_end_matches(".git");

        // Clone/update repo
        let repo_path = self.sync_repo(source_path, source_name).await?;

        // Collect files
        let files = self.collect_files(&repo_path).await?;

        info!(
            "Git sync complete: {} files from {}",
            files.len(),
            source_name
        );

        Ok(SyncResult {
            files,
            errors: vec![],
        })
    }

    fn source_type(&self) -> &'static str {
        "git"
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::process::Command;
    use uuid::Uuid;

    struct TestDirectory(PathBuf);

    impl Drop for TestDirectory {
        fn drop(&mut self) {
            let _ = std::fs::remove_dir_all(&self.0);
        }
    }

    fn git(path: &Path, args: &[&str]) {
        let output = Command::new("git")
            .current_dir(path)
            .args(args)
            .output()
            .expect("git fixture command");
        assert!(output.status.success(), "git fixture command failed");
    }

    #[tokio::test]
    async fn cached_git_source_advances_and_preserves_dirty_cache() {
        let directory = TestDirectory(
            std::env::temp_dir().join(format!("mainrag-git-cache-{}", Uuid::new_v4())),
        );
        let source = directory.0.join("source");
        std::fs::create_dir_all(source.join("src")).unwrap();
        git(&directory.0, &["init", "-b", "main", "source"]);
        std::fs::write(source.join("src/a.rs"), "fn first() {}\n").unwrap();
        git(&source, &["add", "."]);
        git(
            &source,
            &[
                "-c",
                "user.name=Fixture",
                "-c",
                "user.email=fixture@example.invalid",
                "commit",
                "-m",
                "first",
            ],
        );

        let plugin = GitPlugin {
            cache_dir: directory.0.join("cache"),
        };
        let source_path = source.to_str().unwrap();
        let first = plugin.sync(source_path).await.unwrap();
        assert_eq!(first.files.len(), 1);
        assert_eq!(first.files[0].content, "fn first() {}\n");

        std::fs::write(source.join("src/a.rs"), "fn second() {}\n").unwrap();
        git(&source, &["add", "."]);
        git(
            &source,
            &[
                "-c",
                "user.name=Fixture",
                "-c",
                "user.email=fixture@example.invalid",
                "commit",
                "-m",
                "second",
            ],
        );
        let second = plugin.sync(source_path).await.unwrap();
        assert_eq!(second.files.len(), 1);
        assert_eq!(second.files[0].content, "fn second() {}\n");

        let cache_root = directory.0.join("cache/source");
        let cache_head = Repository::open(&cache_root)
            .unwrap()
            .head()
            .unwrap()
            .target()
            .unwrap();
        git(&source, &["checkout", "--orphan", "rewritten"]);
        git(&source, &["add", "."]);
        git(
            &source,
            &[
                "-c",
                "user.name=Fixture",
                "-c",
                "user.email=fixture@example.invalid",
                "commit",
                "-m",
                "rewritten history",
            ],
        );
        git(&source, &["branch", "-M", "main"]);
        assert!(plugin.sync(source_path).await.is_err());
        assert_eq!(
            Repository::open(&cache_root)
                .unwrap()
                .head()
                .unwrap()
                .target()
                .unwrap(),
            cache_head
        );
        assert_eq!(
            std::fs::read_to_string(cache_root.join("src/a.rs")).unwrap(),
            "fn second() {}\n"
        );

        let cached = directory.0.join("cache/source/src/a.rs");
        std::fs::write(&cached, "local change\n").unwrap();
        assert!(plugin.sync(source_path).await.is_err());
        assert_eq!(std::fs::read_to_string(cached).unwrap(), "local change\n");
        #[cfg(target_os = "linux")]
        {
            let staging = directory.0.join("owned-staging");
            let destination = directory.0.join("competing-cache");
            std::fs::create_dir_all(&staging).unwrap();
            std::os::unix::fs::symlink("missing-original", &destination).unwrap();
            assert!(publish_clone(&staging, &destination).is_err());
            assert_eq!(
                std::fs::read_link(destination).unwrap(),
                PathBuf::from("missing-original")
            );
            assert!(staging.is_dir());
        }
    }

    #[cfg(unix)]
    #[tokio::test]
    async fn transport_deadline_and_cancellation_stop_descendants() {
        let directory = TestDirectory(
            std::env::temp_dir().join(format!("mainrag-git-deadline-{}", Uuid::new_v4())),
        );
        std::fs::create_dir_all(&directory.0).unwrap();
        for cancel in [false, true] {
            let ticks = directory.0.join(if cancel { "cancel" } else { "timeout" });
            let output = ticks.clone();
            let task = tokio::spawn(async move {
                run_git(
                    tokio::process::Command::new("sh")
                        .args([
                            "-c",
                            "(while :; do echo tick >> \"$1\"; sleep 0.02; done) & wait",
                            "fixture",
                        ])
                        .arg(output),
                    Duration::from_millis(200),
                )
                .await
            });
            tokio::time::timeout(Duration::from_secs(5), async {
                while !ticks.exists() {
                    tokio::time::sleep(Duration::from_millis(10)).await;
                }
            })
            .await
            .unwrap();
            if cancel {
                task.abort();
                assert!(task.await.unwrap_err().is_cancelled());
            } else {
                assert!(task
                    .await
                    .unwrap()
                    .unwrap_err()
                    .to_string()
                    .contains("deadline"));
            }
            tokio::time::sleep(Duration::from_millis(50)).await;
            let stopped = std::fs::read(&ticks).unwrap();
            tokio::time::sleep(Duration::from_millis(100)).await;
            assert_eq!(
                std::fs::read(&ticks).unwrap(),
                stopped,
                "transport descendant survived"
            );
        }
    }
}

//! Explicit, bounded cleanup of one unpublished build directory. All writers
//! using the root must use the build lease protocol; old binaries, uncoordinated
//! administrators and network filesystems require external quiescence first.
//! Never infer reclamation authority for published packs from this lock.

use super::{ContentStoreError, Result};
use std::fs::{self, File, OpenOptions};
use std::io;
use std::path::Path;
use std::sync::Arc;
use uuid::Uuid;

fn invalid(message: &str) -> ContentStoreError {
    ContentStoreError::InvalidEntry(message.to_owned())
}

fn open_lease(root: &Path) -> Result<File> {
    let path = root.join(".build-writers.lock");
    match fs::symlink_metadata(&path) {
        Ok(metadata) if !metadata.is_file() => {
            return Err(invalid("build lock is not a regular file"))
        }
        Err(error) if error.kind() != io::ErrorKind::NotFound => return Err(error.into()),
        _ => {}
    }
    // This inode is permanent. Removing it would permit independent locks.
    Ok(OpenOptions::new()
        .create(true)
        .truncate(false)
        .read(true)
        .write(true)
        .open(path)?)
}

pub(super) fn writer_lease(root: &Path) -> Result<Arc<File>> {
    let file = open_lease(root)?;
    file.try_lock_shared().map_err(|error| {
        io::Error::other(format!(
            "build recovery active or shared locking unavailable: {error}"
        ))
    })?;
    Ok(Arc::new(file))
}

#[derive(Debug, serde::Serialize)]
pub struct IncompleteBuildCleanup {
    pub removed_files: usize,
    /// Sum of unlinked file lengths, not allocated or reclaimed device bytes.
    pub unlinked_file_bytes: u64,
    pub already_absent: bool,
}

fn uuid_name(name: &str, prefix: &str, suffix: &str) -> bool {
    name.strip_prefix(prefix)
        .and_then(|s| s.strip_suffix(suffix))
        .and_then(|s| Uuid::parse_str(s).ok().map(|id| id.to_string() == s))
        .unwrap_or(false)
}

fn owned_name(name: &str) -> bool {
    uuid_name(name, "", ".candidate")
        || uuid_name(name, "verified-", ".body")
        || name
            .strip_prefix("entry-")
            .and_then(|s| s.strip_suffix(".raw"))
            .and_then(|s| s.parse::<u64>().ok().map(|id| id.to_string() == s))
            .unwrap_or(false)
}

/// Cleanup exactly one build nonce while excluding all participating writers.
/// The caller owns the root and must first stop older/nonparticipating writers.
/// No age heuristic, recursive deletion, published-pack removal or DB mutation.
pub fn cleanup_incomplete_build(
    root: impl AsRef<Path>,
    nonce: Uuid,
    max_files: usize,
) -> Result<IncompleteBuildCleanup> {
    if !(1..=65536).contains(&max_files) {
        return Err(invalid("invalid cleanup file bound"));
    }
    let root = root.as_ref().canonicalize()?;
    let lease = open_lease(&root)?;
    lease.try_lock().map_err(|error| {
        io::Error::other(format!(
            "build writer active or exclusive locking unavailable: {error}"
        ))
    })?;
    let build_root = root.join(".building");
    let absent = || IncompleteBuildCleanup {
        removed_files: 0,
        unlinked_file_bytes: 0,
        already_absent: true,
    };
    match fs::symlink_metadata(&build_root) {
        Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(absent()),
        Ok(metadata) if metadata.is_dir() => {}
        Ok(_) => return Err(invalid("build root is not a directory")),
        Err(error) => return Err(error.into()),
    }
    let directory = build_root.join(nonce.to_string());
    match fs::symlink_metadata(&directory) {
        Err(error) if error.kind() == io::ErrorKind::NotFound => {
            // A prior attempt may have removed the directory but failed its
            // parent sync. Retry must complete that durability boundary too.
            File::open(&build_root)?.sync_all()?;
            return Ok(absent());
        }
        Ok(metadata) if metadata.is_dir() => {}
        Ok(_) => return Err(invalid("build target is not a directory")),
        Err(error) => return Err(error.into()),
    }
    let mut files = Vec::new();
    let mut bytes = 0u64;
    // Complete preflight before any unlink. Unknown names, symlinks and nested
    // directories are retained, not recursively interpreted as owned data.
    for entry in fs::read_dir(&directory)? {
        let entry = entry?;
        if files.len() == max_files {
            return Err(invalid("build exceeds cleanup file bound"));
        }
        if !entry.file_name().to_str().is_some_and(owned_name) || !entry.file_type()?.is_file() {
            return Err(invalid("unknown or nonregular build entry"));
        }
        bytes = bytes
            .checked_add(entry.metadata()?.len())
            .ok_or_else(|| invalid("cleanup byte count overflow"))?;
        // Retain bounded recognized names, not max_files copies of a possibly
        // long root path. Construct one complete path at a time during unlink.
        files.push(entry.file_name());
    }
    for file in &files {
        fs::remove_file(directory.join(file))?;
    }
    fs::remove_dir(&directory)?;
    File::open(build_root)?.sync_all()?;
    Ok(IncompleteBuildCleanup {
        removed_files: files.len(),
        unlinked_file_bytes: bytes,
        already_absent: false,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::services::content_store::{BodyCodec, PackBuilder};
    use std::io::Cursor;

    #[test]
    fn build_recovery_excludes_writers_and_preserves_published_aliases() {
        let root = std::env::temp_dir().join(format!("mainrag-build-recovery-{}", Uuid::new_v4()));
        let nonce = Uuid::new_v4();
        let mut builder = PackBuilder::new(&root, Uuid::new_v4(), nonce, 4096).unwrap();
        let entry = builder
            .add_reader(Cursor::new(b"complete bytes"), BodyCodec::Identity, None)
            .unwrap();
        assert!(cleanup_incomplete_build(&root, nonce, 8).is_err());
        let sealed = builder.seal().unwrap();
        let verified = sealed.verify_entry(&entry, None).unwrap();
        assert!(cleanup_incomplete_build(&root, nonce, 8).is_err());
        drop(verified);
        let published = sealed.publish().unwrap();
        let directory = root.join(".building").join(nonce.to_string());
        fs::create_dir(&directory).unwrap();
        let candidate = directory.join(format!("{}.candidate", entry.pack_id));
        fs::hard_link(&published.path, &candidate).unwrap();
        fs::write(directory.join("entry-0.raw"), b"raw").unwrap();
        assert!(cleanup_incomplete_build(&root, nonce, 1).is_err());
        assert!(candidate.exists());
        fs::write(directory.join("unrelated"), b"keep").unwrap();
        assert!(cleanup_incomplete_build(&root, nonce, 8).is_err());
        assert!(candidate.exists());
        fs::remove_file(directory.join("unrelated")).unwrap();
        let report = cleanup_incomplete_build(&root, nonce, 8).unwrap();
        assert_eq!(report.removed_files, 2);
        assert_eq!(report.unlinked_file_bytes, 17);
        assert!(!report.already_absent);
        assert_eq!(fs::read(&published.path).unwrap(), b"complete bytes");
        assert!(
            cleanup_incomplete_build(&root, nonce, 8)
                .unwrap()
                .already_absent
        );
        assert!(root.join(".build-writers.lock").is_file());
        fs::remove_dir_all(root).unwrap();
    }

    #[cfg(unix)]
    #[test]
    fn build_recovery_rejects_symlinks_and_nested_directories() {
        let root = std::env::temp_dir().join(format!("mainrag-build-recovery-{}", Uuid::new_v4()));
        let nonce = Uuid::new_v4();
        let directory = root.join(".building").join(nonce.to_string());
        fs::create_dir_all(&directory).unwrap();
        let external = root.join("retained");
        fs::write(&external, b"keep").unwrap();
        let raw = directory.join("entry-0.raw");
        std::os::unix::fs::symlink(&external, &raw).unwrap();
        assert!(cleanup_incomplete_build(&root, nonce, 8).is_err());
        assert_eq!(fs::read(&external).unwrap(), b"keep");
        fs::remove_file(&raw).unwrap();
        fs::create_dir(&raw).unwrap();
        assert!(cleanup_incomplete_build(&root, nonce, 8).is_err());
        fs::remove_dir(&raw).unwrap();
        fs::remove_dir(&directory).unwrap();
        std::os::unix::fs::symlink(&external, &directory).unwrap();
        assert!(cleanup_incomplete_build(&root, nonce, 8).is_err());
        assert_eq!(fs::read(&external).unwrap(), b"keep");
        fs::remove_dir_all(root).unwrap();
    }
}

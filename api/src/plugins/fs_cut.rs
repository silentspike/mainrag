//! An explicitly selected, trusted, immutable filesystem read boundary.
//!
//! Registered roots and relative item identities remain unchanged. A privileged
//! producer publishes the descriptor; ordinary API readers cannot redirect it.

use anyhow::{bail, ensure, Context, Result};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::fs;
use std::io::Read;
use std::os::unix::fs::MetadataExt;
use std::os::unix::fs::OpenOptionsExt;
use std::path::{Path, PathBuf};
use std::process::Command;

pub const KIND: &str = "btrfs-cut-v1";
const MAX_DESCRIPTOR_BYTES: u64 = 16384;

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct CutProof {
    pub format: String,
    pub cut_id: uuid::Uuid,
    pub source_root_sha256: String,
    pub descriptor_sha256: String,
    pub snapshot_uuid: uuid::Uuid,
    pub origin_uuid: uuid::Uuid,
    pub captured_at_unix: u64,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct CutObservation {
    pub cut: CutProof,
    pub fixture_sha256: String,
    pub item_count: usize,
    pub input_bytes: u64,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct Descriptor {
    format: String,
    cut_id: uuid::Uuid,
    source_root_sha256: String,
    registered_root: PathBuf,
    origin_subvolume: PathBuf,
    snapshot_root: PathBuf,
    read_root: PathBuf,
    snapshot_uuid: uuid::Uuid,
    origin_uuid: uuid::Uuid,
    captured_at_unix: u64,
}

#[derive(Debug, Clone)]
pub struct ReadCut {
    descriptor: PathBuf,
    snapshot_root: PathBuf,
    pub read_root: PathBuf,
    pub proof: CutProof,
}

pub fn registry_root() -> PathBuf {
    std::env::var_os("MAINRAG_STORAGE_V2_SOURCE_CUT_ROOT")
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from("/var/lib/mainrag-source-cuts"))
}

pub fn root_digest(root: &Path) -> Result<String> {
    Ok(hex::encode(Sha256::digest(
        root.to_str()
            .context("registered source root is not UTF-8")?
            .as_bytes(),
    )))
}

fn trusted_directory(path: &Path, owner: u32) -> Result<()> {
    ensure!(
        path.is_absolute() && path.canonicalize()? == path,
        "cut directory must be canonical"
    );
    for ancestor in path.ancestors() {
        let meta = fs::symlink_metadata(ancestor)?;
        ensure!(
            meta.is_dir() && !meta.file_type().is_symlink(),
            "cut directory is redirected"
        );
        // A root-owned sticky temporary parent cannot rename another owner's
        // entry. Fixture directories use the test owner's boundary below it.
        let trusted_owner = meta.uid() == 0 || meta.uid() == owner;
        let protected = meta.mode() & 0o022 == 0 || meta.uid() == 0 && meta.mode() & 0o1000 != 0;
        ensure!(
            trusted_owner && protected,
            "cut directory authority is unsafe"
        );
    }
    Ok(())
}

fn descriptor_bytes(path: &Path, owner: u32) -> Result<Vec<u8>> {
    let before = fs::symlink_metadata(path)?;
    ensure!(
        before.is_file()
            && !before.file_type().is_symlink()
            && before.uid() == owner
            && before.mode() & 0o027 == 0
            && before.nlink() == 1
            && before.len() <= MAX_DESCRIPTOR_BYTES,
        "cut descriptor authority or size is unsafe"
    );
    let mut bytes = Vec::new();
    let file = fs::OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_NOFOLLOW)
        .open(path)?;
    let opened = file.metadata()?;
    ensure!(
        (opened.dev(), opened.ino()) == (before.dev(), before.ino()),
        "cut descriptor was redirected during open"
    );
    file.take(MAX_DESCRIPTOR_BYTES + 1)
        .read_to_end(&mut bytes)?;
    let after = fs::symlink_metadata(path)?;
    ensure!(
        (
            before.dev(),
            before.ino(),
            before.len(),
            before.mtime(),
            before.mtime_nsec(),
            before.ctime(),
            before.ctime_nsec()
        ) == (
            after.dev(),
            after.ino(),
            after.len(),
            after.mtime(),
            after.mtime_nsec(),
            after.ctime(),
            after.ctime_nsec()
        ) && bytes.len() as u64 == before.len(),
        "cut descriptor changed during read"
    );
    Ok(bytes)
}

fn btrfs_command() -> Result<PathBuf> {
    for candidate in ["/usr/sbin/btrfs", "/usr/bin/btrfs"] {
        let path = Path::new(candidate);
        if !path.exists() {
            continue;
        }
        let resolved = path.canonicalize()?;
        let meta = fs::metadata(&resolved)?;
        ensure!(
            meta.is_file() && meta.uid() == 0 && meta.mode() & 0o022 == 0,
            "filesystem inspector authority is unsafe"
        );
        return Ok(resolved);
    }
    bail!("Btrfs source-cut inspector is unavailable")
}

fn inspect_snapshot(root: &Path) -> Result<(uuid::Uuid, uuid::Uuid)> {
    ensure!(
        fs::metadata(root)?.uid() == 0,
        "snapshot property authority is unsafe"
    );
    let binary = btrfs_command()?;
    let property = Command::new(&binary)
        .args(["property", "get", "-ts"])
        .arg(root)
        .arg("ro")
        .output()?;
    ensure!(
        property.status.success() && property.stdout == b"ro=true\n",
        "source cut is not a read-only subvolume"
    );
    let result = Command::new(binary)
        .args(["subvolume", "show"])
        .arg(root)
        .output()?;
    ensure!(
        result.status.success() && result.stdout.len() < MAX_DESCRIPTOR_BYTES as usize,
        "source cut identity is unavailable"
    );
    let output = std::str::from_utf8(&result.stdout)?;
    let field = |name: &str| -> Result<uuid::Uuid> {
        let values = output
            .lines()
            .filter_map(|line| line.trim().strip_prefix(name))
            .collect::<Vec<_>>();
        ensure!(values.len() == 1, "source cut identity is ambiguous");
        Ok(uuid::Uuid::parse_str(values[0].trim())?)
    };
    Ok((field("UUID:")?, field("Parent UUID:")?))
}

impl ReadCut {
    pub fn select(root: &Path) -> Result<Self> {
        Self::select_with(root, &registry_root(), 0, inspect_snapshot)
    }

    fn select_with(
        root: &Path,
        registry: &Path,
        owner: u32,
        inspect: impl FnOnce(&Path) -> Result<(uuid::Uuid, uuid::Uuid)>,
    ) -> Result<Self> {
        ensure!(
            root.is_absolute() && root.canonicalize()? == root,
            "registered cut source must be a canonical directory"
        );
        trusted_directory(registry, owner)?;
        trusted_directory(&registry.join("views"), owner)?;
        let digest = root_digest(root)?;
        let current = registry.join(format!("current-{digest}.json"));
        let bytes = descriptor_bytes(&current, owner)?;
        let value: Descriptor = serde_json::from_slice(&bytes)?;
        ensure!(
            value.format == "mainrag.fs-read-cut.v1"
                && value.source_root_sha256 == digest
                && value.registered_root == root
                && value.captured_at_unix > 0
                && !value.cut_id.is_nil()
                && !value.snapshot_uuid.is_nil()
                && !value.origin_uuid.is_nil(),
            "cut descriptor differs from the registered source"
        );
        let relative = root
            .strip_prefix(&value.origin_subvolume)
            .context("cut origin does not contain the registered root")?;
        ensure!(
            !relative.as_os_str().is_empty()
                && value.snapshot_root.parent() == Some(registry.join("views").as_path())
                && value.snapshot_root.file_name().and_then(|s| s.to_str())
                    == Some(value.cut_id.to_string().as_str())
                && value.read_root == value.snapshot_root.join(relative)
                && value.read_root.canonicalize()? == value.read_root
                && value.read_root.is_dir(),
            "cut read location differs from its source-relative boundary"
        );
        let (snapshot, origin) = inspect(&value.snapshot_root)?;
        ensure!(
            snapshot == value.snapshot_uuid && origin == value.origin_uuid,
            "cut filesystem identity differs from the trusted descriptor"
        );
        let history = registry.join("history");
        trusted_directory(&history, owner)?;
        let descriptor = history.join(format!("{}-{digest}.json", value.cut_id));
        ensure!(
            descriptor_bytes(&descriptor, owner)? == bytes,
            "selected cut has no identical immutable history descriptor"
        );
        Ok(Self {
            descriptor,
            snapshot_root: value.snapshot_root,
            read_root: value.read_root,
            proof: CutProof {
                format: value.format,
                cut_id: value.cut_id,
                source_root_sha256: digest,
                descriptor_sha256: hex::encode(Sha256::digest(bytes)),
                snapshot_uuid: snapshot,
                origin_uuid: origin,
                captured_at_unix: value.captured_at_unix,
            },
        })
    }

    pub fn validate_pinned(&self) -> Result<()> {
        let bytes = descriptor_bytes(&self.descriptor, 0)?;
        ensure!(
            hex::encode(Sha256::digest(bytes)) == self.proof.descriptor_sha256,
            "pinned source-cut descriptor changed during the operation"
        );
        ensure!(
            inspect_snapshot(&self.snapshot_root)?
                == (self.proof.snapshot_uuid, self.proof.origin_uuid),
            "selected source cut lost its immutable filesystem identity"
        );
        Ok(())
    }
}

/// Capture is a fixed root-owned helper, constrained by its reviewed policy.
/// No registered path or arbitrary command is passed as a privileged argument.
#[cfg(feature = "storage-v2-retrieval")]
pub async fn capture(root: &Path) -> Result<()> {
    let helper = Path::new("/usr/libexec/mainrag/source-cut-capture");
    let meta = fs::symlink_metadata(helper)?;
    ensure!(
        meta.is_file()
            && !meta.file_type().is_symlink()
            && meta.uid() == 0
            && meta.mode() & 0o022 == 0,
        "source-cut helper authority is unsafe"
    );
    let output = tokio::process::Command::new("/usr/bin/sudo")
        .args(["-n"])
        .arg(helper)
        .args(["--source-root-sha256", &root_digest(root)?])
        .output()
        .await?;
    ensure!(
        output.status.success(),
        "controlled source-cut capture failed"
    );
    // The helper's output is private operational context. Validate the installed
    // descriptor and actual kernel identity instead of trusting stdout.
    let root = root.to_path_buf();
    tokio::task::spawn_blocking(move || ReadCut::select(&root)).await??;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::os::unix::fs::PermissionsExt;

    fn fixture() -> (tempfile::TempDir, PathBuf, PathBuf, serde_json::Value) {
        let directory = tempfile::tempdir().unwrap();
        let origin = directory.path().join("origin");
        let root = origin.join("sessions");
        let registry = directory.path().join("registry");
        let cut_id = uuid::Uuid::new_v4();
        let snapshot = registry.join("views").join(cut_id.to_string());
        for path in [&root, &snapshot.join("sessions"), &registry.join("history")] {
            fs::create_dir_all(path).unwrap();
        }
        fs::set_permissions(&registry, fs::Permissions::from_mode(0o750)).unwrap();
        fs::write(root.join("session.jsonl"), b"first\n").unwrap();
        fs::write(snapshot.join("sessions/session.jsonl"), b"first\n").unwrap();
        let value = serde_json::json!({"format":"mainrag.fs-read-cut.v1","cut_id":cut_id,
            "source_root_sha256":root_digest(&root).unwrap(),"registered_root":root,
            "origin_subvolume":origin,"snapshot_root":snapshot,"read_root":snapshot.join("sessions"),
            "snapshot_uuid":uuid::Uuid::new_v4(),"origin_uuid":uuid::Uuid::new_v4(),"captured_at_unix":1});
        publish(&registry, &value);
        (directory, root, registry, value)
    }

    fn publish(registry: &Path, value: &serde_json::Value) {
        let digest = value["source_root_sha256"].as_str().unwrap();
        let raw = serde_json::to_vec(value).unwrap();
        for path in [
            registry.join(format!("current-{digest}.json")),
            registry.join("history").join(format!(
                "{}-{digest}.json",
                value["cut_id"].as_str().unwrap()
            )),
        ] {
            fs::write(&path, &raw).unwrap();
            fs::set_permissions(path, fs::Permissions::from_mode(0o640)).unwrap();
        }
    }

    fn select(root: &Path, registry: &Path, value: &serde_json::Value) -> Result<ReadCut> {
        ReadCut::select_with(root, registry, unsafe { libc::geteuid() }, |_| {
            Ok((
                uuid::Uuid::parse_str(value["snapshot_uuid"].as_str().unwrap()).unwrap(),
                uuid::Uuid::parse_str(value["origin_uuid"].as_str().unwrap()).unwrap(),
            ))
        })
    }

    #[test]
    fn selected_cut_preserves_original_identity_and_bytes_after_live_append() {
        let (_directory, root, registry, value) = fixture();
        let selected = select(&root, &registry, &value).unwrap();
        fs::write(root.join("session.jsonl"), b"first\nnew live tail\n").unwrap();
        assert_eq!(
            fs::read(selected.read_root.join("session.jsonl")).unwrap(),
            b"first\n"
        );
        assert_eq!(
            selected.proof.source_root_sha256,
            root_digest(&root).unwrap()
        );
        assert_eq!(
            selected.descriptor.parent(),
            Some(registry.join("history").as_path())
        );
        let history = fs::read(&selected.descriptor).unwrap();
        fs::write(
            registry.join(format!(
                "current-{}.json",
                selected.proof.source_root_sha256
            )),
            b"later selection",
        )
        .unwrap();
        assert_eq!(fs::read(selected.descriptor).unwrap(), history);
    }

    #[test]
    fn cut_selection_rejects_root_redirection_missing_history_and_unsafe_authority() {
        let (_directory, root, registry, mut value) = fixture();
        value["read_root"] = serde_json::json!(root);
        publish(&registry, &value);
        assert!(select(&root, &registry, &value).is_err());
        value["read_root"] = serde_json::json!(PathBuf::from(
            value["snapshot_root"].as_str().unwrap()
        )
        .join("sessions"));
        publish(&registry, &value);
        let history = registry.join("history").join(format!(
            "{}-{}.json",
            value["cut_id"].as_str().unwrap(),
            value["source_root_sha256"].as_str().unwrap()
        ));
        fs::remove_file(&history).unwrap();
        assert!(select(&root, &registry, &value).is_err());
        publish(&registry, &value);
        fs::set_permissions(&history, fs::Permissions::from_mode(0o660)).unwrap();
        assert!(select(&root, &registry, &value).is_err());
    }

    #[test]
    fn kernel_identity_mismatch_and_descriptor_alias_are_rejected() {
        let (_directory, root, registry, value) = fixture();
        assert!(
            ReadCut::select_with(&root, &registry, unsafe { libc::geteuid() }, |_| Ok((
                uuid::Uuid::new_v4(),
                uuid::Uuid::new_v4()
            )))
            .is_err()
        );
        let current = registry.join(format!(
            "current-{}.json",
            value["source_root_sha256"].as_str().unwrap()
        ));
        fs::rename(&current, registry.join("alias.json")).unwrap();
        std::os::unix::fs::symlink(registry.join("alias.json"), &current).unwrap();
        assert!(select(&root, &registry, &value).is_err());
    }
}

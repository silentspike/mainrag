//! Explicitly managed JSONL segments for storage-v2 shadow ingestion.
//!
//! The producer publishes immutable segments before atomically replacing the
//! manifest. The current adapter still returns every segment for full reads;
//! incremental reuse requires a verified persisted frontier in the writer.

use anyhow::{bail, Context, Result};
use async_trait::async_trait;
use serde::Deserialize;
use sha2::{Digest, Sha256};
use std::path::Path;
use tokio::io::AsyncReadExt;
use uuid::Uuid;

use super::{ObservedSyncResult, RawFile, SourcePlugin, SyncResult};
use crate::services::source_read::ReadAccounting;

const FORMAT: &str = "mainrag.managed-append.v1";
const MAX_MANIFEST_BYTES: u64 = 16 * 1024 * 1024;
const MAX_SEGMENT_BYTES: u64 = 1024 * 1024;

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct Manifest {
    format: String,
    epoch: String,
    chain: String,
    segments: Vec<Segment>,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct Segment {
    sequence: u64,
    name: String,
    bytes: u64,
    sha256: String,
}

pub struct ManagedAppendPlugin;

fn lowercase_sha256(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}

fn segment_name_matches(sequence: u64, name: &str) -> bool {
    let Some(random) = name
        .strip_prefix(&format!("{sequence:08}-"))
        .and_then(|suffix| suffix.strip_suffix(".jsonl"))
    else {
        return false;
    };
    random.len() == 32
        && random
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}

fn segment_chain(previous: [u8; 32], segment: &Segment) -> [u8; 32] {
    let canonical = format!(
        "{{\"bytes\":{},\"name\":\"{}\",\"sequence\":{},\"sha256\":\"{}\"}}",
        segment.bytes, segment.name, segment.sequence, segment.sha256
    );
    let mut digest = Sha256::new();
    digest.update(previous);
    digest.update(canonical.as_bytes());
    digest.finalize().into()
}

async fn verify_segment(path: &Path, segment: &Segment, accounting: &ReadAccounting) -> Result<()> {
    let mut source = accounting.reader(tokio::fs::File::open(path).await?);
    let mut digest = Sha256::new();
    let mut total = 0_u64;
    let mut buffer = [0_u8; 64 * 1024];
    loop {
        let read = source.read(&mut buffer).await?;
        if read == 0 {
            break;
        }
        total = total
            .checked_add(u64::try_from(read)?)
            .context("managed append source read counter overflow")?;
        if total > MAX_SEGMENT_BYTES {
            bail!("managed append segment grew past its byte limit");
        }
        digest.update(&buffer[..read]);
    }
    if total != segment.bytes || hex::encode(digest.finalize()) != segment.sha256 {
        bail!("managed append segment content does not match its manifest");
    }
    Ok(())
}

async fn discover(source_path: &str) -> Result<ObservedSyncResult> {
    let root = Path::new(source_path);
    if !tokio::fs::symlink_metadata(root)
        .await?
        .file_type()
        .is_dir()
        || !tokio::fs::symlink_metadata(root.join("segments"))
            .await?
            .file_type()
            .is_dir()
    {
        bail!("managed append root or segment directory is not a real directory");
    }
    let manifest_path = root.join("manifest.json");
    if !tokio::fs::symlink_metadata(&manifest_path)
        .await?
        .file_type()
        .is_file()
    {
        bail!("managed append manifest is not a regular file");
    }
    let accounting = ReadAccounting::filesystem_adapter();
    let mut bytes = Vec::new();
    accounting
        .reader(tokio::fs::File::open(&manifest_path).await?)
        .take(MAX_MANIFEST_BYTES + 1)
        .read_to_end(&mut bytes)
        .await?;
    if bytes.len() as u64 > MAX_MANIFEST_BYTES {
        bail!("managed append manifest exceeds its size limit");
    }
    let manifest: Manifest =
        serde_json::from_slice(&bytes).context("invalid managed append manifest")?;
    if manifest.format != FORMAT
        || Uuid::parse_str(&manifest.epoch)
            .map(|id| id.to_string() != manifest.epoch)
            .unwrap_or(true)
        || !lowercase_sha256(&manifest.chain)
    {
        bail!("managed append format, epoch or chain is invalid");
    }
    let mut chain: [u8; 32] = Sha256::digest(format!("{FORMAT}:{}", manifest.epoch)).into();
    let mut files = Vec::with_capacity(manifest.segments.len());
    for (index, segment) in manifest.segments.iter().enumerate() {
        let sequence = u64::try_from(index)? + 1;
        if segment.sequence != sequence
            || !segment_name_matches(sequence, &segment.name)
            || segment.bytes == 0
            || segment.bytes > MAX_SEGMENT_BYTES
            || !lowercase_sha256(&segment.sha256)
        {
            bail!("managed append segment metadata is invalid");
        }
        let path = root.join("segments").join(&segment.name);
        let metadata = tokio::fs::symlink_metadata(&path).await?;
        if !metadata.file_type().is_file() || metadata.len() != segment.bytes {
            bail!("managed append segment is missing or changed");
        }
        verify_segment(&path, segment, &accounting).await?;
        chain = segment_chain(chain, segment);
        files.push(RawFile {
            // The epoch is part of the stable logical item key. Rotation
            // cannot silently reuse an earlier generation's occurrences.
            path: format!("{}/segments/{}", manifest.epoch, segment.name),
            content: String::new(),
            size: usize::try_from(segment.bytes)?,
            language: Some("jsonl".to_string()),
            last_modified: None,
            source_path: Some(path),
            source_range: None,
        });
    }
    if hex::encode(chain) != manifest.chain {
        bail!("managed append manifest chain does not match its segments");
    }
    Ok(ObservedSyncResult {
        result: SyncResult {
            files,
            errors: Vec::new(),
        },
        application_read_bytes: Some(accounting.bytes()),
    })
}

#[async_trait]
impl SourcePlugin for ManagedAppendPlugin {
    async fn sync(&self, _source_path: &str) -> Result<SyncResult> {
        bail!("managed append sources require the feature-gated storage-v2 writer")
    }

    async fn sync_observed(&self, source_path: &str) -> Result<ObservedSyncResult> {
        discover(source_path).await
    }

    async fn sync_for_storage_v2(&self, source_path: &str) -> Result<SyncResult> {
        Ok(discover(source_path).await?.result)
    }

    async fn sync_for_storage_v2_observed(&self, source_path: &str) -> Result<ObservedSyncResult> {
        discover(source_path).await
    }

    fn source_type(&self) -> &'static str {
        "managed_append"
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::PathBuf;
    use std::process::Command;

    struct TestDirectory(PathBuf);

    impl Drop for TestDirectory {
        fn drop(&mut self) {
            let _ = std::fs::remove_dir_all(&self.0);
        }
    }

    #[tokio::test]
    async fn first_party_producer_and_adapter_reject_same_length_replacement() {
        let directory = TestDirectory(
            std::env::temp_dir().join(format!("mainrag-managed-append-{}", Uuid::new_v4())),
        );
        std::fs::create_dir_all(&directory.0).unwrap();
        let root = directory.0.join("source");
        let input = directory.0.join("input.jsonl");
        let original = b"{\"event\":\"one\"}\n";
        std::fs::write(&input, original).unwrap();
        let script = Path::new(env!("CARGO_MANIFEST_DIR"))
            .parent()
            .unwrap()
            .join("tools/managed_append.py");
        for (operation, with_input) in [("init", false), ("append", true)] {
            let mut command = Command::new("python3");
            command.arg(&script).arg(operation).arg(&root);
            if with_input {
                command.arg(&input);
            }
            assert!(command.output().unwrap().status.success());
        }

        let plugin = ManagedAppendPlugin;
        assert!(plugin.sync(root.to_str().unwrap()).await.is_err());
        let observed = plugin
            .sync_for_storage_v2_observed(root.to_str().unwrap())
            .await
            .unwrap();
        assert_eq!(observed.result.files.len(), 1);
        let manifest_length = std::fs::metadata(root.join("manifest.json")).unwrap().len();
        assert_eq!(
            observed.application_read_bytes,
            Some(manifest_length + original.len() as u64)
        );
        let file = &observed.result.files[0];
        assert!(file.path.contains("/segments/"));
        let source = file.source_path.as_ref().unwrap();
        let mut permissions = std::fs::metadata(source).unwrap().permissions();
        permissions.set_readonly(false);
        std::fs::set_permissions(source, permissions).unwrap();
        std::fs::write(source, b"{\"event\":\"two\"}\n").unwrap();
        assert_eq!(
            std::fs::metadata(source).unwrap().len(),
            original.len() as u64
        );
        let error = plugin
            .sync_for_storage_v2_observed(root.to_str().unwrap())
            .await
            .unwrap_err();
        assert!(error.to_string().contains("does not match its manifest"));
    }
}

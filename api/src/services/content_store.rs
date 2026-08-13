//! Bounded, fail-closed storage-v2 pack I/O.
//!
//! Database publication is deliberately separate from filesystem publication:
//! callers seal and verify a candidate, atomically publish the complete file,
//! then advance the database pack state. A crash between those steps leaves a
//! complete retryable file, never a partially readable published pack.

use sha2::{Digest, Sha256};
use std::fs::{self, File, OpenOptions};
use std::io::{self, BufReader, Read, Seek, SeekFrom, Write};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};
use thiserror::Error;
use uuid::Uuid;

const MANIFEST_DOMAIN: &[u8] = b"mainrag.storage-v2.pack-manifest.v1\0";
pub const DEFAULT_IO_BUFFER_BYTES: usize = 64 * 1024;

#[derive(Debug, Error)]
pub enum ContentStoreError {
    #[error("I/O error: {0}")]
    Io(#[from] io::Error),
    #[error("invalid pack entry: {0}")]
    InvalidEntry(String),
    #[error("content corruption: {0}")]
    Corruption(String),
    #[error("dictionary mismatch")]
    DictionaryMismatch,
    #[error("digest collision: equal identity resolved to different bytes")]
    DigestCollision,
}

pub type Result<T> = std::result::Result<T, ContentStoreError>;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum BodyCodec {
    Identity,
    Zstd,
}

impl BodyCodec {
    pub fn database_name(self) -> &'static str {
        match self {
            Self::Identity => "identity",
            Self::Zstd => "zstd",
        }
    }

    fn manifest_tag(self) -> u8 {
        match self {
            Self::Identity => 0,
            Self::Zstd => 1,
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct BodyIdentity {
    pub digest_algorithm: &'static str,
    pub digest: [u8; 32],
    pub logical_length: u64,
}

impl BodyIdentity {
    pub fn from_bytes(bytes: &[u8]) -> Self {
        Self {
            digest_algorithm: "sha256-v1",
            digest: Sha256::digest(bytes).into(),
            logical_length: bytes.len() as u64,
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DictionaryIdentity {
    pub id: i64,
    pub digest: [u8; 32],
}

impl DictionaryIdentity {
    pub fn verify(&self, bytes: &[u8]) -> Result<()> {
        let actual: [u8; 32] = Sha256::digest(bytes).into();
        if actual != self.digest {
            return Err(ContentStoreError::DictionaryMismatch);
        }
        Ok(())
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PackEntry {
    pub pack_id: Uuid,
    pub ordinal: u64,
    pub body: BodyIdentity,
    pub pack_offset: u64,
    pub stored_length: u64,
    pub codec: BodyCodec,
    pub dictionary: Option<DictionaryIdentity>,
    pub entry_digest: [u8; 32],
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PackManifest {
    pub pack_id: Uuid,
    pub entries: Vec<PackEntry>,
    pub stored_bytes: u64,
    pub sha256: [u8; 32],
    pub managed_peak_buffer_bytes: usize,
}

impl PackManifest {
    pub fn verify(&self) -> Result<()> {
        let expected = manifest_digest(self.pack_id, &self.entries, self.stored_bytes);
        if expected != self.sha256 {
            return Err(ContentStoreError::Corruption(
                "pack manifest digest mismatch".to_string(),
            ));
        }
        let mut expected_offset = 0_u64;
        for (ordinal, entry) in self.entries.iter().enumerate() {
            if entry.pack_id != self.pack_id
                || entry.ordinal != ordinal as u64
                || entry.pack_offset != expected_offset
            {
                return Err(ContentStoreError::Corruption(
                    "pack manifest entry order or offset mismatch".to_string(),
                ));
            }
            expected_offset = expected_offset
                .checked_add(entry.stored_length)
                .ok_or_else(|| {
                    ContentStoreError::InvalidEntry("manifest length overflow".to_string())
                })?;
        }
        if expected_offset != self.stored_bytes {
            return Err(ContentStoreError::Corruption(
                "pack manifest length mismatch".to_string(),
            ));
        }
        Ok(())
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum EqualityDecision {
    Reuse,
    Distinct,
}

pub fn compare_for_reuse<R1: Read, R2: Read>(
    existing_identity: &BodyIdentity,
    mut existing: R1,
    candidate_identity: &BodyIdentity,
    mut candidate: R2,
    buffer_bytes: usize,
) -> Result<EqualityDecision> {
    if existing_identity != candidate_identity {
        return Ok(EqualityDecision::Distinct);
    }
    let buffer_bytes = checked_buffer_size(buffer_bytes)?;
    let mut left = vec![0_u8; buffer_bytes];
    let mut right = vec![0_u8; buffer_bytes];
    loop {
        let left_read = read_chunk(&mut existing, &mut left)?;
        let right_read = read_chunk(&mut candidate, &mut right)?;
        if left_read != right_read || left[..left_read] != right[..right_read] {
            return Err(ContentStoreError::DigestCollision);
        }
        if left_read == 0 {
            return Ok(EqualityDecision::Reuse);
        }
    }
}

#[derive(Debug, Default)]
pub struct ContentStoreMetrics {
    unique_logical_bytes: AtomicU64,
    stored_bytes: AtomicU64,
    inline_count: AtomicU64,
    packed_count: AtomicU64,
    dedup_hits: AtomicU64,
    corrupt_entries: AtomicU64,
    dead_bytes: AtomicU64,
    reclaimed_bytes: AtomicU64,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct ContentStoreMetricsSnapshot {
    pub unique_logical_bytes: u64,
    pub stored_bytes: u64,
    pub inline_count: u64,
    pub packed_count: u64,
    pub dedup_hits: u64,
    pub corrupt_entries: u64,
    pub dead_bytes: u64,
    pub reclaimed_bytes: u64,
}

impl ContentStoreMetrics {
    pub fn record_inline(&self, logical_bytes: u64, stored_bytes: u64) {
        self.unique_logical_bytes
            .fetch_add(logical_bytes, Ordering::Relaxed);
        self.stored_bytes.fetch_add(stored_bytes, Ordering::Relaxed);
        self.inline_count.fetch_add(1, Ordering::Relaxed);
        metrics::counter!("storage_v2_content_inline_total").increment(1);
        metrics::counter!("storage_v2_content_unique_logical_bytes_total").increment(logical_bytes);
        metrics::counter!("storage_v2_content_stored_bytes_total").increment(stored_bytes);
    }

    pub fn record_packed(&self, logical_bytes: u64, stored_bytes: u64) {
        self.unique_logical_bytes
            .fetch_add(logical_bytes, Ordering::Relaxed);
        self.stored_bytes.fetch_add(stored_bytes, Ordering::Relaxed);
        self.packed_count.fetch_add(1, Ordering::Relaxed);
        metrics::counter!("storage_v2_content_packed_total").increment(1);
        metrics::counter!("storage_v2_content_unique_logical_bytes_total").increment(logical_bytes);
        metrics::counter!("storage_v2_content_stored_bytes_total").increment(stored_bytes);
    }

    pub fn record_dedup_hit(&self) {
        self.dedup_hits.fetch_add(1, Ordering::Relaxed);
        metrics::counter!("storage_v2_content_dedup_hits_total").increment(1);
    }

    pub fn record_corruption(&self) {
        self.corrupt_entries.fetch_add(1, Ordering::Relaxed);
        metrics::counter!("storage_v2_content_corrupt_entries_total").increment(1);
    }

    pub fn record_dead_bytes(&self, bytes: u64) {
        self.dead_bytes.fetch_add(bytes, Ordering::Relaxed);
        metrics::counter!("storage_v2_content_dead_bytes_total").increment(bytes);
    }

    pub fn record_reclaimed_bytes(&self, bytes: u64) {
        self.reclaimed_bytes.fetch_add(bytes, Ordering::Relaxed);
        metrics::counter!("storage_v2_content_reclaimed_bytes_total").increment(bytes);
    }

    pub fn snapshot(&self) -> ContentStoreMetricsSnapshot {
        ContentStoreMetricsSnapshot {
            unique_logical_bytes: self.unique_logical_bytes.load(Ordering::Relaxed),
            stored_bytes: self.stored_bytes.load(Ordering::Relaxed),
            inline_count: self.inline_count.load(Ordering::Relaxed),
            packed_count: self.packed_count.load(Ordering::Relaxed),
            dedup_hits: self.dedup_hits.load(Ordering::Relaxed),
            corrupt_entries: self.corrupt_entries.load(Ordering::Relaxed),
            dead_bytes: self.dead_bytes.load(Ordering::Relaxed),
            reclaimed_bytes: self.reclaimed_bytes.load(Ordering::Relaxed),
        }
    }
}

pub struct PackBuilder {
    root: PathBuf,
    build_dir: PathBuf,
    candidate_path: PathBuf,
    final_path: PathBuf,
    pack_id: Uuid,
    file: File,
    entries: Vec<PackEntry>,
    offset: u64,
    buffer_bytes: usize,
    handed_off: bool,
}

impl PackBuilder {
    pub fn new(
        root: impl AsRef<Path>,
        pack_id: Uuid,
        build_nonce: Uuid,
        buffer_bytes: usize,
    ) -> Result<Self> {
        let buffer_bytes = checked_buffer_size(buffer_bytes)?;
        let root = root.as_ref().to_path_buf();
        fs::create_dir_all(&root)?;
        let build_root = root.join(".building");
        fs::create_dir_all(&build_root)?;
        let build_dir = build_root.join(build_nonce.to_string());
        fs::create_dir(&build_dir)?;
        let candidate_path = build_dir.join(format!("{pack_id}.candidate"));
        let final_path = root.join(format!("{pack_id}.pack"));
        let file = OpenOptions::new()
            .create_new(true)
            .write(true)
            .open(&candidate_path)?;
        Ok(Self {
            root,
            build_dir,
            candidate_path,
            final_path,
            pack_id,
            file,
            entries: Vec::new(),
            offset: 0,
            buffer_bytes,
            handed_off: false,
        })
    }

    pub fn add_reader<R: Read>(
        &mut self,
        reader: R,
        codec: BodyCodec,
        dictionary: Option<(DictionaryIdentity, &[u8])>,
    ) -> Result<PackEntry> {
        if codec == BodyCodec::Identity && dictionary.is_some() {
            return Err(ContentStoreError::InvalidEntry(
                "identity codec cannot use a dictionary".to_string(),
            ));
        }
        if let Some((identity, bytes)) = dictionary.as_ref() {
            identity.verify(bytes)?;
        }

        let ordinal = self.entries.len() as u64;
        let raw_path = self.build_dir.join(format!("entry-{ordinal}.raw"));
        let mut raw = OpenOptions::new()
            .create_new(true)
            .write(true)
            .open(&raw_path)?;
        let (logical_length, body_digest) =
            copy_hash_bounded(reader, &mut raw, self.buffer_bytes, None)?;
        raw.sync_all()?;
        drop(raw);
        if logical_length == 0 && codec == BodyCodec::Identity {
            fs::remove_file(&raw_path)?;
            return Err(ContentStoreError::InvalidEntry(
                "empty bodies must use inline storage or a framed codec".to_string(),
            ));
        }

        let raw_reader = BufReader::with_capacity(self.buffer_bytes, File::open(&raw_path)?);
        let mut encoded: Box<dyn Read + '_> = match (codec, dictionary.as_ref()) {
            (BodyCodec::Identity, None) => Box::new(raw_reader),
            (BodyCodec::Zstd, Some((_, bytes))) => Box::new(
                zstd::stream::read::Encoder::with_dictionary(raw_reader, 3, bytes)?,
            ),
            (BodyCodec::Zstd, None) => Box::new(zstd::stream::read::Encoder::new(raw_reader, 3)?),
            (BodyCodec::Identity, Some(_)) => unreachable!(),
        };
        let (stored_length, entry_digest) =
            copy_hash_bounded(&mut encoded, &mut self.file, self.buffer_bytes, None)?;
        fs::remove_file(&raw_path)?;
        if stored_length == 0 {
            return Err(ContentStoreError::InvalidEntry(
                "pack entry stored length must be positive".to_string(),
            ));
        }
        let entry = PackEntry {
            pack_id: self.pack_id,
            ordinal,
            body: BodyIdentity {
                digest_algorithm: "sha256-v1",
                digest: body_digest,
                logical_length,
            },
            pack_offset: self.offset,
            stored_length,
            codec,
            dictionary: dictionary.map(|(identity, _)| identity),
            entry_digest,
        };
        self.offset = self
            .offset
            .checked_add(stored_length)
            .ok_or_else(|| ContentStoreError::InvalidEntry("pack length overflow".to_string()))?;
        self.entries.push(entry.clone());
        Ok(entry)
    }

    pub fn seal(mut self) -> Result<SealedPack> {
        if self.entries.is_empty() {
            return Err(ContentStoreError::InvalidEntry(
                "a pack must contain at least one entry".to_string(),
            ));
        }
        self.file.flush()?;
        self.file.sync_all()?;
        let manifest = PackManifest {
            pack_id: self.pack_id,
            stored_bytes: self.offset,
            sha256: manifest_digest(self.pack_id, &self.entries, self.offset),
            entries: self.entries.clone(),
            managed_peak_buffer_bytes: self.buffer_bytes,
        };
        manifest.verify()?;
        let metadata_length = fs::metadata(&self.candidate_path)?.len();
        if metadata_length != manifest.stored_bytes {
            return Err(ContentStoreError::Corruption(
                "candidate length differs from manifest".to_string(),
            ));
        }
        self.handed_off = true;
        Ok(SealedPack {
            root: self.root.clone(),
            build_dir: self.build_dir.clone(),
            candidate_path: self.candidate_path.clone(),
            final_path: self.final_path.clone(),
            manifest,
            published: false,
        })
    }
}

impl Drop for PackBuilder {
    fn drop(&mut self) {
        if !self.handed_off {
            let _ = fs::remove_dir_all(&self.build_dir);
        }
    }
}

pub struct SealedPack {
    root: PathBuf,
    build_dir: PathBuf,
    candidate_path: PathBuf,
    final_path: PathBuf,
    pub manifest: PackManifest,
    published: bool,
}

impl SealedPack {
    pub fn candidate_path(&self) -> &Path {
        &self.candidate_path
    }

    pub fn verify_entry(
        &self,
        entry: &PackEntry,
        dictionary: Option<&[u8]>,
    ) -> Result<VerifiedBody> {
        PackReader::new(
            &self.candidate_path,
            self.manifest.pack_id,
            self.manifest.stored_bytes,
        )
        .verify_to_staging(entry, dictionary, &self.build_dir, DEFAULT_IO_BUFFER_BYTES)
    }

    pub fn publish(mut self) -> Result<PublishedPack> {
        if self.final_path.exists() {
            return Err(ContentStoreError::InvalidEntry(
                "final pack path already exists".to_string(),
            ));
        }
        fs::rename(&self.candidate_path, &self.final_path)?;
        File::open(&self.root)?.sync_all()?;
        fs::remove_dir(&self.build_dir)?;
        self.published = true;
        Ok(PublishedPack {
            path: self.final_path.clone(),
            manifest: self.manifest.clone(),
        })
    }
}

impl Drop for SealedPack {
    fn drop(&mut self) {
        if !self.published {
            let _ = fs::remove_dir_all(&self.build_dir);
        }
    }
}

#[derive(Debug, Clone)]
pub struct PublishedPack {
    pub path: PathBuf,
    pub manifest: PackManifest,
}

impl PublishedPack {
    pub fn reader(&self) -> PackReader {
        PackReader::new(
            &self.path,
            self.manifest.pack_id,
            self.manifest.stored_bytes,
        )
    }

    /// Removes bytes only after `db::content_body::reclaim_pack` has advanced
    /// the authoritative database state for this exact pack.
    pub fn remove_after_database_reclamation(self) -> Result<u64> {
        let stored_bytes = fs::metadata(&self.path)?.len();
        if stored_bytes != self.manifest.stored_bytes {
            return Err(ContentStoreError::Corruption(
                "reclaimed pack length changed before removal".to_string(),
            ));
        }
        fs::remove_file(&self.path)?;
        File::open(
            self.path
                .parent()
                .ok_or_else(|| ContentStoreError::InvalidEntry("pack has no parent".to_string()))?,
        )?
        .sync_all()?;
        Ok(stored_bytes)
    }
}

pub struct PackReader {
    path: PathBuf,
    pack_id: Uuid,
    declared_length: u64,
}

impl PackReader {
    pub fn new(path: impl AsRef<Path>, pack_id: Uuid, declared_length: u64) -> Self {
        Self {
            path: path.as_ref().to_path_buf(),
            pack_id,
            declared_length,
        }
    }

    pub fn verify_to_staging(
        &self,
        entry: &PackEntry,
        dictionary: Option<&[u8]>,
        staging_root: impl AsRef<Path>,
        buffer_bytes: usize,
    ) -> Result<VerifiedBody> {
        let buffer_bytes = checked_buffer_size(buffer_bytes)?;
        self.validate_bounds(entry)?;
        let actual_entry_digest = hash_range(
            &self.path,
            entry.pack_offset,
            entry.stored_length,
            buffer_bytes,
        )?;
        if actual_entry_digest != entry.entry_digest {
            return Err(ContentStoreError::Corruption(
                "stored entry digest mismatch".to_string(),
            ));
        }
        match (&entry.dictionary, dictionary) {
            (Some(expected), Some(bytes)) => expected.verify(bytes)?,
            (Some(_), None) | (None, Some(_)) => return Err(ContentStoreError::DictionaryMismatch),
            (None, None) => {}
        }

        let staging_root = staging_root.as_ref();
        fs::create_dir_all(staging_root)?;
        let staging_path = staging_root.join(format!("verified-{}.body", Uuid::new_v4()));
        let mut staging = OpenOptions::new()
            .create_new(true)
            .write(true)
            .open(&staging_path)?;
        let file = File::open(&self.path)?;
        let range = BufReader::with_capacity(
            buffer_bytes,
            RangeReader::new(file, entry.pack_offset, entry.stored_length)?,
        );
        let mut decoded: Box<dyn Read + '_> = match (entry.codec, dictionary) {
            (BodyCodec::Identity, None) => Box::new(range),
            (BodyCodec::Zstd, Some(bytes)) => {
                Box::new(zstd::stream::read::Decoder::with_dictionary(range, bytes)?)
            }
            (BodyCodec::Zstd, None) => Box::new(zstd::stream::read::Decoder::new(range)?),
            (BodyCodec::Identity, Some(_)) => return Err(ContentStoreError::DictionaryMismatch),
        };
        let decode_result = copy_hash_bounded(
            &mut decoded,
            &mut staging,
            buffer_bytes,
            Some(entry.body.logical_length),
        );
        let (logical_length, logical_digest) = match decode_result {
            Ok(value) => value,
            Err(error) => {
                drop(staging);
                let _ = fs::remove_file(&staging_path);
                return Err(error);
            }
        };
        staging.sync_all()?;
        if logical_length != entry.body.logical_length || logical_digest != entry.body.digest {
            drop(staging);
            let _ = fs::remove_file(&staging_path);
            return Err(ContentStoreError::Corruption(
                "decoded body identity mismatch".to_string(),
            ));
        }
        Ok(VerifiedBody {
            path: staging_path,
            logical_length,
            digest: logical_digest,
            managed_peak_buffer_bytes: buffer_bytes,
            buffer_bytes,
        })
    }

    fn validate_bounds(&self, entry: &PackEntry) -> Result<()> {
        if entry.pack_id != self.pack_id || entry.stored_length == 0 {
            return Err(ContentStoreError::InvalidEntry(
                "entry belongs to a different pack or has zero length".to_string(),
            ));
        }
        let end = entry
            .pack_offset
            .checked_add(entry.stored_length)
            .ok_or_else(|| ContentStoreError::InvalidEntry("entry range overflow".to_string()))?;
        let actual_length = fs::metadata(&self.path)?.len();
        if actual_length != self.declared_length || end > self.declared_length {
            return Err(ContentStoreError::Corruption(
                "pack length or entry bounds mismatch".to_string(),
            ));
        }
        Ok(())
    }
}

pub struct VerifiedBody {
    path: PathBuf,
    pub logical_length: u64,
    pub digest: [u8; 32],
    pub managed_peak_buffer_bytes: usize,
    buffer_bytes: usize,
}

impl VerifiedBody {
    pub fn copy_to<W: Write>(&self, mut writer: W) -> Result<u64> {
        let mut source = File::open(&self.path)?;
        let (written, digest) = copy_hash_bounded(
            &mut source,
            &mut writer,
            self.buffer_bytes,
            Some(self.logical_length),
        )?;
        if written != self.logical_length || digest != self.digest {
            return Err(ContentStoreError::Corruption(
                "verified staging body changed before delivery".to_string(),
            ));
        }
        Ok(written)
    }
}

impl Drop for VerifiedBody {
    fn drop(&mut self) {
        let _ = fs::remove_file(&self.path);
    }
}

struct RangeReader {
    file: File,
    remaining: u64,
}

impl RangeReader {
    fn new(mut file: File, offset: u64, length: u64) -> io::Result<Self> {
        file.seek(SeekFrom::Start(offset))?;
        Ok(Self {
            file,
            remaining: length,
        })
    }
}

impl Read for RangeReader {
    fn read(&mut self, buffer: &mut [u8]) -> io::Result<usize> {
        if self.remaining == 0 {
            return Ok(0);
        }
        let maximum = usize::try_from(self.remaining.min(buffer.len() as u64))
            .map_err(|_| io::Error::other("range length does not fit usize"))?;
        let read = self.file.read(&mut buffer[..maximum])?;
        self.remaining = self.remaining.saturating_sub(read as u64);
        Ok(read)
    }
}

fn checked_buffer_size(buffer_bytes: usize) -> Result<usize> {
    if !(4096..=1024 * 1024).contains(&buffer_bytes) {
        return Err(ContentStoreError::InvalidEntry(
            "I/O buffer must be between 4096 and 1048576 bytes".to_string(),
        ));
    }
    Ok(buffer_bytes)
}

fn read_chunk<R: Read>(reader: &mut R, buffer: &mut [u8]) -> io::Result<usize> {
    loop {
        match reader.read(buffer) {
            Ok(0) => return Ok(0),
            Ok(read) => return Ok(read),
            Err(error) if error.kind() == io::ErrorKind::Interrupted => continue,
            Err(error) => return Err(error),
        }
    }
}

fn copy_hash_bounded<R: Read, W: Write>(
    mut reader: R,
    mut writer: W,
    buffer_bytes: usize,
    logical_limit: Option<u64>,
) -> Result<(u64, [u8; 32])> {
    let buffer_bytes = checked_buffer_size(buffer_bytes)?;
    let mut buffer = vec![0_u8; buffer_bytes];
    let mut hasher = Sha256::new();
    let mut total = 0_u64;
    loop {
        let read = read_chunk(&mut reader, &mut buffer)?;
        if read == 0 {
            break;
        }
        total = total
            .checked_add(read as u64)
            .ok_or_else(|| ContentStoreError::InvalidEntry("byte count overflow".to_string()))?;
        if logical_limit.is_some_and(|limit| total > limit) {
            return Err(ContentStoreError::Corruption(
                "decoded body exceeds declared logical length".to_string(),
            ));
        }
        hasher.update(&buffer[..read]);
        writer.write_all(&buffer[..read])?;
    }
    Ok((total, hasher.finalize().into()))
}

fn hash_range(path: &Path, offset: u64, length: u64, buffer_bytes: usize) -> Result<[u8; 32]> {
    let file = File::open(path)?;
    let range = RangeReader::new(file, offset, length)?;
    let mut sink = io::sink();
    let (read, digest) = copy_hash_bounded(range, &mut sink, buffer_bytes, Some(length))?;
    if read != length {
        return Err(ContentStoreError::Corruption(
            "truncated pack entry".to_string(),
        ));
    }
    Ok(digest)
}

fn manifest_digest(pack_id: Uuid, entries: &[PackEntry], stored_bytes: u64) -> [u8; 32] {
    let mut hasher = Sha256::new();
    hasher.update(MANIFEST_DOMAIN);
    hasher.update(pack_id.as_bytes());
    hasher.update((entries.len() as u64).to_be_bytes());
    hasher.update(stored_bytes.to_be_bytes());
    for entry in entries {
        hasher.update(entry.ordinal.to_be_bytes());
        hasher.update(entry.body.digest_algorithm.as_bytes());
        hasher.update([0]);
        hasher.update(entry.body.digest);
        hasher.update(entry.body.logical_length.to_be_bytes());
        hasher.update(entry.pack_offset.to_be_bytes());
        hasher.update(entry.stored_length.to_be_bytes());
        hasher.update([entry.codec.manifest_tag()]);
        match &entry.dictionary {
            Some(dictionary) => {
                hasher.update([1]);
                hasher.update(dictionary.id.to_be_bytes());
                hasher.update(dictionary.digest);
            }
            None => hasher.update([0]),
        }
        hasher.update(entry.entry_digest);
    }
    hasher.finalize().into()
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Cursor;
    use std::time::Instant;

    struct RepeatedByteReader {
        remaining: u64,
        byte: u8,
    }

    impl Read for RepeatedByteReader {
        fn read(&mut self, buffer: &mut [u8]) -> io::Result<usize> {
            let read = usize::try_from(self.remaining.min(buffer.len() as u64)).unwrap();
            buffer[..read].fill(self.byte);
            self.remaining -= read as u64;
            Ok(read)
        }
    }

    fn test_root() -> PathBuf {
        let root = std::env::temp_dir().join(format!("mainrag-content-store-{}", Uuid::new_v4()));
        fs::create_dir(&root).unwrap();
        root
    }

    fn cleanup(root: &Path) {
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn full_byte_comparison_reuses_only_identical_bytes() {
        let identity = BodyIdentity::from_bytes(b"same bytes");
        assert_eq!(
            compare_for_reuse(
                &identity,
                Cursor::new(b"same bytes"),
                &identity,
                Cursor::new(b"same bytes"),
                4096,
            )
            .unwrap(),
            EqualityDecision::Reuse
        );

        let forced_collision = BodyIdentity {
            digest_algorithm: identity.digest_algorithm,
            digest: identity.digest,
            logical_length: identity.logical_length,
        };
        assert!(matches!(
            compare_for_reuse(
                &identity,
                Cursor::new(b"same bytes"),
                &forced_collision,
                Cursor::new(b"evil bytes"),
                4096,
            ),
            Err(ContentStoreError::DigestCollision)
        ));
    }

    #[test]
    fn pack_round_trip_is_bounded_and_atomically_published() {
        let root = test_root();
        let pack_id = Uuid::new_v4();
        let mut builder = PackBuilder::new(&root, pack_id, Uuid::new_v4(), 8192).unwrap();
        let bytes = vec![b'x'; 5 * 1024 * 1024];
        let entry = builder
            .add_reader(Cursor::new(&bytes), BodyCodec::Zstd, None)
            .unwrap();
        let sealed = builder.seal().unwrap();
        assert!(!root.join(format!("{pack_id}.pack")).exists());
        let staged = sealed.verify_entry(&entry, None).unwrap();
        assert_eq!(staged.managed_peak_buffer_bytes, DEFAULT_IO_BUFFER_BYTES);
        let mut output = Vec::new();
        assert_eq!(staged.copy_to(&mut output).unwrap(), bytes.len() as u64);
        assert_eq!(output, bytes);
        drop(staged);
        let published = sealed.publish().unwrap();
        assert!(published.path.exists());
        assert_eq!(published.manifest.managed_peak_buffer_bytes, 8192);
        cleanup(&root);
    }

    #[test]
    fn corruption_codec_bounds_and_dictionary_fail_closed() {
        let root = test_root();
        let pack_id = Uuid::new_v4();
        let dictionary = b"public synthetic dictionary words words words";
        let dictionary_identity = DictionaryIdentity {
            id: 7,
            digest: Sha256::digest(dictionary).into(),
        };
        let mut builder = PackBuilder::new(&root, pack_id, Uuid::new_v4(), 4096).unwrap();
        let first = builder
            .add_reader(
                Cursor::new(b"alpha alpha alpha alpha"),
                BodyCodec::Zstd,
                Some((dictionary_identity.clone(), dictionary)),
            )
            .unwrap();
        let second = builder
            .add_reader(Cursor::new(b"beta beta beta beta"), BodyCodec::Zstd, None)
            .unwrap();
        let published = builder.seal().unwrap().publish().unwrap();
        published.manifest.verify().unwrap();

        assert!(matches!(
            published
                .reader()
                .verify_to_staging(&first, Some(b"wrong dictionary"), &root, 4096,),
            Err(ContentStoreError::DictionaryMismatch)
        ));

        let mut wrong_codec = second.clone();
        wrong_codec.codec = BodyCodec::Identity;
        assert!(matches!(
            published
                .reader()
                .verify_to_staging(&wrong_codec, None, &root, 4096),
            Err(ContentStoreError::Corruption(_))
        ));

        let mut reordered = first.clone();
        reordered.pack_offset = second.pack_offset;
        reordered.stored_length = second.stored_length;
        reordered.entry_digest = second.entry_digest;
        assert!(matches!(
            published
                .reader()
                .verify_to_staging(&reordered, Some(dictionary), &root, 4096,),
            Err(ContentStoreError::Corruption(_))
        ));

        let mut file = OpenOptions::new()
            .write(true)
            .open(&published.path)
            .unwrap();
        file.seek(SeekFrom::Start(second.pack_offset)).unwrap();
        let mut byte = [0_u8; 1];
        File::open(&published.path)
            .unwrap()
            .seek(SeekFrom::Start(second.pack_offset))
            .unwrap();
        byte[0] = 0xff;
        file.write_all(&byte).unwrap();
        file.sync_all().unwrap();
        assert!(matches!(
            published
                .reader()
                .verify_to_staging(&second, None, &root, 4096),
            Err(ContentStoreError::Corruption(_))
        ));

        file.set_len(published.manifest.stored_bytes - 1).unwrap();
        assert!(matches!(
            published
                .reader()
                .verify_to_staging(&first, Some(dictionary), &root, 4096),
            Err(ContentStoreError::Corruption(_))
        ));
        cleanup(&root);
    }

    #[test]
    fn representative_distribution_records_ratio_throughput_and_peak_buffer() {
        let root = test_root();
        let pack_id = Uuid::new_v4();
        let buffer_bytes = 32 * 1024;
        let sizes = [4 * 1024_u64, 256 * 1024, 16 * 1024 * 1024];
        let logical_bytes: u64 = sizes.iter().sum();
        let started = Instant::now();
        let mut builder = PackBuilder::new(&root, pack_id, Uuid::new_v4(), buffer_bytes).unwrap();
        for (index, size) in sizes.into_iter().enumerate() {
            builder
                .add_reader(
                    RepeatedByteReader {
                        remaining: size,
                        byte: b'a' + index as u8,
                    },
                    BodyCodec::Zstd,
                    None,
                )
                .unwrap();
        }
        let published = builder.seal().unwrap().publish().unwrap();
        let mut decoded_bytes = 0_u64;
        for entry in &published.manifest.entries {
            let verified = published
                .reader()
                .verify_to_staging(entry, None, &root, buffer_bytes)
                .unwrap();
            decoded_bytes += verified.copy_to(io::sink()).unwrap();
        }
        let elapsed = started.elapsed();
        let ratio = published.manifest.stored_bytes as f64 / logical_bytes as f64;
        let throughput_mib_s = logical_bytes as f64 / elapsed.as_secs_f64() / 1_048_576.0;
        println!(
            "storage-v2 pack fixture: logical_bytes={logical_bytes} stored_bytes={} ratio={ratio:.6} throughput_mib_s={throughput_mib_s:.3} managed_peak_buffer_bytes={buffer_bytes}",
            published.manifest.stored_bytes
        );
        assert_eq!(decoded_bytes, logical_bytes);
        assert!(ratio < 0.01);
        assert!(throughput_mib_s.is_finite() && throughput_mib_s > 0.0);
        assert_eq!(published.manifest.managed_peak_buffer_bytes, buffer_bytes);
        cleanup(&root);
    }

    #[test]
    fn interrupted_build_is_identifiable_and_cleaned() {
        let root = test_root();
        let build_nonce = Uuid::new_v4();
        let build_dir = root.join(".building").join(build_nonce.to_string());
        {
            let mut builder = PackBuilder::new(&root, Uuid::new_v4(), build_nonce, 4096).unwrap();
            builder
                .add_reader(Cursor::new(b"candidate"), BodyCodec::Zstd, None)
                .unwrap();
            assert!(build_dir.exists());
        }
        assert!(!build_dir.exists());
        cleanup(&root);
    }

    #[test]
    fn metrics_keep_logical_stored_dead_and_reclaimed_dimensions_separate() {
        let metrics = ContentStoreMetrics::default();
        metrics.record_inline(10, 10);
        metrics.record_packed(100, 40);
        metrics.record_dedup_hit();
        metrics.record_corruption();
        metrics.record_dead_bytes(12);
        metrics.record_reclaimed_bytes(12);
        assert_eq!(
            metrics.snapshot(),
            ContentStoreMetricsSnapshot {
                unique_logical_bytes: 110,
                stored_bytes: 50,
                inline_count: 1,
                packed_count: 1,
                dedup_hits: 1,
                corrupt_entries: 1,
                dead_bytes: 12,
                reclaimed_bytes: 12,
            }
        );
    }
}

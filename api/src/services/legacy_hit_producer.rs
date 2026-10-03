//! Byte proofs for bounded legacy hit batches. Rank snapshots are not byte proofs.
//!
//! Scan verified native bytes once for every pattern in a batch. The caller must
//! finish PackReader verification before applying a plan; an interrupted, corrupt
//! or ambiguous scan never authorizes a current-generation mapping.
use aho_corasick::AhoCorasick;
use anyhow::{bail, ensure, Context, Result};
use serde::Serialize;
use sha2::{Digest, Sha256};
use std::{
    collections::{BTreeMap, BTreeSet},
    io::Read,
};

const MAX_BATCH_BYTES: usize = 32 * 1024 * 1024;
const MAX_HITS: usize = 512;

pub mod operation;

#[cfg(test)]
mod operation_tests;

#[derive(Debug)]
pub struct VerifiedLegacyChunk {
    pub old_hit_id: String,
    bytes: Vec<u8>,
    digest: [u8; 32],
}

impl VerifiedLegacyChunk {
    /// Decode the stored original, reject bombs before allocation and check both
    /// recorded SHA-256 and the optional legacy text projection.
    pub fn decode(
        old_hit_id: String,
        compressed: &[u8],
        expected_sha256: [u8; 32],
        text: Option<&str>,
    ) -> Result<Self> {
        ensure!(
            !old_hit_id.is_empty() && old_hit_id.len() <= 512,
            "bounded old hit identity required"
        );
        let decoder = zstd::stream::read::Decoder::new(compressed)?;
        let mut bytes = Vec::new();
        decoder
            .take((MAX_BATCH_BYTES + 1) as u64)
            .read_to_end(&mut bytes)?;
        ensure!(
            bytes.len() <= MAX_BATCH_BYTES,
            "legacy chunk exceeds batch byte budget"
        );
        ensure!(
            <[u8; 32]>::from(Sha256::digest(&bytes)) == expected_sha256,
            "legacy chunk original digest differs"
        );
        if let Some(text) = text {
            ensure!(
                text.as_bytes() == bytes,
                "legacy chunk text differs from original bytes"
            );
        }
        Ok(Self {
            old_hit_id,
            bytes,
            digest: expected_sha256,
        })
    }

    pub fn bytes(&self) -> &[u8] {
        &self.bytes
    }
    pub fn digest(&self) -> [u8; 32] {
        self.digest
    }
}

#[derive(Debug, Clone, Copy)]
pub struct NativeFragment {
    pub occurrence_id: i64,
    pub start: u64,
    pub end: u64,
}

#[derive(Debug, Serialize, PartialEq)]
pub struct ByteTarget {
    pub occurrence_id: i64,
    pub byte_overlap: u64,
    /// Absolute byte offset in the proven native source file, not a line number.
    pub source_offset: u64,
}

#[derive(Debug, Serialize, PartialEq)]
pub struct HitByteProof {
    pub old_hit_id: String,
    pub chunk_sha256: String,
    pub byte_start: Option<u64>,
    pub byte_end: Option<u64>,
    pub requires_preserved_history: bool,
    pub targets: Vec<ByteTarget>,
}

#[derive(Debug, Serialize)]
pub struct FileByteProof {
    pub source_sha256: String,
    pub logical_bytes: u64,
    pub hits: Vec<HitByteProof>,
}

/// Empty or multiply occurring text has no unambiguous byte position. Preserve
/// its old native content instead of assigning a guessed current location.
pub fn align_verified_file<R: Read>(
    mut reader: R,
    expected_file_sha256: [u8; 32],
    fragments: &[NativeFragment],
    chunks: &[VerifiedLegacyChunk],
    buffer_bytes: usize,
) -> Result<FileByteProof> {
    ensure!(
        (4096..=1024 * 1024).contains(&buffer_bytes),
        "bounded source scan buffer required"
    );
    ensure!(
        !chunks.is_empty() && chunks.len() <= MAX_HITS,
        "bounded nonempty hit batch required"
    );
    let mut ids = BTreeSet::new();
    let mut pattern_bytes = 0_usize;
    let mut max_pattern = 1;
    for chunk in chunks {
        ensure!(
            ids.insert(&chunk.old_hit_id),
            "old hit identities must be unique"
        );
        pattern_bytes = pattern_bytes
            .checked_add(chunk.bytes.len())
            .context("pattern byte overflow")?;
        max_pattern = max_pattern.max(chunk.bytes.len());
    }
    ensure!(
        pattern_bytes <= MAX_BATCH_BYTES,
        "legacy pattern batch exceeds byte budget"
    );
    let mut offset = 0_u64;
    let mut occurrence_ids = BTreeSet::new();
    for fragment in fragments {
        ensure!(
            fragment.occurrence_id > 0
                && occurrence_ids.insert(fragment.occurrence_id)
                && fragment.start == offset
                && fragment.end >= fragment.start,
            "native source fragments have invalid identity, gaps or overlaps"
        );
        offset = fragment.end;
    }
    ensure!(!fragments.is_empty(), "native source has no fragments");
    // Empty patterns are deliberately excluded: an empty old chunk has no
    // unique source position and must retain an explicitly historical root.
    let mut grouped = BTreeMap::<&[u8], Vec<usize>>::new();
    for (index, chunk) in chunks
        .iter()
        .enumerate()
        .filter(|(_, chunk)| !chunk.bytes.is_empty())
    {
        grouped
            .entry(chunk.bytes.as_slice())
            .or_default()
            .push(index);
    }
    let patterns = grouped.into_iter().collect::<Vec<_>>();
    let matcher = AhoCorasick::new(patterns.iter().map(|(bytes, _)| bytes))?;
    let mut first = vec![None; patterns.len()];
    let mut ambiguous = vec![false; patterns.len()];
    let mut source_digest = Sha256::new();
    let mut total = 0_u64;
    // Each scan adds at least one longest-pattern span. Otherwise a large
    // retained tail would be scanned again for every tiny I/O buffer. Both
    // patterns and this working buffer remain bounded by MAX_BATCH_BYTES.
    let scan_bytes = buffer_bytes.max(max_pattern);
    let mut window = Vec::with_capacity(scan_bytes + max_pattern);
    let mut buffer = vec![0; scan_bytes];
    loop {
        let mut size = 0;
        while size < buffer.len() {
            match reader.read(&mut buffer[size..]) {
                Ok(0) => break,
                Ok(read) => size += read,
                Err(error) if error.kind() == std::io::ErrorKind::Interrupted => continue,
                Err(error) => return Err(error.into()),
            }
        }
        if size == 0 {
            break;
        }
        let prior_total = total;
        let base = prior_total
            .checked_sub(window.len() as u64)
            .context("source window overflow")?;
        source_digest.update(&buffer[..size]);
        total = total
            .checked_add(size as u64)
            .context("source byte count overflow")?;
        window.extend_from_slice(&buffer[..size]);
        for found in matcher.find_overlapping_iter(&window) {
            let end = base + found.end() as u64;
            // A match wholly inside the retained tail was counted previously.
            if end <= prior_total {
                continue;
            }
            let index = found.pattern().as_usize();
            let start = base + found.start() as u64;
            if first[index].is_some_and(|previous| previous != start) {
                ambiguous[index] = true;
            } else {
                first[index] = Some(start);
            }
        }
        let keep = window.len().min(max_pattern - 1);
        let tail_start = window.len() - keep;
        window.copy_within(tail_start.., 0);
        window.truncate(keep);
    }
    ensure!(
        total == offset && <[u8; 32]>::from(source_digest.finalize()) == expected_file_sha256,
        "complete native source length or digest differs"
    );
    let mut hits = Vec::with_capacity(chunks.len());
    let mut positions = vec![None; chunks.len()];
    for (index, (_, members)) in patterns.iter().enumerate() {
        for &member in members {
            positions[member] = first[index].filter(|_| !ambiguous[index]);
        }
    }
    for (index, chunk) in chunks.iter().enumerate() {
        let position = positions[index];
        let mut targets = Vec::new();
        if let Some(start) = position {
            let end = start
                .checked_add(chunk.bytes.len() as u64)
                .context("hit byte end overflow")?;
            for fragment in fragments {
                let overlap_start = start.max(fragment.start);
                let overlap_end = end.min(fragment.end);
                if overlap_start < overlap_end {
                    targets.push(ByteTarget {
                        occurrence_id: fragment.occurrence_id,
                        byte_overlap: overlap_end - overlap_start,
                        source_offset: overlap_start,
                    });
                }
            }
            if targets
                .iter()
                .map(|target| target.byte_overlap)
                .sum::<u64>()
                != chunk.bytes.len() as u64
            {
                bail!("native mapping does not cover every old chunk byte");
            }
            targets.sort_by_key(|target| {
                (
                    std::cmp::Reverse(target.byte_overlap),
                    target.source_offset,
                    target.occurrence_id,
                )
            });
        }
        hits.push(HitByteProof {
            old_hit_id: chunk.old_hit_id.clone(),
            chunk_sha256: hex::encode(chunk.digest),
            byte_start: position,
            byte_end: position.map(|start| start + chunk.bytes.len() as u64),
            requires_preserved_history: position.is_none(),
            targets,
        });
    }
    Ok(FileByteProof {
        source_sha256: hex::encode(expected_file_sha256),
        logical_bytes: total,
        hits,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Cursor;

    fn chunk(id: &str, bytes: &[u8]) -> VerifiedLegacyChunk {
        VerifiedLegacyChunk::decode(
            id.into(),
            &zstd::encode_all(bytes, 1).unwrap(),
            Sha256::digest(bytes).into(),
            None,
        )
        .unwrap()
    }

    #[test]
    fn byte_proofs_cover_unicode_boundary_splits_nested_and_duplicate_patterns() {
        let mut bytes = vec![b'x'; 4094];
        bytes.extend_from_slice("Über🙂tail\n".as_bytes());
        bytes.extend_from_slice(b"repeat repeat");
        let chunks = [
            chunk("full", "Über🙂tail".as_bytes()),
            chunk("nested", "🙂".as_bytes()),
            chunk("duplicate-a", "Über🙂tail".as_bytes()),
            chunk("repeat", b"repeat"),
            chunk("absent", b"absent"),
            chunk("empty", b""),
        ];
        let fragments = [
            NativeFragment {
                occurrence_id: 10,
                start: 0,
                end: 4098,
            },
            NativeFragment {
                occurrence_id: 20,
                start: 4098,
                end: bytes.len() as u64,
            },
        ];
        let plan = align_verified_file(
            Cursor::new(&bytes),
            Sha256::digest(&bytes).into(),
            &fragments,
            &chunks,
            4096,
        )
        .unwrap();
        assert_eq!(plan.hits[0].byte_start, Some(4094));
        assert_eq!(plan.hits[0].targets.len(), 2);
        assert_eq!(plan.hits[0].targets[0].occurrence_id, 20);
        assert_eq!(plan.hits[1].byte_start, Some(4099));
        assert_eq!(plan.hits[0].targets, plan.hits[2].targets);
        assert!(plan.hits[3..]
            .iter()
            .all(|hit| hit.requires_preserved_history && hit.targets.is_empty()));
    }

    #[test]
    fn corrupt_original_corrupt_source_and_incomplete_ranges_never_produce_mappings() {
        let encoded = zstd::encode_all(&b"original"[..], 1).unwrap();
        assert!(VerifiedLegacyChunk::decode("old".into(), &encoded, [0; 32], None).is_err());
        assert!(VerifiedLegacyChunk::decode(
            "old".into(),
            &encoded,
            Sha256::digest(b"original").into(),
            Some("different")
        )
        .is_err());
        let chunks = [chunk("old", b"original")];
        let valid = [NativeFragment {
            occurrence_id: 1,
            start: 0,
            end: 8,
        }];
        assert!(
            align_verified_file(Cursor::new(b"original"), [0; 32], &valid, &chunks, 4096).is_err()
        );
        assert!(align_verified_file(
            Cursor::new(b"original"),
            Sha256::digest(b"original").into(),
            &[NativeFragment {
                occurrence_id: 1,
                start: 1,
                end: 8
            }],
            &chunks,
            4096
        )
        .is_err());
        assert!(align_verified_file(
            Cursor::new(b"origina"),
            Sha256::digest(b"original").into(),
            &valid,
            &chunks,
            4096
        )
        .is_err());
    }
}

//! Pure storage-v2 content identities and lossless reconstruction.
//!
//! This module is additive: existing parser and chunker callers do not select
//! it unless a storage-v2 producer explicitly projects an artifact into this
//! representation.

use crate::services::content_store::BodyIdentity;
use serde_json::Value;
use sha2::{Digest, Sha256};
use std::collections::HashSet;
use std::io::{self, Write};
use thiserror::Error;

const CONTENT_NODE_DOMAIN: &[u8] = b"mainrag.content-node.v1";
const RETRIEVAL_VIEW_DOMAIN: &[u8] = b"mainrag.retrieval-view.v1";
const MAX_RECONSTRUCTION_DEPTH: usize = 1_024;

#[derive(Debug, Error)]
pub enum ContentGraphError {
    #[error("invalid content graph: {0}")]
    InvalidGraph(String),
    #[error("content graph digest mismatch")]
    DigestMismatch,
    #[error("body bytes do not match their declared identity")]
    BodyMismatch,
    #[error("artifact bytes do not match their expected identity")]
    ArtifactMismatch,
    #[error("content graph exceeds the reconstruction depth limit")]
    DepthLimit,
    #[error("I/O error: {0}")]
    Io(#[from] io::Error),
}

pub type Result<T> = std::result::Result<T, ContentGraphError>;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ContentChild {
    pub edge_type: String,
    pub node: Box<ContentNode>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
enum ContentNodeKind {
    Leaf(BodyIdentity),
    Internal(Vec<ContentChild>),
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ContentNode {
    domain: String,
    node_type: String,
    logical_length: u64,
    digest: [u8; 32],
    kind: ContentNodeKind,
}

impl ContentNode {
    pub fn leaf(
        domain: impl Into<String>,
        node_type: impl Into<String>,
        body: BodyIdentity,
    ) -> Result<Self> {
        let domain = nonempty(domain.into(), "content domain")?;
        let node_type = nonempty(node_type.into(), "node type")?;
        checked_i64(body.logical_length, "body length")?;
        let digest = leaf_digest(&domain, &node_type, &body)?;
        Ok(Self {
            domain,
            node_type,
            logical_length: body.logical_length,
            digest,
            kind: ContentNodeKind::Leaf(body),
        })
    }

    pub fn internal(
        domain: impl Into<String>,
        node_type: impl Into<String>,
        children: Vec<ContentChild>,
    ) -> Result<Self> {
        let domain = nonempty(domain.into(), "content domain")?;
        let node_type = nonempty(node_type.into(), "node type")?;
        if children.is_empty() {
            return Err(ContentGraphError::InvalidGraph(
                "internal nodes require at least one child".to_string(),
            ));
        }
        let mut logical_length = 0_u64;
        for child in &children {
            nonempty(child.edge_type.clone(), "edge type")?;
            logical_length = logical_length
                .checked_add(child.node.logical_length)
                .ok_or_else(|| {
                    ContentGraphError::InvalidGraph("node length overflow".to_string())
                })?;
        }
        checked_i64(logical_length, "node length")?;
        let digest = internal_digest(&domain, &node_type, logical_length, &children)?;
        Ok(Self {
            domain,
            node_type,
            logical_length,
            digest,
            kind: ContentNodeKind::Internal(children),
        })
    }

    pub fn digest(&self) -> [u8; 32] {
        self.digest
    }

    pub fn logical_length(&self) -> u64 {
        self.logical_length
    }

    pub fn node_type(&self) -> &str {
        &self.node_type
    }

    fn verify_identity(&self) -> Result<()> {
        let actual = match &self.kind {
            ContentNodeKind::Leaf(body) => leaf_digest(&self.domain, &self.node_type, body)?,
            ContentNodeKind::Internal(children) => {
                let total = children.iter().try_fold(0_u64, |total, child| {
                    total.checked_add(child.node.logical_length).ok_or_else(|| {
                        ContentGraphError::InvalidGraph("node length overflow".to_string())
                    })
                })?;
                if total != self.logical_length {
                    return Err(ContentGraphError::InvalidGraph(
                        "internal node length differs from its children".to_string(),
                    ));
                }
                internal_digest(&self.domain, &self.node_type, self.logical_length, children)?
            }
        };
        if actual != self.digest {
            return Err(ContentGraphError::DigestMismatch);
        }
        Ok(())
    }
}

#[derive(Debug, Clone, PartialEq)]
pub struct LosslessProviderBlock {
    pub provider: String,
    pub language_id: String,
    pub metadata: Value,
    pub exact_bytes: Vec<u8>,
}

impl LosslessProviderBlock {
    pub fn unknown(
        provider: impl Into<String>,
        metadata: Value,
        exact_bytes: Vec<u8>,
    ) -> Result<Self> {
        Ok(Self {
            provider: nonempty(provider.into(), "provider")?,
            language_id: "unknown".to_string(),
            metadata,
            exact_bytes,
        })
    }
}

pub fn leaf_digest(domain: &str, node_type: &str, body: &BodyIdentity) -> Result<[u8; 32]> {
    checked_i64(body.logical_length, "body length")?;
    Ok(hash_parts(
        CONTENT_NODE_DOMAIN,
        &[
            b"content-node-v1",
            domain.as_bytes(),
            node_type.as_bytes(),
            &body.logical_length.to_be_bytes(),
            body.digest_algorithm.as_bytes(),
            &body.digest,
            &body.logical_length.to_be_bytes(),
        ],
    ))
}

pub fn internal_digest(
    domain: &str,
    node_type: &str,
    logical_length: u64,
    children: &[ContentChild],
) -> Result<[u8; 32]> {
    checked_i64(logical_length, "node length")?;
    let logical_length_bytes = logical_length.to_be_bytes();
    let mut owned_parts = Vec::with_capacity(4 + children.len() * 3);
    owned_parts.push(b"content-node-v1".to_vec());
    owned_parts.push(domain.as_bytes().to_vec());
    owned_parts.push(node_type.as_bytes().to_vec());
    owned_parts.push(logical_length_bytes.to_vec());
    for child in children {
        if child.edge_type.is_empty() {
            return Err(ContentGraphError::InvalidGraph(
                "empty edge type".to_string(),
            ));
        }
        owned_parts.push(child.edge_type.as_bytes().to_vec());
        owned_parts.push(child.node.node_type.as_bytes().to_vec());
        owned_parts.push(child.node.digest.to_vec());
    }
    let parts: Vec<&[u8]> = owned_parts.iter().map(Vec::as_slice).collect();
    Ok(hash_parts(CONTENT_NODE_DOMAIN, &parts))
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ViewComponentIdentity {
    Body(BodyIdentity),
    Node { node_type: String, digest: [u8; 32] },
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ViewComponent {
    pub role: String,
    pub identity: ViewComponentIdentity,
    pub relative_start: u64,
    pub relative_end: u64,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RetrievalViewIdentity {
    pub view_type: String,
    pub profile_id: String,
    pub language_id: String,
    pub tokenizer_version: String,
    pub capability_flags: u64,
    pub components: Vec<ViewComponent>,
    pub digest: [u8; 32],
}

impl RetrievalViewIdentity {
    pub fn new(
        view_type: impl Into<String>,
        profile_id: impl Into<String>,
        language_id: impl Into<String>,
        tokenizer_version: impl Into<String>,
        capability_flags: u64,
        components: Vec<ViewComponent>,
    ) -> Result<Self> {
        let view_type = nonempty(view_type.into(), "view type")?;
        let profile_id = nonempty(profile_id.into(), "profile ID")?;
        let language_id = nonempty(language_id.into(), "language ID")?;
        let tokenizer_version = nonempty(tokenizer_version.into(), "tokenizer version")?;
        checked_i64(capability_flags, "capability flags")?;
        if components.is_empty() {
            return Err(ContentGraphError::InvalidGraph(
                "retrieval views require at least one component".to_string(),
            ));
        }
        let digest = retrieval_view_digest(
            &view_type,
            &profile_id,
            &language_id,
            &tokenizer_version,
            capability_flags,
            &components,
        )?;
        Ok(Self {
            view_type,
            profile_id,
            language_id,
            tokenizer_version,
            capability_flags,
            components,
            digest,
        })
    }
}

pub fn retrieval_view_digest(
    view_type: &str,
    profile_id: &str,
    language_id: &str,
    tokenizer_version: &str,
    capability_flags: u64,
    components: &[ViewComponent],
) -> Result<[u8; 32]> {
    checked_i64(capability_flags, "capability flags")?;
    let mut owned_parts = vec![
        b"retrieval-view-v1".to_vec(),
        view_type.as_bytes().to_vec(),
        profile_id.as_bytes().to_vec(),
        language_id.as_bytes().to_vec(),
        tokenizer_version.as_bytes().to_vec(),
        capability_flags.to_be_bytes().to_vec(),
    ];
    for component in components {
        if component.role.is_empty() || component.relative_end < component.relative_start {
            return Err(ContentGraphError::InvalidGraph(
                "invalid retrieval-view component".to_string(),
            ));
        }
        checked_i64(component.relative_start, "component start")?;
        checked_i64(component.relative_end, "component end")?;
        let (kind, digest) = match &component.identity {
            ViewComponentIdentity::Body(body) => (b"body".as_slice(), body.digest),
            ViewComponentIdentity::Node { digest, .. } => (b"node".as_slice(), *digest),
        };
        owned_parts.push(component.role.as_bytes().to_vec());
        owned_parts.push(kind.to_vec());
        owned_parts.push(digest.to_vec());
        owned_parts.push(component.relative_start.to_be_bytes().to_vec());
        owned_parts.push(component.relative_end.to_be_bytes().to_vec());
    }
    let parts: Vec<&[u8]> = owned_parts.iter().map(Vec::as_slice).collect();
    Ok(hash_parts(RETRIEVAL_VIEW_DOMAIN, &parts))
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum LegacyRelation {
    Exact,
    Split,
    Merged,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct LegacyTarget {
    pub occurrence_id: i64,
    pub byte_overlap: u64,
    pub source_offset: u64,
}

pub fn order_legacy_targets(
    relation: LegacyRelation,
    mut targets: Vec<LegacyTarget>,
) -> Result<Vec<LegacyTarget>> {
    if targets.is_empty() || (relation == LegacyRelation::Exact && targets.len() != 1) {
        return Err(ContentGraphError::InvalidGraph(
            "invalid legacy mapping cardinality".to_string(),
        ));
    }
    targets.sort_by(|left, right| {
        right
            .byte_overlap
            .cmp(&left.byte_overlap)
            .then_with(|| left.source_offset.cmp(&right.source_offset))
            .then_with(|| left.occurrence_id.cmp(&right.occurrence_id))
    });
    let unique_occurrences: HashSet<_> =
        targets.iter().map(|target| target.occurrence_id).collect();
    if unique_occurrences.len() != targets.len() {
        return Err(ContentGraphError::InvalidGraph(
            "legacy mapping occurrences must be unique".to_string(),
        ));
    }
    Ok(targets)
}

pub fn reconstruct_artifact<W, F>(
    root: &ContentNode,
    writer: W,
    expected_sha256: [u8; 32],
    mut load_body: F,
) -> Result<u64>
where
    W: Write,
    F: FnMut(&BodyIdentity, &mut dyn Write) -> io::Result<()>,
{
    let mut artifact_writer = HashingWriter::new(writer);
    reconstruct_node(root, &mut artifact_writer, &mut load_body, 0)?;
    let (logical_length, digest, _) = artifact_writer.finish();
    if logical_length != root.logical_length || digest != expected_sha256 {
        return Err(ContentGraphError::ArtifactMismatch);
    }
    Ok(logical_length)
}

fn reconstruct_node<W, F>(
    node: &ContentNode,
    writer: &mut W,
    load_body: &mut F,
    depth: usize,
) -> Result<()>
where
    W: Write,
    F: FnMut(&BodyIdentity, &mut dyn Write) -> io::Result<()>,
{
    if depth > MAX_RECONSTRUCTION_DEPTH {
        return Err(ContentGraphError::DepthLimit);
    }
    node.verify_identity()?;
    match &node.kind {
        ContentNodeKind::Leaf(body) => {
            let mut body_writer = HashingWriter::new(writer);
            load_body(body, &mut body_writer)?;
            let (logical_length, digest, _) = body_writer.finish();
            if logical_length != body.logical_length || digest != body.digest {
                return Err(ContentGraphError::BodyMismatch);
            }
        }
        ContentNodeKind::Internal(children) => {
            for child in children {
                reconstruct_node(&child.node, writer, load_body, depth + 1)?;
            }
        }
    }
    Ok(())
}

struct HashingWriter<W> {
    inner: W,
    hasher: Sha256,
    written: u64,
}

impl<W> HashingWriter<W> {
    fn new(inner: W) -> Self {
        Self {
            inner,
            hasher: Sha256::new(),
            written: 0,
        }
    }

    fn finish(self) -> (u64, [u8; 32], W) {
        (self.written, self.hasher.finalize().into(), self.inner)
    }
}

impl<W: Write> Write for HashingWriter<W> {
    fn write(&mut self, bytes: &[u8]) -> io::Result<usize> {
        let written = self.inner.write(bytes)?;
        self.hasher.update(&bytes[..written]);
        self.written = self
            .written
            .checked_add(written as u64)
            .ok_or_else(|| io::Error::other("written length overflow"))?;
        Ok(written)
    }

    fn flush(&mut self) -> io::Result<()> {
        self.inner.flush()
    }
}

fn hash_parts(domain: &[u8], parts: &[&[u8]]) -> [u8; 32] {
    let mut hasher = Sha256::new();
    hasher.update((domain.len() as u64).to_be_bytes());
    hasher.update(domain);
    hasher.update((parts.len() as u64).to_be_bytes());
    for part in parts {
        hasher.update((part.len() as u64).to_be_bytes());
        hasher.update(part);
    }
    hasher.finalize().into()
}

fn checked_i64(value: u64, name: &str) -> Result<()> {
    if value > i64::MAX as u64 {
        return Err(ContentGraphError::InvalidGraph(format!(
            "{name} exceeds PostgreSQL bigint"
        )));
    }
    Ok(())
}

fn nonempty(value: String, name: &str) -> Result<String> {
    if value.is_empty() {
        return Err(ContentGraphError::InvalidGraph(format!("empty {name}")));
    }
    Ok(value)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::HashMap;

    fn body(bytes: &[u8]) -> BodyIdentity {
        BodyIdentity::from_bytes(bytes)
    }

    fn leaf(node_type: &str, bytes: &[u8]) -> ContentNode {
        ContentNode::leaf("fixture", node_type, body(bytes)).unwrap()
    }

    fn child(edge_type: &str, node: ContentNode) -> ContentChild {
        ContentChild {
            edge_type: edge_type.to_string(),
            node: Box::new(node),
        }
    }

    #[test]
    fn canonical_hash_is_length_framed() {
        assert_ne!(
            hash_parts(b"domain", &[b"ab", b"c"]),
            hash_parts(b"domain", &[b"a", b"bc"])
        );
        assert_eq!(
            hex::encode(hash_parts(b"mainrag.compat.v1", &[b"ab", b"c"])),
            "9ed0431a7ac7cebd650bb97fbaa8adbc53c9899f35f77c353a4dc474ffd98bbd"
        );
    }

    #[test]
    fn content_digest_changes_for_every_identity_input() {
        let alpha = leaf("text", b"alpha ");
        let omega = leaf("text", b"omega ");
        assert_ne!(alpha.digest(), omega.digest());

        let ordered = vec![
            child("content", alpha.clone()),
            child("suffix", omega.clone()),
        ];
        let reversed = vec![
            child("suffix", omega.clone()),
            child("content", alpha.clone()),
        ];
        let edge_changed = vec![
            child("prefix", alpha.clone()),
            child("suffix", omega.clone()),
        ];
        let base = internal_digest("fixture", "root", 12, &ordered).unwrap();
        assert_ne!(
            base,
            internal_digest("fixture", "root", 12, &reversed).unwrap()
        );
        assert_ne!(
            base,
            internal_digest("fixture", "root", 12, &edge_changed).unwrap()
        );
        assert_ne!(
            base,
            internal_digest("fixture", "other", 12, &ordered).unwrap()
        );
        assert_ne!(
            base,
            internal_digest("fixture", "root", 13, &ordered).unwrap()
        );

        let child_changed = vec![child("content", omega.clone()), child("suffix", omega)];
        assert_ne!(
            base,
            internal_digest("fixture", "root", 12, &child_changed).unwrap()
        );
    }

    #[test]
    fn nested_artifact_reconstructs_exact_unknown_bytes() {
        let pieces: [&[u8]; 5] = [
            b"fn main() {\n",
            b"  /* spacing survives */\n",
            b"  provider_call({\"future_field\":7});\n",
            b"}\n",
            &[0, 255, 10],
        ];
        let provider = LosslessProviderBlock::unknown(
            "future-provider",
            serde_json::json!({"future_field": 7, "nested": {"kept": true}}),
            pieces[2].to_vec(),
        )
        .unwrap();
        assert_eq!(provider.language_id, "unknown");
        assert_eq!(provider.metadata["future_field"], 7);

        let nested = ContentNode::internal(
            "fixture",
            "provider-section",
            vec![child(
                "opaque",
                leaf("opaque-provider-block", &provider.exact_bytes),
            )],
        )
        .unwrap();
        let root = ContentNode::internal(
            "fixture",
            "artifact-root",
            vec![
                child("content", leaf("text", pieces[0])),
                child("comment", leaf("comment", pieces[1])),
                child("provider", nested),
                child("content", leaf("text", pieces[3])),
                child("attachment", leaf("attachment", pieces[4])),
            ],
        )
        .unwrap();
        let expected: Vec<u8> = pieces.concat();
        let mut bodies = HashMap::new();
        for piece in pieces {
            bodies.insert(body(piece).digest, piece.to_vec());
        }
        let mut output = Vec::new();
        reconstruct_artifact(
            &root,
            &mut output,
            Sha256::digest(&expected).into(),
            |identity, writer| writer.write_all(&bodies[&identity.digest]),
        )
        .unwrap();
        assert_eq!(output, expected);
    }

    #[test]
    fn reconstruction_fails_closed_on_body_corruption() {
        let root = leaf("text", b"expected");
        let error = reconstruct_artifact(
            &root,
            Vec::new(),
            Sha256::digest(b"expected").into(),
            |_identity, writer| writer.write_all(b"corrupt!"),
        )
        .unwrap_err();
        assert!(matches!(error, ContentGraphError::BodyMismatch));
    }

    #[test]
    fn generated_nested_artifacts_round_trip() {
        for seed in 0_u8..64 {
            let pieces = [
                vec![b' '; seed as usize % 7],
                (0_u8..=seed).collect::<Vec<_>>(),
                format!("{{\"unknown_{seed}\":{seed}}}\n").into_bytes(),
            ];
            let nested = ContentNode::internal(
                "generated",
                "nested",
                vec![
                    child("separator", leaf("whitespace", &pieces[0])),
                    child("opaque", leaf("unknown-provider", &pieces[2])),
                ],
            )
            .unwrap();
            let root = ContentNode::internal(
                "generated",
                "artifact-root",
                vec![
                    child("payload", leaf("bytes", &pieces[1])),
                    child("nested", nested),
                ],
            )
            .unwrap();
            let expected = [
                pieces[1].as_slice(),
                pieces[0].as_slice(),
                pieces[2].as_slice(),
            ]
            .concat();
            let bodies: HashMap<_, _> = pieces
                .iter()
                .map(|piece| (body(piece).digest, piece.clone()))
                .collect();
            let mut output = Vec::new();
            reconstruct_artifact(
                &root,
                &mut output,
                Sha256::digest(&expected).into(),
                |identity, writer| writer.write_all(&bodies[&identity.digest]),
            )
            .unwrap();
            assert_eq!(output, expected, "seed {seed}");
        }
    }

    #[test]
    fn retrieval_identity_covers_profile_and_ordered_components() {
        let alpha = body(b"alpha");
        let omega = body(b"omega");
        let component = |role: &str, body: BodyIdentity| ViewComponent {
            role: role.to_string(),
            identity: ViewComponentIdentity::Body(body),
            relative_start: 0,
            relative_end: 5,
        };
        let base = RetrievalViewIdentity::new(
            "composed",
            "profile-v1",
            "unknown",
            "tokenizer-v1",
            1,
            vec![
                component("body", alpha.clone()),
                component("context", omega.clone()),
            ],
        )
        .unwrap();
        let reordered = RetrievalViewIdentity::new(
            "composed",
            "profile-v1",
            "unknown",
            "tokenizer-v1",
            1,
            vec![component("context", omega), component("body", alpha)],
        )
        .unwrap();
        assert_ne!(base.digest, reordered.digest);
    }

    #[test]
    fn legacy_mapping_order_is_stable_for_all_ties() {
        let ordered = order_legacy_targets(
            LegacyRelation::Split,
            vec![
                LegacyTarget {
                    occurrence_id: 9,
                    byte_overlap: 10,
                    source_offset: 4,
                },
                LegacyTarget {
                    occurrence_id: 3,
                    byte_overlap: 10,
                    source_offset: 4,
                },
                LegacyTarget {
                    occurrence_id: 1,
                    byte_overlap: 11,
                    source_offset: 9,
                },
            ],
        )
        .unwrap();
        assert_eq!(
            ordered
                .iter()
                .map(|target| target.occurrence_id)
                .collect::<Vec<_>>(),
            vec![1, 3, 9]
        );
    }
}

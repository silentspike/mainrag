use chrono::{DateTime, Utc};
use serde_json::Value;
use tokio_postgres::{Client, Error, Row};

#[derive(Debug, Clone)]
pub struct ContentNodeRecord {
    pub id: i64,
    pub domain: String,
    pub node_type: String,
    pub logical_length: i64,
    pub body_id: Option<i64>,
    pub node_digest: Vec<u8>,
}

impl From<Row> for ContentNodeRecord {
    fn from(row: Row) -> Self {
        Self {
            id: row.get("id"),
            domain: row.get("domain"),
            node_type: row.get("node_type"),
            logical_length: row.get("logical_length"),
            body_id: row.get("body_id"),
            node_digest: row.get("node_digest"),
        }
    }
}

#[derive(Debug, Clone)]
pub struct RetrievalViewRecord {
    pub id: i64,
    pub view_type: String,
    pub profile_id: String,
    pub language_id: String,
    pub tokenizer_version: String,
    pub capability_flags: i64,
    pub view_digest: Vec<u8>,
}

impl From<Row> for RetrievalViewRecord {
    fn from(row: Row) -> Self {
        Self {
            id: row.get("id"),
            view_type: row.get("view_type"),
            profile_id: row.get("profile_id"),
            language_id: row.get("language_id"),
            tokenizer_version: row.get("tokenizer_version"),
            capability_flags: row.get("capability_flags"),
            view_digest: row.get("view_digest"),
        }
    }
}

#[derive(Debug, Clone)]
pub struct OccurrenceRecord {
    pub id: i64,
    pub source_id: i64,
    pub artifact_version_id: i64,
    pub view_id: i64,
    pub source_path: String,
    pub locator: Value,
}

impl From<Row> for OccurrenceRecord {
    fn from(row: Row) -> Self {
        Self {
            id: row.get("id"),
            source_id: row.get("source_id"),
            artifact_version_id: row.get("artifact_version_id"),
            view_id: row.get("view_id"),
            source_path: row.get("source_path"),
            locator: row.get("locator"),
        }
    }
}

#[derive(Debug, Clone)]
pub struct LegacyMappingRecord {
    pub old_hit_id: String,
    pub occurrence_id: i64,
    pub ordinal: i64,
    pub relation_kind: String,
    pub byte_overlap: i64,
    pub source_offset: i64,
}

impl From<Row> for LegacyMappingRecord {
    fn from(row: Row) -> Self {
        Self {
            old_hit_id: row.get("old_hit_id"),
            occurrence_id: row.get("occurrence_id"),
            ordinal: row.get("ordinal"),
            relation_kind: row.get("relation_kind"),
            byte_overlap: row.get("byte_overlap"),
            source_offset: row.get("source_offset"),
        }
    }
}

pub async fn put_leaf_node(
    client: &Client,
    domain: &str,
    node_type: &str,
    body_id: i64,
) -> Result<ContentNodeRecord, Error> {
    client
        .query_one(
            "SELECT id, domain, node_type, logical_length, body_id, node_digest \
             FROM storage_v2_put_leaf_node($1, $2, $3)",
            &[&domain, &node_type, &body_id],
        )
        .await
        .map(ContentNodeRecord::from)
}

pub async fn put_internal_node(
    client: &Client,
    domain: &str,
    node_type: &str,
    logical_length: i64,
    edge_types: &[String],
    child_node_ids: &[i64],
) -> Result<ContentNodeRecord, Error> {
    client
        .query_one(
            "SELECT id, domain, node_type, logical_length, body_id, node_digest \
             FROM storage_v2_put_internal_node($1, $2, $3, $4, $5)",
            &[
                &domain,
                &node_type,
                &logical_length,
                &edge_types,
                &child_node_ids,
            ],
        )
        .await
        .map(ContentNodeRecord::from)
}

#[allow(clippy::too_many_arguments)]
pub async fn put_retrieval_view(
    client: &Client,
    view_type: &str,
    profile_id: &str,
    language_id: &str,
    tokenizer_version: &str,
    capability_flags: i64,
    roles: &[String],
    component_kinds: &[String],
    component_ids: &[i64],
    relative_starts: &[i64],
    relative_ends: &[i64],
) -> Result<RetrievalViewRecord, Error> {
    client
        .query_one(
            "SELECT id, view_type, profile_id, language_id, tokenizer_version, \
                    capability_flags, view_digest \
             FROM storage_v2_put_retrieval_view($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)",
            &[
                &view_type,
                &profile_id,
                &language_id,
                &tokenizer_version,
                &capability_flags,
                &roles,
                &component_kinds,
                &component_ids,
                &relative_starts,
                &relative_ends,
            ],
        )
        .await
        .map(RetrievalViewRecord::from)
}

#[allow(clippy::too_many_arguments)]
pub async fn create_occurrence(
    client: &Client,
    source_id: i64,
    artifact_version_id: i64,
    view_id: i64,
    role: &str,
    ordinal: i64,
    parent_occurrence_id: Option<i64>,
    source_path: &str,
    locator: &Value,
    derivation_recipe: Option<&Value>,
    occurred_at: Option<DateTime<Utc>>,
) -> Result<OccurrenceRecord, Error> {
    client
        .query_one(
            "INSERT INTO occurrence( \
                source_id, artifact_version_id, view_id, role, ordinal, \
                parent_occurrence_id, source_path, locator, derivation_recipe, occurred_at \
             ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10) \
             RETURNING id, source_id, artifact_version_id, view_id, source_path, locator",
            &[
                &source_id,
                &artifact_version_id,
                &view_id,
                &role,
                &ordinal,
                &parent_occurrence_id,
                &source_path,
                &locator,
                &derivation_recipe,
                &occurred_at,
            ],
        )
        .await
        .map(OccurrenceRecord::from)
}

pub async fn visible_occurrences(
    client: &Client,
    source_id: Option<i64>,
    path_prefix: Option<&str>,
    occurred_from: Option<DateTime<Utc>>,
    occurred_to: Option<DateTime<Utc>>,
) -> Result<Vec<OccurrenceRecord>, Error> {
    client
        .query(
            "SELECT occurrence_id AS id, source_id, artifact_version_id, view_id, \
                    source_path, locator \
             FROM storage_v2_visible_occurrences($1, $2, $3, $4)",
            &[&source_id, &path_prefix, &occurred_from, &occurred_to],
        )
        .await
        .map(|rows| rows.into_iter().map(OccurrenceRecord::from).collect())
}

pub async fn replace_legacy_mapping(
    client: &Client,
    old_hit_id: &str,
    occurrence_ids: &[i64],
    relation_kind: &str,
    byte_overlaps: &[i64],
    source_offsets: &[i64],
) -> Result<Vec<LegacyMappingRecord>, Error> {
    client
        .query(
            "SELECT old_hit_id, occurrence_id, ordinal, relation_kind, byte_overlap, source_offset \
             FROM storage_v2_replace_legacy_hit_mapping($1, $2, $3, $4, $5)",
            &[
                &old_hit_id,
                &occurrence_ids,
                &relation_kind,
                &byte_overlaps,
                &source_offsets,
            ],
        )
        .await
        .map(|rows| rows.into_iter().map(LegacyMappingRecord::from).collect())
}

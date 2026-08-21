use serde_json::Value;
use tokio_postgres::{Client, Error, GenericClient, Row};

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct IngestRunRecord {
    pub id: i64,
    pub source_id: i64,
    pub generation_id: i64,
    pub idempotency_key: String,
    pub semantic_manifest_sha256: String,
    pub adapter_profile_id: String,
    pub status: String,
    pub forced: bool,
    pub expected_item_count: Option<i64>,
    pub staged_item_count: i64,
    pub changed_item_count: i64,
    pub deleted_item_count: i64,
    pub bytes_read: i64,
    pub parser_work_count: i64,
    pub error_count: i64,
    pub membership_delta_us: i64,
    pub sealing_us: i64,
}

impl From<Row> for IngestRunRecord {
    fn from(row: Row) -> Self {
        Self {
            id: row.get("id"),
            source_id: row.get("source_id"),
            generation_id: row.get("generation_id"),
            idempotency_key: row.get("idempotency_key"),
            semantic_manifest_sha256: row.get("semantic_manifest_sha256"),
            adapter_profile_id: row.get("adapter_profile_id"),
            status: row.get("status"),
            forced: row.get("forced"),
            expected_item_count: row.get("expected_item_count"),
            staged_item_count: row.get("staged_item_count"),
            changed_item_count: row.get("changed_item_count"),
            deleted_item_count: row.get("deleted_item_count"),
            bytes_read: row.get("bytes_read"),
            parser_work_count: row.get("parser_work_count"),
            error_count: row.get("error_count"),
            membership_delta_us: row.get("membership_delta_us"),
            sealing_us: row.get("sealing_us"),
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct StagedItemRecord {
    pub run_id: i64,
    pub source_id: i64,
    pub source_item_id: i64,
    pub artifact_version_id: i64,
    pub occurrence_id: i64,
    pub content_identity_sha256: Vec<u8>,
    pub analysis_profile_id: String,
    pub byte_length: i64,
    pub parser_pass_count: i16,
}

impl From<Row> for StagedItemRecord {
    fn from(row: Row) -> Self {
        Self {
            run_id: row.get("run_id"),
            source_id: row.get("source_id"),
            source_item_id: row.get("source_item_id"),
            artifact_version_id: row.get("artifact_version_id"),
            occurrence_id: row.get("occurrence_id"),
            content_identity_sha256: row.get("content_identity_sha256"),
            analysis_profile_id: row.get("analysis_profile_id"),
            byte_length: row.get("byte_length"),
            parser_pass_count: row.get("parser_pass_count"),
        }
    }
}

#[derive(Debug, Clone)]
pub struct StageItem<'a> {
    pub run_id: i64,
    pub item_key: &'a str,
    pub item_kind: &'a str,
    pub witness_type: &'a str,
    pub witness: &'a Value,
    pub adapter_profile_id: &'a str,
    pub content_root_node_id: Option<i64>,
    pub raw_body_id: Option<i64>,
    pub expected_content_hash: &'a str,
    pub byte_length: i64,
    pub content_identity_sha256: &'a [u8],
    pub analysis_profile_id: &'a str,
    pub view_id: i64,
    pub source_path: &'a str,
    pub locator: &'a Value,
    pub parser_pass_count: i16,
}

const RUN_COLUMNS: &str = "id, source_id, generation_id, idempotency_key, \
    semantic_manifest_sha256, adapter_profile_id, status, forced, \
    expected_item_count, staged_item_count, changed_item_count, deleted_item_count, \
    bytes_read, parser_work_count, error_count, membership_delta_us, sealing_us";

#[allow(clippy::too_many_arguments)]
pub async fn begin_shadow_ingest<C>(
    client: &C,
    source_id: i64,
    idempotency_key: &str,
    semantic_manifest_sha256: &str,
    adapter_profile_id: &str,
    witness_type: &str,
    witness: &Value,
    force: bool,
) -> Result<IngestRunRecord, Error>
where
    C: GenericClient + Sync,
{
    client
        .query_one(
            &format!(
                "SELECT {RUN_COLUMNS} FROM storage_v2_begin_shadow_ingest($1,$2,$3,$4,$5,$6,$7)"
            ),
            &[
                &source_id,
                &idempotency_key,
                &semantic_manifest_sha256,
                &adapter_profile_id,
                &witness_type,
                witness,
                &force,
            ],
        )
        .await
        .map(IngestRunRecord::from)
}

pub async fn stage_shadow_item<C>(
    client: &C,
    item: &StageItem<'_>,
) -> Result<StagedItemRecord, Error>
where
    C: GenericClient + Sync,
{
    client
        .query_one(
            "SELECT run_id, source_id, source_item_id, artifact_version_id, occurrence_id, \
                    content_identity_sha256, analysis_profile_id, byte_length, parser_pass_count \
             FROM storage_v2_stage_shadow_item( \
                $1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16)",
            &[
                &item.run_id,
                &item.item_key,
                &item.item_kind,
                &item.witness_type,
                item.witness,
                &item.adapter_profile_id,
                &item.content_root_node_id,
                &item.raw_body_id,
                &item.expected_content_hash,
                &item.byte_length,
                &item.content_identity_sha256,
                &item.analysis_profile_id,
                &item.view_id,
                &item.source_path,
                item.locator,
                &item.parser_pass_count,
            ],
        )
        .await
        .map(StagedItemRecord::from)
}

pub async fn begin_analysis_attempt<C>(
    client: &C,
    content_identity_sha256: &[u8],
    analysis_profile_id: &str,
) -> Result<String, Error>
where
    C: GenericClient + Sync,
{
    client
        .query_one(
            "SELECT status FROM storage_v2_begin_analysis_attempt($1, $2)",
            &[&content_identity_sha256, &analysis_profile_id],
        )
        .await
        .map(|row| row.get("status"))
}

pub async fn finish_analysis_attempt<C>(
    client: &C,
    content_identity_sha256: &[u8],
    analysis_profile_id: &str,
    result: Option<&Value>,
    error_code: Option<&str>,
) -> Result<String, Error>
where
    C: GenericClient + Sync,
{
    client
        .query_one(
            "SELECT status FROM storage_v2_finish_analysis_attempt($1, $2, $3, $4)",
            &[
                &content_identity_sha256,
                &analysis_profile_id,
                &result,
                &error_code,
            ],
        )
        .await
        .map(|row| row.get("status"))
}

pub async fn commit_shadow_ingest<C>(
    client: &C,
    run_id: i64,
    expected_item_count: i64,
    generation_root_sha256: &str,
) -> Result<IngestRunRecord, Error>
where
    C: GenericClient + Sync,
{
    client
        .query_one(
            &format!("SELECT {RUN_COLUMNS} FROM storage_v2_commit_shadow_ingest($1,$2,$3)"),
            &[&run_id, &expected_item_count, &generation_root_sha256],
        )
        .await
        .map(IngestRunRecord::from)
}

pub async fn cancel_shadow_ingest(
    client: &Client,
    run_id: i64,
    error_count: i64,
) -> Result<IngestRunRecord, Error> {
    client
        .query_one(
            &format!("SELECT {RUN_COLUMNS} FROM storage_v2_cancel_shadow_ingest($1,$2)"),
            &[&run_id, &error_count],
        )
        .await
        .map(IngestRunRecord::from)
}

#[allow(clippy::too_many_arguments)]
pub async fn update_append_frontier(
    client: &Client,
    source_id: i64,
    source_item_id: i64,
    adapter_profile_id: &str,
    expected_prefix_bytes: i64,
    expected_prefix_sha256: Option<&[u8]>,
    new_prefix_bytes: i64,
    new_prefix_sha256: &[u8],
    full_sha256: Option<&[u8]>,
    full_compare_every: i64,
) -> Result<i64, Error> {
    client
        .query_one(
            "SELECT prefix_bytes FROM storage_v2_update_append_frontier( \
                $1,$2,$3,$4,$5,$6,$7,$8,$9)",
            &[
                &source_id,
                &source_item_id,
                &adapter_profile_id,
                &expected_prefix_bytes,
                &expected_prefix_sha256,
                &new_prefix_bytes,
                &new_prefix_sha256,
                &full_sha256,
                &full_compare_every,
            ],
        )
        .await
        .map(|row| row.get("prefix_bytes"))
}

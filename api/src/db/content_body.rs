use tokio_postgres::{Client, Error, GenericClient, Row};
use uuid::Uuid;

#[derive(Debug, Clone)]
pub struct ContentBodyRecord {
    pub id: i64,
    pub digest_algorithm: String,
    pub digest: Vec<u8>,
    pub logical_length: i64,
    pub inline_bytes: Option<Vec<u8>>,
    pub pack_id: Option<Uuid>,
}

impl From<Row> for ContentBodyRecord {
    fn from(row: Row) -> Self {
        Self {
            id: row.get("id"),
            digest_algorithm: row.get("digest_algorithm"),
            digest: row.get("digest"),
            logical_length: row.get("logical_length"),
            inline_bytes: row.get("inline_bytes"),
            pack_id: row.get("pack_id"),
        }
    }
}

pub async fn put_inline_body(client: &Client, bytes: &[u8]) -> Result<ContentBodyRecord, Error> {
    client
        .query_one(
            "SELECT id, digest_algorithm, digest, logical_length, inline_bytes, pack_id \
             FROM storage_v2_put_inline_body($1)",
            &[&bytes],
        )
        .await
        .map(ContentBodyRecord::from)
}

#[allow(clippy::too_many_arguments)]
pub async fn put_packed_body<C>(
    client: &C,
    pack_id: Uuid,
    ordinal: i64,
    digest: &[u8],
    logical_length: i64,
    pack_offset: i64,
    stored_length: i64,
    codec: &str,
    entry_digest: &[u8],
) -> Result<ContentBodyRecord, Error>
where
    C: GenericClient + Sync,
{
    client
        .query_one(
            "SELECT id, digest_algorithm, digest, logical_length, inline_bytes, pack_id \
             FROM storage_v2_put_packed_body(\
                $1,$2,$3,$4,$5,$6,$7::TEXT::storage_v2_body_codec,$8)",
            &[
                &pack_id,
                &ordinal,
                &digest,
                &logical_length,
                &pack_offset,
                &stored_length,
                &codec,
                &entry_digest,
            ],
        )
        .await
        .map(ContentBodyRecord::from)
}

pub async fn create_pack<C>(
    client: &C,
    pack_id: Uuid,
    storage_key: &str,
    build_nonce: Uuid,
) -> Result<(), Error>
where
    C: GenericClient + Sync,
{
    client
        .execute(
            "SELECT storage_v2_create_pack($1, $2, $3)",
            &[&pack_id, &storage_key, &build_nonce],
        )
        .await
        .map(|_| ())
}

pub async fn verify_pack<C>(
    client: &C,
    pack_id: Uuid,
    manifest_sha256: &[u8],
    stored_bytes: i64,
) -> Result<(), Error>
where
    C: GenericClient + Sync,
{
    client
        .execute(
            "SELECT storage_v2_verify_pack($1, $2, $3)",
            &[&pack_id, &manifest_sha256, &stored_bytes],
        )
        .await
        .map(|_| ())
}

pub async fn publish_pack<C>(client: &C, pack_id: Uuid) -> Result<(), Error>
where
    C: GenericClient + Sync,
{
    client
        .execute("SELECT storage_v2_publish_pack($1)", &[&pack_id])
        .await
        .map(|_| ())
}

pub async fn begin_reader_epoch<C: GenericClient + Sync>(client: &C) -> Result<Uuid, Error> {
    client
        .query_one("SELECT storage_v2_begin_reader_epoch()", &[])
        .await
        .map(|row| row.get(0))
}

pub async fn end_reader_epoch<C: GenericClient + Sync>(
    client: &C,
    epoch_id: Uuid,
) -> Result<(), Error> {
    client
        .execute("SELECT storage_v2_end_reader_epoch($1)", &[&epoch_id])
        .await
        .map(|_| ())
}

/// Register before polling work that fetches placement. Keep the epoch until
/// all pack-dependent I/O has ended, including failed verification. Cancellation
/// deliberately leaves an open epoch (or an uncommitted registration lock):
/// abandoned work must never make reclamation appear safe.
pub async fn with_reader_epoch<C, T>(
    client: &C,
    work: impl std::future::Future<Output = anyhow::Result<T>>,
) -> anyhow::Result<T>
where
    C: GenericClient + Sync,
{
    let epoch = begin_reader_epoch(client).await?;
    let result = work.await;
    let finish = end_reader_epoch(client, epoch).await;
    match (result, finish) {
        (Ok(value), Ok(())) => Ok(value),
        (Err(error), Ok(())) => Err(error),
        (Ok(_), Err(error)) => Err(anyhow::Error::new(error)
            .context("pack reader epoch could not be closed; retention remains required")),
        (Err(error), Err(finish)) => Err(error.context(format!(
            "pack reader epoch could not be closed; retention remains required: {finish}"
        ))),
    }
}

pub async fn switch_pack(
    client: &Client,
    old_pack_id: Uuid,
    new_pack_id: Uuid,
    gc_epoch_id: i64,
) -> Result<i64, Error> {
    client
        .query_one(
            "SELECT storage_v2_switch_pack($1, $2, $3)",
            &[&old_pack_id, &new_pack_id, &gc_epoch_id],
        )
        .await
        .map(|row| row.get(0))
}

pub async fn mark_pack_readers_drained(client: &Client, pack_id: Uuid) -> Result<(), Error> {
    client
        .execute(
            "SELECT storage_v2_mark_pack_readers_drained($1)",
            &[&pack_id],
        )
        .await
        .map(|_| ())
}

/// Advances database state to `reclaimed`. The caller may remove pack bytes
/// only after this function succeeds.
pub async fn reclaim_pack(client: &Client, pack_id: Uuid) -> Result<(), Error> {
    client
        .execute("SELECT storage_v2_reclaim_pack($1)", &[&pack_id])
        .await
        .map(|_| ())
}

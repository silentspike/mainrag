//! Real producer, body publication and resume against an explicitly isolated fixture.
use super::operation::{produce_batch, ProduceBatchInput};
use anyhow::{ensure, Context, Result};
use serde_json::Value;
use sha2::{Digest, Sha256};
use tokio_postgres::{Client, NoTls};

const PRINCIPAL: &str = "00000000-0000-4000-8000-000000000031";
const FILE_ID: i64 = 90900;
const FIRST_HIT: i64 = 90901;
const SOURCE: &str = "alpha Über🙂 beta";
const HISTORY: &str = "retained original 東京";

async fn insert_chunk(client: &Client, id: i64, text: &str) -> Result<()> {
    let digest = Sha256::digest(text.as_bytes()).to_vec();
    let compressed = zstd::encode_all(text.as_bytes(), 1)?;
    client.execute("INSERT INTO chunks(id,file_id,chunk_type,content_hash,content_compressed,content_text,start_line,end_line) \
        VALUES($1,$2,'text',$3,$4,$5,1,1)",&[&id,&FILE_ID,&digest,&compressed,&text]).await?;
    Ok(())
}

async fn input(client: &Client, generation: i64) -> Result<ProduceBatchInput> {
    let hash = Sha256::digest(SOURCE.as_bytes()).to_vec();
    let witness: Value = client
        .query_one(
            "SELECT storage_v2_lock_legacy_hit_source(3,$1,$2)",
            &[&FILE_ID, &hash],
        )
        .await?
        .get(0);
    Ok(ProduceBatchInput {
        generation_id: generation,
        file_id: FILE_ID,
        expected_file_sha256: hex::encode(Sha256::digest(SOURCE.as_bytes())),
        expected_file_revision: witness["file_revision"]
            .as_i64()
            .context("file revision required")?,
        expected_legacy_epoch: witness["legacy_epoch"]
            .as_i64()
            .context("legacy epoch required")?,
        after_hit_id: 0,
        source_spool_budget_bytes: 4096,
        include_test: false,
    })
}

async fn snapshot(client: &Client) -> Result<Value> {
    Ok(client.query_one("SELECT jsonb_build_object('generations',(SELECT count(*) FROM source_generation WHERE source_id=3),\
        'memberships',(SELECT count(*) FROM generation_item_version WHERE source_id=3),\
        'active',(SELECT count(*) FROM logical_source WHERE active_generation_id IS NOT NULL),\
        'packs',(SELECT count(*) FROM content_pack),'entries',(SELECT count(*) FROM content_pack_entry),\
        'proofs',(SELECT jsonb_agg(jsonb_build_array(old_hit_id,ctid,mapping_sha256) ORDER BY old_hit_id) \
            FROM storage_v2_legacy_hit_proof WHERE source_id=3),\
        'mappings',(SELECT jsonb_agg(jsonb_build_array(old_hit_id,ordinal,occurrence_id,ctid) ORDER BY old_hit_id,ordinal) \
            FROM legacy_hit_mapping WHERE old_hit_id LIKE '9090%'))",&[]).await?.get(0))
}

#[tokio::test]
#[ignore = "requires an explicitly prepared disposable native legacy fixture"]
async fn real_producer_reuses_bytes_commits_all_hits_replays_and_rejects_corruption() -> Result<()>
{
    let url = std::env::var("MAINRAG_LEGACY_HIT_FIXTURE_URL")
        .context("explicit isolated fixture URL required")?;
    let config: tokio_postgres::Config = url.parse()?;
    let name = config
        .get_dbname()
        .context("fixture database identity required")?;
    ensure!(
        name.starts_with("storage_v2_ingest_")
            && name.len() == 50
            && name[18..].bytes().all(|byte| byte.is_ascii_hexdigit()),
        "refusing non-fixture database"
    );
    let (mut client, connection) = config.connect(NoTls).await?;
    tokio::spawn(async move {
        let _ = connection.await;
    });
    client
        .batch_execute(&format!("SET ROLE mainrag; SET app.user_id='{PRINCIPAL}'"))
        .await?;
    let generation: i64 = client
        .query_one(
            "SELECT id FROM source_generation WHERE source_id=3 AND generation_seq=1 \
        AND status='verified'",
            &[],
        )
        .await?
        .get(0);
    ensure!(client.query_one("SELECT count(*) FROM occurrence WHERE source_id=3 AND source_path='/synthetic/producer.txt'",
        &[]).await?.get::<_,i64>(0)==2,"prepared native fragment fixture required");
    let compressed = zstd::encode_all(SOURCE.as_bytes(), 1)?;
    let hash = Sha256::digest(SOURCE.as_bytes()).to_vec();
    client.execute("INSERT INTO files(id,source_id,path,hash,content,content_text,size_original,size_compressed,last_modified) \
        VALUES($1,3,'/synthetic/producer.txt',$2,$3,$4,$5,$6,NOW())",&[&FILE_ID,&hash,&compressed,&SOURCE,
            &i32::try_from(SOURCE.len())?,&i32::try_from(compressed.len())?]).await?;
    for (index, text) in ["Über🙂", "beta", "beta", HISTORY, HISTORY]
        .iter()
        .enumerate()
    {
        insert_chunk(&client, FIRST_HIT + i64::try_from(index)?, text).await?;
    }
    let request = input(&client, generation).await?;
    let packs = tempfile::tempdir()?;
    let _lease = crate::services::content_store::build_recovery::writer_lease(packs.path())?;
    let before = snapshot(&client).await?;
    let transaction = client.transaction().await?;
    let result = produce_batch(&transaction, 3, &request, packs.path(), 4096).await?;
    transaction.commit().await?;
    ensure!(
        result["processed"] == 5 && result["file_done"] == true,
        "all-hit producer batch did not commit"
    );
    ensure!(
        result["batch_proof"]["historical_hits"] == 2
            && result["batch_proof"]["native_targets"] == 4,
        "native split/merge or historical routing differs"
    );
    let after = snapshot(&client).await?;
    ensure!(
        after["generations"] == before["generations"]
            && after["memberships"] == before["memberships"]
            && after["active"] == before["active"],
        "bootstrap changed source generations, membership or activation"
    );
    ensure!(
        after["packs"] == 1 && after["entries"] == 1,
        "duplicate historical bodies were packed more than once"
    );
    let split: Value = client
        .query_one(
            "SELECT storage_v2_resolve_legacy_hit(3,'1',$1)",
            &[&FIRST_HIT.to_string()],
        )
        .await?
        .get(0);
    ensure!(
        split["targets"]
            .as_array()
            .is_some_and(|targets| targets.len() == 2)
            && split["targets"][0]["relation_kind"] == "split",
        "native boundary split did not resolve"
    );
    for id in [FIRST_HIT + 1, FIRST_HIT + 2] {
        let merged: Value = client
            .query_one(
                "SELECT storage_v2_resolve_legacy_hit(3,'1',$1)",
                &[&id.to_string()],
            )
            .await?
            .get(0);
        ensure!(
            merged["targets"][0]["relation_kind"] == "merged",
            "shared old IDs did not resolve as merged"
        );
    }
    let transaction = client.transaction().await?;
    let replay = produce_batch(&transaction, 3, &request, packs.path(), 4096).await?;
    transaction.commit().await?;
    ensure!(
        replay["processed"] == 0
            && replay["pack_stored_bytes"] == 0
            && snapshot(&client).await? == after,
        "committed retry changed native data or allocated another pack"
    );
    ensure!(
        !packs
            .path()
            .join(".legacy-hit-bootstrap/3/verified-source.native")
            .exists(),
        "completed source spool was retained"
    );
    let storage: String = client
        .query_one(
            "SELECT pack.storage_key FROM content_body body \
        JOIN content_pack pack ON pack.id=body.pack_id WHERE body.digest=$1",
            &[&Sha256::digest(HISTORY.as_bytes()).to_vec()],
        )
        .await?
        .get(0);
    let path = packs.path().join(storage);
    let mut corrupt = std::fs::read(&path)?;
    corrupt[0] ^= 1;
    use std::os::unix::fs::PermissionsExt;
    std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o600))?;
    std::fs::write(&path, corrupt)?;
    insert_chunk(&client, FIRST_HIT + 5, HISTORY).await?;
    let request = input(&client, generation).await?;
    let transaction = client.transaction().await?;
    let rejected = produce_batch(&transaction, 3, &request, packs.path(), 4096).await;
    ensure!(
        rejected.is_err(),
        "corrupt reused pack authorized an old-hit mapping"
    );
    transaction.rollback().await?;
    ensure!(
        client
            .query_one(
                "SELECT count(*) FROM legacy_hit_mapping WHERE old_hit_id=$1",
                &[&(FIRST_HIT + 5).to_string()]
            )
            .await?
            .get::<_, i64>(0)
            == 0,
        "corrupt body left a committed mapping"
    );
    ensure!(
        snapshot(&client).await? == after,
        "rejected corruption changed committed native state"
    );
    println!("real legacy producer: verified split/merge, one deduplicated history pack, committed replay, spool cleanup, and corruption rollback PASS");
    Ok(())
}

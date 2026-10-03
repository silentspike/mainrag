//! Temporary legacy bootstrap. Retire this reader before deleting legacy tables.
use super::{align_verified_file, NativeFragment, VerifiedLegacyChunk, MAX_BATCH_BYTES, MAX_HITS};
use crate::{
    db::content_body,
    services::{
        content_store::{BodyCodec, PackBuilder},
        shadow_slice::deliver_stored_body_row,
    },
};
use anyhow::{ensure, Context, Result};
use serde::Deserialize;
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::{
    collections::BTreeMap,
    fs,
    io::{Cursor, Write},
    os::unix::fs::PermissionsExt,
    path::Path,
};
use tokio_postgres::GenericClient;
use uuid::Uuid;

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ProduceBatchInput {
    pub generation_id: i64,
    pub file_id: i64,
    pub expected_file_sha256: String,
    pub expected_file_revision: i64,
    pub expected_legacy_epoch: i64,
    pub after_hit_id: i64,
    /// Caller admission bounds the one decoded source file, independent of the
    /// bounded chunk-pattern batch. This is not an all-source spool allowance.
    pub source_spool_budget_bytes: u64,
    #[serde(default)]
    pub include_test: bool,
}

const BODY_COLUMNS:&str="body.id AS body_id,body.digest_algorithm,body.digest,body.logical_length,body.inline_bytes,body.pack_id,\
    pack.storage_key,pack.stored_bytes,pack.status::TEXT AS pack_status,entry.ordinal,entry.pack_offset,\
    entry.stored_length,entry.codec::TEXT AS codec,entry.entry_digest,dictionary.id AS dictionary_id,\
    dictionary.digest AS dictionary_digest,dictionary.dictionary_bytes";
const BODY_JOINS: &str = "LEFT JOIN content_pack pack ON pack.id=body.pack_id \
    LEFT JOIN content_pack_entry entry ON entry.pack_id=body.pack_id AND entry.body_id=body.id \
    LEFT JOIN content_dictionary dictionary ON dictionary.id=entry.dictionary_id";

struct SourceSpool {
    file: fs::File,
    hash: [u8; 32],
    fragments: Vec<NativeFragment>,
}

struct DigestFile<'a> {
    file: &'a mut fs::File,
    hash: Sha256,
}
impl Write for DigestFile<'_> {
    fn write(&mut self, bytes: &[u8]) -> std::io::Result<usize> {
        self.file.write_all(bytes)?;
        self.hash.update(bytes);
        Ok(bytes.len())
    }
    fn flush(&mut self) -> std::io::Result<()> {
        self.file.flush()
    }
}

async fn source_spool<C: GenericClient + Sync>(
    client: &C,
    source: i64,
    generation: i64,
    path: &str,
    budget: u64,
    root: &Path,
    buffer: usize,
) -> Result<Option<SourceSpool>> {
    let identity=client.query("SELECT occurrence_row.id,occurrence_row.locator,body.id AS body_id,body.digest,body.logical_length \
        FROM occurrence occurrence_row JOIN artifact_version artifact ON artifact.id=occurrence_row.artifact_version_id \
        JOIN generation_item_version membership ON membership.source_id=occurrence_row.source_id \
            AND membership.source_item_id=artifact.item_id AND membership.artifact_version_id=artifact.id \
        JOIN source_generation generation ON generation.id=$2 AND generation.source_id=$1 \
        LEFT JOIN content_node node ON node.id=artifact.content_root_node_id \
        JOIN content_body body ON body.id=coalesce(artifact.raw_body_id,node.body_id) \
        WHERE occurrence_row.source_id=$1 AND occurrence_row.source_path=$3 \
            AND membership.valid_from_seq<=generation.generation_seq \
            AND (membership.valid_to_seq IS NULL OR membership.valid_to_seq>generation.generation_seq) \
        ORDER BY (occurrence_row.locator->>'byte_start')::BIGINT,occurrence_row.id",&[&source,&generation,&path]).await?;
    if identity.is_empty() {
        return Ok(None);
    }
    let mut fragments = Vec::with_capacity(identity.len());
    let mut end = 0;
    let mut manifest = Sha256::new();
    manifest.update(b"mainrag.legacy-hit-source-spool.v1\0");
    manifest.update(source.to_be_bytes());
    manifest.update(generation.to_be_bytes());
    manifest.update((path.len() as u64).to_be_bytes());
    manifest.update(path.as_bytes());
    for row in &identity {
        let locator: Value = row.get("locator");
        let start = locator["byte_start"]
            .as_u64()
            .context("native source byte start omitted")?;
        let next = locator["byte_end"]
            .as_u64()
            .context("native source byte end omitted")?;
        let length = u64::try_from(row.get::<_, i64>("logical_length"))?;
        ensure!(
            start == end
                && next.checked_sub(start) == Some(length)
                && locator["fragmented"].as_bool().is_some()
                && (identity.len() == 1 || locator["fragmented"] == true),
            "native source ranges contain a gap, overlap or mixed representation"
        );
        end = next;
        ensure!(end <= budget, "native source exceeds admitted spool budget");
        let id: i64 = row.get("id");
        fragments.push(NativeFragment {
            occurrence_id: id,
            start,
            end,
        });
        manifest.update(id.to_be_bytes());
        manifest.update(row.get::<_, i64>("body_id").to_be_bytes());
        manifest.update(row.get::<_, Vec<u8>>("digest"));
        manifest.update(start.to_be_bytes());
        manifest.update(end.to_be_bytes());
    }
    let key = hex::encode(manifest.finalize());
    let directory = root.join(".legacy-hit-bootstrap").join(source.to_string());
    fs::create_dir_all(&directory)?;
    fs::set_permissions(
        directory.parent().unwrap(),
        fs::Permissions::from_mode(0o700),
    )?;
    fs::set_permissions(&directory, fs::Permissions::from_mode(0o700))?;
    let data = directory.join("verified-source.native");
    let receipt = directory.join("verified-source.json");
    if let (Ok(data_meta), Ok(receipt_meta)) =
        (fs::symlink_metadata(&data), fs::symlink_metadata(&receipt))
    {
        ensure!(
            data_meta.is_file()
                && receipt_meta.is_file()
                && receipt_meta.len() <= 4096
                && data_meta.permissions().mode() & 0o077 == 0
                && receipt_meta.permissions().mode() & 0o077 == 0,
            "owned native source cache is not private regular data"
        );
        let prior: Value = serde_json::from_slice(&fs::read(&receipt)?)?;
        if prior["manifest_sha256"] == key && data_meta.len() == end {
            let bytes = hex::decode(
                prior["source_sha256"]
                    .as_str()
                    .context("cached native source digest omitted")?,
            )?;
            let hash: [u8; 32] = bytes
                .as_slice()
                .try_into()
                .context("cached native source digest differs")?;
            // align_verified_file checks the entire cached file digest again;
            // no cached byte can authorize a mapping without that check.
            return Ok(Some(SourceSpool {
                file: fs::File::open(data)?,
                hash,
                fragments,
            }));
        }
    }
    let mut temporary = tempfile::NamedTempFile::new_in(&directory)?;
    let mut output = DigestFile {
        file: temporary.as_file_mut(),
        hash: Sha256::new(),
    };
    for group in identity.chunks(32) {
        let ids = group
            .iter()
            .map(|row| row.get::<_, i64>("body_id"))
            .collect::<Vec<_>>();
        let rows=client.query(&format!("SELECT {BODY_COLUMNS} FROM content_body body {BODY_JOINS} WHERE body.id=ANY($1)"),&[&ids]).await?;
        let rows = rows
            .into_iter()
            .map(|row| (row.get::<_, i64>("body_id"), row))
            .collect::<BTreeMap<_, _>>();
        for item in group {
            let body = rows
                .get(&item.get::<_, i64>("body_id"))
                .context("native source body disappeared")?;
            ensure!(
                body.get::<_, Vec<u8>>("digest") == item.get::<_, Vec<u8>>("digest"),
                "native source identity drifted"
            );
            deliver_stored_body_row(body, root, buffer, Some(&mut output))?;
        }
    }
    output.flush()?;
    let hash: [u8; 32] = output.hash.finalize().into();
    temporary.as_file().sync_all()?;
    ensure!(
        temporary.as_file().metadata()?.len() == end,
        "native source spool length differs"
    );
    temporary.persist(&data).map_err(|error| error.error)?;
    let mut note = tempfile::NamedTempFile::new_in(&directory)?;
    note.write_all(&serde_json::to_vec(
        &json!({"manifest_sha256":key,"source_sha256":hex::encode(hash),"logical_bytes":end}),
    )?)?;
    note.as_file().sync_all()?;
    note.persist(receipt).map_err(|error| error.error)?;
    fs::File::open(&directory)?.sync_all()?;
    Ok(Some(SourceSpool {
        file: fs::File::open(data)?,
        hash,
        fragments,
    }))
}

async fn historical_bodies<C: GenericClient + Sync>(
    client: &C,
    chunks: &[VerifiedLegacyChunk],
    needed: &[usize],
    root: &Path,
    buffer: usize,
) -> Result<(BTreeMap<usize, i64>, u64)> {
    let mut groups = BTreeMap::<[u8; 32], Vec<usize>>::new();
    for &index in needed {
        groups
            .entry(chunks[index].digest())
            .or_default()
            .push(index);
    }
    if groups.is_empty() {
        return Ok((BTreeMap::new(), 0));
    }
    let digests = groups
        .keys()
        .map(|digest| digest.to_vec())
        .collect::<Vec<_>>();
    let rows = client
        .query(
            &format!(
                "SELECT {BODY_COLUMNS} FROM content_body body {BODY_JOINS} \
        WHERE body.digest_algorithm='sha256-v1' AND body.digest=ANY($1::BYTEA[])"
            ),
            &[&digests],
        )
        .await?;
    let mut bodies = BTreeMap::new();
    for row in rows {
        let digest: [u8; 32] = row
            .get::<_, Vec<u8>>("digest")
            .as_slice()
            .try_into()
            .context("native body digest differs")?;
        let members = groups
            .remove(&digest)
            .context("native historical body identity differs")?;
        ensure!(
            row.get::<_, i64>("logical_length") == i64::try_from(chunks[members[0]].bytes().len())?,
            "native historical body length differs"
        );
        deliver_stored_body_row(&row, root, buffer, None)?;
        for index in members {
            bodies.insert(index, row.get::<_, i64>("body_id"));
        }
    }
    if groups.is_empty() {
        return Ok((bodies, 0));
    }
    let pack = Uuid::new_v4();
    let nonce = Uuid::new_v4();
    let mut builder = PackBuilder::new(root, pack, nonce, buffer)?;
    let mut entries = Vec::new();
    for (digest, members) in groups {
        let entry = builder.add_reader(
            Cursor::new(chunks[members[0]].bytes()),
            BodyCodec::Zstd,
            None,
        )?;
        ensure!(
            entry.body.digest == digest,
            "historical pack source digest differs"
        );
        entries.push((entry, members));
    }
    let sealed = builder.seal()?;
    for (entry, _) in &entries {
        sealed.verify_entry(entry, None)?;
    }
    content_body::create_pack(client, pack, &format!("{pack}.pack"), nonce).await?;
    for (entry, members) in &entries {
        let body = content_body::put_packed_body(
            client,
            pack,
            i64::try_from(entry.ordinal)?,
            &entry.body.digest,
            i64::try_from(entry.body.logical_length)?,
            i64::try_from(entry.pack_offset)?,
            i64::try_from(entry.stored_length)?,
            entry.codec.database_name(),
            &entry.entry_digest,
        )
        .await?;
        for &index in members {
            bodies.insert(index, body.id);
        }
    }
    let stored = sealed.manifest.stored_bytes;
    content_body::verify_pack(
        client,
        pack,
        &sealed.manifest.sha256,
        i64::try_from(stored)?,
    )
    .await?;
    let published = sealed.publish()?;
    for (entry, _) in &entries {
        published.reader().verify_integrity(entry, None, buffer)?;
    }
    content_body::publish_pack(client, pack).await?;
    // On an unknown COMMIT outcome the caller reads committed hit proofs before
    // retrying. Never unlink this published pack from an HTTP error handler.
    Ok((bodies, stored))
}

pub async fn produce_batch<C: GenericClient + Sync>(
    client: &C,
    source: i64,
    input: &ProduceBatchInput,
    root: &Path,
    buffer: usize,
) -> Result<Value> {
    ensure!(
        source > 0
            && input.generation_id > 0
            && input.file_id > 0
            && input.after_hit_id >= 0
            && input.source_spool_budget_bytes > 0
            && input.source_spool_budget_bytes <= 4 * 1024 * 1024 * 1024,
        "bounded legacy producer identities and spool admission required"
    );
    let expected = hex::decode(&input.expected_file_sha256)?;
    ensure!(
        expected.len() == 32 && hex::encode(&expected) == input.expected_file_sha256,
        "canonical legacy file digest required"
    );
    client
        .execute(
            "SELECT storage_v2_require_test_scope($1,$2)",
            &[&Some(source), &input.include_test],
        )
        .await?;
    let revision: Value = client
        .query_one(
            "SELECT storage_v2_lock_legacy_hit_source($1,$2,$3)",
            &[&source, &input.file_id, &expected],
        )
        .await?
        .get(0);
    client
        .execute(
            "SELECT set_config('application_name',$1,true)",
            &[&format!("mainrag.legacy-hit-producer:{source}")],
        )
        .await?;
    ensure!(
        revision["file_revision"].as_i64() == Some(input.expected_file_revision)
            && revision["legacy_epoch"].as_i64() == Some(input.expected_legacy_epoch),
        "legacy inventory revision drifted"
    );
    let file = client
        .query_one(
            "SELECT path FROM files WHERE id=$1 AND source_id=$2 AND hash=$3",
            &[&input.file_id, &source, &expected],
        )
        .await?;
    let path: String = file.get("path");
    let generation=client.query_one("SELECT generation_seq FROM source_generation WHERE id=$1 AND source_id=$2 \
        AND abandoned_at IS NULL AND status::TEXT IN ('verified','release_candidate','active','superseded') \
        AND verification_manifest_sha256 IS NOT NULL FOR SHARE",&[&input.generation_id,&source]).await?;
    let sequence: i64 = generation.get(0);
    let metadata=client.query("SELECT id,octet_length(content_compressed)::BIGINT AS stored_bytes,\
        octet_length(content_text)::BIGINT AS text_bytes FROM chunks WHERE file_id=$1 AND id>$2 ORDER BY id LIMIT 512",&[&input.file_id,&input.after_hit_id]).await?;
    if metadata.is_empty() {
        return Ok(
            json!({"source_id":source,"generation_id":input.generation_id,"file_id":input.file_id,
        "after_hit_id":input.after_hit_id,"file_done":true,"processed":0}),
        );
    }
    let mut ids = Vec::new();
    let mut amount = 0_usize;
    for row in &metadata {
        let stored = usize::try_from(row.get::<_, i64>("stored_bytes"))?;
        let text = row
            .get::<_, Option<i64>>("text_bytes")
            .map(usize::try_from)
            .transpose()?;
        let bound = stored.max(text.unwrap_or(MAX_BATCH_BYTES));
        if !ids.is_empty()
            && amount
                .checked_add(bound)
                .is_none_or(|sum| sum > MAX_BATCH_BYTES)
        {
            break;
        }
        ensure!(
            bound <= MAX_BATCH_BYTES,
            "old chunk exceeds admitted batch byte budget"
        );
        ids.push(row.get::<_, i64>("id"));
        amount += bound;
    }
    ensure!(
        !ids.is_empty() && ids.len() <= MAX_HITS,
        "bounded old chunk selection required"
    );
    let rows = client
        .query(
            "SELECT id,content_hash,content_compressed,content_text,start_line,end_line \
        FROM chunks WHERE file_id=$1 AND id=ANY($2) ORDER BY id FOR SHARE",
            &[&input.file_id, &ids],
        )
        .await?;
    ensure!(rows.len() == ids.len(), "old chunk inventory changed");
    let mut chunks = Vec::new();
    let mut proofs = Vec::new();
    let mut bytes = 0_usize;
    for row in &rows {
        let digest: [u8; 32] = row
            .get::<_, Vec<u8>>("content_hash")
            .as_slice()
            .try_into()
            .context("old chunk digest differs")?;
        let encoded: Vec<u8> = row.get("content_compressed");
        let text: Option<String> = row.get("content_text");
        let chunk = VerifiedLegacyChunk::decode(
            row.get::<_, i64>("id").to_string(),
            &encoded,
            digest,
            text.as_deref(),
        )?;
        bytes = bytes
            .checked_add(chunk.bytes().len())
            .context("old chunk batch overflow")?;
        ensure!(
            bytes <= MAX_BATCH_BYTES,
            "old chunk batch exceeds byte budget"
        );
        proofs.push(json!({"chunk_sha256":hex::encode(digest),"file_sha256":input.expected_file_sha256,
            "file_id":input.file_id,"file_revision":input.expected_file_revision,
            "source_path":path,"logical_bytes":chunk.bytes().len(),"start_line":row.get::<_,i32>("start_line"),
            "end_line":row.get::<_,i32>("end_line")}));
        chunks.push(chunk);
    }
    let old_ids = chunks
        .iter()
        .map(|chunk| chunk.old_hit_id.clone())
        .collect::<Vec<_>>();
    let state: Value = client
        .query_one(
            "SELECT storage_v2_legacy_hit_mapping_states($1,$2)",
            &[&source, &old_ids],
        )
        .await?
        .get(0);
    let states = state["mappings"]
        .as_array()
        .context("old hit state envelope differs")?
        .iter()
        .map(|row| {
            Ok((
                row["old_hit_id"]
                    .as_str()
                    .context("old hit identity omitted")?
                    .to_owned(),
                row["expected_mapping_sha256"]
                    .as_str()
                    .context("old hit mapping digest omitted")?
                    .to_owned(),
            ))
        })
        .collect::<Result<BTreeMap<_, _>>>()?;
    let prior = client
        .query(
            "SELECT old_hit_id,proof,mapping_sha256 FROM storage_v2_legacy_hit_proof \
        WHERE source_id=$1 AND generation_id=$2 AND old_hit_id=ANY($3)",
            &[&source, &input.generation_id, &old_ids],
        )
        .await?;
    let mut completed = std::collections::BTreeSet::new();
    for row in prior {
        let id: String = row.get("old_hit_id");
        let proof: Value = row.get("proof");
        if let Some(index) = old_ids.iter().position(|old| old == &id) {
            if proofs[index]
                .as_object()
                .unwrap()
                .iter()
                .all(|(key, value)| proof.get(key) == Some(value))
                && states.get(&id) == Some(&row.get::<_, String>("mapping_sha256"))
            {
                completed.insert(index);
            }
        }
    }
    let pending = chunks
        .iter()
        .enumerate()
        .filter(|(index, _)| !completed.contains(index))
        .map(|(_, chunk)| chunk.old_hit_id.clone())
        .collect::<Vec<_>>();
    if pending.is_empty() {
        return finish_batch(
            client,
            source,
            input,
            *ids.last().unwrap(),
            ids.len(),
            0,
            0,
            sequence,
            root,
        )
        .await;
    }
    content_body::with_reader_epoch(client,async {
        let spool=source_spool(client,source,input.generation_id,&path,input.source_spool_budget_bytes,root,buffer).await?;
        let plan=if let Some(spool)=spool {
            Some(align_verified_file(spool.file,spool.hash,&spool.fragments,&chunks,buffer)?)
        } else {None};
        let needed=(0..chunks.len()).filter(|index|!completed.contains(index) && plan.as_ref()
            .is_none_or(|plan|plan.hits[*index].requires_preserved_history)).collect::<Vec<_>>();
        let (history,stored)=historical_bodies(client,&chunks,&needed,root,buffer).await?;
        let mut records=Vec::new();let mut targets=0;
        for (index,chunk) in chunks.iter().enumerate().filter(|(index,_)|!completed.contains(index)) {
            let mut proof=proofs[index].clone();let mut occurrence_ids=Vec::new();let mut overlaps=Vec::new();let mut offsets=Vec::new();
            if let Some(plan)=&plan { if !plan.hits[index].requires_preserved_history {
                let hit=&plan.hits[index];proof["native_file_sha256"]=json!(plan.source_sha256);
                proof["byte_start"]=json!(hit.byte_start);proof["byte_end"]=json!(hit.byte_end);
                for target in &hit.targets {occurrence_ids.push(target.occurrence_id);overlaps.push(target.byte_overlap);offsets.push(target.source_offset);}
            }}
            targets+=occurrence_ids.len();ensure!(targets<=2048,"legacy mapping batch exceeds native target budget");
            records.push(json!({"mapping":{"old_hit_id":chunk.old_hit_id,"expected_mapping_sha256":states[&chunk.old_hit_id],
                "occurrence_ids":occurrence_ids,"byte_overlaps":overlaps,"source_offsets":offsets,
                "relation_kind":if occurrence_ids.len()>1 {"split"} else {"exact"}},
                "proof":proof,"history_body_id":history.get(&index)}));
        }
        let applied:Value=client.query_one("SELECT storage_v2_complete_legacy_hit_batch($1,$2,$3)",
            &[&source,&input.generation_id,&json!(records)]).await?.get(0);
        let mut result=finish_batch(client,source,input,*ids.last().unwrap(),ids.len(),records.len(),stored,sequence,root).await?;
        result["batch_proof"]=applied;result["reused_proofs"]=json!(completed.len());Ok(result)
    }).await
}

async fn finish_batch<C: GenericClient + Sync>(
    client: &C,
    source: i64,
    input: &ProduceBatchInput,
    last: i64,
    selected: usize,
    processed: usize,
    stored: u64,
    sequence: i64,
    root: &Path,
) -> Result<Value> {
    let remaining: bool = client
        .query_one(
            "SELECT EXISTS(SELECT 1 FROM chunks WHERE file_id=$1 AND id>$2)",
            &[&input.file_id, &last],
        )
        .await?
        .get(0);
    if !remaining {
        // These two names belong only to this producer. Their data is a
        // disposable decoded native file, never a published pack or old file.
        let directory = root.join(".legacy-hit-bootstrap").join(source.to_string());
        for name in ["verified-source.native", "verified-source.json"] {
            match fs::remove_file(directory.join(name)) {
                Ok(()) => {}
                Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
                Err(error) => return Err(error.into()),
            }
        }
    }
    Ok(
        json!({"schema_version":"mainrag.storage-v2.legacy-hit-producer.v1","source_id":source,
        "generation_id":input.generation_id,"generation_seq":sequence,"file_id":input.file_id,"after_hit_id":last,
        "file_done":!remaining,"selected":selected,"processed":processed,"pack_stored_bytes":stored}),
    )
}

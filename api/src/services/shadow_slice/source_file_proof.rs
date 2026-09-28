//! Full-file digest from verified, ordered immutable source bodies.

use super::*;
use std::io::{self, Write};

struct DigestWriter(Sha256);
impl Write for DigestWriter {
    fn write(&mut self, bytes: &[u8]) -> io::Result<usize> {
        self.0.update(bytes);
        Ok(bytes.len())
    }
    fn flush(&mut self) -> io::Result<()> {
        Ok(())
    }
}

fn range_end(
    offset: u64,
    start: u64,
    end: u64,
    length: u64,
    fragmented: Option<bool>,
    total: i64,
) -> Result<u64> {
    if start != offset
        || end.checked_sub(start) != Some(length)
        || fragmented.is_none()
        || total > 1 && fragmented != Some(true)
    {
        bail!("complete source byte ranges contain a gap, overlap or mixed representation");
    }
    Ok(end)
}

pub(super) async fn complete_source_file<C: GenericClient + Sync>(
    client: &C,
    source_id: i64,
    input: &CandidateQueryEvidenceInput,
    evidence: &serde_json::Value,
    path_sha: &str,
    pack_root: &Path,
    io_buffer_bytes: usize,
) -> Result<serde_json::Value> {
    let generation_seq = evidence["generation_seq"]
        .as_i64()
        .context("missing generation sequence")?;
    let scope="FROM occurrence occurrence_row \
        JOIN artifact_version artifact ON artifact.id=occurrence_row.artifact_version_id \
        JOIN generation_item_version membership ON membership.source_id=occurrence_row.source_id \
            AND membership.source_item_id=artifact.item_id AND membership.artifact_version_id=artifact.id \
        JOIN content_node node ON node.id=artifact.content_root_node_id \
        JOIN content_body body ON body.id=node.body_id \
        LEFT JOIN content_pack pack ON pack.id=body.pack_id \
        LEFT JOIN content_pack_entry entry ON entry.pack_id=body.pack_id AND entry.body_id=body.id \
        LEFT JOIN content_dictionary dictionary ON dictionary.id=entry.dictionary_id \
        WHERE occurrence_row.source_id=$1 \
            AND encode(sha256(convert_to(occurrence_row.source_path,'UTF8')),'hex')=$2 \
            AND membership.valid_from_seq <= $3 \
            AND (membership.valid_to_seq IS NULL OR membership.valid_to_seq > $3)";
    let total: i64 = client
        .query_one(
            &format!("SELECT count(*) {scope}"),
            &[&source_id, &path_sha, &generation_seq],
        )
        .await?
        .get(0);
    if total <= 0 {
        bail!("complete source proof has no named-generation bodies");
    }
    let mut output = DigestWriter(Sha256::new());
    let mut manifest = Sha256::new();
    manifest.update(b"mainrag.storage-v2.complete-source-file.v1\0");
    let mut count = 0_i64;
    let mut offset = 0_u64;
    while count < total {
        // A fixed row batch bounds inline body memory as well as pack decoding.
        let rows=client.query(&format!("SELECT occurrence_row.id AS occurrence_id,occurrence_row.locator, \
            body.digest_algorithm,body.digest,body.logical_length,body.inline_bytes,body.pack_id, \
            pack.storage_key,pack.stored_bytes,pack.status::TEXT AS pack_status,entry.ordinal, \
            entry.pack_offset,entry.stored_length,entry.codec::TEXT AS codec,entry.entry_digest, \
            dictionary.id AS dictionary_id,dictionary.digest AS dictionary_digest,dictionary.dictionary_bytes \
            {scope} ORDER BY (occurrence_row.locator->>'byte_start')::BIGINT,occurrence_row.id \
            LIMIT 32 OFFSET $4"),&[&source_id,&path_sha,&generation_seq,&count]).await?;
        if rows.is_empty() {
            bail!("complete source body set changed during proof");
        }
        for row in rows {
            let locator: serde_json::Value = row.get("locator");
            let start = locator["byte_start"]
                .as_u64()
                .context("source body omitted byte start")?;
            let end = locator["byte_end"]
                .as_u64()
                .context("source body omitted byte end")?;
            let length = u64::try_from(row.get::<_, i64>("logical_length"))?;
            let next = range_end(
                offset,
                start,
                end,
                length,
                locator["fragmented"].as_bool(),
                total,
            )?;
            let delivered =
                deliver_stored_body_row(&row, pack_root, io_buffer_bytes, Some(&mut output))?;
            if delivered != length {
                bail!("complete source body delivery length differs");
            }
            manifest.update(row.get::<_, i64>("occurrence_id").to_be_bytes());
            manifest.update(start.to_be_bytes());
            manifest.update(end.to_be_bytes());
            manifest.update(row.get::<_, Vec<u8>>("digest"));
            offset = next;
            count += 1;
        }
    }
    if count != total {
        bail!("complete source body count differs");
    }
    Ok(
        json!({"schema_version":"mainrag.storage-v2.complete-source-file.v1",
        "source_id":source_id,"generation_id":input.generation_id,"generation_seq":generation_seq,
        "commit_sha":input.commit_sha,"path_sha256":path_sha,
        "body_sha256":hex::encode(output.0.finalize()),"item_manifest_sha256":hex::encode(manifest.finalize()),
        "fragment_count":count,"logical_bytes":offset,"byte_start":0,"byte_end":offset,
        "all_fragments_verified":true}),
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn full_file_hash_streams_verified_unicode_fragments_and_rejects_incomplete_ranges() {
        let directory = tempfile::tempdir().unwrap();
        let pack_id = Uuid::new_v4();
        let mut builder =
            PackBuilder::new(directory.path(), pack_id, Uuid::new_v4(), 4096).unwrap();
        let parts = ["alpha Über🙂\n".as_bytes(), "beta 甲\n".as_bytes()];
        let entries = parts
            .iter()
            .map(|bytes| {
                builder
                    .add_reader(Cursor::new(bytes), BodyCodec::Zstd, None)
                    .unwrap()
            })
            .collect::<Vec<_>>();
        let pack = builder.seal().unwrap().publish().unwrap();
        let reader = pack.reader();
        let mut output = DigestWriter(Sha256::new());
        let mut offset = 0;
        for entry in &entries {
            let next = range_end(
                offset,
                offset,
                offset + entry.body.logical_length,
                entry.body.logical_length,
                Some(true),
                2,
            )
            .unwrap();
            reader
                .verify_to_staging(entry, None, directory.path(), 4096)
                .unwrap()
                .copy_to(&mut output)
                .unwrap();
            offset = next;
        }
        assert_eq!(
            hex::encode(output.0.finalize()),
            hex::encode(Sha256::digest(parts.concat()))
        );
        assert!(range_end(5, 6, 9, 3, Some(true), 2).is_err());
        assert!(range_end(5, 4, 9, 5, Some(true), 2).is_err());
        assert!(range_end(5, 5, 9, 3, Some(true), 2).is_err());
        assert!(range_end(0, 0, 0, 0, None, 1).is_err());
        assert!(range_end(0, 0, 4, 4, Some(false), 2).is_err());
        assert_eq!(range_end(0, 0, 0, 0, Some(false), 1).unwrap(), 0);
        let mut corrupt = entries[0].clone();
        corrupt.body.digest[0] ^= 1;
        assert!(reader
            .verify_to_staging(&corrupt, None, directory.path(), 4096)
            .is_err());
    }
}

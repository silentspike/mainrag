use super::*;
use std::io::Cursor;

fn identity(bytes: &[u8]) -> CompleteFileIdentity {
    CompleteFileIdentity {
        source_id: 3,
        file_id: 99100,
        revision: 0,
        sha256: hex::encode(Sha256::digest(bytes)),
        logical_bytes: bytes.len() as u64,
        path_sha256: hex::encode(Sha256::digest(b"/synthetic/full-file.txt")),
    }
}
#[test]
fn namespace_is_disjoint_canonical_and_revision_specific() -> Result<()> {
    let mut first = identity(b"public sample");
    let id = first.mapping_id()?;
    ensure!(
        id.starts_with("legacy-file-v1:3:99100:0:13:"),
        "namespace identity differs"
    );
    first.revision += 1;
    ensure!(first.mapping_id()? != id, "revision was not bound");
    first.sha256 = "A".repeat(64);
    ensure!(first.mapping_id().is_err(), "noncanonical SHA accepted");
    first = identity(b"");
    first.source_id = 0;
    ensure!(first.mapping_id().is_err(), "invalid source accepted");
    Ok(())
}
#[test]
fn local_transport_rejects_remote_hostaddr_overrides() -> Result<()> {
    for connection in [
        "host=/var/run/postgresql",
        "host=127.0.0.1",
        "host=::1",
        "host=127.0.0.1 hostaddr=127.0.0.2",
    ] {
        ensure!(
            local_complete_file_connection(&connection.parse()?),
            "local complete-file transport rejected"
        );
    }
    for connection in [
        "host=localhost",
        "host=203.0.113.1",
        "host=127.0.0.1 hostaddr=203.0.113.1",
        "host=/var/run/postgresql hostaddr=203.0.113.1",
        "host=127.0.0.1,127.0.0.2 hostaddr=127.0.0.1,203.0.113.1",
        "hostaddr=127.0.0.1",
    ] {
        ensure!(
            !local_complete_file_connection(&connection.parse()?),
            "nonlocal or ambiguous complete-file transport accepted"
        );
    }
    Ok(())
}
#[test]
fn source_offset_order_and_complete_bounds_are_required() -> Result<()> {
    let (pack, scratch) = pack_allocation_bound(65536)?;
    ensure!(
        pack >= 65536 && scratch >= 2 * 65536 + pack,
        "preallocation reserve is not conservative"
    );
    let mut next = 0;
    for (offset, length) in [(0, 2), (2, 7), (9, 1)] {
        next = advance_range(next, offset, length, length, 3, 10)?;
    }
    ensure!(next == 10, "ordered full file differs");
    for (offset, overlap, length, count) in [(1, 1, 1, 1), (0, 2, 1, 1), (0, 0, 0, 2)] {
        ensure!(
            advance_range(0, offset, overlap, length, count, 10).is_err(),
            "invalid range accepted"
        );
    }
    ensure!(
        advance_range(0, 0, 0, 0, 1, 0)? == 0,
        "single empty file rejected"
    );
    ensure!(
        advance_range(u64::MAX, u64::MAX, 1, 1, 1, u64::MAX).is_err(),
        "overflow accepted"
    );
    ensure!(
        advance_range(0, 0, 1024 * 1024, 1024 * 1024, 1, 1).is_err(),
        "oversized component must fail before any pack staging"
    );
    Ok(())
}
#[test]
fn digest_staging_refuses_overrun_before_publication() -> Result<()> {
    let mut file = tempfile::tempfile()?;
    let mut output = DigestWriter {
        output: &mut file,
        hash: Sha256::new(),
        bytes: 0,
        limit: 4,
    };
    output.write_all(b"test")?;
    ensure!(
        output.write_all(b"x").is_err() && output.bytes == 4,
        "bounded staging overrun"
    );
    ensure!(
        hex::encode(output.hash.finalize()) == hex::encode(Sha256::digest(b"test")),
        "digest differs"
    );
    Ok(())
}

#[test]
fn encoded_cap_is_not_silent_eof_and_multiframe_is_exact() -> Result<()> {
    let mut a = zstd::encode_all(&b"alpha"[..], 1)?;
    a.extend(zstd::encode_all(&b"beta"[..], 1)?);
    let mut decoded = Vec::new();
    LazyZstd::new(BoundedEncoded::new(Cursor::new(&a), a.len() as u64)?)
        .read_to_end(&mut decoded)?;
    ensure!(decoded == b"alphabeta", "multi-frame raw bytes differ");
    let mut bounded = BoundedEncoded::new(Cursor::new(b"12345"), 4)?;
    ensure!(
        bounded.read_to_end(&mut Vec::new()).is_err(),
        "cap was accepted as a fabricated EOF"
    );
    let mut trailing = a.clone();
    trailing.extend_from_slice(b"not another frame");
    ensure!(
        LazyZstd::new(BoundedEncoded::new(
            Cursor::new(&trailing),
            trailing.len() as u64
        )?)
        .read_to_end(&mut Vec::new())
        .is_err(),
        "trailing encoded garbage accepted"
    );
    ensure!(
        LazyZstd::new(BoundedEncoded::new(
            Cursor::new(&a[..a.len() - 1]),
            a.len() as u64
        )?)
        .read_to_end(&mut Vec::new())
        .is_err(),
        "truncated final frame accepted"
    );
    Ok(())
}

/// Root runs once in the existing explicitly disposable schema fixture after
/// loading the proposed read boundary. No production URL is ever accepted.
#[tokio::test]
#[ignore = "requires an explicitly prepared disposable native complete-file fixture"]
async fn native_zero_hit_preservation_authorized_read_after_legacy_drop_and_unknown() -> Result<()>
{
    use tokio_postgres::NoTls;
    const ADMIN: &str = "00000000-0000-4000-8000-000000000031";
    const WRITER: &str = "00000000-0000-4000-8000-000000000032";
    const DENIED: &str = "00000000-0000-4000-8000-000000000033";
    let url = std::env::var("MAINRAG_COMPLETE_FILE_FIXTURE_URL")
        .context("explicit disposable URL required")?;
    let config: tokio_postgres::Config = url.parse()?;
    let name = config.get_dbname().context("fixture name required")?;
    ensure!(
        name.starts_with("storage_v2_ingest_")
            && name.len() == 50
            && name[18..].bytes().all(|b| b.is_ascii_hexdigit()),
        "refusing nonfixture database"
    );
    let (mut client, connection) = config.connect(NoTls).await?;
    tokio::spawn(async move {
        let _ = connection.await;
    });
    client
        .batch_execute(&format!("SET ROLE mainrag;SET app.user_id='{ADMIN}'"))
        .await?;
    let raw = ("complete historical α🙂 東京\n".repeat(9000)).into_bytes();
    let mut id = identity(&raw);
    // The original metadata hash deliberately differs from complete raw bytes.
    let legacy_hash = hex::encode(Sha256::digest(b"original legacy extracted-text hash"));
    let digest = hex::decode(&legacy_hash)?;
    let compressed = zstd::encode_all(raw.as_slice(), 1)?;
    client.execute("INSERT INTO files(id,source_id,path,hash,content,content_text,size_original,size_compressed,last_modified) \
        VALUES($1,3,'/synthetic/full-file.txt',$2,$3,NULL,$4,$5,NOW())",
        &[&id.file_id,&digest,&compressed,&i32::try_from(raw.len())?,&i32::try_from(compressed.len())?]).await?;
    ensure!(
        client
            .query_one(
                "SELECT count(*) FROM chunks WHERE file_id=$1",
                &[&id.file_id]
            )
            .await?
            .get::<_, i64>(0)
            == 0,
        "zero-hit original fixture required"
    );
    let lock: Value = client
        .query_one(
            "SELECT storage_v2_lock_legacy_hit_source(3,$1,$2)",
            &[&id.file_id, &digest],
        )
        .await?
        .get(0);
    id.revision = lock["file_revision"]
        .as_i64()
        .context("revision required")?;
    let state: String = client
        .query_one(
            "SELECT storage_v2_legacy_hit_mapping_state($1)",
            &[&id.mapping_id()?],
        )
        .await?
        .get(0);
    let before:Value=client.query_one("SELECT jsonb_build_array((SELECT count(*) FROM source_generation),(SELECT count(*) FROM generation_item_version))",&[]).await?.get(0);
    let mut request = Registration {
        identity: id.clone(),
        legacy_file_hash_sha256: legacy_hash.clone(),
        generation: "1".into(),
        expected_legacy_epoch: lock["legacy_epoch"].as_i64().context("epoch required")?,
        expected_mapping_sha256: state,
        maximum_logical_bytes: raw.len() as u64,
        maximum_pack_bytes: 2 * 1024 * 1024,
        maximum_scratch_bytes: 4 * 1024 * 1024,
        include_test: false,
    };
    let root = tempfile::tempdir()?;
    let _lease = registration_lease(root.path())?;
    let tx = begin_complete_file_transaction(&mut client).await?;
    let created = register_payload(&tx, &request, Cursor::new(&raw), root.path(), 4096).await?;
    ensure!(
        created["read_proof"]["full_raw_integrity_verified"] == true
            && created["new_payload_bytes"].as_u64().is_some_and(|n| n > 0),
        "native byte publication was not proved"
    );
    tx.commit().await?;
    let occurrence: i64 = client
        .query_one(
            "SELECT occurrence_id FROM legacy_hit_mapping WHERE old_hit_id=$1",
            &[&id.mapping_id()?],
        )
        .await?
        .get(0);
    request.expected_mapping_sha256 = created["mapping_sha256"]
        .as_str()
        .context("committed mapping hash required")?
        .into();
    // Same actual body and mapping: no new payload, generation or membership.
    let tx = client.transaction().await?;
    let reused = register_existing(
        &tx,
        &request,
        &[ExistingFragment {
            occurrence_id: occurrence,
            source_offset: 0,
            logical_bytes: raw.len() as i64,
        }],
        root.path(),
        4096,
    )
    .await?;
    ensure!(
        reused["new_payload_bytes"] == 0,
        "existing body was duplicated"
    );
    tx.commit().await?;
    struct NeverRead;
    impl Read for NeverRead {
        fn read(&mut self, _: &mut [u8]) -> std::io::Result<usize> {
            Err(std::io::Error::other(
                "deduplicated native body must not read input",
            ))
        }
    }
    let tx = client.transaction().await?;
    let replay = register_payload(
        &tx,
        &request,
        LazyZstd::new(BoundedEncoded::new(NeverRead, 1)?),
        root.path(),
        4096,
    )
    .await?;
    ensure!(
        replay["new_payload_bytes"] == 0,
        "same body replay copied original payload"
    );
    tx.commit().await?;
    let after:Value=client.query_one("SELECT jsonb_build_array((SELECT count(*) FROM source_generation),(SELECT count(*) FROM generation_item_version))",&[]).await?.get(0);
    ensure!(before == after, "preservation changed generation state");
    let tx = client.transaction().await?;
    let mut stale = request.clone();
    stale.expected_mapping_sha256 = "0".repeat(64);
    ensure!(
        register_existing(
            &tx,
            &stale,
            &[ExistingFragment {
                occurrence_id: occurrence,
                source_offset: 0,
                logical_bytes: raw.len() as i64
            }],
            root.path(),
            4096
        )
        .await
        .is_err(),
        "stale CAS accepted"
    );
    tx.rollback().await?;
    let page_raw = (0..33)
        .map(|i| format!("part {i:02} α🙂 {}\n", "x".repeat(i)))
        .collect::<String>()
        .into_bytes();
    let mut page_id = identity(&page_raw);
    page_id.file_id = 99101;
    page_id.path_sha256 = hex::encode(Sha256::digest(b"/synthetic/full-page.txt"));
    let page_hash = hex::decode(&page_id.sha256)?;
    let page_zstd = zstd::encode_all(page_raw.as_slice(), 1)?;
    client.execute("INSERT INTO files(id,source_id,path,hash,content,content_text,size_original,size_compressed,last_modified) \
        VALUES($1,3,'/synthetic/full-page.txt',$2,$3,NULL,$4,$5,NOW())",
        &[&page_id.file_id,&page_hash,&page_zstd,&i32::try_from(page_raw.len())?,&i32::try_from(page_zstd.len())?]).await?;
    let page_lock: Value = client
        .query_one(
            "SELECT storage_v2_lock_legacy_hit_source(3,$1,$2)",
            &[&page_id.file_id, &page_hash],
        )
        .await?
        .get(0);
    page_id.revision = page_lock["file_revision"]
        .as_i64()
        .context("page revision required")?;
    let page_state: String = client
        .query_one(
            "SELECT storage_v2_legacy_hit_mapping_state($1)",
            &[&page_id.mapping_id()?],
        )
        .await?
        .get(0);
    let page_request = Registration {
        identity: page_id.clone(),
        legacy_file_hash_sha256: page_id.sha256.clone(),
        generation: "1".into(),
        expected_legacy_epoch: page_lock["legacy_epoch"]
            .as_i64()
            .context("page epoch required")?,
        expected_mapping_sha256: page_state,
        maximum_logical_bytes: page_raw.len() as u64,
        maximum_pack_bytes: 2 * 1024 * 1024,
        maximum_scratch_bytes: 4 * 1024 * 1024,
        include_test: false,
    };
    let rows=client.query("SELECT id,(locator->>'byte_start')::BIGINT AS start,(locator->>'byte_end')::BIGINT-(locator->>'byte_start')::BIGINT AS length \
        FROM occurrence WHERE source_id=3 AND source_path='/synthetic/full-page.txt' ORDER BY (locator->>'byte_start')::BIGINT",&[]).await?;
    ensure!(
        rows.len() == 33,
        "companion fixture must supply33 native windows"
    );
    let fragments = rows
        .iter()
        .map(|row| ExistingFragment {
            occurrence_id: row.get("id"),
            source_offset: row.get("start"),
            logical_bytes: row.get("length"),
        })
        .collect::<Vec<_>>();
    let tx = client.transaction().await?;
    let page_proof = register_existing(&tx, &page_request, &fragments, root.path(), 4096).await?;
    ensure!(
        page_proof["read_proof"]["fragments"] == 33 && page_proof["new_payload_bytes"] == 0,
        "paged native windows were not reused"
    );
    tx.commit().await?;
    let offsets = client
        .query(
            "SELECT source_offset FROM legacy_hit_mapping WHERE old_hit_id=$1 ORDER BY ordinal",
            &[&page_id.mapping_id()?],
        )
        .await?
        .iter()
        .map(|r| r.get::<_, i64>(0))
        .collect::<Vec<_>>();
    ensure!(
        offsets.windows(2).any(|v| v[0] > v[1]),
        "fixture must exercise mapping ordinal distinct from source order"
    );
    let page1 = client
        .query(
            "SELECT * FROM storage_v2_complete_file_components(3,'1',$1,-1,false)",
            &[&page_id.mapping_id()?],
        )
        .await?;
    ensure!(page1.len() == 32, "first native page bound differs");
    let cursor: i64 = page1.last().context("page1 required")?.get("source_offset");
    let page2 = client
        .query(
            "SELECT * FROM storage_v2_complete_file_components(3,'1',$1,$2,false)",
            &[&page_id.mapping_id()?, &cursor],
        )
        .await?;
    ensure!(page2.len() == 1, "last native page differs");
    // Existing full-file service is the caller after legacy content is gone.
    client
        .batch_execute("DROP TABLE chunks CASCADE;DROP TABLE files CASCADE")
        .await?;
    client
        .batch_execute(&format!("SET app.user_id='{WRITER}'"))
        .await?;
    // Use the CLI's actual transaction path: the SQL reader epoch rejects
    // repeatable-read isolation even when service-only autocommit reads pass.
    let read_tx = begin_complete_file_transaction(&mut client).await?;
    let mut verified = read_verified(
        &read_tx,
        &id,
        "1",
        false,
        raw.len() as u64,
        root.path(),
        4096,
    )
    .await?;
    let mut output = Vec::new();
    verified.copy_to(&mut output)?;
    ensure!(
        output == raw,
        "nonadmin authorized complete bytes differ after legacy drop"
    );
    read_tx.commit().await?;
    let first: i64 = client
        .query_one(
            "SELECT count(*) FROM storage_v2_complete_file_components(3,'1',$1,-1,false)",
            &[&id.mapping_id()?],
        )
        .await?
        .get(0);
    let eof: i64 = client
        .query_one(
            "SELECT count(*) FROM storage_v2_complete_file_components(3,'1',$1,0,false)",
            &[&id.mapping_id()?],
        )
        .await?
        .get(0);
    ensure!(first == 1 && eof == 0, "component keyset boundary differs");
    let native_pack = std::fs::read_dir(root.path())?
        .filter_map(|r| r.ok())
        .find(|r| r.path().extension().is_some_and(|v| v == "pack"))
        .context("actual published native pack required")?
        .path();
    let original = std::fs::read(&native_pack)?;
    let mut corrupt = original.clone();
    ensure!(!corrupt.is_empty(), "published pack is empty");
    let last = corrupt.len() - 1;
    corrupt[last] ^= 1;
    std::fs::write(&native_pack, &corrupt)?;
    ensure!(
        read_verified(
            &client,
            &id,
            "1",
            false,
            raw.len() as u64,
            root.path(),
            4096
        )
        .await
        .is_err(),
        "physical native pack corruption escaped integrity verification"
    );
    std::fs::write(&native_pack, &original)?;
    client
        .batch_execute(&format!("SET app.user_id='{ADMIN}'"))
        .await?;
    let tx = client.transaction().await?;
    tx.query(
        "SELECT * FROM storage_v2_replace_legacy_hit_mapping($1,$2,'exact',$3,$4)",
        &[
            &id.mapping_id()?,
            &vec![occurrence],
            &vec![raw.len() as i64],
            &vec![1i64],
        ],
    )
    .await?;
    ensure!(
        read_verified(&tx, &id, "1", false, raw.len() as u64, root.path(), 4096)
            .await
            .is_err(),
        "gap in native complete-file mapping escaped verification"
    );
    tx.rollback().await?;
    // A genuine generation-member body cannot be returned under a complete
    // identity smaller than that body, even before inline bytes are projected.
    let mut too_short = page_id.clone();
    too_short.logical_bytes = 1;
    too_short.sha256 = hex::encode(Sha256::digest(b"x"));
    let tx = begin_complete_file_transaction(&mut client).await?;
    tx.query(
        "SELECT * FROM storage_v2_replace_legacy_hit_mapping($1,$2,'exact',$3,$4)",
        &[
            &too_short.mapping_id()?,
            &vec![fragments[0].occurrence_id],
            &vec![fragments[0].logical_bytes],
            &vec![0i64],
        ],
    )
    .await?;
    let rejected = tx
        .query(
            "SELECT * FROM storage_v2_complete_file_components(3,'1',$1,-1,false)",
            &[&too_short.mapping_id()?],
        )
        .await;
    ensure!(
        rejected
            .as_ref()
            .err()
            .and_then(|error| error.as_db_error())
            .is_some_and(|error| error.message()
                == "native component outside admitted complete-file byte range"),
        "SQL must reject an oversized component before returning payload"
    );
    tx.rollback().await?;
    client
        .batch_execute(&format!("SET app.user_id='{WRITER}'"))
        .await?;
    let reconciliation = reconcile(&client, &id, "1", false).await?;
    ensure!(
        reconciliation["new_dispatches"] == 0
            && reconciliation["automatic_retry_allowed"] == false
            && reconciliation["full_raw_integrity_verified"] == false,
        "UNKNOWN reconciliation fabricated byte acceptance"
    );
    let mut bad = id.clone();
    bad.sha256 = "0".repeat(64);
    ensure!(
        read_verified(
            &client,
            &bad,
            "1",
            false,
            raw.len() as u64,
            root.path(),
            4096
        )
        .await
        .is_err(),
        "wrong full SHA accepted"
    );
    bad = id.clone();
    bad.path_sha256 = "0".repeat(64);
    ensure!(
        read_verified(
            &client,
            &bad,
            "1",
            false,
            raw.len() as u64,
            root.path(),
            4096
        )
        .await
        .is_err(),
        "wrong original path accepted"
    );
    client
        .batch_execute(&format!("SET app.user_id='{DENIED}'"))
        .await?;
    ensure!(
        read_verified(
            &client,
            &id,
            "1",
            false,
            raw.len() as u64,
            root.path(),
            4096
        )
        .await
        .is_err(),
        "unauthorized source bytes exposed"
    );
    ensure!(
        client
            .query(
                "SELECT * FROM storage_v2_complete_file_components(3,'1',$1,-1,false)",
                &[&id.mapping_id()?]
            )
            .await
            .is_err(),
        "direct metadata authorization bypass"
    );
    client
        .batch_execute(&format!("SET app.user_id='{WRITER}'"))
        .await?;
    bad = id.clone();
    bad.source_id = 2;
    ensure!(
        read_verified(
            &client,
            &bad,
            "1",
            false,
            raw.len() as u64,
            root.path(),
            4096
        )
        .await
        .is_err(),
        "cross-source read accepted"
    );
    Ok(())
}

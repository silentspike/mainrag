//! Additive maintenance API. Callers supply stable operation identities and an
//! already accepted global GC epoch. This module never advances GC authority.
//! All maintenance of a pack root must share its file lock. No background file
//! tasks escape an invocation, including cancellation. Network filesystems and
//! out-of-band administrative placement mutations are not qualified here.

use super::content_store::{
    BodyCodec, BodyIdentity, DictionaryIdentity, PackBuilder, PackEntry, PackManifest, PackReader,
};
use crate::db::content_body;
use anyhow::{bail, ensure, Context, Result};
use std::fs::{self, File, OpenOptions};
use std::path::{Path, PathBuf};
use tokio_postgres::{Client, GenericClient};
use uuid::Uuid;

#[derive(Debug, Clone)]
pub struct RepackPolicy {
    pub minimum_dead_bytes: u64,
    pub minimum_dead_basis_points: u16,
    pub max_entries: usize,
    pub max_logical_bytes: u64,
    pub reserve_free_bytes: u64,
    pub io_buffer_bytes: usize,
    pub codec: BodyCodec,
}

impl RepackPolicy {
    fn validate(&self) -> Result<()> {
        ensure!(
            (1..=65536).contains(&self.max_entries),
            "invalid repack entry bound"
        );
        ensure!(
            (4096..=1048576).contains(&self.io_buffer_bytes),
            "invalid repack buffer bound"
        );
        ensure!(
            self.minimum_dead_basis_points <= 10000 && self.max_logical_bytes > 0,
            "invalid repack policy"
        );
        Ok(())
    }
}

#[derive(Debug, serde::Serialize)]
pub struct RepackReport {
    pub old_pack: Uuid,
    pub new_pack: Uuid,
    pub moved_entries: usize,
    pub moved_logical_bytes: u64,
    pub old_file_bytes: u64,
    pub new_file_bytes: u64,
    pub excluded_entry_bytes: u64,
    pub writer_buffer_bytes: usize,
    pub resumed_after_switch: bool,
}

#[derive(Debug, serde::Serialize)]
pub struct RemovalReport {
    pub pack_id: Uuid,
    pub file_bytes: u64,
    pub unlinked_this_call: bool,
    pub receipt_already_present: bool,
}

struct LoadedPack {
    status: String,
    manifest: PackManifest,
    bodies: Vec<i64>,
    assigned_here: Vec<bool>,
}

fn lock_root(root: &Path) -> Result<(PathBuf, File)> {
    let root = root.canonicalize()?;
    ensure!(root.is_dir(), "pack root must exist");
    let path = root.join(".maintenance.lock");
    if let Ok(metadata) = fs::symlink_metadata(&path) {
        ensure!(
            metadata.is_file(),
            "maintenance lock must be a regular file"
        );
    }
    let lock = OpenOptions::new()
        .create(true)
        .truncate(false)
        .read(true)
        .write(true)
        .open(path)?;
    lock.try_lock()
        .context("pack maintenance is already active or locking is unavailable")?;
    // Keep this inode: deleting a lock file permits a second independent lock.
    Ok((root, lock))
}

fn available_bytes(root: &Path) -> Result<u64> {
    let disks = sysinfo::Disks::new_with_refreshed_list();
    disks
        .iter()
        .filter(|disk| root.starts_with(disk.mount_point()))
        .max_by_key(|disk| disk.mount_point().components().count())
        .map(|disk| disk.available_space())
        .context("pack filesystem free space is not measurable")
}

async fn load<C: GenericClient + Sync>(client: &C, id: Uuid, limit: usize) -> Result<LoadedPack> {
    ensure!((1..=65536).contains(&limit), "invalid pack entry bound");
    let pack = client.query_one("SELECT status::TEXT, storage_key, stored_bytes, manifest_sha256 FROM content_pack WHERE id=$1", &[&id]).await?;
    ensure!(
        pack.get::<_, String>("storage_key") == format!("{id}.pack"),
        "pack storage key differs from identity"
    );
    let rows = client.query(
        "SELECT entry.ordinal, entry.body_id, entry.pack_offset, entry.stored_length, entry.codec::TEXT, entry.entry_digest, \
         body.digest, body.logical_length, body.pack_id=$1 AS assigned_here, entry.dictionary_id, dictionary.digest AS dictionary_digest \
         FROM content_pack_entry entry JOIN content_body body ON body.id=entry.body_id \
         LEFT JOIN content_dictionary dictionary ON dictionary.id=entry.dictionary_id \
         WHERE entry.pack_id=$1 ORDER BY entry.ordinal LIMIT $2",
        &[&id, &i64::try_from(limit + 1)?],
    ).await?;
    ensure!(
        !rows.is_empty() && rows.len() <= limit,
        "pack exceeds entry admission bound or is empty"
    );
    let mut entries = Vec::with_capacity(rows.len());
    let mut bodies = Vec::with_capacity(rows.len());
    let mut assigned_here = Vec::with_capacity(rows.len());
    for row in rows {
        let dictionary = row
            .get::<_, Option<i64>>("dictionary_id")
            .map(|id| -> Result<_> {
                Ok(DictionaryIdentity {
                    id,
                    digest: row
                        .get::<_, Vec<u8>>("dictionary_digest")
                        .try_into()
                        .map_err(|_| anyhow::anyhow!("invalid dictionary digest"))?,
                })
            })
            .transpose()?;
        entries.push(PackEntry {
            pack_id: id,
            ordinal: u64::try_from(row.get::<_, i64>("ordinal"))?,
            body: BodyIdentity {
                digest_algorithm: "sha256-v1",
                digest: row
                    .get::<_, Vec<u8>>("digest")
                    .try_into()
                    .map_err(|_| anyhow::anyhow!("invalid body digest"))?,
                logical_length: u64::try_from(row.get::<_, i64>("logical_length"))?,
            },
            pack_offset: u64::try_from(row.get::<_, i64>("pack_offset"))?,
            stored_length: u64::try_from(row.get::<_, i64>("stored_length"))?,
            codec: match row.get::<_, String>("codec").as_str() {
                "identity" => BodyCodec::Identity,
                "zstd" => BodyCodec::Zstd,
                _ => bail!("unsupported pack codec"),
            },
            dictionary,
            entry_digest: row
                .get::<_, Vec<u8>>("entry_digest")
                .try_into()
                .map_err(|_| anyhow::anyhow!("invalid entry digest"))?,
        });
        bodies.push(row.get("body_id"));
        assigned_here.push(row.get::<_, Option<bool>>("assigned_here").unwrap_or(false));
    }
    let manifest = PackManifest {
        pack_id: id,
        entries,
        stored_bytes: u64::try_from(pack.get::<_, i64>("stored_bytes"))?,
        sha256: pack
            .get::<_, Vec<u8>>("manifest_sha256")
            .try_into()
            .map_err(|_| anyhow::anyhow!("invalid manifest digest"))?,
        managed_peak_buffer_bytes: 0,
    };
    manifest.verify()?;
    Ok(LoadedPack {
        status: pack.get("status"),
        manifest,
        bodies,
        assigned_here,
    })
}

async fn dictionary<C: GenericClient + Sync>(
    client: &C,
    entry: &PackEntry,
) -> Result<Option<Vec<u8>>> {
    let Some(identity) = &entry.dictionary else {
        return Ok(None);
    };
    let row = client.query_one("SELECT CASE WHEN octet_length(dictionary_bytes)<=1048576 THEN dictionary_bytes END FROM content_dictionary WHERE id=$1", &[&identity.id]).await?;
    let bytes: Vec<u8> = row
        .get::<_, Option<Vec<u8>>>(0)
        .context("dictionary exceeds maintenance admission bound")?;
    identity.verify(&bytes)?;
    Ok(Some(bytes))
}

async fn verify<C: GenericClient + Sync>(
    client: &C,
    root: &Path,
    pack: &LoadedPack,
    buffer: usize,
) -> Result<()> {
    let path = root.join(format!("{}.pack", pack.manifest.pack_id));
    ensure!(
        fs::symlink_metadata(&path)?.is_file(),
        "pack must be a regular file"
    );
    let reader = PackReader::new(path, pack.manifest.pack_id, pack.manifest.stored_bytes);
    for entry in &pack.manifest.entries {
        let dict = dictionary(client, entry).await?;
        reader.verify_integrity(entry, dict.as_deref(), buffer)?;
    }
    Ok(())
}

/// Rewrite all currently assigned bodies, conservatively retaining even bodies
/// whose graph reachability has not been swept. Reference counters are ignored.
/// Stable new_pack is the caller's persisted idempotency key. Unreferenced files
/// after a crash are retained, verified and adopted only if every byte matches.
pub async fn repack(
    client: &mut Client,
    root: &Path,
    old_pack: Uuid,
    new_pack: Uuid,
    gc_epoch: i64,
    policy: &RepackPolicy,
) -> Result<RepackReport> {
    policy.validate()?;
    ensure!(old_pack != new_pack, "replacement must have a new identity");
    ensure!(
        client
            .query_one("SELECT storage_v2_is_admin()", &[])
            .await?
            .get::<_, bool>(0),
        "pack maintenance requires administrator authority"
    );
    let (root, _lock) = lock_root(root)?;
    let transaction = client.transaction().await?;
    let reader_epoch = content_body::begin_reader_epoch(&transaction).await?;
    let old = load(&transaction, old_pack, policy.max_entries).await?;
    if matches!(old.status.as_str(), "retired" | "reclaimed") {
        let retirement = transaction.query_one("SELECT replacement_pack_id,gc_epoch_id FROM content_pack_retirement WHERE pack_id=$1", &[&old_pack]).await?;
        ensure!(
            retirement.get::<_, Uuid>(0) == new_pack && retirement.get::<_, i64>(1) == gc_epoch,
            "repack retry identity differs"
        );
        let new = load(&transaction, new_pack, policy.max_entries).await?;
        let completed = transaction
            .query_opt(
                "SELECT 1 FROM storage_v2_pack_removal_receipt WHERE pack_id=$1",
                &[&old_pack],
            )
            .await?
            .is_some();
        if !completed {
            ensure!(
                new.status == "published",
                "replacement requires separate chain recovery"
            );
            verify(&transaction, &root, &new, policy.io_buffer_bytes).await?;
        }
        let moved: std::collections::BTreeSet<_> = new.bodies.iter().copied().collect();
        let copied = old
            .bodies
            .iter()
            .zip(&old.manifest.entries)
            .filter(|(id, _)| moved.contains(*id))
            .try_fold(0_u64, |sum, (_, entry)| {
                sum.checked_add(entry.stored_length)
                    .context("retry size overflow")
            })?;
        let excluded = old
            .manifest
            .stored_bytes
            .checked_sub(copied)
            .context("retry placement size differs")?;
        let report = report(&old, &new.manifest, excluded, policy.io_buffer_bytes, true)?;
        content_body::end_reader_epoch(&transaction, reader_epoch).await?;
        transaction.commit().await?;
        return Ok(report);
    }
    ensure!(
        old.status == "published",
        "only published packs can be rewritten"
    );
    ensure!(transaction.query_opt("SELECT 1 FROM content_pack_retirement retirement WHERE replacement_pack_id=$1 AND NOT EXISTS(SELECT 1 FROM storage_v2_pack_removal_receipt receipt WHERE receipt.pack_id=retirement.pack_id)", &[&old_pack]).await?.is_none(), "finish predecessor removals before rewriting their replacement");
    ensure!(transaction.query_opt("SELECT 1 FROM storage_v2_gc_epoch WHERE id=$1 AND source_id IS NULL AND status IN ('verified','sweeping')", &[&gc_epoch]).await?.is_some(), "accepted global GC epoch required");
    ensure!(
        transaction
            .query_opt("SELECT 1 FROM content_pack WHERE id=$1", &[&new_pack])
            .await?
            .is_none(),
        "replacement identity is already registered"
    );
    let live: Vec<usize> = old
        .assigned_here
        .iter()
        .enumerate()
        .filter_map(|(index, live)| live.then_some(index))
        .collect();
    ensure!(
        !live.is_empty(),
        "empty-pack retirement requires a separate verified sweep"
    );
    let mut logical = 0_u64;
    let mut live_stored = 0_u64;
    let mut largest = 0_u64;
    for index in &live {
        let entry = &old.manifest.entries[*index];
        logical = logical
            .checked_add(entry.body.logical_length)
            .context("logical admission overflow")?;
        live_stored = live_stored
            .checked_add(entry.stored_length)
            .context("stored admission overflow")?;
        largest = largest.max(entry.body.logical_length);
    }
    let dead = old
        .manifest
        .stored_bytes
        .checked_sub(live_stored)
        .context("invalid live placement size")?;
    ensure!(
        logical <= policy.max_logical_bytes,
        "repack logical-byte budget exceeded"
    );
    ensure!(
        dead >= policy.minimum_dead_bytes
            && u128::from(dead) * 10000
                >= u128::from(old.manifest.stored_bytes)
                    * u128::from(policy.minimum_dead_basis_points),
        "pack does not meet dead-byte policy"
    );
    let required = logical
        .checked_mul(3)
        .and_then(|v| v.checked_add(largest.checked_mul(2)?))
        .and_then(|v| v.checked_add((live.len() as u64).checked_mul(65536)?))
        .and_then(|v| v.checked_add(policy.reserve_free_bytes))
        .context("repack headroom overflow")?;
    ensure!(
        available_bytes(&root)? >= required,
        "insufficient measured filesystem headroom"
    );
    let mut builder = PackBuilder::new(&root, new_pack, Uuid::new_v4(), policy.io_buffer_bytes)?;
    let source = PackReader::new(
        root.join(format!("{old_pack}.pack")),
        old_pack,
        old.manifest.stored_bytes,
    );
    ensure!(
        fs::symlink_metadata(root.join(format!("{old_pack}.pack")))?.is_file(),
        "source pack must be a regular file"
    );
    for index in &live {
        let entry = &old.manifest.entries[*index];
        let dict = dictionary(&transaction, entry).await?;
        let body =
            source.verify_to_staging(entry, dict.as_deref(), &root, policy.io_buffer_bytes)?;
        body.repack_into(&mut builder, policy.codec)?;
    }
    let sealed = builder.seal()?;
    for entry in &sealed.manifest.entries {
        sealed.verify_entry(entry, None)?;
    }
    let manifest = sealed.manifest.clone();
    let target = root.join(format!("{new_pack}.pack"));
    if target.try_exists()? {
        ensure!(
            fs::symlink_metadata(&target)?.is_file(),
            "retry target must be a regular file"
        );
        let retry_reader = PackReader::new(&target, new_pack, manifest.stored_bytes);
        for entry in &manifest.entries {
            retry_reader.verify_integrity(entry, None, policy.io_buffer_bytes)?;
        }
        File::open(&root)?.sync_all()?;
        drop(sealed);
    } else {
        sealed.publish()?;
    }
    content_body::create_pack(
        &transaction,
        new_pack,
        &format!("{new_pack}.pack"),
        Uuid::new_v4(),
    )
    .await?;
    for (ordinal, index) in live.iter().enumerate() {
        let entry = &manifest.entries[ordinal];
        transaction.execute("INSERT INTO content_pack_entry(pack_id,ordinal,body_id,pack_offset,stored_length,codec,entry_digest) VALUES($1,$2,$3,$4,$5,$6::TEXT::storage_v2_body_codec,$7)", &[&new_pack, &i64::try_from(ordinal)?, &old.bodies[*index], &i64::try_from(entry.pack_offset)?, &i64::try_from(entry.stored_length)?, &entry.codec.database_name(), &entry.entry_digest.as_slice()]).await?;
    }
    content_body::verify_pack(
        &transaction,
        new_pack,
        &manifest.sha256,
        i64::try_from(manifest.stored_bytes)?,
    )
    .await?;
    content_body::publish_pack(&transaction, new_pack).await?;
    content_body::end_reader_epoch(&transaction, reader_epoch).await?;
    ensure!(
        content_body::switch_pack(&transaction, old_pack, new_pack, gc_epoch).await?
            == i64::try_from(live.len())?,
        "switched body count differs"
    );
    let switched = load(&transaction, new_pack, policy.max_entries).await?;
    ensure!(
        switched.assigned_here.iter().all(|assigned| *assigned),
        "post-switch placement verification failed"
    );
    verify(&transaction, &root, &switched, policy.io_buffer_bytes).await?;
    let report = report(&old, &manifest, dead, policy.io_buffer_bytes, false)?;
    transaction.commit().await?;
    Ok(report)
}

fn report(
    old: &LoadedPack,
    new: &PackManifest,
    dead: u64,
    buffer: usize,
    resumed: bool,
) -> Result<RepackReport> {
    Ok(RepackReport {
        old_pack: old.manifest.pack_id,
        new_pack: new.pack_id,
        moved_entries: new.entries.len(),
        moved_logical_bytes: new.entries.iter().try_fold(0_u64, |sum, entry| {
            sum.checked_add(entry.body.logical_length)
                .context("report length overflow")
        })?,
        old_file_bytes: old.manifest.stored_bytes,
        new_file_bytes: new.stored_bytes,
        excluded_entry_bytes: dead,
        writer_buffer_bytes: buffer,
        resumed_after_switch: resumed,
    })
}

/// Re-verify replacement bytes on every unfinished attempt. Commit DB permission before
/// unlink and fsync, then issue the durable receipt. A retry after unlink but
/// before receipt observes reclaimed state and safely completes accounting.
pub async fn finish(
    client: &mut Client,
    root: &Path,
    old_pack: Uuid,
    max_entries: usize,
    buffer: usize,
) -> Result<RemovalReport> {
    ensure!(
        (4096..=1048576).contains(&buffer),
        "invalid verification buffer"
    );
    ensure!(
        client
            .query_one("SELECT storage_v2_is_admin()", &[])
            .await?
            .get::<_, bool>(0),
        "pack maintenance requires administrator authority"
    );
    let (root, _lock) = lock_root(root)?;
    let transaction = client.transaction().await?;
    let epoch = content_body::begin_reader_epoch(&transaction).await?;
    let old = load(&transaction, old_pack, max_entries).await?;
    ensure!(
        matches!(old.status.as_str(), "retired" | "reclaimed"),
        "pack is not retired"
    );
    let receipt = transaction
        .query_opt(
            "SELECT file_bytes FROM storage_v2_pack_removal_receipt WHERE pack_id=$1",
            &[&old_pack],
        )
        .await?;
    if let Some(receipt) = receipt {
        ensure!(
            old.status == "reclaimed"
                && receipt.get::<_, i64>(0) == i64::try_from(old.manifest.stored_bytes)?,
            "removal receipt identity differs"
        );
        ensure!(
            matches!(fs::symlink_metadata(root.join(format!("{old_pack}.pack"))), Err(error) if error.kind()==std::io::ErrorKind::NotFound),
            "removed pack unexpectedly reappeared or cannot be inspected"
        );
        content_body::end_reader_epoch(&transaction, epoch).await?;
        transaction.commit().await?;
        return Ok(RemovalReport {
            pack_id: old_pack,
            file_bytes: old.manifest.stored_bytes,
            unlinked_this_call: false,
            receipt_already_present: true,
        });
    }
    if old.status == "reclaimed" {
        ensure!(transaction.query_opt("SELECT 1 FROM content_pack_retirement retirement JOIN storage_v2_gc_epoch epoch ON epoch.id=retirement.gc_epoch_id WHERE retirement.pack_id=$1 AND retirement.readers_drained_at IS NOT NULL AND epoch.status IN ('sweeping','complete') AND NOT EXISTS(SELECT 1 FROM content_body WHERE pack_id=$1)", &[&old_pack]).await?.is_some(), "reclamation permission is no longer valid");
    }
    let retirement = transaction
        .query_one(
            "SELECT replacement_pack_id FROM content_pack_retirement WHERE pack_id=$1",
            &[&old_pack],
        )
        .await?;
    let new = load(&transaction, retirement.get(0), max_entries).await?;
    ensure!(
        new.status == "published",
        "replacement requires separate chain recovery"
    );
    verify(&transaction, &root, &new, buffer).await?;
    if old.status == "retired" {
        content_body::mark_pack_readers_drained(&transaction, old_pack).await?;
        content_body::reclaim_pack(&transaction, old_pack).await?;
    }
    content_body::end_reader_epoch(&transaction, epoch).await?;
    transaction.commit().await?;
    let path = root.join(format!("{old_pack}.pack"));
    let unlinked = match fs::symlink_metadata(&path) {
        Ok(metadata) => {
            ensure!(
                metadata.is_file() && metadata.len() == old.manifest.stored_bytes,
                "retired pack identity or length differs"
            );
            fs::remove_file(&path)?;
            true
        }
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
            ensure!(
                old.status == "reclaimed",
                "retired bytes disappeared before removal permission"
            );
            false
        }
        Err(error) => return Err(error.into()),
    };
    File::open(&root)?.sync_all()?;
    client
        .execute(
            "SELECT storage_v2_record_pack_removal($1,$2)",
            &[&old_pack, &i64::try_from(old.manifest.stored_bytes)?],
        )
        .await?;
    Ok(RemovalReport {
        pack_id: old_pack,
        file_bytes: old.manifest.stored_bytes,
        unlinked_this_call: unlinked,
        receipt_already_present: false,
    })
}

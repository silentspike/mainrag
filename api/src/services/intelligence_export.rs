//! Complete v1 intelligence export hashing without a source-wide JSON buffer.

use anyhow::{bail, Context, Result};
use futures::TryStreamExt;
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use tokio_postgres::GenericClient;

// PostgreSQL JSONB orders object keys by byte length, then binary value.
const COLLECTIONS: [&str; 8] = [
    "cards",
    "entities",
    "profiles",
    "relations",
    "call_edges",
    "annotations",
    "unresolved_calls",
    "negative_evidence",
];

struct ExportDigest {
    hash: Sha256,
    collection: usize,
    counts: [u64; 8],
    empty_marker_seen: bool,
    serialized_bytes: u64,
}

impl ExportDigest {
    fn new() -> Self {
        Self {
            hash: Sha256::new(),
            collection: 0,
            counts: [0; 8],
            empty_marker_seen: false,
            serialized_bytes: 0,
        }
    }

    fn write(&mut self, bytes: &[u8]) -> Result<()> {
        self.serialized_bytes = self
            .serialized_bytes
            .checked_add(bytes.len() as u64)
            .context("intelligence export byte count overflow")?;
        self.hash.update(bytes);
        Ok(())
    }

    fn push(&mut self, collection: i64, ordinal: Option<i64>, record: Option<&str>) -> Result<()> {
        let collection =
            usize::try_from(collection).context("invalid export collection ordinal")?;
        if collection != self.collection {
            if collection != self.collection + 1 || collection > COLLECTIONS.len() {
                bail!("intelligence export collection order differs");
            }
            if self.collection == 0 {
                self.write(b"{")?;
            } else {
                self.write(b"], ")?;
            }
            self.collection = collection;
            self.empty_marker_seen = false;
            self.write(b"\"")?;
            self.write(COLLECTIONS[collection - 1].as_bytes())?;
            self.write(b"\": [")?;
        }
        if collection == 0 {
            bail!("intelligence export collection ordinal is invalid");
        }
        let count = self.counts[collection - 1];
        match (ordinal, record) {
            (None, None) if count == 0 && !self.empty_marker_seen => {
                self.empty_marker_seen = true;
            }
            (Some(ordinal), Some(record))
                if !self.empty_marker_seen
                    && u64::try_from(ordinal).ok() == count.checked_add(1) =>
            {
                if count > 0 {
                    self.write(b", ")?;
                }
                // Keep native JSONB serialization, including numeric precision,
                // Unicode and escaping. Never reserialize the protected record.
                self.write(record.as_bytes())?;
                self.counts[collection - 1] = count + 1;
            }
            _ => bail!("intelligence export record order or empty marker differs"),
        }
        Ok(())
    }

    fn finish(mut self) -> Result<(Value, String, u64)> {
        if self.collection != COLLECTIONS.len() {
            bail!("intelligence export collection set is incomplete");
        }
        self.write(b"]}")?;
        let counts: serde_json::Map<String, Value> = COLLECTIONS
            .iter()
            .zip(self.counts)
            .map(|(name, count)| (name.to_string(), json!(count)))
            .collect();
        Ok((
            Value::Object(counts),
            format!("{:x}", self.hash.finalize()),
            self.serialized_bytes,
        ))
    }
}

/// One statement supplies every collection from one PostgreSQL snapshot. The
/// parameterized lateral calls preserve native per-collection order. Validate
/// both ordinals while consuming the stream so unexpected planner reordering
/// cannot produce an accepted partial or reordered export.
pub async fn public_export<C: GenericClient + Sync>(
    client: &C,
    source_id: i64,
    generation: &str,
) -> Result<(Value, u64)> {
    let collections = COLLECTIONS.map(str::to_string).to_vec();
    let parameters: [&(dyn tokio_postgres::types::ToSql + Sync); 3] =
        [&source_id, &generation, &collections];
    let rows = client
        .query_raw(
            "SELECT collection.ordinal, record.record_ordinal, record.record_text \
               FROM unnest($3::TEXT[]) WITH ORDINALITY collection(name,ordinal) \
               LEFT JOIN LATERAL storage_v2_intelligence_export_records(\
                   $1,$2,collection.name) record ON TRUE",
            parameters,
        )
        .await?;
    futures::pin_mut!(rows);
    let mut digest = ExportDigest::new();
    while let Some(row) = rows.try_next().await? {
        digest.push(
            row.try_get(0)?,
            row.try_get(1)?,
            row.try_get::<_, Option<&str>>(2)?,
        )?;
    }
    let (counts, protected_sha256, serialized_bytes) = digest.finish()?;
    // The public envelope is small. Let PostgreSQL reproduce its exact v1
    // JSONB serialization and digest instead of approximating that encoding.
    let row = client
        .query_one(
            "WITH payload AS (SELECT jsonb_build_object(\
                'record_counts',$3::JSONB,'protected_payload_sha256',$4::TEXT) value) \
             SELECT jsonb_build_object(\
                'schema_version','mainrag.storage-v2-intelligence-export.v1',\
                'redaction','public',\
                'source_ref',encode(storage_v2_hash_parts(\
                    'mainrag.export-source.v1',ARRAY[int8send($1::BIGINT)]),'hex'),\
                'generation_seq',$2::BIGINT,\
                'payload_sha256',encode(digest(convert_to(value::TEXT,'UTF8'),'sha256'),'hex'),\
                'payload',value) FROM payload",
            &[
                &source_id,
                &generation.parse::<i64>()?,
                &counts,
                &protected_sha256,
            ],
        )
        .await?;
    Ok((row.try_get(0)?, serialized_bytes))
}

#[cfg(test)]
mod qualification_reader_tests {
    use super::*;

    #[test]
    fn complete_native_record_stream_matches_canonical_payload_bytes() {
        let first = r#"{"a": 1.2300, "b": "Grüße 東京 😀", "c": [null, true]}"#;
        let second = r#"{"a": "quote\" slash\\ newline\n", "b": {"x": []}}"#;
        let mut digest = ExportDigest::new();
        let mut expected = String::from("{");
        for (index, name) in COLLECTIONS.iter().enumerate() {
            if index > 0 {
                expected.push_str(", ");
            }
            expected.push_str(&format!("\"{name}\": ["));
            if index == 0 {
                digest.push(1, Some(1), Some(first)).unwrap();
                digest.push(1, Some(2), Some(second)).unwrap();
                expected.push_str(first);
                expected.push_str(", ");
                expected.push_str(second);
            } else {
                digest.push((index + 1) as i64, None, None).unwrap();
            }
            expected.push(']');
        }
        expected.push('}');
        let (counts, hash, bytes) = digest.finish().unwrap();
        assert_eq!(hash, format!("{:x}", Sha256::digest(expected.as_bytes())));
        assert_eq!(bytes, expected.len() as u64);
        assert_eq!(counts["cards"], 2);
        assert_eq!(counts["entities"], 0);
    }

    #[test]
    fn malformed_or_reordered_streams_fail_before_export_acceptance() {
        for (collection, ordinal, record) in [
            (0, None, None),
            (2, None, None),
            (1, Some(2), Some("{}")),
            (1, Some(1), None),
            (1, None, Some("{}")),
        ] {
            assert!(ExportDigest::new()
                .push(collection, ordinal, record)
                .is_err());
        }
        let mut digest = ExportDigest::new();
        digest.push(1, None, None).unwrap();
        assert!(digest.push(1, Some(1), Some("{}")).is_err());
        assert!(digest.finish().is_err());
        let mut digest = ExportDigest::new();
        digest.push(1, Some(1), Some("{}")).unwrap();
        assert!(digest.push(1, Some(1), Some("{}")).is_err());
    }

    #[test]
    fn payload_larger_than_postgresql_datum_limit_uses_one_reusable_record() {
        let record = format!("{{\"fixture\": \"{}\"}}", "x".repeat(65536));
        let mut digest = ExportDigest::new();
        for ordinal in 1..=16384 {
            digest.push(1, Some(ordinal), Some(&record)).unwrap();
        }
        for collection in 2..=8 {
            digest.push(collection, None, None).unwrap();
        }
        let (counts, hash, bytes) = digest.finish().unwrap();
        assert!(bytes > 1_073_741_823);
        assert_eq!(counts["cards"], 16384);
        assert_eq!(hash.len(), 64);
    }
}

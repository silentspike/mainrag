//! Lossless cache encoding for complete parser results with repeated long lines.

use anyhow::{bail, Context, Result};
use base64::{engine::general_purpose::STANDARD, Engine};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use sha2::{Digest, Sha256};
use std::io::{BufReader, Read, Write};

use crate::services::parser::{ExtractedCall, ExtractedSymbol, ParseResult};

const FORMAT: &str = "mainrag.analysis-cache.zstd.v1";
const INLINE_BYTES: u64 = 1024 * 1024;
const GROUP_BYTES: usize = 8 * 1024 * 1024;
const GROUP_ITEMS: usize = 64;

struct CountingWriter(usize);

impl Write for CountingWriter {
    fn write(&mut self, bytes: &[u8]) -> std::io::Result<usize> {
        self.0 += bytes.len();
        Ok(bytes.len())
    }

    fn flush(&mut self) -> std::io::Result<()> {
        Ok(())
    }
}

/// Preserve complete records and order while bounding aggregate JSON requests.
/// An indivisible record larger than the target is emitted alone.
pub struct JsonGroups<I> {
    input: I,
    pending: Option<(Value, usize)>,
    failed: bool,
}

impl<I: Iterator<Item = Result<Value>>> JsonGroups<I> {
    pub fn new(input: I) -> Self {
        Self {
            input,
            pending: None,
            failed: false,
        }
    }

    fn group(&mut self) -> Result<Option<Value>> {
        let mut rows = Vec::new();
        let mut size = 2;
        loop {
            let next = if let Some(value) = self.pending.take() {
                Some(value)
            } else if let Some(value) = self.input.next() {
                let value = value?;
                let mut counter = CountingWriter(0);
                serde_json::to_writer(&mut counter, &value)?;
                Some((value, counter.0))
            } else {
                None
            };
            let Some((value, bytes)) = next else { break };
            let combined = size + bytes + usize::from(!rows.is_empty());
            if !rows.is_empty() && (rows.len() == GROUP_ITEMS || combined > GROUP_BYTES) {
                self.pending = Some((value, bytes));
                break;
            }
            rows.push(value);
            size = combined;
            if rows.len() == GROUP_ITEMS {
                break;
            }
        }
        Ok((!rows.is_empty()).then_some(Value::Array(rows)))
    }
}

impl<I: Iterator<Item = Result<Value>>> Iterator for JsonGroups<I> {
    type Item = Result<Value>;

    fn next(&mut self) -> Option<Self::Item> {
        if self.failed {
            return None;
        }
        match self.group() {
            Ok(group) => group.map(Ok),
            Err(error) => {
                self.failed = true;
                Some(Err(error))
            }
        }
    }
}

#[derive(Serialize)]
struct AnalysisRef<'a> {
    symbols: &'a [ExtractedSymbol],
    calls: &'a [ExtractedCall],
    language: &'a str,
}

#[derive(Deserialize)]
struct Analysis {
    symbols: Vec<ExtractedSymbol>,
    calls: Vec<ExtractedCall>,
    language: String,
}

#[derive(Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Encoded {
    cache_format: String,
    content_sha256: String,
    raw_bytes: u64,
    raw_sha256: String,
    compressed_sha256: String,
    zstd_base64: String,
}

struct HashingWriter<W> {
    inner: W,
    hash: Sha256,
    bytes: u64,
}

impl<W: Write> Write for HashingWriter<W> {
    fn write(&mut self, bytes: &[u8]) -> std::io::Result<usize> {
        let written = self.inner.write(bytes)?;
        self.hash.update(&bytes[..written]);
        self.bytes += written as u64;
        Ok(written)
    }

    fn flush(&mut self) -> std::io::Result<()> {
        self.inner.flush()
    }
}

struct HashingReader<R> {
    inner: R,
    hash: Sha256,
    bytes: u64,
}

impl<R: Read> Read for HashingReader<R> {
    fn read(&mut self, bytes: &mut [u8]) -> std::io::Result<usize> {
        let read = self.inner.read(bytes)?;
        self.hash.update(&bytes[..read]);
        self.bytes += read as u64;
        Ok(read)
    }
}

pub fn encode(parsed: &ParseResult, content_digest: &[u8]) -> Result<Value> {
    if content_digest.len() != 32 {
        bail!("analysis cache requires a complete content digest");
    }
    // Serialize borrowed fields directly into the compressor. Constructing an
    // intermediate JSON array would duplicate every repeated signature in RAM.
    let encoder = zstd::stream::write::Encoder::new(Vec::new(), 3)?;
    let mut writer = HashingWriter {
        inner: encoder,
        hash: Sha256::new(),
        bytes: 0,
    };
    serde_json::to_writer(
        &mut writer,
        &AnalysisRef {
            symbols: &parsed.symbols,
            calls: &parsed.calls,
            language: &parsed.language,
        },
    )?;
    let compressed = writer.inner.finish()?;
    if writer.bytes <= INLINE_BYTES {
        // Keep the existing representation for ordinary cache entries.
        let decoder = zstd::stream::read::Decoder::new(compressed.as_slice())?;
        return Ok(serde_json::from_reader(BufReader::new(decoder))?);
    }
    Ok(serde_json::to_value(Encoded {
        cache_format: FORMAT.to_string(),
        content_sha256: hex::encode(content_digest),
        raw_bytes: writer.bytes,
        raw_sha256: hex::encode(writer.hash.finalize()),
        compressed_sha256: hex::encode(Sha256::digest(&compressed)),
        zstd_base64: STANDARD.encode(compressed),
    })?)
}

pub fn decode(mut value: Value, content_digest: &[u8]) -> Result<ParseResult> {
    let parsed: Analysis = if value.get("cache_format").is_none() {
        // Match the prior reader's language fallback for legacy entries.
        if !value["language"].is_string() {
            value
                .as_object_mut()
                .context("legacy analysis cache must be an object")?
                .insert("language".into(), Value::String("text".to_string()));
        }
        serde_json::from_value(value)?
    } else {
        let encoded: Encoded = serde_json::from_value(value)?;
        if encoded.cache_format != FORMAT
            || content_digest.len() != 32
            || encoded.content_sha256 != hex::encode(content_digest)
            || encoded.raw_bytes == 0
        {
            bail!("encoded analysis cache identity differs");
        }
        let compressed = STANDARD.decode(encoded.zstd_base64)?;
        if hex::encode(Sha256::digest(&compressed)) != encoded.compressed_sha256 {
            bail!("encoded analysis cache compressed digest differs");
        }
        let decoder = zstd::stream::read::Decoder::new(compressed.as_slice())?;
        let limit = encoded
            .raw_bytes
            .checked_add(1)
            .context("analysis cache size overflow")?;
        let mut reader = HashingReader {
            inner: decoder.take(limit),
            hash: Sha256::new(),
            bytes: 0,
        };
        // JSON decoding requests individual bytes. Buffer above the digest
        // reader so decompression and hashing operate on complete byte blocks.
        let result = serde_json::from_reader(BufReader::new(&mut reader))?;
        if reader.bytes != encoded.raw_bytes
            || hex::encode(reader.hash.finalize()) != encoded.raw_sha256
        {
            bail!("encoded analysis cache complete result digest differs");
        }
        result
    };
    Ok(ParseResult {
        symbols: parsed.symbols,
        calls: parsed.calls,
        language: parsed.language,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::services::parser::{CallType, SymbolType};

    fn fixture() -> ParseResult {
        ParseResult {
            symbols: vec![ExtractedSymbol {
                name: "fixture".into(),
                qualified_name: Some("module::fixture".into()),
                symbol_type: SymbolType::Function,
                line_start: 1,
                line_end: 2,
                column_start: 0,
                column_end: 7,
                signature: Some("fn fixture()".into()),
                doc_comment: Some("complete fixture documentation".into()),
                visibility: Some("pub".into()),
                language: "rust".into(),
            }],
            calls: vec![ExtractedCall {
                caller_name: "fixture".into(),
                callee_name: "callee".into(),
                call_type: CallType::Direct,
                call_line: 2,
                call_column: 4,
            }],
            language: "rust".into(),
        }
    }

    fn full_result(parsed: &ParseResult) -> Value {
        serde_json::to_value(AnalysisRef {
            symbols: &parsed.symbols,
            calls: &parsed.calls,
            language: &parsed.language,
        })
        .unwrap()
    }

    #[test]
    fn legacy_and_large_cache_entries_keep_every_parser_field() {
        let mut parsed = fixture();
        let digest = [7; 32];
        assert_eq!(encode(&parsed, &digest).unwrap(), full_result(&parsed));
        let old = decode(full_result(&parsed), &digest).unwrap();
        assert_eq!(full_result(&old), full_result(&parsed));
        // Repeated long signatures reproduce the serialization growth without
        // changing the parser, trimming metadata or dropping any symbol/call.
        parsed.symbols[0].signature = Some("αβ repeated source line ".repeat(120_000));
        parsed.symbols = vec![parsed.symbols[0].clone(); 4];
        let encoded = encode(&parsed, &digest).unwrap();
        assert_eq!(encoded["cache_format"], FORMAT);
        assert!(encoded["raw_bytes"].as_u64().unwrap() > INLINE_BYTES);
        assert!(serde_json::to_vec(&encoded).unwrap().len() < 64 * 1024);
        let restored = decode(encoded, &digest).unwrap();
        assert_eq!(full_result(&restored), full_result(&parsed));
    }

    #[test]
    fn cache_roundtrip_exceeds_postgres_jsonb_array_limit() {
        let mut parsed = fixture();
        parsed.symbols[0].signature = Some("x".repeat(1024 * 1024));
        parsed.symbols = vec![parsed.symbols[0].clone(); 260];
        let encoded = encode(&parsed, &[7; 32]).unwrap();
        assert!(encoded["raw_bytes"].as_u64().unwrap() > 268_435_455);
        assert!(serde_json::to_vec(&encoded).unwrap().len() < 1024 * 1024);
        let restored = decode(encoded, &[7; 32]).unwrap();
        assert_eq!(restored.language, parsed.language);
        assert_eq!(restored.symbols.len(), parsed.symbols.len());
        for (actual, expected) in restored.symbols.iter().zip(&parsed.symbols) {
            assert_eq!(
                serde_json::to_value(actual).unwrap(),
                serde_json::to_value(expected).unwrap()
            );
        }
        assert_eq!(
            serde_json::to_value(restored.calls).unwrap(),
            serde_json::to_value(parsed.calls).unwrap()
        );
    }

    #[test]
    fn corrupt_cache_and_cross_content_reuse_fail_closed() {
        assert!(decode(Value::Null, &[7; 32]).is_err());
        assert!(decode(serde_json::json!({"calls": []}), &[7; 32]).is_err());
        let mut parsed = fixture();
        parsed.symbols[0].signature = Some("long signature ".repeat(120_000));
        let encoded = encode(&parsed, &[7; 32]).unwrap();
        assert!(decode(encoded.clone(), &[8; 32]).is_err());
        for (key, value) in [
            ("cache_format", Value::String("unknown".into())),
            ("raw_sha256", Value::String("00".repeat(32))),
            ("compressed_sha256", Value::String("00".repeat(32))),
            ("raw_bytes", Value::from(1)),
            ("zstd_base64", Value::String("invalid!".into())),
        ] {
            let mut corrupted = encoded.clone();
            corrupted[key] = value;
            assert!(decode(corrupted, &[7; 32]).is_err(), "{key}");
        }
    }

    #[test]
    fn byte_bounded_groups_keep_order_fields_and_indivisible_records() {
        let input = (0..20)
            .map(|id| serde_json::json!({"id":id,"value":"δ".repeat(300_000)}))
            .collect::<Vec<_>>();
        let groups = JsonGroups::new(input.iter().cloned().map(Ok))
            .collect::<Result<Vec<_>>>()
            .unwrap();
        assert!(groups.len() > 1);
        let mut restored = Vec::new();
        for group in groups {
            assert!(serde_json::to_vec(&group).unwrap().len() <= GROUP_BYTES);
            let rows = group.as_array().unwrap();
            assert!(rows.len() <= GROUP_ITEMS);
            restored.extend(rows.iter().cloned());
        }
        assert_eq!(restored, input);
        let indivisible = Value::String("x".repeat(GROUP_BYTES + 1));
        let groups = JsonGroups::new(vec![Ok(indivisible.clone()), Ok(Value::Null)].into_iter())
            .collect::<Result<Vec<_>>>()
            .unwrap();
        assert_eq!(
            groups,
            vec![
                Value::Array(vec![indivisible]),
                Value::Array(vec![Value::Null])
            ]
        );
        let mut failed = JsonGroups::new(
            vec![
                Ok(Value::Null),
                Err(anyhow::anyhow!("fixture")),
                Ok(Value::Null),
            ]
            .into_iter(),
        );
        assert!(failed.next().unwrap().is_err());
        assert!(failed.next().is_none());
    }
}

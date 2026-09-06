//! Real PostgreSQL fault injection through the production non-streaming path.
//! The opt-in test refuses general-purpose databases and owns only its schema.

use super::*;
use crate::config::{QdrantConfig, TeiConfig};
use crate::services::chunker::ChunkType;
use anyhow::{ensure, Context};
use std::sync::atomic::{AtomicUsize, Ordering};
use tokio_postgres::NoTls;

const CHUNK_TEXT: &str = "fixed public fixture chunk";
const INITIAL: &str = "fn helper() {}\nfn entry() { helper(); }\n";
const CHANGED: &str = "fn helper() {}\nfn updated() { helper(); }\n";

struct FixtureChunker(Arc<AtomicUsize>);

impl Chunker for FixtureChunker {
    fn chunk(&self, content: &str, _language: Option<&str>) -> Vec<Chunk> {
        self.0.fetch_add(1, Ordering::SeqCst);
        if content.starts_with("// empty-chunk fixture") {
            return vec![];
        }
        vec![Chunk {
            text: CHUNK_TEXT.to_string(),
            start_line: 1,
            end_line: 1,
            start_byte: 0,
            end_byte: CHUNK_TEXT.len(),
            chunk_type: ChunkType::Function,
            metadata: None,
            parent_idx: None,
            level: 0,
            context_prefix: None,
        }]
    }

    fn name(&self) -> &str {
        "intelligence-retry-fixture"
    }
}

fn raw(path: &str, content: &str) -> RawFile {
    RawFile {
        path: path.to_string(),
        content: content.to_string(),
        size: content.len(),
        language: Some("rust".to_string()),
        last_modified: None,
        source_path: None,
        source_range: None,
    }
}

async fn seed(client: &tokio_postgres::Client, path: &str, content: &str) -> anyhow::Result<i64> {
    let hash = Sha256::digest(content.as_bytes()).to_vec();
    let compressed = zstd::encode_all(content.as_bytes(), 3)?;
    Ok(client
        .query_one(
            "INSERT INTO files (source_id, path, hash, content, size_original, size_compressed, last_modified) \
             VALUES (1, $1, $2, $3, $4, $5, NOW()) RETURNING id",
            &[&path, &hash, &compressed, &(content.len() as i32), &(compressed.len() as i32)],
        )
        .await?
        .get(0))
}

async fn state(
    client: &tokio_postgres::Client,
    id: i64,
) -> anyhow::Result<(bool, String, i32, i32)> {
    let row = client.query_one(
        "SELECT intelligence_analyzed_at IS NOT NULL, COALESCE(intelligence_analyzed_at::text, ''), \
         intelligence_symbols_count, intelligence_calls_count FROM files WHERE id = $1", &[&id],
    ).await?;
    Ok((row.get(0), row.get(1), row.get(2), row.get(3)))
}

async fn symbol_ids(client: &tokio_postgres::Client, id: i64) -> anyhow::Result<Vec<i64>> {
    Ok(client
        .query(
            "SELECT id FROM symbols WHERE file_id = $1 ORDER BY id",
            &[&id],
        )
        .await?
        .iter()
        .map(|row| row.get(0))
        .collect())
}

async fn skip(service: &IndexService, path: &str, content: &str) -> anyhow::Result<()> {
    ensure!(matches!(
        service
            .process_raw_file(1, "public-fixture", raw(path, content))
            .await?,
        ProcessResult::Skipped
    ));
    Ok(())
}

async fn observed_skip(
    service: &IndexService,
    path: &str,
    content: &str,
) -> anyhow::Result<super::super::ingest_observation::IngestObservation> {
    let (result, work) =
        super::super::ingest_observation::observe(skip(service, path, content)).await;
    result?;
    Ok(work)
}

async fn exercise(client: &tokio_postgres::Client, pool: PostgresPool) -> anyhow::Result<()> {
    client
        .batch_execute(include_str!("intelligence_retry_fixture.sql"))
        .await?;
    // Keep a dependency trap bound for the whole test. No vector endpoint is
    // served; an accidental connection is detected, not silently mocked away.
    let trap = std::net::TcpListener::bind("127.0.0.1:0")?;
    trap.set_nonblocking(true)?;
    let url = format!("http://{}", trap.local_addr()?);
    let calls = Arc::new(AtomicUsize::new(0));
    let service = IndexService {
        db: pool.clone(),
        tei: Arc::new(TeiClient::new(&TeiConfig {
            url: url.clone(),
            reranker_url: None,
            model: None,
            embedding_dim: None,
        })),
        qdrant: Arc::new(QdrantClient::new(&QdrantConfig {
            url,
            api_key: None,
            chunk_collection: "fixture".into(),
            code_collection: "fixture".into(),
            synonyms_collection: None,
        })),
        chunker: Box::new(FixtureChunker(calls.clone())),
        intelligence: Arc::new(IntelligenceService::new(pool)?),
    };

    let file_id = seed(client, "retry.rs", INITIAL).await?;
    let chunk_id: i64 = client.query_one(
        "INSERT INTO chunks (file_id, start_line, chunk_content_hash, chunker_version, embedding_model_id, tokenizer_version) \
         VALUES ($1, 1, $2, $3, $4, $5) RETURNING id",
        &[&file_id, &chunk_content_sha256(CHUNK_TEXT), &chunker_version(), &embedding_model_id(), &tokenizer_version()],
    ).await?.get(0);

    // First hash skip encounters a real failed INSERT, not a mocked parser.
    client
        .batch_execute("ALTER TABLE symbols ENABLE TRIGGER fixture_failure")
        .await?;
    let work = observed_skip(&service, "retry.rs", INITIAL).await?;
    ensure!((work.chunker_calls, work.intelligence_parser_calls) == (0, 1));
    ensure!(!state(client, file_id).await?.0);
    ensure!(calls.load(Ordering::SeqCst) == 0);
    client
        .batch_execute("ALTER TABLE symbols DISABLE TRIGGER fixture_failure")
        .await?;
    let work = observed_skip(&service, "retry.rs", INITIAL).await?;
    ensure!((work.chunker_calls, work.intelligence_parser_calls) == (0, 1));
    let completed = state(client, file_id).await?;
    ensure!(completed.0 && completed.2 == 2 && completed.3 == 1);
    let symbols = symbol_ids(client, file_id).await?;
    ensure!(symbols.len() == 2);
    let work = observed_skip(&service, "retry.rs", INITIAL).await?;
    ensure!((work.chunker_calls, work.intelligence_parser_calls) == (0, 0));
    ensure!(state(client, file_id).await? == completed);
    ensure!(symbol_ids(client, file_id).await? == symbols);
    ensure!(calls.load(Ordering::SeqCst) == 0);

    // Cover both normal and metadata-only (>5 MiB) file UPSERTs. A failed
    // call-graph write leaves partial symbols, which the next retry replaces.
    let large = format!("//{}\n{}", "x".repeat(5 * 1024 * 1024), CHANGED);
    for (attempt, content) in [CHANGED, large.as_str()].into_iter().enumerate() {
        client
            .batch_execute("ALTER TABLE call_graph ENABLE TRIGGER fixture_failure")
            .await?;
        skip(&service, "retry.rs", content).await?;
        let pending = state(client, file_id).await?;
        ensure!(!pending.0 && pending.2 == 0 && pending.3 == 0);
        ensure!(symbol_ids(client, file_id).await?.len() == 2);
        let stored_hash: Vec<u8> = client
            .query_one("SELECT hash FROM files WHERE id = $1", &[&file_id])
            .await?
            .get(0);
        ensure!(stored_hash == Sha256::digest(content.as_bytes()).to_vec());
        ensure!(calls.load(Ordering::SeqCst) == attempt + 1);
        client
            .batch_execute("ALTER TABLE call_graph DISABLE TRIGGER fixture_failure")
            .await?;
        skip(&service, "retry.rs", content).await?;
        let recovered = state(client, file_id).await?;
        ensure!(recovered.0 && recovered.2 == 2 && recovered.3 == 1);
        let names: Vec<String> = client
            .query(
                "SELECT name FROM symbols WHERE file_id = $1 ORDER BY name",
                &[&file_id],
            )
            .await?
            .iter()
            .map(|row| row.get(0))
            .collect();
        ensure!(names == ["helper", "updated"]);
        let graph_rows: i64 = client
            .query_one("SELECT COUNT(*) FROM call_graph", &[])
            .await?
            .get(0);
        ensure!(graph_rows == 1, "retry must not accumulate calls");
        ensure!(calls.load(Ordering::SeqCst) == attempt + 1);
    }

    let empty_id = seed(client, "comment.rs", "// no symbols\n").await?;
    skip(&service, "comment.rs", "// no symbols\n").await?;
    let empty_state = state(client, empty_id).await?;
    ensure!(empty_state.0 && empty_state.2 == 0 && empty_state.3 == 0);
    skip(&service, "comment.rs", "// no symbols\n").await?;
    ensure!(state(client, empty_id).await? == empty_state);

    ensure!(matches!(
        service
            .process_raw_file(
                1,
                "public-fixture",
                raw("no-chunks.rs", "// empty-chunk fixture\nfn present() {}\n")
            )
            .await?,
        ProcessResult::Processed {
            chunks: 0,
            embeddings: 0
        }
    ));
    let no_chunks_id: i64 = client
        .query_one("SELECT id FROM files WHERE path = 'no-chunks.rs'", &[])
        .await?
        .get(0);
    let no_chunks = state(client, no_chunks_id).await?;
    ensure!(no_chunks.0 && no_chunks.2 == 1);
    let chunks: Vec<i64> = client
        .query("SELECT id FROM chunks ORDER BY id", &[])
        .await?
        .iter()
        .map(|row| row.get(0))
        .collect();
    ensure!(
        chunks == [chunk_id],
        "all skip paths preserve chunk identity"
    );
    let outbox: i64 = client
        .query_one("SELECT COUNT(*) FROM indexing_outbox", &[])
        .await?
        .get(0);
    ensure!(outbox == 0, "no chunk or vector writes on skip");
    ensure!(calls.load(Ordering::SeqCst) == 3);
    ensure!(matches!(trap.accept(), Err(e) if e.kind() == std::io::ErrorKind::WouldBlock));
    println!("issue51: hash retry, both version-skip UPSERTs, partial-write recovery, zero-symbol completion, empty-chunk analysis, stable chunk IDs and vector isolation PASS");
    Ok(())
}

#[tokio::test]
#[ignore = "requires explicitly isolated PostgreSQL fixture; executed by CI"]
async fn postgres_intelligence_retry_preserves_chunk_skips() -> anyhow::Result<()> {
    let url = std::env::var("MAINRAG_INDEX_TEST_DATABASE_URL")
        .context("explicit isolated fixture URL required")?;
    let mut config: tokio_postgres::Config = url.parse()?;
    ensure!(
        config.get_dbname() == Some("mainrag_index_fixture"),
        "refusing non-fixture database"
    );
    let schema = format!("issue51_{}", uuid::Uuid::new_v4().simple());
    let (admin, connection) = config.connect(NoTls).await?;
    let task = tokio::spawn(connection);
    admin
        .batch_execute(&format!(
            "CREATE SCHEMA {schema}; SET search_path TO {schema}"
        ))
        .await?;
    config.options(&format!("-c search_path={schema}"));
    let pool = deadpool_postgres::Pool::builder(deadpool_postgres::Manager::new(config, NoTls))
        .max_size(4)
        .build()?;
    let result = tokio::time::timeout(
        std::time::Duration::from_secs(60),
        exercise(&admin, pool.clone()),
    )
    .await;
    pool.close();
    let cleanup = admin
        .batch_execute(&format!("DROP SCHEMA {schema} CASCADE"))
        .await;
    drop(admin);
    task.await??;
    cleanup?;
    result.context("isolated regression exceeded its time budget")?
}

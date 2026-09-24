//! Actual adapter-to-persistence measurements in an explicitly disposable database.

use super::*;
use crate::config::{QdrantConfig, TeiConfig};
use anyhow::{ensure, Context};
use tokio_postgres::NoTls;

const CONTENT: &str = "pub fn baseline_helper() -> usize { 42 }\npub fn baseline_entry() -> usize { baseline_helper() }\n";

#[path = "corpus_baseline.rs"]
mod corpus;

struct FixtureDirectory(std::path::PathBuf);

impl Drop for FixtureDirectory {
    fn drop(&mut self) {
        let _ = std::fs::remove_file(self.0.join("baseline.rs"));
        let _ = std::fs::remove_dir(&self.0);
    }
}

async fn create_schema(client: &tokio_postgres::Client) -> anyhow::Result<()> {
    client
        .batch_execute(include_str!("intelligence_retry_fixture.sql"))
        .await?;
    client
        .batch_execute(
            "CREATE TABLE sources (id BIGINT PRIMARY KEY, name TEXT, type TEXT, path TEXT,
         last_synced TIMESTAMPTZ, file_count BIGINT, total_size BIGINT, updated_at TIMESTAMPTZ);
         ALTER TABLE chunks ADD COLUMN chunk_type TEXT, ADD COLUMN content_hash BYTEA,
         ADD COLUMN content_compressed BYTEA, ADD COLUMN content_text TEXT,
         ADD COLUMN end_line INTEGER, ADD COLUMN level SMALLINT,
         ADD COLUMN context_prefix TEXT, ADD COLUMN parent_chunk_id BIGINT;
         CREATE TABLE sync_ledger (source_id BIGINT, pg_chunk_count BIGINT,
         qdrant_point_count BIGINT, drift_count BIGINT, status TEXT, details TEXT);",
        )
        .await?;
    Ok(())
}

async fn exercise(client: &tokio_postgres::Client, pool: PostgresPool) -> anyhow::Result<()> {
    create_schema(client).await?;
    let path =
        std::env::temp_dir().join(format!("mainrag-ingest-baseline-{}", uuid::Uuid::new_v4()));
    std::fs::create_dir(&path)?;
    let directory = FixtureDirectory(path);
    std::fs::write(directory.0.join("baseline.rs"), CONTENT)?;
    let path = directory.0.to_str().context("fixture path must be UTF-8")?;
    client
        .execute(
            "INSERT INTO sources(id,name,type,path) VALUES(1,'public-baseline','fs',$1)",
            &[&path],
        )
        .await?;

    // Do not serve vector dependencies: even a connection attempt is a failure.
    let trap = std::net::TcpListener::bind("127.0.0.1:0")?;
    trap.set_nonblocking(true)?;
    let url = format!("http://{}", trap.local_addr()?);
    let service = IndexService::new(
        pool,
        Arc::new(TeiClient::new(&TeiConfig {
            url: url.clone(),
            reranker_url: None,
            model: None,
            embedding_dim: None,
        })),
        Arc::new(QdrantClient::new(&QdrantConfig {
            url,
            api_key: None,
            chunk_collection: "fixture".into(),
            code_collection: "fixture".into(),
            synonyms_collection: None,
        })),
    )?;
    let mut initial_ids = Vec::<i64>::new();
    for phase in ["initial", "unchanged"] {
        let start = std::time::Instant::now();
        let stats = service.index_source(1).await?;
        let elapsed = start.elapsed();
        ensure!(
            stats.errors.is_empty(),
            "supported source ingest reported errors"
        );
        ensure!(stats.files_deleted == 0 && stats.embeddings_generated == 0);
        ensure!(stats.source_io["content_read_coverage"] == "COMPLETE");
        ensure!(stats.source_io["total_content_read_bytes"] == 2 * CONTENT.len() as u64);
        ensure!(stats.source_io["deferred_read_bytes"] == 0);
        ensure!(stats.source_io["device_read_bytes"].is_null());
        let ids: Vec<i64> = client
            .query("SELECT id FROM chunks ORDER BY id", &[])
            .await?
            .iter()
            .map(|row| row.get(0))
            .collect();
        ensure!(
            !ids.is_empty(),
            "real chunker must produce persisted chunks"
        );
        if phase == "initial" {
            ensure!(stats.files_processed == 1 && stats.files_skipped == 0);
            ensure!(stats.work.chunker_calls == 1 && stats.work.intelligence_parser_calls == 1);
            ensure!(stats.chunks_created == ids.len());
            initial_ids = ids;
        } else {
            ensure!(stats.files_processed == 0 && stats.files_skipped == 1);
            ensure!(stats.work.chunker_calls == 0 && stats.work.intelligence_parser_calls == 0);
            ensure!(stats.chunks_created == 0 && ids == initial_ids);
        }
        let row = client.query_one("SELECT hash,content,intelligence_analyzed_at IS NOT NULL FROM files WHERE source_id=1", &[]).await?;
        ensure!(row.get::<_, Vec<u8>>(0) == Sha256::digest(CONTENT.as_bytes()).to_vec());
        ensure!(zstd::decode_all(row.get::<_, Vec<u8>>(1).as_slice())? == CONTENT.as_bytes());
        ensure!(row.get::<_, bool>(2));
        println!(
            "ingest baseline: {}",
            serde_json::json!({
                "phase": phase, "status": "PASS", "logical_input_bytes": CONTENT.len(),
                "source_io": stats.source_io, "work": stats.work,
                "files_processed": stats.files_processed, "files_skipped": stats.files_skipped,
                "chunks_created": stats.chunks_created, "elapsed_seconds": elapsed.as_secs_f64(),
                "fixture_sha256": format!("{:x}", Sha256::digest(CONTENT.as_bytes())),
                "scope": "public_eager_filesystem_cpu_ingest"
            })
        );
    }
    let rows: i64 = client
        .query_one(
            "SELECT COUNT(*) FROM sync_ledger WHERE status='cpu_mode'",
            &[],
        )
        .await?
        .get(0);
    ensure!(
        rows == 2,
        "both supported ingests must complete their ledger writes"
    );
    let outbox: i64 = client
        .query_one("SELECT COUNT(*) FROM indexing_outbox", &[])
        .await?
        .get(0);
    ensure!(outbox == 0);
    ensure!(matches!(trap.accept(), Err(e) if e.kind() == std::io::ErrorKind::WouldBlock));
    Ok(())
}

#[tokio::test]
#[ignore = "requires an explicitly isolated PostgreSQL fixture and CPU mode"]
async fn postgres_supported_source_initial_and_repeat() -> anyhow::Result<()> {
    run_fixture(false).await
}

#[tokio::test]
#[ignore = "requires an explicitly isolated PostgreSQL fixture and CPU mode"]
async fn postgres_supported_frozen_corpus_baseline() -> anyhow::Result<()> {
    run_fixture(true).await
}

async fn run_fixture(frozen_corpus: bool) -> anyhow::Result<()> {
    ensure!(
        cpu_mode_enabled(),
        "CPU mode must be configured before starting the test process"
    );
    let url = std::env::var("MAINRAG_INDEX_TEST_DATABASE_URL")
        .context("explicit fixture URL required")?;
    let mut config: tokio_postgres::Config = url.parse()?;
    ensure!(
        config.get_dbname() == Some("mainrag_index_fixture"),
        "refusing non-fixture database"
    );
    let schema = format!("baseline55_{}", uuid::Uuid::new_v4().simple());
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
    let result = tokio::time::timeout(std::time::Duration::from_secs(60), async {
        if frozen_corpus {
            corpus::exercise(&admin, pool.clone()).await
        } else {
            exercise(&admin, pool.clone()).await
        }
    })
    .await;
    pool.close();
    let cleanup = admin
        .batch_execute(&format!("DROP SCHEMA {schema} CASCADE"))
        .await;
    drop(admin);
    task.await??;
    cleanup?;
    result.context("supported ingestion baseline exceeded its time budget")?
}

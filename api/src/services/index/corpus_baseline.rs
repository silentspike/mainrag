//! Frozen public corpus, ingested by the actual current path before FTS measurement.

use super::*;
use serde_json::{json, Value};

macro_rules! corpus {
    ($($name:literal),+ $(,)?) => {
        &[$(($name, include_str!(concat!("../../../../eval/storage_v2/fixtures/corpus/", $name)))),+]
    };
}

const DOCUMENTS: &[(&str, &str)] = corpus!(
    "authorization.md",
    "common-alpha.md",
    "common-beta.md",
    "generations.md",
    "graph.md",
    "intelligence.md",
    "legacy.md",
    "migration.md",
    "mutable-decoy.md",
    "phrase-decoy.md",
    "search.md",
    "storage.md",
);
const QUERIES: &str = include_str!("../../../../eval/storage_v2/fixtures/queries.jsonl");
const QUERY_SQL: &str = include_str!("../../../../eval/storage_v2/current_path_query.sql");
const SEARCH_SCHEMA: &str = "
ALTER TABLE chunks ADD COLUMN fts_simple TSVECTOR GENERATED ALWAYS AS
    (to_tsvector('simple', content_text)) STORED,
    ADD COLUMN fts_english TSVECTOR GENERATED ALWAYS AS
    (to_tsvector('english', content_text)) STORED;
CREATE INDEX fixture_simple ON chunks USING gin(fts_simple);
CREATE INDEX fixture_english ON chunks USING gin(fts_english);
CREATE VIEW documents AS SELECT c.id,f.path,c.fts_simple,c.fts_english
    FROM chunks c JOIN files f ON f.id=c.file_id;";

struct CorpusDirectory(std::path::PathBuf);
impl CorpusDirectory {
    fn cleanup(&self) -> std::io::Result<()> {
        for (name, _) in DOCUMENTS {
            std::fs::remove_file(self.0.join(name))?;
        }
        std::fs::remove_dir(&self.0)
    }
}
impl Drop for CorpusDirectory {
    fn drop(&mut self) {
        for (name, _) in DOCUMENTS {
            let _ = std::fs::remove_file(self.0.join(name));
        }
        let _ = std::fs::remove_dir(&self.0);
    }
}

fn hash(bytes: &[u8]) -> String {
    format!("{:x}", Sha256::digest(bytes))
}

pub(super) async fn exercise(
    client: &tokio_postgres::Client,
    pool: PostgresPool,
) -> anyhow::Result<()> {
    create_schema(client).await?;
    client.batch_execute(SEARCH_SCHEMA).await?;
    let path =
        std::env::temp_dir().join(format!("mainrag-corpus-baseline-{}", uuid::Uuid::new_v4()));
    std::fs::create_dir(&path)?;
    let directory = CorpusDirectory(path);
    let mut corpus_digest = Sha256::new();
    let mut logical_bytes = 0_u64;
    let mut expected_read_bytes = 0_u64;
    for (name, content) in DOCUMENTS {
        std::fs::write(directory.0.join(name), content)?;
        corpus_digest.update((name.len() as u64).to_be_bytes());
        corpus_digest.update(name.as_bytes());
        corpus_digest.update((content.len() as u64).to_be_bytes());
        corpus_digest.update(content.as_bytes());
        logical_bytes += content.len() as u64;
        expected_read_bytes += content.len() as u64 + (content.len() as u64).min(512);
    }
    let path = directory
        .0
        .to_str()
        .context("fixture directory must be UTF-8")?;
    client
        .execute(
            "INSERT INTO sources(id,name,type,path) VALUES(1,'public-corpus','fs',$1)",
            &[&path],
        )
        .await?;
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
    let mut observations = Vec::new();
    let mut identities = Vec::<(i64, String, Vec<u8>)>::new();
    for phase in ["initial", "unchanged"] {
        let start = std::time::Instant::now();
        let stats = service.index_source(1).await?;
        let elapsed_ms = start.elapsed().as_secs_f64() * 1000.0;
        ensure!(
            stats.errors.is_empty(),
            "supported corpus ingestion reported errors"
        );
        ensure!(stats.source_io["total_content_read_bytes"] == expected_read_bytes);
        ensure!(stats.source_io["content_read_coverage"] == "COMPLETE");
        ensure!(stats.files_deleted == 0 && stats.embeddings_generated == 0);
        let rows = client.query("SELECT c.id,f.path,c.content_hash FROM chunks c JOIN files f ON f.id=c.file_id ORDER BY c.id", &[]).await?;
        let current: Vec<(i64, String, Vec<u8>)> = rows
            .iter()
            .map(|row| (row.get(0), row.get(1), row.get(2)))
            .collect();
        ensure!(!current.is_empty());
        if phase == "initial" {
            ensure!(stats.files_processed == DOCUMENTS.len() && stats.files_skipped == 0);
            ensure!(stats.work.chunker_calls == DOCUMENTS.len() as u64);
            ensure!(stats.work.intelligence_parser_calls == DOCUMENTS.len() as u64);
            ensure!(stats.chunks_created == current.len());
            identities = current;
        } else {
            ensure!(stats.files_processed == 0 && stats.files_skipped == DOCUMENTS.len());
            ensure!(stats.work.chunker_calls == 0 && stats.work.intelligence_parser_calls == 0);
            ensure!(stats.chunks_created == 0 && current == identities);
        }
        for (name, content) in DOCUMENTS {
            let row = client.query_one("SELECT hash,content,intelligence_analyzed_at IS NOT NULL FROM files WHERE path=$1", &[name]).await?;
            ensure!(row.get::<_, Vec<u8>>(0) == Sha256::digest(content.as_bytes()).to_vec());
            ensure!(zstd::decode_all(row.get::<_, Vec<u8>>(1).as_slice())? == content.as_bytes());
            ensure!(row.get::<_, bool>(2));
        }
        let stored: i64 = client
            .query_one(
                "SELECT COALESCE(SUM(octet_length(content_compressed)),0)::bigint FROM chunks",
                &[],
            )
            .await?
            .get(0);
        observations.push(
            json!({"phase":phase,"status":"PASS","logical_input_bytes":logical_bytes,
            "source_io":stats.source_io,"work":stats.work,"files_processed":stats.files_processed,
            "files_skipped":stats.files_skipped,"chunks_created":stats.chunks_created,
            "chunk_compressed_bytes":stored,"elapsed_ms":elapsed_ms,"errors":stats.errors.len()}),
        );
    }
    client
        .batch_execute("ANALYZE chunks; ANALYZE files;")
        .await?;
    let queries: Vec<Value> = QUERIES
        .lines()
        .filter(|line| !line.trim().is_empty())
        .map(serde_json::from_str)
        .collect::<std::result::Result<_, _>>()?;
    ensure!(!queries.is_empty());
    let mut query_results = Vec::new();
    for query in queries {
        let text = query["query"]
            .as_str()
            .context("public query text missing")?;
        let constructor = if query["phrase"] == true {
            "phraseto_tsquery"
        } else {
            "websearch_to_tsquery"
        };
        let sql = QUERY_SQL
            .replace("{constructor}", constructor)
            .replace("{query}", "$1");
        let mut stable: Option<Value> = None;
        let mut first_ms = 0.0;
        let mut samples = Vec::new();
        for iteration in 0..34 {
            let start = std::time::Instant::now();
            let result: Value = client.query_one(&sql, &[&text]).await?.get(0);
            let ms = start.elapsed().as_secs_f64() * 1000.0;
            if iteration == 0 {
                first_ms = ms;
            }
            if iteration >= 4 {
                samples.push(ms);
            }
            if let Some(previous) = &stable {
                ensure!(*previous == result, "non-deterministic query results");
            }
            stable = Some(result);
        }
        query_results.push(json!({"id":query["id"],"observation":stable,"first_ms":first_ms,"warm_samples_ms":samples}));
    }
    let ledger_count: i64 = client
        .query_one(
            "SELECT COUNT(*) FROM sync_ledger WHERE status='cpu_mode'",
            &[],
        )
        .await?
        .get(0);
    ensure!(ledger_count == 2);
    let outbox_count: i64 = client
        .query_one("SELECT COUNT(*) FROM indexing_outbox", &[])
        .await?
        .get(0);
    ensure!(outbox_count == 0);
    ensure!(matches!(trap.accept(),Err(e) if e.kind()==std::io::ErrorKind::WouldBlock));
    let schema: String = client.query_one("SELECT string_agg(table_name || '.' || column_name || ':' || data_type, ',' ORDER BY table_name,ordinal_position) FROM information_schema.columns WHERE table_schema=current_schema()", &[]).await?.get(0);
    let server_version: String = client.query_one("SHOW server_version", &[]).await?.get(0);
    let mut fixture_definition = Sha256::new();
    fixture_definition.update(include_bytes!("baseline_tests.rs"));
    fixture_definition.update(include_bytes!("intelligence_retry_fixture.sql"));
    fixture_definition.update(include_bytes!("corpus_baseline.rs"));
    let lexical_asset = std::env::var("TOKENIZER_ASSET_PATH")
        .context("explicit pinned lexical asset required for reproducible baseline")?;
    let lexical_asset_sha256 = hash(&std::fs::read(lexical_asset)?);
    directory
        .cleanup()
        .context("owned corpus directory cleanup failed")?;
    println!(
        "corpus baseline: {}",
        json!({"schema_version":"supported-ingest-observation/v1",
        "corpus_sha256":format!("{:x}",corpus_digest.finalize()),"corpus_items":DOCUMENTS.len(),
        "query_set_sha256":hash(QUERIES.as_bytes()),"query_sql_sha256":hash(QUERY_SQL.as_bytes()),
        "schema_columns_sha256":hash(schema.as_bytes()),"backend_version":server_version,
        "fixture_definition_sha256":format!("{:x}",fixture_definition.finalize()),
        "configuration":{"cpu_mode":cpu_mode_enabled(),"chunker":service.chunker.name(),
            "chunker_version":chunker_version(),"active_lexical_profile":std::env::var("TOKENIZER_VERSION").unwrap_or_else(|_|"hf_bge_wordpiece".into()),
            "indexed_lexical_version":tokenizer_version(),"embedding_model_id":embedding_model_id(),
            "lexical_asset_sha256":lexical_asset_sha256,
            "warmups_per_query":3,"measured_iterations_per_query":30,"concurrency":1},
        "ingest":observations,"queries":query_results,"stable_chunk_count":identities.len(),
        "vector_connection_attempts":0,"outbox_rows":outbox_count,"ledger_rows":ledger_count,
        "scope":"supported_eager_filesystem_cpu_ingest_and_chunk_fts"})
    );
    Ok(())
}

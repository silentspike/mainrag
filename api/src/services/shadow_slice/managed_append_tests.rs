//! Complete producer-to-persistence exercise against a disposable database.

use super::*;
use anyhow::ensure;
use std::os::unix::fs::PermissionsExt;
use std::process::Command;
use tokio_postgres::{Client, NoTls};

const PRINCIPAL: &str = "00000000-0000-4000-8000-000000000063";
const COMMIT: &str = "6363636363636363636363636363636363636363";

struct Directory(PathBuf);

impl Drop for Directory {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.0);
    }
}

async fn connect(config: &tokio_postgres::Config) -> Result<Client> {
    let (client, connection) = config.connect(NoTls).await?;
    tokio::spawn(async move {
        let _ = connection.await;
    });
    Ok(client)
}

fn producer(script: &Path, operation: &str, root: &Path, input: Option<&Path>) -> Result<()> {
    let mut command = Command::new("python3");
    command.arg(script).arg(operation).arg(root);
    if let Some(input) = input {
        command.arg(input);
    }
    let output = command.output()?;
    ensure!(output.status.success(), "managed fixture producer failed");
    Ok(())
}

async fn run(client: &mut Client, root: &Path, packs: &Path) -> Result<ShadowSliceResult> {
    let transaction = client.transaction().await?;
    transaction
        .batch_execute(&format!("SET LOCAL app.user_id='{PRINCIPAL}'"))
        .await?;
    let result = Box::pin(run_release_candidate_build(
        &transaction,
        63,
        "managed_append",
        root,
        packs,
        4096,
        COMMIT,
    ))
    .await?;
    transaction.commit().await?;
    Ok(result)
}

fn assert_measured_reads(
    result: &ShadowSliceResult,
    adapter_bytes: u64,
    deferred_bytes: u64,
    parser_passes: u64,
) -> Result<()> {
    let telemetry = &result.telemetry;
    ensure!(
        telemetry["ablauf"]["adapter_source_read_bytes"].as_u64() == Some(adapter_bytes)
            && telemetry["ablauf"]["parser_passes"].as_u64() == Some(parser_passes)
            && telemetry["source_io"]["application_read_bytes"].as_u64() == Some(deferred_bytes)
            && telemetry["source_io"]["adapter_read_bytes"].as_u64() == Some(adapter_bytes)
            && telemetry["source_io"]["total_content_read_bytes"].as_u64()
                == adapter_bytes.checked_add(deferred_bytes)
            && telemetry["source_io"]["content_read_coverage"] == "COMPLETE"
            && telemetry["source_io"]["coverage"] == "PARTIAL"
            && telemetry["source_io"]["device_read_bytes"].is_null(),
        "managed release candidate read, parser or coverage telemetry differs"
    );
    Ok(())
}

#[tokio::test]
#[ignore = "requires the isolated pgvector CI database"]
async fn managed_append_producer_to_verified_delta_and_periodic_full() -> Result<()> {
    let url = std::env::var("MAINRAG_INDEX_TEST_DATABASE_URL")
        .context("explicit fixture database URL required")?;
    let mut config: tokio_postgres::Config = url.parse()?;
    ensure!(
        config.get_dbname() == Some("mainrag_index_fixture"),
        "refusing non-fixture database"
    );
    let admin = connect(&config).await?;
    admin.batch_execute("DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='mainrag') THEN CREATE ROLE mainrag; END IF; END $$;").await?;
    let database = format!("managed_append_{}", Uuid::new_v4().simple());
    admin
        .batch_execute(&format!("CREATE DATABASE {database}"))
        .await?;
    config.dbname(&database);
    let directory = Directory(std::env::temp_dir().join(format!("mainrag-{database}")));
    std::fs::create_dir_all(&directory.0)?;
    let result: Result<()> = async {
        let root = directory.0.join("source");
        let packs = directory.0.join("packs");
        let input = directory.0.join("input.jsonl");
        let project = Path::new(env!("CARGO_MANIFEST_DIR")).parent()
            .context("API manifest has no repository parent")?;
        let script = project.join("tools/managed_append.py");
        producer(&script, "init", &root, None)?;
        let first_bytes = b"{\"event\":\"first\"}\n";
        std::fs::write(&input, first_bytes)?;
        producer(&script, "append", &root, Some(&input))?;

        let schema = Command::new("psql")
            .arg("-X").arg("--no-psqlrc")
            .arg("--set=ON_ERROR_STOP=1")
            .arg("--host=127.0.0.1").arg("--username=fixture")
            .arg("--dbname").arg(&database)
            .arg("--file").arg(project.join("schema.sql"))
            .env("PGPASSWORD", "fixture_only")
            .output()?;
        ensure!(schema.status.success(), "managed fixture schema installation failed: {}",
            String::from_utf8_lossy(&schema.stderr));
        let mut client = connect(&config).await?;
        client.batch_execute(
            "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles \
             WHERE rolname='mainrag_v2_frontier_owner') THEN \
             CREATE ROLE mainrag_v2_frontier_owner NOLOGIN INHERIT; END IF; END $$; \
             GRANT mainrag TO mainrag_v2_frontier_owner; \
             GRANT SELECT ON ALL TABLES IN SCHEMA public TO mainrag;"
        ).await?;
        client.batch_execute(&format!(
            "CREATE TABLE users(id UUID PRIMARY KEY, is_admin BOOLEAN NOT NULL); \
             INSERT INTO users VALUES ('{PRINCIPAL}', TRUE); \
             CREATE FUNCTION user_can_access_source(p_user_id UUID, p_source_id BIGINT, p_action TEXT DEFAULT 'read') \
             RETURNS BOOLEAN LANGUAGE SQL STABLE SECURITY DEFINER SET search_path=pg_catalog,public \
             AS $$ SELECT EXISTS(SELECT 1 FROM users WHERE id=p_user_id AND is_admin) $$;"
        )).await?;
        // Match persistent table and routine ownership before installing the controlled
        // definers. SELECT alone cannot authorize their checked FOR UPDATE
        // publishers; the production role owns this pre-frontier schema.
        client
            .batch_execute(
                r#"DO $fixture_owner$
                DECLARE relation RECORD; routine REGPROCEDURE;
                BEGIN
                    FOR relation IN
                        SELECT c.relname,c.relkind FROM pg_class c
                        JOIN pg_namespace n ON n.oid=c.relnamespace
                        WHERE n.nspname='public' AND c.relkind IN ('r','p')
                    LOOP
                        EXECUTE format('ALTER TABLE public.%I OWNER TO mainrag',relation.relname);
                    END LOOP;
                    FOR routine IN
                        SELECT oid::REGPROCEDURE FROM pg_proc
                        WHERE pronamespace='public'::REGNAMESPACE
                          AND proowner=current_user::REGROLE
                          AND (proname LIKE 'storage_v2_%' OR proname='user_can_access_source')
                    LOOP
                        EXECUTE format('ALTER FUNCTION %s OWNER TO mainrag',routine);
                    END LOOP;
                END $fixture_owner$;"#,
            )
            .await?;
        // Run the actual native producer/delta/full path with the complete
        // current schema, including the located writer and compact readers.
        let migrations = std::fs::read_dir(project.join("migrations"))?
            .map(|entry| entry.map(|value| value.path()))
            .collect::<std::io::Result<Vec<_>>>()?;
        for number in 66..=105 {
            let prefix = format!("{number:03}_");
            let matching = migrations
                .iter()
                .filter(|path| {
                    path.file_name()
                        .is_some_and(|name| name.to_string_lossy().starts_with(&prefix))
                })
                .collect::<Vec<_>>();
            ensure!(matching.len() == 1, "one current fixture migration required");
            let installed = Command::new("psql")
                .arg("-X")
                .arg("--no-psqlrc")
                .arg("--set=ON_ERROR_STOP=1")
                .arg("--host=127.0.0.1")
                .arg("--username=fixture")
                .arg("--dbname")
                .arg(&database)
                .arg("--file")
                .arg(matching[0])
                .env("PGPASSWORD", "fixture_only")
                .output()?;
            ensure!(
                installed.status.success(),
                "current fixture migration failed: {}",
                String::from_utf8_lossy(&installed.stderr)
            );
        }
        client.execute(
            "INSERT INTO sources(id,name,type,path,is_test) VALUES (63,'managed-fixture','managed_append',$1,TRUE)",
            &[&root.to_str().context("managed fixture path is not UTF-8")?],
        ).await?;

        let code_root=directory.0.join("code-source");
        std::fs::create_dir(&code_root)?;
        let code=(0..130).map(|n| format!("pub fn fixture_{n}() {{}}\n")).collect::<String>();
        std::fs::write(code_root.join("fixture.rs"),code)?;
        client.execute("INSERT INTO sources(id,name,type,path,is_test) VALUES (164,'bounded-card-fixture','fs',$1,TRUE)",
            &[&code_root.to_str().context("fixture root is not UTF-8")?]).await?;
        let progress_id=Uuid::new_v4();
        let progress=crate::services::build_progress::BuildProgressRecorder::create(&packs,164,COMMIT,Uuid::new_v4(),progress_id)?;
        let transaction=client.transaction().await?;
        transaction.batch_execute(&format!("SET LOCAL app.user_id='{PRINCIPAL}'")).await?;
        let grouped=Box::pin(run_release_candidate_build_with_progress(&transaction,164,"fs",&code_root,&packs,4096,COMMIT,Some(&progress))).await?;
        let before_commit=crate::services::build_progress::read(&packs,164,COMMIT,progress_id)?;
        ensure!(before_commit.staged_items==grouped.item_count && before_commit.phase=="transaction_pending" && !before_commit.transaction_committed,
            "progress confused staged rows with transaction commit");
        transaction.commit().await?;
        progress.finish(true)?;
        ensure!(crate::services::build_progress::read(&packs,164,COMMIT,progress_id)?.transaction_committed,
            "progress did not retain the observed commit");
        ensure!(grouped.symbol_count>=130 && grouped.telemetry["ablauf"]["structural_card_batch_calls"].as_u64()==Some(3)
            && grouped.telemetry["ablauf"]["db_staging_round_trips"].as_u64().is_some_and(|calls| calls<10),
            "bounded card projection did not reduce round trips or retain all symbols");
        println!("{}",json!({"fixture":"bounded-card-projection","items":grouped.item_count,"symbols":grouped.symbol_count,"telemetry":grouped.telemetry}));

        // Interrupt after the first complete-item commit but before the
        // progress receipt. Resume must use database evidence and the same run.
        let resume_root = directory.0.join("resume-source");
        std::fs::create_dir(&resume_root)?;
        for index in 0..256 {
            std::fs::write(resume_root.join(format!("item-{index:04}.txt")),
                format!("checkpoint fixture item {index} alpha beta"))?;
        }
        // The filesystem adapter accepts .txt; the Dockerfile prefix still
        // identifies the unavailable grammar, as it does in Git ingestion.
        let unavailable_root = resume_root.join("z-unavailable-grammar");
        std::fs::create_dir(&unavailable_root)?;
        let dockerfile = "FROM scratch\nRUN echo checkpoint availability\n";
        std::fs::write(unavailable_root.join("Dockerfile.txt"), dockerfile)?;
        client.execute("INSERT INTO sources(id,name,type,path,is_test) VALUES \
            (165,'checkpoint-fixture','fs',$1,TRUE)",
            &[&resume_root.to_str().context("fixture path is not UTF-8")?]).await?;
        let manager = deadpool_postgres::Manager::new(config.clone(), NoTls);
        let pool = deadpool_postgres::Pool::builder(manager).max_size(3).build()?;
        let principal = Uuid::parse_str(PRINCIPAL)?;
        let session = crate::db::build_checkpoint::BuildCheckpointSession::open(
            &pool, principal, 165, &packs).await?;
        session.fail_after_checkpoints(2);
        ensure!(crate::db::build_checkpoint::BuildCheckpointSession::open(
            &pool, principal, 165, &packs).await.is_err(), "concurrent source writer was admitted");
        let maintenance = std::fs::OpenOptions::new().read(true).write(true)
            .open(packs.join(".maintenance.lock"))?;
        ensure!(maintenance.try_lock().is_err(), "pack maintenance bypassed an active build");
        let interrupted = Box::pin(run_release_candidate_build_checkpointed(
            &session, 165, "fs", &resume_root, &packs, 4096, COMMIT, None, None, None)).await;
        ensure!(interrupted.unwrap_err().to_string().contains("controlled interruption"),
            "fixture did not stop at the durable checkpoint");
        drop(session);
        let saved = client.query_one("SELECT r.id,r.generation_id, \
            (SELECT count(*) FROM storage_v2_ingest_run_item WHERE run_id=r.id) AS items \
            FROM storage_v2_ingest_run r WHERE source_id=165 AND status='building'", &[]).await?;
        let saved_run: i64 = saved.get("id");
        let saved_generation: i64 = saved.get("generation_id");
        ensure!(saved.get::<_, i64>("items") == 128, "complete-item checkpoint was not retained");
        let resumed_progress_id = Uuid::new_v4();
        let resumed_progress = crate::services::build_progress::BuildProgressRecorder::create(
            &packs,165,COMMIT,Uuid::new_v4(),resumed_progress_id)?;
        let session = crate::db::build_checkpoint::BuildCheckpointSession::open(
            &pool, principal, 165, &packs).await?;
        let session = session.expect_run(Some(saved_run));
        let resumed = Box::pin(run_release_candidate_build_checkpointed(
            &session,165,"fs",&resume_root,&packs,4096,COMMIT,None,None,Some(&resumed_progress))).await?;
        session.finish().await?;
        drop(session);
        resumed_progress.finish(true)?;
        ensure!(resumed.run_id==saved_run && resumed.generation_id==saved_generation && resumed.item_count==257,
            "resume changed generation identity or omitted items");
        let counts = client.query_one("SELECT \
            (SELECT count(*) FROM source_generation WHERE source_id=165) AS generations, \
            (SELECT count(*) FROM occurrence WHERE source_id=165) AS occurrences", &[]).await?;
        ensure!(counts.get::<_,i64>("generations")==1 && counts.get::<_,i64>("occurrences")==257,
            "resume duplicated semantic rows");
        let unavailable = client.query_one("SELECT cache.result,body.id AS body_id \
            FROM occurrence occurrence_row JOIN artifact_version artifact \
              ON artifact.id=occurrence_row.artifact_version_id \
            JOIN content_node node ON node.id=artifact.content_root_node_id \
            JOIN content_body body ON body.id=node.body_id \
            JOIN storage_v2_analysis_cache cache ON cache.content_identity_sha256=body.digest \
            WHERE occurrence_row.source_id=165 AND occurrence_row.source_path LIKE '%/Dockerfile.txt'",&[]).await?;
        let analysis: serde_json::Value=unavailable.get("result");
        let verification = client.transaction().await?;
        verification.batch_execute(&format!("SET LOCAL app.user_id='{PRINCIPAL}'")).await?;
        let stored = find_and_verify_existing_body(&verification,&packs,dockerfile.as_bytes(),4096)
            .await?.context("unavailable grammar body is missing")?;
        verification.commit().await?;
        ensure!(stored.id==unavailable.get::<_,i64>("body_id")
            && analysis["parser_availability"]["status"]=="unavailable"
            && analysis["parser_availability"]["recognized_language"]=="dockerfile"
            && analysis["symbols"]==json!([]) && analysis["calls"]==json!([]),
            "unavailable grammar lost source bytes or fabricated intelligence");
        let receipt = crate::services::build_progress::read(&packs,165,COMMIT,resumed_progress_id)?;
        ensure!(receipt.committed_items==257 && receipt.transaction_committed,
            "resume progress omitted the observed final commit");
        println!("{}",json!({"fixture":"durable-item-resume","committed_before_interruption":128,
            "final_items":257,"same_generation":true,"duplicate_occurrences":0}));

        let initial = Box::pin(run(&mut client, &root, &packs)).await?;
        ensure!(initial.item_count == 1 && !initial.reused_generation,
            "initial managed generation is incomplete");
        ensure!(initial.telemetry["ablauf"]["append_full_comparisons"] == 1,
            "initial full comparison was not counted");
        let manifest_path = root.join("manifest.json");
        let initial_manifest = std::fs::read(&manifest_path)?;
        assert_measured_reads(
            &initial,
            2 * (initial_manifest.len() + first_bytes.len()) as u64,
            3 * first_bytes.len() as u64,
            1,
        )?;
        let transaction = client.transaction().await?;
        transaction
            .batch_execute(&format!(
                "SET LOCAL app.user_id='{PRINCIPAL}'; SET LOCAL statement_timeout='10s'"
            ))
            .await?;
        let verified = Box::pin(verify_release_candidate(
            &transaction,
            63,
            &ReleaseCandidateVerifyInput {
                generation_id: initial.generation_id,
            },
            &packs,
            4096,
        ))
        .await?;
        ensure!(
            verified.lexical_segment_verification["invalid_count"] == 0,
            "managed lexical projection differs"
        );
        let restored_timeout: String = transaction
            .query_one("SHOW statement_timeout", &[])
            .await?
            .get(0);
        ensure!(
            restored_timeout == "10s",
            "managed verification changed the caller's statement timeout"
        );
        transaction.rollback().await?;
        drop(client);
        let mut client = connect(&config).await?;
        let repeated = Box::pin(run(&mut client, &root, &packs)).await?;
        ensure!(repeated.reused_generation && repeated.generation_id == initial.generation_id,
            "unchanged managed generation was duplicated");
        assert_measured_reads(&repeated, initial_manifest.len() as u64, 0, 0)?;

        let second_bytes = b"{\"event\":\"second\"}\n";
        std::fs::write(&input, second_bytes)?;
        producer(&script, "append", &root, Some(&input))?;
        let delta = Box::pin(run(&mut client, &root, &packs)).await?;
        ensure!(delta.item_count == 2 && delta.generation_seq > initial.generation_seq,
            "managed delta did not advance its generation");
        ensure!(delta.telemetry["ablauf"]["reuse_bodies"].as_u64().unwrap_or(0) >= 1
            && delta.telemetry["ablauf"]["reuse_analysis"].as_u64().unwrap_or(0) >= 1,
            "prior segment was not reused");
        let manifest_size = std::fs::metadata(root.join("manifest.json"))?.len();
        let adapter_reads = delta.telemetry["ablauf"]["adapter_source_read_bytes"]
            .as_u64().context("managed adapter reads were not measured")?;
        ensure!(adapter_reads == 2 * (manifest_size + second_bytes.len() as u64),
            "delta adapter read the old segment or omitted a verification pass");
        assert_measured_reads(&delta, adapter_reads, 3 * second_bytes.len() as u64, 1)?;
        ensure!(delta.telemetry["ablauf"]["eingang_bytes"].as_u64()
            == Some((first_bytes.len() + second_bytes.len()) as u64),
            "logical input length was not preserved");
        let staged: i64 = client.query_one(
            "SELECT COUNT(*) FROM storage_v2_ingest_run_item WHERE run_id=$1 AND parser_pass_count=0",
            &[&delta.run_id],
        ).await?.get(0);
        ensure!(staged >= 1, "managed prefix did not stage reused items");
        let old_interval: i64 = client.query_one(
            "SELECT COUNT(*) FROM generation_item_version WHERE source_id=63 \
             AND valid_from_seq=$1 AND valid_to_seq IS NULL",
            &[&initial.generation_seq],
        ).await?.get(0);
        ensure!(old_interval == 1, "unchanged interval was copied or closed");

        let stable_manifest = std::fs::read(&manifest_path)?;
        std::fs::write(&manifest_path, &initial_manifest)?;
        let shrink = Box::pin(run(&mut client, &root, &packs))
            .await
            .unwrap_err();
        ensure!(shrink.to_string().contains("prefix shrank"),
            "a shortened managed manifest did not fail at the trusted prefix");
        std::fs::write(&manifest_path, &stable_manifest)?;
        let mut rotated: serde_json::Value = serde_json::from_slice(&stable_manifest)?;
        rotated["epoch"] = serde_json::json!(Uuid::new_v4().to_string());
        std::fs::write(&manifest_path, serde_json::to_vec(&rotated)?)?;
        let rotation = Box::pin(run(&mut client, &root, &packs))
            .await
            .unwrap_err();
        ensure!(rotation.to_string().contains("epoch changed"),
            "an unapproved managed epoch did not fail at the trusted prefix");
        std::fs::write(&manifest_path, &stable_manifest)?;
        let mut drifted: serde_json::Value = serde_json::from_slice(&stable_manifest)?;
        drifted["segments"][0]["sha256"] = serde_json::json!("00".repeat(32));
        std::fs::write(&manifest_path, serde_json::to_vec(&drifted)?)?;
        let prefix_drift = Box::pin(run(&mut client, &root, &packs))
            .await
            .unwrap_err();
        ensure!(prefix_drift.to_string().contains("trusted prefix chain changed"),
            "managed prefix-chain drift was not classified");
        std::fs::write(&manifest_path, &stable_manifest)?;

        let manifest: serde_json::Value = serde_json::from_slice(&stable_manifest)?;
        let old_name = manifest["segments"][0]["name"].as_str()
            .context("managed fixture lost the old segment")?;
        let old_path = root.join("segments").join(old_name);
        std::fs::set_permissions(&old_path, std::fs::Permissions::from_mode(0o600))?;
        std::fs::write(&old_path, b"{\"event\":\"other\"}\n")?;
        // The fixture owner schedules the next complete comparison through
        // the dedicated non-login table owner; ordinary clients retain the
        // production prohibition on direct frontier mutation.
        let schedule = client.transaction().await?;
        schedule
            .batch_execute(&format!(
                "SET LOCAL app.user_id='{PRINCIPAL}'; \
                 SET LOCAL ROLE mainrag_v2_frontier_owner; \
                 UPDATE storage_v2_managed_append_frontier \
                 SET appends_since_full=31 WHERE source_id=63"
            ))
            .await?;
        schedule.commit().await?;
        std::fs::write(&input, b"{\"event\":\"third\"}\n")?;
        producer(&script, "append", &root, Some(&input))?;
        let replacement = Box::pin(run(&mut client, &root, &packs))
            .await
            .unwrap_err();
        ensure!(replacement.to_string().contains("does not match its manifest"),
            "scheduled full comparison did not detect the replaced segment");
        let pointer: Option<i64> = client.query_one(
            "SELECT active_generation_id FROM logical_source WHERE id=63", &[],
        ).await?.get(0);
        ensure!(pointer.is_none(), "managed fixture changed the active pointer");
        Ok(())
    }.await;
    drop(admin);
    let cleanup_admin = connect(&url.parse()?).await?;
    let cleanup = cleanup_admin
        .batch_execute(&format!("DROP DATABASE {database} WITH (FORCE)"))
        .await;
    cleanup?;
    result
}

//! Reviewed source-consistency selection and one immutable cut per sync.

use super::*;
use crate::plugins::{fs_cut, fs_scope::FilesystemScope};
use serde_json::{Map, Value};
use sha2::{Digest, Sha256};

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct FilesystemCutConfiguration {
    pub expected_source_root_sha256: String,
    pub expected_config_sha256: String,
    pub config: Value,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct FilesystemCutCapture {
    pub expected_source_root_sha256: String,
    pub expected_config_sha256: String,
}

fn config_digest(config: &Value) -> anyhow::Result<String> {
    Ok(hex::encode(Sha256::digest(serde_json::to_vec(config)?)))
}

fn normalized(config: &Value) -> anyhow::Result<Map<String, Value>> {
    let decoded;
    let value = if let Some(text) = config.as_str() {
        decoded = serde_json::from_str::<Value>(text)?;
        &decoded
    } else {
        config
    };
    match value {
        Value::Null => Ok(Map::new()),
        Value::Object(object) => Ok(object.clone()),
        _ => anyhow::bail!("filesystem configuration must be an object or null"),
    }
}

fn validate_change(old: &Value, new: &Value) -> anyhow::Result<()> {
    FilesystemScope::from_config(old)?;
    FilesystemScope::from_config(new)?;
    let mut before = normalized(old)?;
    let mut after = normalized(new)?;
    before.remove("filesystem_consistency");
    after.remove("filesystem_consistency");
    before.remove("conversation_text_projection");
    after.remove("conversation_text_projection");
    anyhow::ensure!(
        before == after,
        "cut configuration cannot change registered source scope"
    );
    Ok(())
}

pub async fn admin_configure_filesystem_cut(
    State(state): State<Arc<AppState>>,
    Extension(claims): Extension<Arc<crate::auth::Claims>>,
    Path(source_id): Path<i64>,
    JsonBody(request): JsonBody<FilesystemCutConfiguration>,
) -> Result<Json<Value>> {
    let user_id = Uuid::parse_str(&claims.sub)
        .map_err(|_| AppError::Unauthorized("invalid user id".to_string()))?;
    state.rls_client.with_rls(user_id,true,move |transaction|Box::pin(async move {
        let row=transaction.query_opt(
            "SELECT source.type,source.path,source.config,source.is_test,pointer.active_generation_id \
             FROM sources source LEFT JOIN logical_source pointer ON pointer.id=source.id \
             WHERE source.id=$1 FOR UPDATE OF source", &[&source_id]).await?
            .ok_or_else(||AppError::NotFound("registered source is unavailable".to_string()))?;
        let path=row.get::<_,String>("path");
        let old=row.get::<_,Option<Value>>("config").unwrap_or(Value::Null);
        let invalid=||AppError::BadRequest("source-cut configuration identity or scope differs".to_string());
        if source_id<=0 || row.get::<_,String>("type")!="fs" || row.get::<_,bool>("is_test")
            || fs_cut::root_digest(FsPath::new(&path)).map_err(|_|invalid())?!=request.expected_source_root_sha256
            || config_digest(&old).map_err(|_|invalid())?!=request.expected_config_sha256
            || validate_change(&old,&request.config).is_err() {
            return Err(invalid());
        }
        let scope=FilesystemScope::from_config(&request.config).map_err(|_|invalid())?;
        if row.get::<_,Option<i64>>("active_generation_id").is_some()
            && scope.release_profile()!=FilesystemScope::from_config(&old).map_err(|_|invalid())?.release_profile() {
            return Err(AppError::BadRequest("active source consistency profile cannot change here".to_string()));
        }
        if scope.release_profile()!=FilesystemScope::from_config(&old).map_err(|_|invalid())?.release_profile()
            && transaction.query_one("SELECT EXISTS(SELECT 1 FROM storage_v2_ingest_run WHERE source_id=$1 AND status='building')",
                &[&source_id]).await?.get::<_,bool>(0) {
            return Err(AppError::BadRequest("building source consistency profile cannot change here".to_string()));
        }
        transaction.execute("UPDATE sources SET config=$2,updated_at=NOW() WHERE id=$1",
            &[&source_id,&request.config]).await?;
        Ok(Json(serde_json::json!({"source_id":source_id,"source_root_sha256":request.expected_source_root_sha256,
            "previous_config_sha256":request.expected_config_sha256,"config_sha256":config_digest(&request.config).map_err(|_|invalid())?,
            "adapter_profile_id":scope.release_profile(),"scope_preserved":true})))
    })).await
}

async fn registration(
    state: &Arc<AppState>,
    user_id: Uuid,
    source_id: i64,
) -> Result<(String, String, Value, bool)> {
    state
        .rls_client
        .with_rls(user_id, true, move |transaction| {
            Box::pin(async move {
                let row = transaction
                    .query_opt(
                        "SELECT type,path,config,is_test FROM sources WHERE id=$1",
                        &[&source_id],
                    )
                    .await?
                    .ok_or_else(|| {
                        AppError::NotFound("registered source is unavailable".to_string())
                    })?;
                Ok((
                    row.get("type"),
                    row.get("path"),
                    row.get::<_, Option<Value>>("config").unwrap_or(Value::Null),
                    row.get("is_test"),
                ))
            })
        })
        .await
}

async fn capture_registration(
    state: &Arc<AppState>,
    user_id: Uuid,
    source_id: i64,
    expected: (String, String, Value, bool),
) -> Result<fs_cut::CutProof> {
    if source_id <= 0
        || expected.0 != "fs"
        || expected.3
        || !FilesystemScope::from_config(&expected.2)
            .map_err(|_| {
                AppError::BadRequest("invalid filesystem consistency selection".to_string())
            })?
            .cut_consistency
    {
        return Err(AppError::BadRequest(
            "source has no selected immutable-cut contract".to_string(),
        ));
    }
    fs_cut::capture(FsPath::new(&expected.1))
        .await
        .map_err(|_| AppError::Internal("controlled source-cut capture failed".to_string()))?;
    if registration(state, user_id, source_id).await? != expected {
        return Err(AppError::BadRequest(
            "registered source changed during cut capture".to_string(),
        ));
    }
    let root = PathBuf::from(expected.1);
    let selected = tokio::task::spawn_blocking(move || fs_cut::ReadCut::select(&root))
        .await
        .map_err(|_| AppError::Internal("source-cut inspector failed".to_string()))?
        .map_err(|_| AppError::Internal("source-cut identity verification failed".to_string()))?;
    Ok(selected.proof)
}

pub async fn admin_capture_filesystem_cut(
    State(state): State<Arc<AppState>>,
    Extension(claims): Extension<Arc<crate::auth::Claims>>,
    Path(source_id): Path<i64>,
    JsonBody(request): JsonBody<FilesystemCutCapture>,
) -> Result<Json<fs_cut::CutProof>> {
    let user_id = Uuid::parse_str(&claims.sub)
        .map_err(|_| AppError::Unauthorized("invalid user id".to_string()))?;
    let registered = registration(&state, user_id, source_id).await?;
    if fs_cut::root_digest(FsPath::new(&registered.1))
        .map_err(|_| AppError::BadRequest("invalid source root".to_string()))?
        != request.expected_source_root_sha256
        || config_digest(&registered.2)
            .map_err(|_| AppError::BadRequest("invalid source config".to_string()))?
            != request.expected_config_sha256
    {
        return Err(AppError::BadRequest(
            "source-cut capture identity differs".to_string(),
        ));
    }
    let pack_root = state.config.storage_v2_pack_root.clone();
    let capture_state = state.clone();
    state.rls_client.with_checkpointed_source(
        user_id, source_id, None, pack_root, move |session| Box::pin(async move {
            let pending: bool = session.client().query_one(
                "SELECT EXISTS(SELECT 1 FROM storage_v2_ingest_run WHERE source_id=$1 AND status='building')",
                &[&source_id]).await?.get(0);
            if pending {
                return Err(AppError::Conflict("cannot replace the cut of a retained building run".to_string()));
            }
            Ok(Json(capture_registration(&capture_state, user_id, source_id, registered).await?))
        }),
    ).await
}

pub(super) async fn capture_before_active_ingest(
    state: &Arc<AppState>,
    session: &crate::db::build_checkpoint::BuildCheckpointSession,
    user_id: Uuid,
    source_id: i64,
    manifest: &str,
    commit: &str,
) -> Result<()> {
    session
        .validate_source(source_id)
        .map_err(|_| AppError::Conflict("source-cut writer session differs".to_string()))?;
    let registered = registration(state, user_id, source_id).await?;
    if registered.0 != "fs" {
        return Ok(());
    }
    let scope = FilesystemScope::from_config(&registered.2).map_err(|_| {
        AppError::BadRequest("invalid filesystem consistency selection".to_string())
    })?;
    if !scope.cut_consistency {
        return Ok(());
    }
    let client = session.client();
    client
        .query_one(
            "SELECT storage_v2_require_complete_active_set($1)",
            &[&manifest],
        )
        .await?;
    let active: i64 = client.query_opt(
        "SELECT generation.id FROM logical_source pointer JOIN source_generation generation \
         ON generation.id=pointer.active_generation_id WHERE pointer.id=$1 AND generation.status='active'",
        &[&source_id]).await?.ok_or_else(|| AppError::BadRequest(
            "source-cut regular ingest requires an active source".to_string()))?.get(0);
    if let Some(pending) = client.query_opt(
        "SELECT run.id,run.expected_active_generation_id,generation.witness \
         FROM storage_v2_ingest_run run JOIN source_generation generation ON generation.id=run.generation_id \
         WHERE run.source_id=$1 AND run.status='building'", &[&source_id]).await? {
        session.validate_run(pending.get("id")).map_err(|_| AppError::Conflict(
            "retained active-source run differs".to_string()))?;
        if pending.get::<_,Option<i64>>("expected_active_generation_id") != Some(active) {
            return Err(AppError::Conflict("retained source-cut predecessor differs".to_string()));
        }
        let expected = retained_active_cut(&pending.get::<_,Value>("witness"), commit)
            .map_err(|_| AppError::Conflict("retained source-cut writer identity differs".to_string()))?;
        let root = PathBuf::from(registered.1);
        let selected = tokio::task::spawn_blocking(move || fs_cut::ReadCut::select(&root))
            .await.map_err(|_| AppError::Internal("retained source-cut inspector failed".to_string()))?
            .map_err(|_| AppError::Conflict("retained source cut is unavailable".to_string()))?;
        if selected.proof != expected {
            return Err(AppError::Conflict("retained immutable source cut changed".to_string()));
        }
        return Ok(());
    }
    capture_registration(state, user_id, source_id, registered).await?;
    Ok(())
}

fn retained_active_cut(witness: &Value, commit: &str) -> anyhow::Result<fs_cut::CutProof> {
    anyhow::ensure!(
        witness["lexical_input"] == "native"
            && witness["checkpoint_protocol"] == "complete-items-v1"
            && witness["commit_sha"].as_str() == Some(commit),
        "retained run is not the current native checkpointed writer"
    );
    let observation: fs_cut::CutObservation =
        serde_json::from_value(witness["filesystem_cut"].clone())?;
    Ok(observation.cut)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn retained_cut_resume_requires_original_native_checkpoint_writer() {
        let commit = "6363636363636363636363636363636363636363";
        let cut = fs_cut::CutProof {
            format: "mainrag.fs-read-cut.v1".into(),
            cut_id: Uuid::from_u128(1),
            source_root_sha256: "a".repeat(64),
            descriptor_sha256: "b".repeat(64),
            snapshot_uuid: Uuid::from_u128(2),
            origin_uuid: Uuid::from_u128(3),
            captured_at_unix: 1,
        };
        let witness = serde_json::json!({
            "lexical_input":"native", "checkpoint_protocol":"complete-items-v1",
            "commit_sha":commit, "filesystem_cut":fs_cut::CutObservation {
                cut:cut.clone(), fixture_sha256:"c".repeat(64), item_count:257, input_bytes:4096,
            },
        });
        assert_eq!(retained_active_cut(&witness, commit).unwrap(), cut);
        assert!(retained_active_cut(&witness, &"d".repeat(40)).is_err());
        for (field, value) in [
            ("lexical_input", serde_json::json!("legacy")),
            ("checkpoint_protocol", Value::Null),
            ("filesystem_cut", Value::Null),
        ] {
            let mut incompatible = witness.clone();
            incompatible[field] = value;
            assert!(retained_active_cut(&incompatible, commit).is_err());
        }
        let mut replaced = cut.clone();
        replaced.descriptor_sha256 = "e".repeat(64);
        assert_ne!(retained_active_cut(&witness, commit).unwrap(), replaced);
    }

    #[test]
    fn cut_selection_preserves_historical_configuration_and_exact_scope() {
        let old = serde_json::json!("{\"file_patterns\":[\"*.jsonl\"],\"retained\":7}");
        let new = serde_json::json!({"file_patterns":["*.jsonl"],"retained":7,"filesystem_consistency":fs_cut::KIND});
        validate_change(&old, &new).unwrap();
        validate_change(&new, &old).unwrap();
        let mut wrong = new.clone();
        wrong["file_patterns"] = serde_json::json!(["*.txt"]);
        assert!(validate_change(&old, &wrong).is_err());
        wrong = new.clone();
        wrong["retained"] = serde_json::json!(8);
        assert!(validate_change(&old, &wrong).is_err());
        wrong = new.clone();
        wrong["filesystem_consistency"] = serde_json::json!("unknown");
        assert!(validate_change(&old, &wrong).is_err());
        assert_ne!(config_digest(&old).unwrap(), config_digest(&new).unwrap());
        let mut projected = new.clone();
        projected["conversation_text_projection"] = serde_json::json!("utf8-nul-space-v1");
        validate_change(&new, &projected).unwrap();
        validate_change(&projected, &new).unwrap();
        projected["retained"] = serde_json::json!(8);
        assert!(validate_change(&new, &projected).is_err());
        projected = new.clone();
        projected["conversation_text_projection"] = serde_json::json!("unknown");
        assert!(validate_change(&new, &projected).is_err());
    }
}

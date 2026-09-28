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
    Ok(Json(
        capture_registration(&state, user_id, source_id, registered).await?,
    ))
}

pub(super) async fn capture_before_active_ingest(
    state: &Arc<AppState>,
    user_id: Uuid,
    source_id: i64,
    manifest: String,
) -> Result<()> {
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
    state.rls_client.with_rls(user_id,true,move |transaction|Box::pin(async move {
        transaction.query_one("SELECT storage_v2_require_complete_active_set($1)",&[&manifest]).await?;
        let count:i64=transaction.query_one(
            "SELECT count(*) FROM logical_source pointer JOIN source_generation generation \
             ON generation.id=pointer.active_generation_id WHERE pointer.id=$1 AND generation.status='active'",&[&source_id]).await?.get(0);
        if count!=1 {return Err(AppError::BadRequest("source-cut regular ingest requires an active source".to_string()));}
        Ok(())
    })).await?;
    capture_registration(state, user_id, source_id, registered).await?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

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
    }
}

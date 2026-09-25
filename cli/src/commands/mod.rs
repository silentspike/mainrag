pub mod add;
pub mod auth;
pub mod backfill;
pub mod call_graph;
pub mod card;
pub mod config;
pub mod dead_end;
pub mod explain;
pub mod explore;
pub mod health;
pub mod layers;
pub mod lifecycle;
pub mod ownership;
pub mod search;
pub mod source;
pub mod stats;
pub mod symbols;
pub mod watch;

pub async fn intelligence_active(
    client: &crate::client::ApiClient,
    read_path: &str,
    generation: Option<&str>,
) -> anyhow::Result<bool> {
    if generation.is_some() {
        if matches!(read_path, "auto" | "storage_v2") {
            return Ok(false);
        }
        anyhow::bail!("--generation requires --read-path storage_v2 or auto");
    }
    match read_path {
        "auto" => Ok(client.default_intelligence_read_path().await? == "storage_v2_active"),
        "current" => Ok(false),
        "storage_v2_active" => Ok(true),
        "storage_v2" => anyhow::bail!("--read-path storage_v2 requires --generation and --source"),
        _ => anyhow::bail!("unsupported intelligence read path"),
    }
}

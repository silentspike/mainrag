use anyhow::Result;
use colored::Colorize;

use crate::client::ApiClient;

/// Run orphaned chunk backfill (admin-only maintenance command)
/// Finds chunks without embeddings and processes them in batches
pub async fn run_orphaned(client: &ApiClient, json_output: bool) -> Result<()> {
    if !json_output {
        println!("{}", "Triggering orphaned chunk backfill...".cyan());
    }

    let result = client.backfill_orphaned().await?;

    if json_output {
        println!("{}", serde_json::to_string_pretty(&result)?);
    } else if result.processed == 0 {
        println!("{}", "No orphaned chunks found.".green());
    } else {
        println!(
            "{} {} chunks in {} batches",
            "Backfill complete:".green(),
            result.processed,
            result.batches
        );
        if !result.message.is_empty() {
            println!("{}", result.message.dimmed());
        }
    }

    Ok(())
}

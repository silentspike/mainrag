use anyhow::Result;
use colored::Colorize;

use crate::client::{ApiClient, IntelligenceBackfillRequest};

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

/// Run code intelligence backfill for files missing symbols/call graph rows.
pub async fn run_intelligence(
    client: &ApiClient,
    source_id: Option<i64>,
    limit: Option<i64>,
    force: bool,
    json_output: bool,
) -> Result<()> {
    if !json_output {
        println!("{}", "Triggering intelligence backfill...".cyan());
    }

    let request = IntelligenceBackfillRequest {
        source_id,
        limit,
        force,
    };
    let result = client.backfill_intelligence(&request).await?;

    if json_output {
        println!("{}", serde_json::to_string_pretty(&result)?);
    } else {
        println!(
            "{} {} processed, {} skipped, {} errors ({} candidates)",
            "Intelligence backfill complete:".green(),
            result.processed,
            result.skipped,
            result.errors,
            result.candidates
        );
        if !result.message.is_empty() {
            println!("{}", result.message.dimmed());
        }
        for file in result
            .files
            .iter()
            .filter(|file| file.status != "processed")
        {
            let reason = file.reason.as_deref().unwrap_or("no reason reported");
            println!(
                "{} {}: {}",
                file.status.yellow(),
                file.path,
                reason.dimmed()
            );
        }
    }

    Ok(())
}

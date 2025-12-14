use crate::client::ApiClient;
use colored::Colorize;
use humansize::{format_size, BINARY};
use serde_json::json;
use std::io::{self, Write};
use tabled::{Table, settings::Style};

pub async fn run(
    client: &ApiClient,
    action: super::super::SourceAction,
    json_output: bool,
) -> anyhow::Result<()> {
    use super::super::SourceAction;

    match action {
        SourceAction::List => list_sources(client, json_output).await,
        SourceAction::Sync { name } => sync_source(client, &name, json_output).await,
        SourceAction::Delete { name, force } => delete_source(client, &name, force, json_output).await,
    }
}

async fn list_sources(client: &ApiClient, json_output: bool) -> anyhow::Result<()> {
    if !json_output {
        eprint!("{}", "Fetching sources...".cyan());
        eprint!(" ");
    }

    let resp = client.list_sources().await?;

    if json_output {
        println!(
            "{}",
            serde_json::to_string_pretty(&json!({
                "sources": resp.sources,
                "count": resp.count,
            }))?
        );
        return Ok(());
    }

    if resp.sources.is_empty() {
        println!("{}", "\r✓ No sources found".yellow());
        return Ok(());
    }

    println!("{}", format!("\r✓ Found {} sources", resp.count).green());
    println!();

    for source in resp.sources {
        let last_synced = source
            .last_synced
            .as_ref()
            .map(|s| s.split('T').next().unwrap_or("never"))
            .unwrap_or("never");

        println!("  {} {}", format!("[{}]", source.id).dimmed(), source.name.bold());
        println!("    {} {}", "Type:".dimmed(), source.source_type.cyan());
        println!("    {} {}", "Files:".dimmed(), source.file_count);
        println!("    {} {}", "Size:".dimmed(), format_size(source.total_size as u64, BINARY));
        println!("    {} {}", "Last synced:".dimmed(), last_synced);
        println!();
    }

    Ok(())
}

async fn sync_source(
    client: &ApiClient,
    name: &str,
    json_output: bool,
) -> anyhow::Result<()> {
    if !json_output {
        eprint!("{}", format!("Syncing source '{}'...", name).cyan());
        eprint!(" ");
    }

    let result = client.sync_source(name).await?;

    if json_output {
        println!(
            "{}",
            serde_json::to_string_pretty(&json!({
                "source_id": result.source_id,
                "source_name": result.source_name,
                "status": result.status,
                "files_processed": result.files_processed,
                "chunks_created": result.chunks_created,
            }))?
        );
        return Ok(());
    }

    println!(
        "{}",
        format!(
            "\r✓ Synced '{}' ({} files, {} chunks)",
            result.source_name, result.files_processed, result.chunks_created
        )
        .green()
    );

    Ok(())
}

async fn delete_source(
    client: &ApiClient,
    name: &str,
    force: bool,
    json_output: bool,
) -> anyhow::Result<()> {
    if !force && !json_output {
        print!(
            "{}",
            format!("Delete source '{}'? This cannot be undone. (yes/no): ", name)
                .yellow()
        );
        io::stdout().flush()?;

        let mut input = String::new();
        io::stdin().read_line(&mut input)?;

        if !input.trim().eq_ignore_ascii_case("yes") {
            println!("{}", "Cancelled.".dimmed());
            return Ok(());
        }
    }

    if !json_output {
        eprint!("{}", "Deleting...".cyan());
        eprint!(" ");
    }

    client.delete_source(name).await?;

    if json_output {
        println!("{}", json!({"status": "deleted", "source_name": name}));
        return Ok(());
    }

    println!(
        "{}",
        format!("\r✓ Source '{}' deleted", name).green()
    );

    Ok(())
}

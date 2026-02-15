use crate::client::ApiClient;
use colored::Colorize;
use humansize::{format_size, BINARY};
use serde_json::json;
use std::io::{self, Write};

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
                "total": resp.total,
            }))?
        );
        return Ok(());
    }

    if resp.sources.is_empty() {
        println!("{}", "\r✓ No sources found".yellow());
        return Ok(());
    }

    println!("{}", format!("\r✓ Found {} sources", resp.total).green());
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
                "source_name": name,
                "status": result.status,
                "files_processed": result.stats.files_processed,
                "chunks_created": result.stats.chunks_created,
                "embeddings_generated": result.stats.embeddings_generated,
            }))?
        );
        return Ok(());
    }

    println!(
        "{}",
        format!(
            "\r✓ Synced '{}' ({} files, {} chunks, {} embeddings)",
            name,
            result.stats.files_processed,
            result.stats.chunks_created,
            result.stats.embeddings_generated
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
    // Get source stats before deletion for display
    let sources = client.list_sources().await?;
    let source = sources.sources.iter().find(|s| s.name == name);

    let (file_count, total_size) = match source {
        Some(s) => (s.file_count, s.total_size),
        None => {
            return Err(anyhow::anyhow!("Source '{}' not found", name));
        }
    };

    // Get detailed stats via stats endpoint if available
    let stats = client.get_source_deletion_stats(name).await.ok();

    if !force && !json_output {
        println!();
        println!("{}", "━━━ Source Deletion Preview ━━━".yellow().bold());
        println!("  {} {}", "Source:".dimmed(), name.bold());
        println!("  {} {}", "Files:".dimmed(), file_count);
        println!("  {} {}", "Size:".dimmed(), format_size(total_size as u64, BINARY));

        if let Some(ref s) = stats {
            println!("  {} {}", "Chunks:".dimmed(), s.chunks);
            println!("  {} {}", "Symbols:".dimmed(), s.symbols);
            println!("  {} {}", "Call Graph:".dimmed(), s.call_graph);
            println!("  {} {}", "Qdrant Vectors:".dimmed(), s.qdrant_vectors);
        }

        println!();
        println!("{}", "This will delete:".red());
        println!("  • All files and chunks from PostgreSQL");
        println!("  • All symbols and call-graph data");
        println!("  • All vectors from Qdrant");
        println!();

        print!("{}", "Type 'yes' to confirm: ".yellow());
        io::stdout().flush()?;

        let mut input = String::new();
        io::stdin().read_line(&mut input)?;

        if !input.trim().eq_ignore_ascii_case("yes") {
            println!("{}", "Cancelled.".dimmed());
            return Ok(());
        }
    }

    if !json_output {
        println!();
        println!("{}", "Deleting source...".cyan());
        println!("  {} Cleaning indexing queue...", "→".dimmed());
    }

    client.delete_source(name).await?;

    if json_output {
        println!("{}", json!({
            "status": "deleted",
            "source_name": name,
            "files_deleted": file_count,
            "bytes_freed": total_size
        }));
        return Ok(());
    }

    println!("  {} PostgreSQL data removed", "✓".green());
    println!("  {} Qdrant vectors removed", "✓".green());
    println!();
    println!(
        "{}",
        format!("✓ Source '{}' completely deleted", name).green().bold()
    );

    Ok(())
}

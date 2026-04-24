use crate::client::ApiClient;
use colored::Colorize;
use humansize::{format_size, BINARY};
use serde_json::json;

pub async fn run(client: &ApiClient, json_output: bool) -> anyhow::Result<()> {
    if !json_output {
        eprint!("{}", "Fetching statistics...".cyan());
        eprint!(" ");
    }

    let stats = client.stats().await?;

    if json_output {
        println!(
            "{}",
            serde_json::to_string_pretty(&json!({
                "sources_count": stats.sources_count,
                "files_count": stats.files_count,
                "chunks_count": stats.chunks_count,
                "total_size_bytes": stats.total_size_bytes,
            }))?
        );
        return Ok(());
    }

    println!("{}", "\r✓ System Statistics".green());
    println!();
    println!(
        "  {} {}",
        "Sources:".cyan(),
        stats.sources_count.to_string().bold()
    );
    println!(
        "  {} {}",
        "Files:".cyan(),
        stats.files_count.to_string().bold()
    );
    println!(
        "  {} {}",
        "Chunks:".cyan(),
        stats.chunks_count.to_string().bold()
    );
    println!(
        "  {} {}",
        "Total Size:".cyan(),
        format_size(stats.total_size_bytes as u64, BINARY).bold()
    );

    Ok(())
}

use crate::client::ApiClient;
use colored::Colorize;
use serde_json::json;
use tabled::{Table, settings::Style};

pub async fn run(
    client: &ApiClient,
    query: &str,
    mode: &str,
    limit: u32,
    source: Option<&str>,
    json_output: bool,
) -> anyhow::Result<()> {
    if !json_output {
        eprint!("{}", "Searching...".cyan());
        eprint!(" ");
    }

    let results = client.search(query, mode, limit, source).await?;

    if json_output {
        println!(
            "{}",
            serde_json::to_string_pretty(&json!({
                "results": results.results,
                "count": results.count,
                "query_time_ms": results.query_time_ms,
            }))?
        );
        return Ok(());
    }

    // Human-readable output
    if results.results.is_empty() {
        println!("{}", "No results found.".yellow());
        return Ok(());
    }

    println!(
        "{}",
        format!(
            "\r✓ Found {} results in {}ms",
            results.count, results.query_time_ms
        )
        .green()
    );
    println!();

    // Print results
    for (idx, result) in results.results.iter().enumerate() {
        let score = format!("{:.4}", result.score);
        println!();
        println!("  {} {}", format!("#{}", idx + 1).bold(), score.yellow());
        println!("  {} {}", "File:".dimmed(), result.file_path);
        println!("  {} {}", "Content:".dimmed(),
            if result.content.len() > 100 {
                format!("{}...", &result.content[..97])
            } else {
                result.content.clone()
            }
        );
    }
    println!();
    println!("{}", "Tips:".bright_black());
    println!("  {}", "• Use --mode keyword|semantic|hybrid to change search type".dimmed());
    println!("  {}", "• Use --source NAME to filter by source".dimmed());
    println!("  {}", "• Use --limit N to get more results".dimmed());

    Ok(())
}

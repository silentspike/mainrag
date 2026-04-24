//! Dead-end command - Record and search known dead-end paths

use crate::client::ApiClient;
use anyhow::Result;
use colored::Colorize;

pub async fn run_add(
    client: &ApiClient,
    concept: &str,
    path: &str,
    reason: &str,
    symbols: &[String],
    source: Option<&str>,
) -> Result<()> {
    let id = client.create_negative_evidence(concept, path, reason, symbols, source).await?;
    println!("{} Dead-end recorded (id: {})", "OK".green(), id);
    println!("  Concept: {}", concept);
    println!("  Path: {}", path);
    println!("  Reason: {}", reason);
    if !symbols.is_empty() {
        println!("  Symbols: {}", symbols.join(", "));
    }
    Ok(())
}

pub async fn run_list(
    client: &ApiClient,
    concept: &str,
    json_output: bool,
) -> Result<()> {
    let results = client.search_negative_evidence(concept).await?;

    if json_output {
        println!("{}", serde_json::to_string_pretty(&results)?);
        return Ok(());
    }

    if results.is_empty() {
        println!("{}", format!("No dead-ends found for '{}'", concept).yellow());
        return Ok(());
    }

    println!("{}", format!("{} Found {} dead-end(s)", "OK".green(), results.len()).bold());

    for de in &results {
        println!();
        println!("  {} #{} — {}", "X".red().bold(), de.id, de.concept.bold());
        println!("    {} {}", "Path:".dimmed(), de.path_description);
        println!("    {} {}", "Reason:".dimmed(), de.reason);
        println!("    {} {}", "Severity:".dimmed(), de.severity);
        if let Some(arr) = de.symbols.as_array() {
            if !arr.is_empty() {
                let names: Vec<&str> = arr.iter().filter_map(|v| v.as_str()).collect();
                println!("    {} {}", "Symbols:".dimmed(), names.join(", "));
            }
        }
    }

    Ok(())
}

//! Ownership command - Show ownership/containment relations

use crate::client::ApiClient;
use anyhow::Result;
use colored::Colorize;

pub async fn run(client: &ApiClient, symbol: &str, json_output: bool) -> Result<()> {
    if !json_output {
        eprint!("{}", "Loading ownership...".cyan());
    }

    let url = format!(
        "{}/api/v1/intelligence/ownership?symbol={}",
        client.base_url(),
        urlencoding::encode(symbol)
    );
    let body = client.raw_get(&url).await?;

    if json_output {
        println!("{}", body);
        return Ok(());
    }

    let results: Vec<serde_json::Value> = serde_json::from_str(&body).unwrap_or_default();

    if results.is_empty() {
        println!(
            "{}",
            format!("\rNo ownership relations found for '{}'", symbol).yellow()
        );
        return Ok(());
    }

    println!(
        "{}",
        format!(
            "\r{} Found {} relation(s) for '{}'",
            "OK".green(),
            results.len(),
            symbol
        )
        .bold()
    );

    for rel in &results {
        let rel_type = rel["relation_type"].as_str().unwrap_or("?");
        let direction = rel["direction"].as_str().unwrap_or("outgoing");
        let source_name = rel["symbol_name"].as_str().unwrap_or("?");
        let target = rel["target_name"].as_str().unwrap_or("?");
        let confidence = rel["confidence"].as_f64().unwrap_or(0.0);
        let file = rel["target_file"].as_str().unwrap_or("");

        let rel_colored = match rel_type {
            "contains" => "contains".green().to_string(),
            "owned_by" => "owned_by".blue().to_string(),
            "wraps_target" => "wraps_target".magenta().to_string(),
            "uses" => "uses".cyan().to_string(),
            "creates_via" => "creates_via".cyan().to_string(),
            "deletes_via" => "deletes_via".red().to_string(),
            "delegates_to" => "delegates_to".yellow().to_string(),
            _ => rel_type.to_string(),
        };

        if direction == "incoming" {
            println!(
                "  {} {} {} {} ({:.0}%) {}",
                source_name.bold(),
                "<-".dimmed(),
                rel_colored,
                format!("<- {}", target).dimmed(),
                confidence * 100.0,
                file.dimmed(),
            );
        } else {
            println!(
                "  {} -> {} {} ({:.0}%) {}",
                source_name.bold(),
                rel_colored,
                target.bold(),
                confidence * 100.0,
                file.dimmed(),
            );
        }
    }

    Ok(())
}

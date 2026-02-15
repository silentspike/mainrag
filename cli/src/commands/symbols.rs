//! Symbols command - Search for code symbols (functions, classes, structs, etc.)

use crate::client::ApiClient;
use anyhow::Result;
use colored::Colorize;

pub async fn run(
    client: &ApiClient,
    query: &str,
    symbol_type: Option<&str>,
    limit: u32,
    json_output: bool,
) -> Result<()> {
    if !json_output {
        eprint!("{}", "Searching symbols...".cyan());
    }

    let results = client.search_symbols(query, symbol_type, limit).await?;

    if json_output {
        println!("{}", serde_json::to_string_pretty(&results)?);
        return Ok(());
    }

    if results.is_empty() {
        println!("{}", "\rNo symbols found.".yellow());
        return Ok(());
    }

    println!("{}", format!("\r{} Found {} symbols", "OK".green(), results.len()).bold());

    for sym in results {
        println!();
        // symbol_type is String, not Option
        println!("  {} {}", sym.name.bold(), format!("({})", sym.symbol_type).dimmed());
        println!("    {} {}:{}-{}",
            "Location:".dimmed(),
            sym.file_path,
            sym.line_start,
            sym.line_end
        );
        if let Some(ctx) = sym.context {
            let preview = if ctx.len() > 60 {
                format!("{}...", &ctx[..57])
            } else {
                ctx
            };
            println!("    {} {}", "Preview:".dimmed(), preview);
        }
    }

    Ok(())
}

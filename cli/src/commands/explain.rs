//! Explain command - Trace delegation chain through proxy -> dispatch -> mutation

use crate::client::ApiClient;
use anyhow::Result;
use colored::Colorize;

pub async fn run(
    client: &ApiClient,
    symbol: &str,
    source: Option<&str>,
    depth: Option<u32>,
    json_output: bool,
) -> Result<()> {
    if !json_output {
        eprint!("{}", "Tracing delegation chain...".cyan());
    }

    let chains = client.explain_path(symbol, source, depth).await?;

    if json_output {
        println!("{}", serde_json::to_string_pretty(&chains)?);
        return Ok(());
    }

    if chains.is_empty() {
        println!("{}", format!("\rNo delegation chain found for '{}'", symbol).yellow());
        return Ok(());
    }

    println!("{}", format!("\r{} Found {} chain(s) for '{}'", "OK".green(), chains.len(), symbol).bold());

    for (i, chain) in chains.iter().enumerate() {
        println!();
        let entry = &chain.entry_point;
        let layer = entry.layer.as_deref().unwrap_or("?");
        println!("  {} Chain {} — {} [{}]",
            ">>".blue().bold(),
            i + 1,
            entry.name.bold(),
            layer.cyan(),
        );
        println!("    {} {}:{}-{}", "Entry:".dimmed(), entry.file_path, entry.line_start, entry.line_end);

        // Show entry annotations
        for ann in &chain.annotations {
            println!("    {} {} = {}", "!".magenta(), ann.annotation_type.dimmed(), ann.value);
        }

        // Show delegation steps
        for (j, step) in chain.steps.iter().enumerate() {
            let role_colored = match step.role.as_str() {
                "proxy" => step.role.cyan().to_string(),
                "dispatch" => step.role.yellow().to_string(),
                "mutation" => step.role.green().bold().to_string(),
                _ => step.role.dimmed().to_string(),
            };

            let dispatch_info = step.dispatch_via.as_ref()
                .map(|d| format!(" via {}", d.yellow()))
                .unwrap_or_default();

            println!("    {} Step {} [{}]{} — {}",
                "->".blue(),
                j + 1,
                role_colored,
                dispatch_info,
                step.symbol.name.bold(),
            );

            if !step.symbol.file_path.is_empty() {
                println!("       {}:{}-{}", step.symbol.file_path, step.symbol.line_start, step.symbol.line_end);
            }

            // Code snippet
            if let Some(ref snippet) = step.code_snippet {
                for line in snippet.lines().take(5) {
                    println!("       {}", line.dimmed());
                }
            }

            // Step annotations
            for ann in &step.step_annotations {
                println!("       {} {} = {}", "!".magenta(), ann.annotation_type.dimmed(), ann.value);
            }
        }

        if chain.steps.is_empty() {
            println!("    {} (no delegation targets — likely an interface/abstract definition)", "~".dimmed());
        }
    }

    Ok(())
}

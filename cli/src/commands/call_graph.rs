//! Call-Graph command - Query function call relationships (1-hop and N-hop)

use crate::client::ApiClient;
use anyhow::Result;
use colored::Colorize;

pub async fn run(
    client: &ApiClient,
    function: &str,
    direction: &str,
    source: Option<&str>,
    depth: i32,
    json_output: bool,
) -> Result<()> {
    // N-hop mode: use call-chain endpoint
    if depth > 1 {
        return run_chain(client, function, direction, source, depth, json_output).await;
    }

    // Standard 1-hop mode
    if !json_output {
        eprint!("{}", "Analyzing call graph...".cyan());
    }

    let callers = if direction == "callers" || direction == "both" {
        client
            .find_callers(function, source)
            .await
            .unwrap_or_default()
    } else {
        vec![]
    };

    let callees = if direction == "callees" || direction == "both" {
        client
            .find_callees(function, source)
            .await
            .unwrap_or_default()
    } else {
        vec![]
    };

    if json_output {
        println!(
            "{}",
            serde_json::to_string_pretty(&serde_json::json!({
                "function": function,
                "callers": callers,
                "callees": callees
            }))?
        );
        return Ok(());
    }

    println!(
        "{}",
        format!("\r{} Call graph for '{}'", "OK".green(), function).bold()
    );

    if !callers.is_empty() {
        println!();
        println!(
            "  {} ({} functions)",
            "CALLERS".bold().blue(),
            callers.len()
        );
        for c in &callers {
            println!(
                "    {} {} ({}:{})",
                "<-".blue(),
                c.name,
                c.file_path,
                c.line
            );
        }
    }

    if !callees.is_empty() {
        println!();
        println!(
            "  {} ({} functions)",
            "CALLEES".bold().magenta(),
            callees.len()
        );
        for callee_name in &callees {
            println!("    {} {}", "->".magenta(), callee_name);
        }
    }

    if callers.is_empty() && callees.is_empty() {
        println!("{}", "  No call relationships found.".yellow());
    }

    Ok(())
}

/// N-hop call chain traversal
async fn run_chain(
    client: &ApiClient,
    function: &str,
    direction: &str,
    source: Option<&str>,
    depth: i32,
    json_output: bool,
) -> Result<()> {
    if !json_output {
        eprint!(
            "{}",
            format!("Tracing {}-hop {} chain...", depth, direction).cyan()
        );
    }

    let directions = if direction == "both" {
        vec!["callers", "callees"]
    } else {
        vec![direction]
    };

    let mut all_entries = vec![];
    for dir in &directions {
        let chain = client.find_call_chain(function, dir, depth, source).await?;
        all_entries.push((dir.to_string(), chain));
    }

    if json_output {
        let chains: serde_json::Value = all_entries
            .iter()
            .map(|(dir, entries)| (dir.clone(), serde_json::json!(entries)))
            .collect::<serde_json::Map<String, serde_json::Value>>()
            .into();
        println!(
            "{}",
            serde_json::to_string_pretty(&serde_json::json!({
                "function": function,
                "depth": depth,
                "chains": chains,
            }))?
        );
        return Ok(());
    }

    println!(
        "{}",
        format!(
            "\r{} {}-hop call chain for '{}'",
            "OK".green(),
            depth,
            function
        )
        .bold()
    );

    for (dir, entries) in &all_entries {
        if entries.is_empty() {
            continue;
        }

        println!();
        let label = if *dir == "callers" {
            "CALLER CHAIN (who calls this, transitively)"
        } else {
            "CALLEE CHAIN (what this calls, transitively)"
        };
        println!(
            "  {} ({} edges, up to {} hops)",
            label.bold(),
            entries.len(),
            depth
        );

        let mut current_depth = 0u32;
        for entry in entries {
            if entry.depth != current_depth {
                current_depth = entry.depth;
                println!("    {}:", format!("Hop {}", current_depth).dimmed());
            }
            let arrow = if *dir == "callers" { "<-" } else { "->" };
            println!(
                "      {} {} {} ({}:{})",
                arrow.cyan(),
                entry.from_name,
                format!("→ {}", entry.to_name).dimmed(),
                entry.file_path,
                entry.line
            );
        }
    }

    Ok(())
}

//! Call-Graph command - Query function call relationships

use crate::client::ApiClient;
use anyhow::Result;
use colored::Colorize;

pub async fn run(
    client: &ApiClient,
    function: &str,
    direction: &str,
    json_output: bool,
) -> Result<()> {
    if !json_output {
        eprint!("{}", "Analyzing call graph...".cyan());
    }

    let callers = if direction == "callers" || direction == "both" {
        client.find_callers(function).await.unwrap_or_default()
    } else {
        vec![]
    };

    let callees = if direction == "callees" || direction == "both" {
        client.find_callees(function).await.unwrap_or_default()
    } else {
        vec![]
    };

    if json_output {
        println!("{}", serde_json::to_string_pretty(&serde_json::json!({
            "function": function,
            "callers": callers,
            "callees": callees
        }))?);
        return Ok(());
    }

    println!("{}", format!("\r{} Call graph for '{}'", "OK".green(), function).bold());

    if !callers.is_empty() {
        println!();
        println!("  {} ({} functions)", "CALLERS".bold().blue(), callers.len());
        for c in &callers {
            println!("    {} {} ({}:{})", "<-".blue(), c.name, c.file_path, c.line);
        }
    }

    if !callees.is_empty() {
        println!();
        println!("  {} ({} functions)", "CALLEES".bold().magenta(), callees.len());
        // find_callees() returns Vec<String> (just names), not objects
        for callee_name in &callees {
            println!("    {} {}", "->".magenta(), callee_name);
        }
    }

    if callers.is_empty() && callees.is_empty() {
        println!("{}", "  No call relationships found.".yellow());
    }

    Ok(())
}

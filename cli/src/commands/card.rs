//! Card command - Show enriched symbol card with layer, delegation, side effects

use crate::client::ApiClient;
use anyhow::Result;
use colored::Colorize;

pub async fn run(
    client: &ApiClient,
    symbol: &str,
    source: Option<&str>,
    json_output: bool,
) -> Result<()> {
    if !json_output {
        eprint!("{}", "Loading symbol card...".cyan());
    }

    let cards = client.get_symbol_cards(symbol, source).await?;

    if json_output {
        println!("{}", serde_json::to_string_pretty(&cards)?);
        return Ok(());
    }

    if cards.is_empty() {
        println!(
            "{}",
            format!("\rNo symbol card found for '{}'", symbol).yellow()
        );
        return Ok(());
    }

    println!(
        "{}",
        format!(
            "\r{} Found {} symbol card(s) for '{}'",
            "OK".green(),
            cards.len(),
            symbol
        )
        .bold()
    );

    for card in &cards {
        println!();
        println!(
            "  {} {}",
            card.name.bold(),
            format!("({})", card.symbol_type).dimmed()
        );
        println!(
            "    {} {}:{}-{}",
            "Location:".dimmed(),
            card.file_path,
            card.line_start,
            card.line_end
        );
        println!("    {} {}", "Source:".dimmed(), card.source_name);

        if let Some(ref vis) = card.visibility {
            println!("    {} {}", "Visibility:".dimmed(), vis);
        }
        if let Some(ref layer) = card.layer {
            println!("    {} {}", "Layer:".dimmed(), layer.cyan());
        }
        if let Some(ref se) = card.side_effect_type {
            let colored = match se.as_str() {
                "create" => se.green().to_string(),
                "delete" => se.red().to_string(),
                "get" => se.blue().to_string(),
                "set" => se.yellow().to_string(),
                "unknown" => se.dimmed().to_string(),
                _ => se.to_string(),
            };
            println!("    {} {}", "Side Effect:".dimmed(), colored);
        }
        if let Some(ref res) = card.affected_resource {
            println!("    {} {}", "Resource:".dimmed(), res);
        }
        if let Some(ref thread) = card.thread_requirement {
            println!("    {} {}", "Thread:".dimmed(), thread.magenta());
        }
        if let Some(conf) = card.classification_confidence {
            let color = if conf >= 0.8 {
                "green"
            } else if conf >= 0.5 {
                "yellow"
            } else {
                "red"
            };
            let conf_str = format!("{:.0}%", conf * 100.0);
            let colored = match color {
                "green" => conf_str.green().to_string(),
                "yellow" => conf_str.yellow().to_string(),
                _ => conf_str.red().to_string(),
            };
            println!("    {} {}", "Confidence:".dimmed(), colored);
        }
        if let Some(ref sig) = card.signature {
            let preview = if sig.len() > 80 {
                format!("{}...", &sig[..77])
            } else {
                sig.clone()
            };
            println!("    {} {}", "Signature:".dimmed(), preview);
        }

        // Delegation targets
        if let Some(ref targets) = card.delegation_targets {
            if let Some(arr) = targets.as_array() {
                if !arr.is_empty() {
                    println!("    {}", "Delegation:".dimmed());
                    for t in arr.iter().take(5) {
                        let name = t["name"].as_str().unwrap_or("?");
                        let role = t["role"].as_str().unwrap_or("unknown");
                        let conf = t["confidence"].as_f64().unwrap_or(0.0);
                        let role_colored = match role {
                            "proxy" => role.cyan().to_string(),
                            "dispatch" => role.yellow().to_string(),
                            "mutation" => role.green().to_string(),
                            _ => role.dimmed().to_string(),
                        };
                        println!(
                            "      {} {} [{}] ({:.0}%)",
                            "->".blue(),
                            name,
                            role_colored,
                            conf * 100.0
                        );
                    }
                }
            }
        }

        if let Some(ref summary) = card.summary {
            println!("    {} {}", "Summary:".dimmed(), summary);
        }
    }

    Ok(())
}

use crate::client::ApiClient;
use colored::Colorize;
use serde_json::json;

pub async fn run(client: &ApiClient, json_output: bool) -> anyhow::Result<()> {
    let health = client.health().await?;

    if json_output {
        println!(
            "{}",
            serde_json::to_string_pretty(&json!({
                "status": health.status,
                "mode": health.mode,
                "services": {
                    "postgres": health.services.postgres,
                    "qdrant": health.services.qdrant,
                    "tei": health.services.tei,
                }
            }))?
        );
        return Ok(());
    }

    println!("{}", "=== Health Status ===".bold());
    println!();

    let mode = health.mode.as_deref().unwrap_or("unknown");
    println!("  {} {}", "Mode:".cyan(), mode);

    let pg_status = if health.services.postgres {
        "✓ OK".green()
    } else {
        "✗ DOWN".red()
    };
    println!("  {} {}", "PostgreSQL:".cyan(), pg_status);

    let cpu_mode = mode == "cpu";

    let qdrant_status = if health.services.qdrant {
        "✓ OK".green()
    } else if cpu_mode {
        "off (cpu mode)".dimmed()
    } else {
        "✗ DOWN".red()
    };
    println!("  {} {}", "Qdrant:".cyan(), qdrant_status);

    let tei_status = if health.services.tei {
        "✓ OK".green()
    } else if cpu_mode {
        "off (cpu mode)".dimmed()
    } else {
        "✗ DOWN".red()
    };
    println!("  {} {}", "TEI:".cyan(), tei_status);

    println!();

    let healthy = health.status == "healthy";
    if healthy && cpu_mode {
        println!("{}", "Status: Healthy (CPU mode)".green());
    } else if healthy {
        println!("{}", "Status: All services operational ✓".green());
    } else {
        println!("{}", "Status: Some services are unavailable".yellow());
    }

    Ok(())
}

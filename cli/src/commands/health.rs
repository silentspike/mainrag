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

    let pg_status = if health.services.postgres {
        "✓ OK".green()
    } else {
        "✗ DOWN".red()
    };
    println!("  {} {}", "PostgreSQL:".cyan(), pg_status);

    let qdrant_status = if health.services.qdrant {
        "✓ OK".green()
    } else {
        "✗ DOWN".red()
    };
    println!("  {} {}", "Qdrant:".cyan(), qdrant_status);

    let tei_status = if health.services.tei {
        "✓ OK".green()
    } else {
        "✗ DOWN".red()
    };
    println!("  {} {}", "TEI:".cyan(), tei_status);

    println!();

    if health.services.postgres && health.services.qdrant && health.services.tei {
        println!("{}", "Status: All services operational ✓".green());
    } else {
        println!("{}", "Status: Some services are unavailable".yellow());
    }

    Ok(())
}

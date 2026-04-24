use colored::Colorize;
use serde_json::json;
use std::fs;

pub async fn run(
    action: Option<super::super::ConfigAction>,
    json_output: bool,
) -> anyhow::Result<()> {
    use super::super::ConfigAction;

    match action {
        None => show_config(json_output).await,
        Some(ConfigAction::Show) => show_config(json_output).await,
        Some(ConfigAction::Set { key, value }) => set_config(&key, &value, json_output).await,
    }
}

async fn show_config(json_output: bool) -> anyhow::Result<()> {
    let config_dir = directories::ProjectDirs::from("", "", "mainrag")
        .ok_or_else(|| anyhow::anyhow!("Could not determine config directory"))?
        .config_dir()
        .to_path_buf();

    if json_output {
        let mut config = serde_json::json!({
            "config_dir": config_dir.display().to_string(),
            "api_url": "http://localhost:3001 (default)",
            "token_file": config_dir.join("token").display().to_string(),
        });

        if let Ok(token) = fs::read_to_string(config_dir.join("token")) {
            config["token_set"] = json!(!token.is_empty());
        }

        println!("{}", serde_json::to_string_pretty(&config)?);
        return Ok(());
    }

    println!("{}", "=== Configuration ===".bold());
    println!();
    println!("  {} {}", "Config Dir:".cyan(), config_dir.display());
    println!(
        "  {} {}",
        "API URL:".cyan(),
        "http://localhost:3001 (default)".dimmed()
    );
    println!(
        "  {} {}",
        "Token File:".cyan(),
        config_dir.join("token").display()
    );

    let token_file = config_dir.join("token");
    if token_file.exists() {
        println!("  {} {}", "Authentication:".cyan(), "Logged in ✓".green());
    } else {
        println!(
            "  {} {}",
            "Authentication:".cyan(),
            "Not logged in".yellow()
        );
    }

    println!();
    println!("{}", "Environment Variables:".bold());
    println!("  {}", "MAINRAG_API_URL - Override API URL".dimmed());

    Ok(())
}

async fn set_config(_key: &str, _value: &str, _json_output: bool) -> anyhow::Result<()> {
    // Config setting would be expanded in Phase 11
    println!("{}", "Config setting coming in Phase 11".yellow());
    Ok(())
}

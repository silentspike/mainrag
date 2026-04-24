use crate::client::{ApiClient, CreateSourceRequest};
use colored::Colorize;
use serde_json::json;
use std::path::Path;

pub async fn run(
    client: &ApiClient,
    path: &str,
    name: Option<&str>,
    json_output: bool,
) -> anyhow::Result<()> {
    // Auto-detect source type and generate name if not provided
    let (source_type, source_name) = detect_source(path, name)?;

    if !json_output {
        eprint!("{}", format!("Adding {} source: ", source_type).cyan());
        eprint!("{}", path.bold());
    }

    let req = CreateSourceRequest {
        name: Some(source_name.clone()),
        source_type: Some(source_type.clone()),
        path: path.to_string(),
        config: None,
    };

    let source = client.create_source(req).await?;

    if json_output {
        println!(
            "{}",
            serde_json::to_string_pretty(&json!({
                "id": source.id,
                "name": source.name,
                "type": source.source_type,
                "path": source.path,
            }))?
        );
        return Ok(());
    }

    println!(
        "{}",
        format!("\r✓ Source '{}' added (ID: {})", source.name, source.id).green()
    );
    println!("  Path: {}", source.path);
    println!("  Type: {}", source.source_type);
    println!();
    println!("{}", "Next step:".bright_black());
    println!(
        "  {}",
        format!("mainrag source sync {}", source.name).bold()
    );

    Ok(())
}

fn detect_source(path: &str, custom_name: Option<&str>) -> anyhow::Result<(String, String)> {
    let source_type = if path.ends_with(".git")
        || path.contains("github.com")
        || path.contains("gitlab.com")
        || path.starts_with("git@")
    {
        "git"
    } else if path.starts_with("http://") || path.starts_with("https://") {
        "web"
    } else if path.to_lowercase().ends_with(".pdf") {
        // PDF files get the specialized PDF plugin (MuPDF or pdf-extract)
        "pdf"
    } else {
        "fs"
    };

    let source_name = if let Some(n) = custom_name {
        n.to_string()
    } else {
        // Extract name from path
        if let Some(name) = Path::new(path).file_name().and_then(|n| n.to_str()) {
            name.to_string()
        } else {
            // Fallback: use hash of path
            format!("source-{}", hash_short(path))
        }
    };

    Ok((source_type.to_string(), source_name))
}

fn hash_short(s: &str) -> String {
    use std::collections::hash_map::DefaultHasher;
    use std::hash::{Hash, Hasher};

    let mut hasher = DefaultHasher::new();
    s.hash(&mut hasher);
    let hash = hasher.finish();
    format!("{:x}", hash).chars().take(6).collect()
}

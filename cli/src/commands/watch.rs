//! Watch command - monitor sources for changes

use anyhow::Result;
use notify_debouncer_mini::new_debouncer;
use notify::RecursiveMode;
use std::path::PathBuf;
use std::time::Duration;
use crate::client::ApiClient;
use colored::Colorize;

/// Watch sources for changes and trigger re-sync
pub async fn watch(
    client: &ApiClient,
    source_name: Option<&str>,
    daemon: bool,
) -> Result<()> {
    // Get sources to watch
    let response = client.list_sources().await?;
    let sources = &response.sources;

    let sources_to_watch: Vec<(i64, String, PathBuf)> = if let Some(name) = source_name {
        // Watch specific source
        let source = sources.iter()
            .find(|s| s.name == name)
            .ok_or_else(|| anyhow::anyhow!("Source not found: {}", name))?;

        vec![(
            source.id,
            source.name.clone(),
            PathBuf::from(&source.path),
        )]
    } else {
        // Watch all fs sources
        sources.iter()
            .filter(|s| s.source_type == "fs")
            .filter_map(|s| {
                let path = PathBuf::from(&s.path);
                if path.exists() {
                    Some((s.id, s.name.clone(), path))
                } else {
                    None
                }
            })
            .collect()
    };

    if sources_to_watch.is_empty() {
        println!("{}", "No sources to watch".yellow());
        return Ok(());
    }

    println!("{}", format!("Watching {} sources:", sources_to_watch.len()).cyan());
    for (_, name, path) in &sources_to_watch {
        println!("  {} {}", format!("[{}]", name).cyan(), path.display());
    }
    println!();

    if daemon {
        println!("{}", "Running in daemon mode (Ctrl+C to stop)...".dimmed());
    } else {
        println!("{}", "Watching for changes (Ctrl+C to stop)...".dimmed());
    }

    // Set up debounced watcher
    let (tx, rx) = std::sync::mpsc::channel();
    let mut debouncer = new_debouncer(Duration::from_millis(500), tx)?;

    // Watch all paths
    for (_, _, path) in &sources_to_watch {
        debouncer.watcher().watch(path, RecursiveMode::Recursive)?;
    }

    // Event loop
    let ignore_dirs = vec![".git", "node_modules", "target", "__pycache__", ".venv", "venv"];

    loop {
        match rx.recv() {
            Ok(Ok(events)) => {
                for event in events {
                    let path = &event.path;

                    // Check if should ignore
                    let should_ignore = path.components().any(|c| {
                        if let std::path::Component::Normal(name) = c {
                            ignore_dirs.contains(&name.to_str().unwrap_or(""))
                        } else {
                            false
                        }
                    });

                    if should_ignore {
                        continue;
                    }

                    // Find source for this path
                    if let Some((_id, name, _)) = sources_to_watch.iter().find(|(_, _, p)| path.starts_with(p)) {
                        println!("{} {} ({})", "Changed:".yellow(), path.display(), name);

                        // Trigger sync via API
                        match client.sync_source(name).await {
                            Ok(result) => {
                                println!("  {} Synced {} files, {} chunks",
                                    "✓".green(),
                                    result.stats.files_processed,
                                    result.stats.chunks_created
                                );
                            }
                            Err(e) => {
                                println!("  {} Sync failed: {}", "✗".red(), e);
                            }
                        }
                    }
                }
            }
            Ok(Err(e)) => {
                eprintln!("{} Watch error: {:?}", "Error:".red(), e);
            }
            Err(e) => {
                eprintln!("{} Channel error: {}", "Error:".red(), e);
                break;
            }
        }
    }

    #[allow(unreachable_code)]
    Ok(())
}

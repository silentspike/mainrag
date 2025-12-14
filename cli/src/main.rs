use clap::{Parser, Subcommand};
use colored::Colorize;

mod client;
mod commands;
mod mcp;

use client::ApiClient;

#[derive(Parser)]
#[command(name = "mainrag")]
#[command(about = "MAINRAG CLI - Search and manage your knowledge base", long_about = None)]
#[command(version = "0.1.0")]
#[command(author = "obtFusi")]
struct Cli {
    /// API server URL (default: http://localhost:3001)
    #[arg(long, global = true, env = "MAINRAG_API_URL", default_value = "http://localhost:3001")]
    api_url: String,

    /// Output as JSON (machine-readable)
    #[arg(long, global = true)]
    json: bool,

    /// Verbose output for debugging
    #[arg(short, long, global = true)]
    verbose: bool,

    #[command(subcommand)]
    command: Commands,
}

#[derive(Subcommand)]
enum Commands {
    /// Search the knowledge base
    Search {
        /// Search query
        query: String,

        /// Search mode: hybrid (default), keyword, or semantic
        #[arg(short, long, default_value = "hybrid")]
        mode: String,

        /// Maximum results to return
        #[arg(short, long, default_value = "10")]
        limit: u32,

        /// Filter by source name
        #[arg(short, long)]
        source: Option<String>,
    },

    /// Add a new source (filesystem, git, or web)
    Add {
        /// Path to add (local directory, git URL, or web URL)
        path: String,

        /// Custom source name (defaults to directory/repo name)
        #[arg(short, long)]
        name: Option<String>,
    },

    /// Manage sources
    Source {
        #[command(subcommand)]
        action: SourceAction,
    },

    /// Authentication
    Auth {
        #[command(subcommand)]
        action: AuthAction,
    },

    /// Show system statistics
    Stats,

    /// Show configuration
    Config {
        #[command(subcommand)]
        action: Option<ConfigAction>,
    },

    /// Check health status
    Health,

    /// Watch sources for changes and auto-sync
    Watch {
        /// Optional source name to watch (defaults to all filesystem sources)
        #[arg(short, long)]
        source: Option<String>,

        /// Run as daemon (background)
        #[arg(short, long)]
        daemon: bool,
    },

    /// Start MCP server for Claude Code
    Mcp,

    /// Show version
    Version,
}

#[derive(Subcommand)]
enum SourceAction {
    /// List all sources
    List,

    /// Sync a source (re-index files)
    Sync {
        /// Source name to sync
        name: String,
    },

    /// Delete a source
    Delete {
        /// Source name to delete
        name: String,

        /// Skip confirmation
        #[arg(long)]
        force: bool,
    },
}

#[derive(Subcommand)]
enum AuthAction {
    /// Login to MAINRAG
    Login,

    /// Logout (remove stored token)
    Logout,

    /// Show current user info
    Me,
}

#[derive(Subcommand)]
enum ConfigAction {
    /// Show all configuration
    Show,

    /// Set configuration value
    Set {
        key: String,
        value: String,
    },
}

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    let cli = Cli::parse();

    // Create API client
    let mut client = ApiClient::new(&cli.api_url)?;

    // Load existing token if available
    if let Some(token) = ApiClient::load_token_from_file() {
        client.set_token(token);
    }

    // Verbose mode
    if cli.verbose {
        eprintln!("{}", format!("Debug: API URL: {}", cli.api_url).dimmed());
        if client.token().is_some() {
            eprintln!("{}", "Debug: Using stored authentication token".dimmed());
        }
    }

    // Execute command
    match cli.command {
        Commands::Search {
            query,
            mode,
            limit,
            source,
        } => commands::search::run(&client, &query, &mode, limit, source.as_deref(), cli.json).await,

        Commands::Add { path, name } => {
            commands::add::run(&client, &path, name.as_deref(), cli.json).await
        }

        Commands::Source { action } => {
            commands::source::run(&client, action, cli.json).await
        }

        Commands::Auth { action } => {
            commands::auth::run(&mut client, action, cli.json).await
        }

        Commands::Stats => commands::stats::run(&client, cli.json).await,

        Commands::Config { action } => {
            commands::config::run(action, cli.json).await
        }

        Commands::Health => commands::health::run(&client, cli.json).await,

        Commands::Watch { source, daemon } => {
            commands::watch::watch(&client, source.as_deref(), daemon).await
        }

        Commands::Mcp => {
            let server = mcp::McpServer::new(client);
            server.run_stdio().await
        }

        Commands::Version => {
            println!("mainrag {}", env!("CARGO_PKG_VERSION"));
            Ok(())
        }
    }
}

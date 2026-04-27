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
    #[arg(
        long,
        global = true,
        env = "MAINRAG_API_URL",
        default_value = "http://localhost:3001"
    )]
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
        /// Search query (use "quotes" for exact phrase)
        query: String,

        /// Search mode: hybrid (default), keyword, or semantic
        #[arg(short, long, default_value = "hybrid")]
        mode: String,

        /// Maximum results to return
        #[arg(short, long, default_value = "10")]
        limit: u32,

        /// Skip first N results (for pagination)
        #[arg(short, long, default_value = "0")]
        offset: u32,

        /// Filter by source name
        #[arg(short = 'S', long)]
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

    /// Search for code symbols (functions, classes, structs)
    Symbols {
        /// Symbol name query
        query: String,

        /// Filter by type (function, class, struct, method, etc.)
        #[arg(short = 't', long)]
        symbol_type: Option<String>,

        /// Maximum results to return
        #[arg(short, long, default_value = "10")]
        limit: u32,
    },

    /// Query function call graph (callers/callees)
    CallGraph {
        /// Function name to analyze
        function: String,

        /// Direction: callers, callees, or both (default)
        #[arg(short, long, default_value = "both")]
        direction: String,

        /// Filter by source name (e.g. internal-java-corpus)
        #[arg(short, long)]
        source: Option<String>,

        /// N-hop depth for transitive call chain (default: 1 = direct only)
        #[arg(long, default_value = "1")]
        depth: i32,
    },

    /// Show enriched symbol card (layer, delegation, side effects, confidence)
    Card {
        /// Symbol name
        symbol: String,

        /// Filter by source name
        #[arg(short, long)]
        source: Option<String>,
    },

    /// Trace delegation chain through proxy -> dispatch -> mutation
    Explain {
        /// Symbol name to trace
        symbol: String,

        /// Filter by source name
        #[arg(short, long)]
        source: Option<String>,

        /// Maximum chain depth
        #[arg(short, long, default_value = "6")]
        depth: u32,
    },

    /// Browse API layers, resources, and side-effects
    Layers {
        /// Filter by layer (e.g. controller_api, proxy, internal)
        #[arg(short, long)]
        layer: Option<String>,

        /// Filter by resource (e.g. clip, track, device)
        #[arg(short, long)]
        resource: Option<String>,

        /// Filter by side-effect (e.g. create, delete, get)
        #[arg(short = 'e', long)]
        side_effect: Option<String>,

        /// Maximum results
        #[arg(long, default_value = "20")]
        limit: u32,
    },

    /// Show ownership/containment relations for a symbol
    Ownership {
        /// Symbol or class name
        symbol: String,
    },

    /// Explore a concept: query rewriting + path tracing + dead-end warnings
    Explore {
        /// Natural language question (e.g. "how do I delete an arranger clip")
        query: String,

        /// Filter by source name
        #[arg(short, long)]
        source: Option<String>,
    },

    /// Record or search known dead-end paths
    DeadEnd {
        #[command(subcommand)]
        action: DeadEndAction,
    },

    /// Admin maintenance commands (requires admin privileges)
    Admin {
        #[command(subcommand)]
        action: AdminAction,
    },

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
pub enum AuthAction {
    /// Login to MAINRAG
    Login {
        /// Username (email) - if provided with password, skips interactive prompt
        #[arg(short, long)]
        username: Option<String>,

        /// Password - if provided with username, skips interactive prompt
        #[arg(short, long)]
        password: Option<String>,
    },

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
    Set { key: String, value: String },
}

#[derive(Subcommand)]
enum DeadEndAction {
    /// Record a dead-end path
    Add {
        /// What was attempted (e.g. "delete arranger clip")
        #[arg(long)]
        concept: String,

        /// The path that failed (e.g. "clearTime")
        #[arg(long)]
        path: String,

        /// Why it fails
        #[arg(long)]
        reason: String,

        /// Involved symbol names (comma-separated)
        #[arg(long, value_delimiter = ',')]
        symbols: Vec<String>,

        /// Source name
        #[arg(short, long)]
        source: Option<String>,
    },

    /// Search for known dead-ends
    List {
        /// Search concept
        concept: String,
    },
}

#[derive(Subcommand)]
enum AdminAction {
    /// Backfill operations for data maintenance
    Backfill {
        #[command(subcommand)]
        action: BackfillAction,
    },
}

#[derive(Subcommand)]
enum BackfillAction {
    /// Find and embed orphaned chunks (chunks without embeddings)
    Orphaned,
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
            offset,
            source,
        } => {
            commands::search::run(
                &client,
                &query,
                &mode,
                limit,
                offset,
                source.as_deref(),
                cli.json,
            )
            .await
        }

        Commands::Add { path, name } => {
            commands::add::run(&client, &path, name.as_deref(), cli.json).await
        }

        Commands::Source { action } => commands::source::run(&client, action, cli.json).await,

        Commands::Auth { action } => commands::auth::run(&mut client, action, cli.json).await,

        Commands::Stats => commands::stats::run(&client, cli.json).await,

        Commands::Config { action } => commands::config::run(action, cli.json).await,

        Commands::Health => commands::health::run(&client, cli.json).await,

        Commands::Watch { source, daemon } => {
            commands::watch::watch(&client, source.as_deref(), daemon).await
        }

        Commands::Mcp => {
            let server = mcp::McpServer::new(client);
            server.run_stdio().await
        }

        Commands::Symbols {
            query,
            symbol_type,
            limit,
        } => commands::symbols::run(&client, &query, symbol_type.as_deref(), limit, cli.json).await,

        Commands::CallGraph {
            function,
            direction,
            source,
            depth,
        } => {
            commands::call_graph::run(
                &client,
                &function,
                &direction,
                source.as_deref(),
                depth,
                cli.json,
            )
            .await
        }

        Commands::Card { symbol, source } => {
            commands::card::run(&client, &symbol, source.as_deref(), cli.json).await
        }

        Commands::Explain {
            symbol,
            source,
            depth,
        } => {
            commands::explain::run(&client, &symbol, source.as_deref(), Some(depth), cli.json).await
        }

        Commands::Layers {
            layer,
            resource,
            side_effect,
            limit,
        } => {
            commands::layers::run(
                &client,
                layer.as_deref(),
                resource.as_deref(),
                side_effect.as_deref(),
                limit,
                cli.json,
            )
            .await
        }

        Commands::Ownership { symbol } => {
            commands::ownership::run(&client, &symbol, cli.json).await
        }

        Commands::Explore { query, source } => {
            commands::explore::run(&client, &query, source.as_deref(), cli.json).await
        }

        Commands::DeadEnd { action } => match action {
            DeadEndAction::Add {
                concept,
                path,
                reason,
                symbols,
                source,
            } => {
                commands::dead_end::run_add(
                    &client,
                    &concept,
                    &path,
                    &reason,
                    &symbols,
                    source.as_deref(),
                )
                .await
            }
            DeadEndAction::List { concept } => {
                commands::dead_end::run_list(&client, &concept, cli.json).await
            }
        },

        Commands::Admin { action } => match action {
            AdminAction::Backfill {
                action: backfill_action,
            } => match backfill_action {
                BackfillAction::Orphaned => {
                    commands::backfill::run_orphaned(&client, cli.json).await
                }
            },
        },

        Commands::Version => {
            println!("mainrag {}", env!("CARGO_PKG_VERSION"));
            Ok(())
        }
    }
}

use serde::Deserialize;
use std::env;

#[derive(Debug, Clone, Deserialize)]
pub struct Config {
    pub server: ServerConfig,
    pub database: DatabaseConfig,
    pub qdrant: QdrantConfig,
    pub tei: TeiConfig,
    pub jwt: JwtConfig,
    pub ocr: OcrConfig,
}

/// OCR Service configuration (GPU-only PaddleOCR microservice)
#[derive(Debug, Clone, Deserialize, Default)]
pub struct OcrConfig {
    /// OCR service URL (e.g., "http://localhost:8090")
    #[serde(default)]
    pub url: Option<String>,
    /// Enable OCR fallback for scanned PDFs (default: false)
    #[serde(default)]
    pub enabled: bool,
}

#[derive(Debug, Clone, Deserialize)]
pub struct ServerConfig {
    pub host: String,
    pub port: u16,
    pub search_default_limit: Option<u32>,
    pub search_max_limit: Option<u32>,
    pub cors_origins: Vec<String>,
    /// HMAC pepper for API-Key hashing (env: API_KEY_PEPPER)
    pub api_key_pepper: String,
    /// Previous pepper for zero-downtime rotation (env: API_KEY_PEPPER_PREVIOUS)
    pub api_key_pepper_previous: Option<String>,
    /// HTTP client connect timeout in seconds (env: HTTP_CONNECT_TIMEOUT_S, default: 10)
    pub http_connect_timeout_secs: u64,
    /// HTTP client request timeout in seconds (env: HTTP_REQUEST_TIMEOUT_S, default: 30)
    pub http_request_timeout_secs: u64,
    /// DB pool wait timeout in seconds (env: DB_POOL_WAIT_TIMEOUT_S, default: 5)
    pub db_pool_wait_timeout_secs: u64,
    /// K4: Qdrant backfill active — enables PG-RLS post-filter + oversampling
    /// (env: QDRANT_BACKFILL_ACTIVE, default: false)
    pub qdrant_backfill_active: bool,
    /// K4: Oversampling factor when backfill post-filter is active (default: 3)
    pub backfill_oversampling_factor: u64,
    /// Service user UUID for background jobs/migrations (env: SERVICE_USER_ID)
    /// Used when RLS context is needed but no HTTP request is present.
    pub service_user_id: Option<String>,
    /// Optional token for /metrics endpoint access (env: METRICS_TOKEN)
    pub metrics_token: Option<String>,
}

#[derive(Debug, Clone, Deserialize)]
pub struct DatabaseConfig {
    pub host: String,
    pub port: u16,
    pub name: String,
    pub user: String,
    pub password: String,
    pub max_connections: usize,
    /// TLS mode: "require", "prefer", "disable" (env: POSTGRES_TLS, default: "disable")
    pub tls_mode: String,
}

#[derive(Debug, Clone, Deserialize)]
pub struct QdrantConfig {
    pub url: String,
    pub api_key: Option<String>,
    pub chunk_collection: String,
    pub code_collection: String,
    /// Synonyms collection for query expansion (default: synonyms_v1)
    pub synonyms_collection: Option<String>,
}

#[derive(Debug, Clone, Deserialize)]
pub struct TeiConfig {
    pub url: String,
    pub reranker_url: Option<String>,
    /// Embedding model name (e.g., "bge-base-en-v1.5", "nomic-embed-text-v1.5")
    pub model: Option<String>,
    /// Embedding dimension (e.g., 768 for BGE, 1024 for BGE-m3)
    pub embedding_dim: Option<usize>,
}

#[derive(Debug, Clone, Deserialize)]
pub struct JwtConfig {
    pub secret: String,
    /// Previous JWT secret for zero-downtime rotation (Sprint 4.3)
    pub secret_previous: Option<String>,
    pub expiry_hours: u64,
}

impl Config {
    pub fn from_env() -> anyhow::Result<Self> {
        dotenvy::dotenv().ok();

        Ok(Config {
            server: ServerConfig {
                host: env::var("API_HOST").unwrap_or_else(|_| "0.0.0.0".to_string()),
                port: env::var("API_PORT")
                    .unwrap_or_else(|_| "3001".to_string())  // Default 3001 (Grafana uses 3000)
                    .parse()?,
                search_default_limit: env::var("SEARCH_DEFAULT_LIMIT")
                    .ok()
                    .and_then(|v| v.parse().ok()),
                search_max_limit: env::var("SEARCH_MAX_LIMIT")
                    .ok()
                    .and_then(|v| v.parse::<u32>().ok())
                    // H8: Hard ceiling to prevent excessive allocations
                    .map(|v| v.min(500)),
                cors_origins: env::var("CORS_ORIGINS")
                    .unwrap_or_default()  // Empty = no CORS (fail-closed)
                    .split(',')
                    .map(|s| s.trim().to_string())
                    .filter(|s| !s.is_empty())
                    .collect(),
                api_key_pepper: {
                    let pepper = env::var("API_KEY_PEPPER")
                        .unwrap_or_else(|_| "<REDACTED_PEPPER_PREV>".to_string());
                    const WEAK_PEPPERS: &[&str] = &[
                        "<REDACTED_PEPPER_PREV>",
                        "changeme",
                        "pepper",
                    ];
                    if pepper.len() < 16 {
                        panic!("API_KEY_PEPPER must be at least 16 characters (got {}). Aborting startup.", pepper.len());
                    }
                    if WEAK_PEPPERS.iter().any(|w| *w == pepper) {
                        if env::var("ALLOW_WEAK_JWT").is_ok() {
                            tracing::warn!("API_KEY_PEPPER matches a known default value — ALLOW_WEAK_JWT set, continuing in dev mode");
                        } else {
                            panic!("API_KEY_PEPPER matches a known default value. Set a unique pepper or ALLOW_WEAK_JWT=1 for development.");
                        }
                    }
                    pepper
                },
                api_key_pepper_previous: env::var("API_KEY_PEPPER_PREVIOUS")
                    .ok()
                    .filter(|s| !s.is_empty()),
                http_connect_timeout_secs: env::var("HTTP_CONNECT_TIMEOUT_S")
                    .unwrap_or_else(|_| "10".to_string())
                    .parse()?,
                http_request_timeout_secs: env::var("HTTP_REQUEST_TIMEOUT_S")
                    .unwrap_or_else(|_| "30".to_string())
                    .parse()?,
                db_pool_wait_timeout_secs: env::var("DB_POOL_WAIT_TIMEOUT_S")
                    .unwrap_or_else(|_| "5".to_string())
                    .parse()?,
                qdrant_backfill_active: env::var("QDRANT_BACKFILL_ACTIVE")
                    .map(|v| v == "true" || v == "1")
                    .unwrap_or(false),
                backfill_oversampling_factor: {
                    let factor: u64 = env::var("BACKFILL_OVERSAMPLING_FACTOR")
                        .unwrap_or_else(|_| "3".to_string())
                        .parse()?;
                    // S6: Bound oversampling to prevent OOM (1-100)
                    factor.clamp(1, 100)
                },
                service_user_id: env::var("SERVICE_USER_ID").ok().filter(|s| !s.is_empty()),
                metrics_token: env::var("METRICS_TOKEN").ok().filter(|s| !s.is_empty()),
            },
            database: DatabaseConfig {
                host: env::var("POSTGRES_HOST").unwrap_or_else(|_| "localhost".to_string()),
                port: env::var("POSTGRES_PORT")
                    .unwrap_or_else(|_| "5432".to_string())
                    .parse()?,
                name: env::var("POSTGRES_DB").unwrap_or_else(|_| "mainrag".to_string()),
                user: env::var("POSTGRES_USER").unwrap_or_else(|_| "mainrag".to_string()),
                password: env::var("POSTGRES_PASSWORD")
                    .expect("POSTGRES_PASSWORD must be set"),
                max_connections: env::var("DB_MAX_CONNECTIONS")
                    .unwrap_or_else(|_| "32".to_string())
                    .parse()?,
                tls_mode: env::var("POSTGRES_TLS")
                    .unwrap_or_else(|_| "disable".to_string()),
            },
            qdrant: QdrantConfig {
                url: env::var("QDRANT_REST_URL")
                    .or_else(|_| env::var("QDRANT_URL"))
                    .unwrap_or_else(|_| "http://localhost:6333".to_string()),
                api_key: {
                    let key = env::var("QDRANT_API_KEY").ok().filter(|s| !s.is_empty());
                    if key.is_none() {
                        tracing::warn!("QDRANT_API_KEY not set — Qdrant requests will be unauthenticated");
                    }
                    key
                },
                chunk_collection: env::var("QDRANT_CHUNK_COLLECTION")
                    .unwrap_or_else(|_| "mainrag_chunks".to_string()),
                code_collection: env::var("QDRANT_CODE_COLLECTION")
                    .unwrap_or_else(|_| "mainrag_code".to_string()),
                synonyms_collection: env::var("QDRANT_SYNONYMS_COLLECTION").ok(),
            },
            tei: TeiConfig {
                url: env::var("TEI_REST_URL")
                    .or_else(|_| env::var("TEI_URL"))
                    .unwrap_or_else(|_| "http://localhost:8080".to_string()),
                reranker_url: env::var("TEI_RERANKER_URL").ok(),
                model: env::var("TEI_MODEL").ok(),
                embedding_dim: env::var("EMBEDDING_DIMENSION")
                    .ok()
                    .and_then(|v| v.parse().ok()),
            },
            jwt: {
                let secret = env::var("JWT_SECRET")
                    .expect("JWT_SECRET must be set");

                // Sprint 2.6: JWT Secret Validation — fail startup on weak secrets
                if secret.len() < 32 {
                    panic!("JWT_SECRET must be at least 32 characters (got {}). Aborting startup.", secret.len());
                }
                // Blocklist known defaults
                const WEAK_SECRETS: &[&str] = &[
                    "<REDACTED_JWT_SECRET_PREV>",
                    "changeme",
                    "secret",
                    "jwt_secret",
                ];
                if WEAK_SECRETS.iter().any(|w| *w == secret) {
                    if env::var("ALLOW_WEAK_JWT").is_ok() {
                        tracing::warn!("JWT_SECRET matches a known default value — ALLOW_WEAK_JWT set, continuing in dev mode");
                    } else {
                        panic!("JWT_SECRET matches a known default value. Set a unique secret or ALLOW_WEAK_JWT=1 for development.");
                    }
                }

                // Sprint 4.3: Dual-Key rotation — accept tokens signed with previous secret
                let secret_previous = env::var("JWT_SECRET_PREVIOUS").ok().filter(|s| !s.is_empty());
                if secret_previous.is_some() {
                    tracing::info!("JWT_SECRET_PREVIOUS configured — dual-key rotation active");
                }

                JwtConfig {
                    secret,
                    secret_previous,
                    expiry_hours: env::var("JWT_ACCESS_EXPIRY_HOURS")
                        .unwrap_or_else(|_| "24".to_string())
                        .parse()?,
                }
            },
            ocr: OcrConfig {
                url: env::var("OCR_SERVICE_URL").ok(),
                enabled: env::var("OCR_ENABLED")
                    .map(|v| v == "true" || v == "1")
                    .unwrap_or(false),
            },
        })
    }

    pub fn database_url(&self) -> String {
        format!(
            "host={} port={} user={} password={} dbname={}",
            self.database.host,
            self.database.port,
            self.database.user,
            self.database.password,
            self.database.name
        )
    }

    /// S8: Redacted database URL safe for logging (password replaced with ***)
    pub fn database_url_redacted(&self) -> String {
        format!(
            "host={} port={} user={} password=*** dbname={}",
            self.database.host,
            self.database.port,
            self.database.user,
            self.database.name
        )
    }
}

# MAINRAG Architecture Overview

## System Overview

MAINRAG ist ein Enterprise-Grade RAG (Retrieval-Augmented Generation) System basierend auf PostgreSQL 18.1 + pgvector 0.8.1.

```
┌─────────────────────────────────────────────────────────────────────┐
│                        MAINRAG Architecture                         │
├─────────────────────────────────────────────────────────────────────┤
│                                                                     │
│  ┌──────────────┐    ┌──────────────┐    ┌──────────────────────┐  │
│  │  Web Frontend │    │ Claude Code  │    │  External Agents     │  │
│  │  (Next.js)    │    │   Agents     │    │  (API Keys)          │  │
│  └──────┬───────┘    └──────┬───────┘    └──────────┬───────────┘  │
│         │                   │                       │               │
│         └───────────────────┼───────────────────────┘               │
│                             │                                       │
│                    ┌────────▼────────┐                             │
│                    │   Auth Layer    │                              │
│                    │  (JWT/Sessions) │                              │
│                    └────────┬────────┘                             │
│                             │                                       │
│         ┌───────────────────┼───────────────────┐                  │
│         │                   │                   │                   │
│  ┌──────▼──────┐    ┌──────▼──────┐    ┌──────▼──────┐            │
│  │  Search API  │    │  Import API │    │  Admin API  │            │
│  │  (Hybrid)    │    │  (Sources)  │    │  (Users)    │            │
│  └──────┬──────┘    └──────┬──────┘    └──────┬──────┘            │
│         │                   │                   │                   │
│         └───────────────────┼───────────────────┘                  │
│                             │                                       │
│                    ┌────────▼────────┐                             │
│                    │  Rust Backend   │                              │
│                    │ (tokio-postgres)│                              │
│                    └────────┬────────┘                             │
│                             │                                       │
│                    ┌────────▼────────┐                             │
│                    │   PostgreSQL    │                              │
│                    │  18.1 + pgvector│                              │
│                    └─────────────────┘                             │
│                                                                     │
└─────────────────────────────────────────────────────────────────────┘
```

## Database Schema (44 Tables)

### Core RAG Tables
| Table | Purpose |
|-------|---------|
| `sources` | Registered data sources (git, fs, web, conversation) |
| `files` | Indexed files with compressed content |
| `chunks` | Content chunks for RAG retrieval |
| `chunk_embeddings` | SBERT vectors (384-dim) for semantic search |
| `symbols` | Tree-sitter extracted code symbols |
| `call_graph` | Function call relationships |

### Advanced RAG Features
| Table | Purpose | Reference |
|-------|---------|-----------|
| `hypothetical_questions` | HyPE pre-computed questions | [arxiv](https://arxiv.org/pdf/2409.04701) |
| `entities` | Named Entity Recognition results | GraphRAG |
| `entity_relations` | Knowledge Graph edges | GraphRAG |
| `multi_embeddings` | CodeBERT, GraphCodeBERT vectors | Multi-model |
| `colbert_embeddings` | Late interaction token vectors | ColBERT |
| `late_chunk_tokens` | JinaAI late chunking | Late Chunking |
| `ast_nodes` | Abstract Syntax Tree storage | cAST |
| `reranker_cache` | Cross-encoder score cache | Reranking |
| `query_analytics` | Query patterns for optimization | Analytics |

### Security & Auth Tables
| Table | Purpose |
|-------|---------|
| `users` | User accounts (bcrypt passwords, MFA) |
| `roles` | RBAC roles (admin, engineer, user, agent) |
| `user_roles` | User-role assignments |
| `permissions` | Fine-grained permissions |
| `role_permissions` | Role-permission mapping |
| `source_permissions` | ReBAC per-source access |
| `api_keys` | API key management |
| `sessions` | Web session management |
| `audit_log` | Partitioned audit trail (SIEM-ready) |
| `rate_limits` | Per-user rate limiting |
| `sensitive_patterns` | DLP regex patterns |
| `sensitive_findings` | Detected PII/secrets |

### Web Frontend Tables
| Table | Purpose |
|-------|---------|
| `conversations` | Chat threads |
| `messages` | Chat messages with RAG context |
| `message_attachments` | Uploaded files |
| `search_history` | User search history |
| `collections` | User-curated content collections |
| `collection_items` | Items in collections |
| `user_preferences` | UI/behavior settings |
| `bookmarks` | Quick access bookmarks |
| `shared_links` | Public sharing tokens |
| `notifications` | In-app notifications |
| `agent_sessions` | Claude Code agent tracking |
| `feature_flags` | Feature rollout control |

## State-of-the-Art RAG Features

### 1. HyPE (Hypothetical Prompt Embeddings)
- Pre-generates 2-5 questions per chunk at index time
- Query matched against questions, not content
- **+42% precision** vs HyDE at query time
- Zero LLM calls at search time

### 2. GraphRAG / Knowledge Graph
- Entity extraction (NER) stored in `entities`
- Relations stored in `entity_relations`
- Multi-hop graph traversal via `find_related_entities()`
- **+29% F1** on multi-hop QA (HotpotQA)

### 3. Hybrid Search (RRF Fusion)
- Combines semantic (HNSW) + keyword (GIN/FTS)
- Reciprocal Rank Fusion with k=60
- `search_chunks_hybrid()` function
- Best of both worlds

### 4. Multi-Model Embeddings
- SBERT (all-MiniLM-L6-v2) for general text
- CodeBERT/GraphCodeBERT for code
- Voyage-code-2 for production code (**+17% accuracy**)
- Query routing selects optimal model

### 5. ColBERT Late Interaction
- Token-level embeddings (128-dim)
- MaxSim scoring at rerank time
- **+10% accuracy** vs single-vector
- Used for top-K reranking

### 6. Late Chunking (JinaAI)
- Full document embedding first
- Then chunk boundaries applied
- Each token retains document context
- Better than naive chunking

### 7. Contextual Retrieval (Anthropic)
- LLM-generated context prefix per chunk
- "This chunk is from Kubernetes docs about..."
- **-49% failed retrievals**
- Stored in `chunks.context_prefix`

## Security Architecture

### Authentication
- **bcrypt/argon2** password hashing
- **MFA** via TOTP (mfa_secret, backup_codes)
- **JWT** for API authentication
- **Session tokens** for web frontend

### Authorization
- **RBAC**: roles (admin, engineer, user, agent)
- **ABAC**: attribute-based permissions
- **ReBAC**: relationship-based source access
- **RLS**: Row-Level Security policies on PostgreSQL

### Audit & Compliance
- **Partitioned audit_log** (by half-year)
- All actions logged with user, IP, duration
- **DLP patterns** for PII/secret detection
- **Rate limiting** per user/API key

### Security Best Practices
- Zero-trust: no implicit trust
- Least privilege: minimal permissions
- Encryption at rest (AES-256 capable)
- Encryption in transit (TLS)

## Performance Optimizations

### PostgreSQL Tuning
```ini
shared_buffers = 4GB         # 25% of RAM
effective_cache_size = 12GB  # 75% of RAM
work_mem = 32MB              # Per-operation
maintenance_work_mem = 1GB   # For HNSW builds
random_page_cost = 1.1       # NVMe optimized
effective_io_concurrency = 200
max_parallel_workers = 8
```

### HNSW Index Settings
- `m = 16`: Max connections per node
- `ef_construction = 100`: Build quality
- `ef_search = 100`: Query quality (runtime adjustable)

### Indexes
- **HNSW** on all vector columns (chunk_embeddings, hypothetical_questions, etc.)
- **GIN** on FTS tsvector columns
- **GIN + pg_trgm** for fuzzy text search
- **B-tree** on foreign keys, timestamps

## Credentials

**Location**: `/etc/mainrag/mainrag.env` (systemd `EnvironmentFile=`,
  mode `600`, owner `mainrag:mainrag`). A redacted reference template
  lives in `mainrag.env.example` at the repository root.

| User        | Purpose              | Secret source                       |
|-------------|----------------------|-------------------------------------|
| `mainrag`   | Application DB user  | `$POSTGRES_PASSWORD` in env file    |
| `admin`     | Initial admin login  | `<REDACTED>` — must be rotated on first login; dual-key graceful rotation via `API_KEY_PEPPER_PREVIOUS` |

Bcrypt password column in `users`: Argon2id (`$argon2id$v=19$...`) — the
specific parameters are `m=65536,t=3,p=4`. Real hashes are never
committed; use `argon2 <password>` locally to generate one.

## Files

| File | Purpose |
|------|---------|
| `schema.sql` | Core RAG tables (742 lines) |
| `schema_security.sql` | Security/auth tables (601 lines) |
| `schema_web.sql` | Web frontend tables (487 lines) |
| `mainrag.env.example` | Env-var template with redacted placeholders |
| `ARCHITECTURE.md` | This file |

## References

- [RAG Enterprise Guide 2025](https://datanucleus.dev/rag-and-agentic-ai/what-is-rag-enterprise-guide-2025)
- [GraphRAG Paper](https://arxiv.org/abs/2501.00309)
- [HyPE Paper](https://arxiv.org/pdf/2409.04701)
- [Contextual Retrieval (Anthropic)](https://www.anthropic.com/news/contextual-retrieval)
- [RAG Security Best Practices](https://www.daxa.ai/blogs/secure-retrieval-augmented-generation-rag-in-enterprise-environments)
- [Vercel AI SDK RAG Guide](https://sdk.vercel.ai/docs/guides/rag-chatbot)
- [NirDiamant/RAG_Techniques](https://github.com/NirDiamant/RAG_Techniques)

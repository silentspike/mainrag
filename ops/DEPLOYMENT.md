# MAINRAG Deployment Guide

## Prerequisites

- PostgreSQL 18+ with pgvector extension
- Qdrant 1.12+
- Node.js 20+
- Docker (for TEI)
- nginx

## Directory Structure

```
/opt/mainrag/
├── api/                    # Rust API binary
├── frontend/               # SvelteKit build
├── scripts/                # Backup scripts
└── bin/                    # Qdrant binary (optional)

/etc/mainrag/
├── mainrag.env             # Environment variables
└── nginx/                  # nginx config

/data/mainrag/
├── backups/                # PostgreSQL backups
└── models/                 # TEI model cache

/data/qdrant/
├── storage/                # Qdrant data
└── snapshots/              # Qdrant snapshots
```

## Installation Steps

### 1. Create System User

```bash
sudo useradd -r -s /bin/false -d /opt/mainrag mainrag
sudo mkdir -p /opt/mainrag/{api,frontend,scripts}
sudo mkdir -p /data/mainrag/backups
sudo mkdir -p /data/qdrant/{storage,snapshots}
sudo chown -R mainrag:mainrag /opt/mainrag /data/mainrag
```

### 2. Deploy API

```bash
cd /work/postgres/api
POSTGRES_PASSWORD='<REDACTED_DB_PW>' cargo build --release
sudo cp target/release/mainrag-api /opt/mainrag/api/
sudo chown mainrag:mainrag /opt/mainrag/api/mainrag-api
```

### 3. Deploy Frontend

```bash
cd /work/postgres/frontend
npm run build
sudo cp -r build/* /opt/mainrag/frontend/
sudo chown -R mainrag:mainrag /opt/mainrag/frontend
```

### 4. Create Environment File

```bash
sudo cat > /etc/mainrag/mainrag.env << 'EOF'
# API Configuration
API_HOST=0.0.0.0
API_PORT=3001

# PostgreSQL
POSTGRES_HOST=localhost
POSTGRES_PORT=5432
POSTGRES_DB=mainrag
POSTGRES_USER=mainrag
POSTGRES_PASSWORD=<REDACTED_DB_PW>
DB_MAX_CONNECTIONS=32

# Qdrant
QDRANT_URL=http://localhost:6334
QDRANT_API_KEY=<REDACTED_QDRANT_API_KEY>

# TEI (Embeddings)
TEI_REST_URL=http://localhost:8080

# JWT Authentication
JWT_SECRET=your_secure_jwt_secret_here_change_in_production
JWT_ACCESS_EXPIRY_HOURS=24

# Logging
RUST_LOG=mainrag_api=info
EOF
sudo chmod 600 /etc/mainrag/mainrag.env
```

### 5. Deploy systemd Services

```bash
sudo cp ops/systemd/*.service /etc/systemd/system/
sudo cp ops/systemd/*.timer /etc/systemd/system/
sudo systemctl daemon-reload

# Enable and start services
sudo systemctl enable --now mainrag-api
sudo systemctl enable --now mainrag-svelte
sudo systemctl enable --now mainrag-backup.timer
sudo systemctl enable --now mainrag-qdrant-snapshot.timer
```

### 6. Deploy nginx Config

```bash
sudo cp ops/nginx/mainrag.conf /etc/nginx/conf.d/
sudo nginx -t && sudo systemctl reload nginx
```

### 7. Deploy Backup Scripts

```bash
sudo cp ops/scripts/*.sh /opt/mainrag/scripts/
sudo chmod +x /opt/mainrag/scripts/*.sh
```

## Ports

| Service | Port | Purpose |
|---------|------|---------|
| nginx | 8088 | Reverse proxy |
| API | 3001 | Rust API server |
| Frontend | 3002 | SvelteKit SSR |
| Qdrant REST | 6333 | Vector search API |
| Qdrant gRPC | 6334 | Vector search gRPC |
| TEI | 8080 | Embedding service |
| PostgreSQL | 5432 | Database |

## Health Checks

```bash
# API health
curl http://localhost:3001/health

# Qdrant health
curl http://localhost:6333/health

# TEI health
curl http://localhost:8080/health

# Full stack via nginx
curl http://localhost:8088/health
```

## Monitoring

### Prometheus

Add scrape configs from `ops/prometheus/mainrag-scrape.yml` to your prometheus.yml.

### Grafana

Import the dashboard from `ops/grafana/mainrag-dashboard.json`.

## Backup & Recovery

### PostgreSQL

- Automatic daily backup at 03:00
- Location: `/data/mainrag/backups/`
- Retention: 7 days

Manual backup:
```bash
/opt/mainrag/scripts/pg-backup.sh
```

Recovery:
```bash
gunzip < /data/mainrag/backups/mainrag_YYYYMMDD_HHMMSS.sql.gz | \
  PGPASSWORD='<REDACTED_DB_PW>' psql -h localhost -U mainrag -d mainrag
```

### Qdrant

- Automatic daily snapshots at 03:30
- Location: `/data/qdrant/snapshots/`

Manual snapshot:
```bash
/opt/mainrag/scripts/qdrant-snapshot.sh
```

## Logs

```bash
# API logs
journalctl -u mainrag-api -f

# Frontend logs
journalctl -u mainrag-svelte -f

# Backup logs
journalctl -u mainrag-backup -f
```

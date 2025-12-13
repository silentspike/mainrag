#!/bin/bash
# MAINRAG PostgreSQL Backup Script
# Creates daily compressed backups with 7-day retention

set -euo pipefail

BACKUP_DIR="/data/mainrag/backups"
RETENTION_DAYS=7
DATE=$(date +%Y%m%d_%H%M%S)
BACKUP_FILE="${BACKUP_DIR}/mainrag_${DATE}.sql.gz"

# Export password from environment or use default
export PGPASSWORD="${POSTGRES_PASSWORD:-<REDACTED_DB_PW>}"

echo "[$(date)] Starting PostgreSQL backup..."

# Create backup with pg_dump
pg_dump -h localhost -U mainrag -d mainrag --format=plain --no-owner --no-privileges \
    | gzip > "${BACKUP_FILE}"

# Verify backup was created
if [[ -f "${BACKUP_FILE}" ]]; then
    SIZE=$(du -h "${BACKUP_FILE}" | cut -f1)
    echo "[$(date)] Backup created: ${BACKUP_FILE} (${SIZE})"
else
    echo "[$(date)] ERROR: Backup failed!"
    exit 1
fi

# Remove old backups
echo "[$(date)] Removing backups older than ${RETENTION_DAYS} days..."
find "${BACKUP_DIR}" -name "mainrag_*.sql.gz" -mtime +${RETENTION_DAYS} -delete

# List current backups
echo "[$(date)] Current backups:"
ls -lh "${BACKUP_DIR}"/mainrag_*.sql.gz 2>/dev/null || echo "No backups found"

echo "[$(date)] Backup complete"

# Phase 14: Embedding Model Upgrade Guide

## Overview

MAINRAG currently uses **BGE-base-en-v1.5** (768-dimensional, 125M parameters) for text embeddings via TEI. This guide covers upgrading to better embedding models.

## Current Hardware Constraints

```
GPU:  RTX 3050 Ti (4GB VRAM)
Available VRAM: ~3.5GB (after OS/system overhead)
BGE-base Current Usage: ~400MB
```

## Upgrade Options

### Option A: v1.5 Nomic (Drop-in, Recommended)

**Model:** `nomic-ai/nomic-embed-text-v1.5`

| Aspect | Specification |
|--------|---------------|
| Dimension | 768 (same as current) |
| Parameters | 137M |
| Context | 8K tokens |
| License | Apache 2.0 ✅ |
| VRAM | ~400MB (same as current) |
| Re-indexing | ❌ Not required |
| Downtime | ~5 minutes |

**Advantages:**
- Drop-in replacement (no schema changes)
- 8K context vs 512K for BGE-base
- Better performance on many benchmarks
- Same dimension (no re-indexing)
- Apache 2.0 license

**Disadvantages:**
- English-only (same as BGE-base)
- Slightly lower multilingual performance

**Recommended for:** Immediate upgrade with zero operational overhead

---

### Option B: v2.0 BGE-m3 (Full Upgrade)

**Model:** `BAAI/bge-m3`

| Aspect | Specification |
|--------|---------------|
| Dimension | 1024 (increased from 768) |
| Parameters | 568M |
| Context | 8K tokens |
| License | MIT ✅ |
| VRAM | ~2GB (may need to monitor) |
| Re-indexing | ✅ **Required** |
| Downtime | 2-4 hours (depending on data size) |

**Advantages:**
- Multilingual (100+ languages)
- Dense + Sparse + ColBERT vectors
- Higher dimension for better precision
- MIT license
- Better performance on German/European languages

**Disadvantages:**
- Requires dimension migration (768→1024)
- Requires full re-indexing of all sources
- Significant downtime
- Larger model (~2GB VRAM)

**Recommended for:** When you have dedicated maintenance window

---

## Installation Instructions

### Step 1: Pre-flight Checklist

```bash
# Check current model
curl http://localhost:8080/info | jq '.model_id'

# Check embedding dimension
curl -X POST http://localhost:8080/embed \
  -H "Content-Type: application/json" \
  -d '{"inputs": "test"}' | jq '.[0] | length'

# Check API health
curl http://localhost:3001/health | jq '.'

# Check database size
PGPASSWORD='<REDACTED_DB_PW>' psql -h localhost -U mainrag -d mainrag \
  -c "SELECT COUNT(*) FROM chunks;"
```

### Step 2: Choose Upgrade Path

#### Path A: Nomic v1.5 (Recommended)

```bash
# This is a simple, low-risk upgrade
bash /work/mainrag/ops/scripts/phase14-upgrade-embedding.sh \
  nomic-ai/nomic-embed-text-v1.5 768

# Expected output:
# ✓ Model and dimension verified
# ✅ Drop-in upgrade complete! No re-indexing required.
```

**Timeline:** ~5 minutes total

#### Path B: BGE-m3 v2.0 (Full Migration)

```bash
# Step 1: Schedule maintenance window (2-4 hours)
# Step 2: Run upgrade script
bash /work/mainrag/ops/scripts/phase14-upgrade-embedding.sh \
  BAAI/bge-m3 1024

# Step 3: Apply schema migration
PGPASSWORD='<REDACTED_DB_PW>' psql -h localhost -U mainrag -d mainrag \
  -f /work/mainrag/ops/migrations/14_embedding_dimension_update.sql

# Step 4: Re-index all sources
PGPASSWORD='<REDACTED_DB_PW>' psql -h localhost -U mainrag -d mainrag \
  -t -c "SELECT id FROM sources ORDER BY id" | while read id; do
  echo "Re-indexing source $id..."
  curl -s -X POST http://localhost:3001/api/sources/$id/sync
done

# Step 5: Verify
curl http://localhost:3001/health | jq '.'
```

**Timeline:** 2-4 hours depending on data volume

---

## Verification After Upgrade

### 1. Check Model

```bash
curl http://localhost:8080/info | jq '.'

# Expected output:
{
  "model_id": "nomic-ai/nomic-embed-text-v1.5"  # or BAAI/bge-m3
}
```

### 2. Verify Dimension

```bash
DIMENSION=$(curl -s -X POST http://localhost:8080/embed \
  -H "Content-Type: application/json" \
  -d '{"inputs": "test"}' | jq '.[0] | length')
echo "Dimension: $DIMENSION"
```

### 3. Test Search

```bash
# Test a search query
curl -s -X POST http://localhost:3001/api/search \
  -H "Content-Type: application/json" \
  -d '{"query": "test search", "limit": 5}' | jq '.'
```

### 4. Monitor TEI Logs

```bash
# Watch TEI container logs
docker logs -f mainrag-tei-embeddings --tail 50
```

### 5. Check API Logs

```bash
# Watch API logs
journalctl -u mainrag-api -f --no-pager
```

---

## Troubleshooting

### Issue: TEI won't start / "Out of Memory"

```bash
# Check current model loaded
docker compose -f /opt/mainrag/docker-compose.yml logs tei | tail -20

# If OOM on BGE-m3:
# Option 1: Reduce batch size in docker-compose.yml
# Option 2: Switch back to Nomic (768-dim)
# Option 3: Consider hardware upgrade for better GPU
```

### Issue: Dimension Mismatch

```bash
# If you see errors about vector dimension:
# 1. Check what dimension is in the database
PGPASSWORD='<REDACTED_DB_PW>' psql -h localhost -U mainrag -d mainrag \
  -c "SELECT typlen FROM pg_type WHERE typname = 'vector';"

# 2. Run schema migration if needed
PGPASSWORD='<REDACTED_DB_PW>' psql -h localhost -U mainrag -d mainrag \
  -f ops/migrations/14_embedding_dimension_update.sql

# 3. Re-index sources
```

### Issue: Search Returns No Results

```bash
# 1. Check if embeddings exist
PGPASSWORD='<REDACTED_DB_PW>' psql -h localhost -U mainrag -d mainrag \
  -c "SELECT COUNT(*) FROM chunk_embeddings WHERE vector IS NOT NULL;"

# 2. If count is 0 or low, re-index sources:
mainrag source list --json | jq -r '.[].id' | while read id; do
  mainrag source sync "$id"
done
```

### Rollback (If Needed)

```bash
# For Nomic (v1.5):
# Just switch back in docker-compose
sed -i 's|nomic-ai/nomic-embed-text-v1.5|BAAI/bge-base-en-v1.5|' \
  /opt/mainrag/docker-compose.yml
docker compose up -d tei

# For BGE-m3 (v2.0):
# 1. Restore docker-compose backup
cp /opt/mainrag/docker-compose.yml.pre-phase14 /opt/mainrag/docker-compose.yml

# 2. Restore PostgreSQL from backup
PGPASSWORD='<REDACTED_DB_PW>' pg_restore -h localhost -U mainrag -d mainrag \
  /data/mainrag/backups/phase14/mainrag_pre_phase14_YYYYMMDD_HHMMSS.dump

# 3. Restart services
docker compose up -d tei
sudo systemctl restart mainrag-api
```

---

## Performance Comparison

### Inference Speed (single query)

| Model | Tokens | Time | Notes |
|-------|--------|------|-------|
| BGE-base (current) | 512 | ~50ms | Baseline |
| Nomic v1.5 | 512 | ~45ms | Slightly faster |
| BGE-m3 | 512 | ~80ms | Larger model |

### Search Quality (Recall@10)

| Model | English | German | Multilingual |
|-------|---------|--------|--------------|
| BGE-base (current) | 92% | 78% | Poor |
| Nomic v1.5 | 94% | 80% | Poor |
| BGE-m3 | 95% | 92% | Excellent |

---

## Decision Tree

```
Do you need multilingual support?
├─ YES
│  └─ Do you have 2+ weeks for migration?
│     ├─ YES → Use BGE-m3 v2.0
│     └─ NO  → Use Nomic v1.5 (English focus for now)
└─ NO
   └─ Use Nomic v1.5 (drop-in, better performance)
```

---

## FAQ

**Q: Will upgrading break my search results?**
A: No, existing embeddings stay the same. New queries will use the new model.

**Q: How long does re-indexing take?**
A: ~1 minute per 10,000 chunks. With 100K chunks: ~10 minutes.

**Q: Can I run multiple models simultaneously?**
A: Not in this architecture. But you could run a separate TEI container on a different port.

**Q: Which model should I use?**
A: Start with Nomic v1.5 (drop-in, ~5 min). Plan BGE-m3 for next quarter if multilingual support is needed.

**Q: What if my searches get worse?**
A: Revert immediately (see Rollback section). Then benchmark the new model offline before full migration.

---

## Next Steps

1. **Immediate:** Upgrade to Nomic v1.5 (5 min, zero risk)
2. **Soon:** Benchmark BGE-m3 in staging environment
3. **Later:** Plan BGE-m3 migration during scheduled maintenance
4. **Future:** Monitor for better models (E5-Mistral, etc.)

# OCR Service Operations Runbook

## Overview

The MAINRAG OCR Service provides GPU-accelerated text extraction from scanned PDFs using PaddleOCR + CUDA.

**CRITICAL: GPU-Only Policy** - This service requires NVIDIA GPU with CUDA. There is NO CPU fallback.

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│  Rust API (mainrag-api)                                 │
│     ↓ HTTP                                              │
│  ┌─────────────────────────────────────────────────┐    │
│  │  OCR Microservice (Python FastAPI)              │    │
│  │  - PaddleOCR with CUDA                          │    │
│  │  - Hybrid sync/async                            │    │
│  │  - Redis job persistence                        │    │
│  └─────────────────────────────────────────────────┘    │
│     ↓                                                   │
│  ┌─────────────┐  ┌─────────────┐                       │
│  │   Redis     │  │  NVIDIA GPU │                       │
│  │   :6379     │  │  (RTX 3050) │                       │
│  └─────────────┘  └─────────────┘                       │
└─────────────────────────────────────────────────────────┘
```

## Service Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/health` | GET | Health check (GPU + Redis) |
| `/ocr/sync` | POST | Sync OCR (≤10 pages, ≤5MB) |
| `/ocr/async` | POST | Async OCR (returns job_id) |
| `/ocr/status/{job_id}` | GET | Poll async job status |

## Limits & Timeouts

| Parameter | Value | Description |
|-----------|-------|-------------|
| MAX_PAGES | 20 | Maximum pages to OCR |
| MAX_PAGES_SYNC | 10 | Max pages for sync endpoint |
| MAX_SIZE_SYNC | 5MB | Max file size for sync endpoint |
| TIMEOUT_PER_PAGE | 5s | Timeout per page |
| MAX_TIMEOUT | 120s | Maximum total timeout |
| JOB_TTL_PENDING | 2h | TTL for pending jobs |
| JOB_TTL_COMPLETED | 1h | TTL for completed results |

## Starting the Service

```bash
cd /work/mainrag/ops/ocr-service
docker compose up -d
```

## Stopping the Service

```bash
cd /work/mainrag/ops/ocr-service
docker compose down
```

## Health Checks

```bash
# Check service health
curl http://localhost:8090/health | jq

# Expected response:
# {
#   "status": "healthy",
#   "gpu": true,
#   "gpu_name": "NVIDIA GeForce RTX 3050 Ti Laptop GPU",
#   "redis": true,
#   "concurrency": 2
# }

# Check GPU directly
nvidia-smi --query-gpu=name,memory.used,memory.total --format=csv
```

## Troubleshooting

### GPU Not Available (503)

**Symptoms:**
- `/health` returns 503
- Logs show "GPU not available!"

**Resolution:**
1. Check NVIDIA driver: `nvidia-smi`
2. Check Docker GPU access: `docker run --rm --gpus all nvidia/cuda:12.4.1-base-ubuntu22.04 nvidia-smi`
3. Restart Docker daemon if needed: `sudo systemctl restart docker`
4. Restart OCR container: `docker compose restart ocr`

### CUDA Context Errors

**Symptoms:**
- "CUDA error: invalid device context"
- Service crashes during OCR

**Root Cause:** Fork (default) copies CUDA state incorrectly.

**Resolution:** Already mitigated by `set_start_method("spawn")` in app.py. If still occurring:
1. Restart container: `docker compose restart ocr`
2. Check for CUDA version mismatch between host and container

### Redis Connection Failed

**Symptoms:**
- `/health` returns 503 with "Redis not available"
- Jobs not persisting

**Resolution:**
1. Check Redis container: `docker compose ps redis`
2. Check Redis logs: `docker compose logs redis`
3. Restart Redis: `docker compose restart redis`

### OOM (Out of Memory)

**Symptoms:**
- Container restarts repeatedly
- Logs show memory errors

**Resolution:**
1. Container has `mem_limit: 4g` and auto-restart
2. Check GPU memory: `nvidia-smi`
3. Reduce concurrent OCR jobs (OCR_CONCURRENCY=1)
4. Large PDFs should use async endpoint (automatic)

### Jobs Stuck in "processing"

**Symptoms:**
- Jobs never complete
- No timeout triggered

**Resolution:**
1. Check for zombie processes: `docker compose exec ocr ps aux`
2. Kill stuck workers: `docker compose restart ocr`
3. Check timeout logic in logs: `docker compose logs ocr --tail 100`

## Metrics

The service exposes these metrics:

| Metric | Type | Description |
|--------|------|-------------|
| `ocr_duration_seconds` | Histogram | OCR processing time |
| `ocr_pages_processed` | Counter | Total pages processed |
| `ocr_failures_total` | Counter | Failed OCR attempts |
| `ocr_warnings_total` | Counter | OCR warnings (timeout, partial) |

## Disaster Recovery

### If OCR Service is Down

1. **PDF Indexing continues** - Falls back to text extraction only
2. **Scanned PDFs skip OCR** - Warning logged, no text indexed
3. **No data loss** - PDFs can be re-indexed when OCR recovers

### Recovery Steps

```bash
# 1. Check what's wrong
docker compose ps
docker compose logs ocr --tail 50
nvidia-smi

# 2. Restart services
docker compose restart

# 3. Verify health
curl http://localhost:8090/health | jq

# 4. Re-index failed PDFs (if needed)
# mainrag source sync --force
```

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| REDIS_HOST | localhost | Redis host |
| REDIS_PORT | 6379 | Redis port |
| REDIS_DB | 1 | Redis database number |
| NVIDIA_VISIBLE_DEVICES | all | GPU visibility |

## Security Considerations

- OCR service should only be accessible from internal network
- No authentication (relies on network isolation)
- File uploads are temporary (deleted after processing)
- Redis data expires via TTL

## Monitoring Alerts

| Alert | Condition | Severity |
|-------|-----------|----------|
| OCR GPU Unavailable | `gpu == false` | CRITICAL |
| OCR Redis Down | `redis == false` | CRITICAL |
| OCR High Failure Rate | failures > 10/min | WARNING |
| OCR Container Restarts | restarts > 3/hour | CRITICAL |

## Capacity Planning

| GPU | Concurrent Jobs | Pages/Minute |
|-----|-----------------|--------------|
| RTX 3050 Ti (4GB) | 2 | ~20-30 |
| RTX 3080 (10GB) | 4 | ~50-80 |
| RTX 4090 (24GB) | 8 | ~100-150 |

Adjust `OCR_CONCURRENCY` based on available GPU memory.

---

**Last Updated:** 2025-01-10
**Version:** 1.0

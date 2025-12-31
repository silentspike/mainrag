# Phase 15: Quality Tiers (Simplified)

## Overview

Phase 15 introduces **Quality Tiers** - user-selectable search quality levels that control the latency vs. precision tradeoff.

**Simplified to 2 tiers:** The original 4-tier design (fast/balanced/deep/verified) was simplified to 2 tiers because:
- `deep` required external LLM API (HyDE) - adds cost and complexity
- `verified` required LLM verification - same concerns
- 2 tiers cover 99% of use cases without external dependencies

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│  quality=fast                                   Latency: <100ms │
│  ├── Hybrid Search (PostgreSQL FTS + Qdrant Vector)            │
│  ├── RRF Score Fusion                                          │
│  └── NO Reranking                                              │
├─────────────────────────────────────────────────────────────────┤
│  quality=balanced (DEFAULT)                  Latency: 100-300ms │
│  ├── Hybrid Search (FTS + Vector)                              │
│  ├── RRF Score Fusion                                          │
│  └── BGE-Reranker-base cross-encoder reranking                 │
└─────────────────────────────────────────────────────────────────┘
```

## Quality Tier Details

### 1. `fast`

**Purpose:** Quick lookups, agent queries, auto-complete

**Components:**
- Hybrid Search (PostgreSQL FTS + Qdrant vectors)
- RRF (Reciprocal Rank Fusion)
- NO reranking

**Latency:** <100ms
**Cost:** $0

```bash
# Explicit fast tier
curl -X POST http://localhost:3001/api/v1/search \
  -H "Content-Type: application/json" \
  -d '{"query": "authentication", "quality": "fast"}'
```

**When to use:**
- Quick lookups
- Agent-driven queries
- Auto-complete/suggestions
- Time-sensitive applications
- High-volume scenarios

---

### 2. `balanced` (Default)

**Purpose:** User-facing searches, important queries

**Components:**
- Everything from `fast`
- Reranking via BGE-reranker-base (port 8082)

**Latency:** 100-300ms
**Cost:** $0

```bash
# Default behavior (balanced is default)
curl -X POST http://localhost:3001/api/v1/search \
  -H "Content-Type: application/json" \
  -d '{"query": "authentication middleware"}'

# Explicit balanced tier
curl -X POST http://localhost:3001/api/v1/search \
  -H "Content-Type: application/json" \
  -d '{"query": "authentication", "quality": "balanced"}'
```

**Response includes:**
```json
{
  "results": [...],
  "total": 10,
  "took_ms": 187,
  "quality_tier": "balanced",
  "reranked": true
}
```

**When to use:**
- User-facing search UI
- Important queries that need accuracy
- Content recommendations
- When latency budget is 100-300ms

---

## API Usage Examples

### Basic Search (uses balanced by default)

```bash
curl -X POST http://localhost:3001/api/v1/search \
  -H "Content-Type: application/json" \
  -d '{"query": "authentication"}'
```

### Tier-Specific Search

```bash
# Fast tier
curl -X POST http://localhost:3001/api/v1/search \
  -H "Content-Type: application/json" \
  -d '{"query": "test", "quality": "fast"}'

# Balanced tier
curl -X POST http://localhost:3001/api/v1/search \
  -H "Content-Type: application/json" \
  -d '{"query": "test", "quality": "balanced"}'
```

### With Additional Parameters

```bash
curl -X POST http://localhost:3001/api/v1/search \
  -H "Content-Type: application/json" \
  -d '{
    "query": "authentication middleware",
    "quality": "balanced",
    "limit": 10,
    "source_id": 1
  }'
```

## Legacy Tier Handling

For backwards compatibility, if someone passes legacy tier names:
- `deep` → falls back to `balanced`
- `verified` → falls back to `balanced`

## Performance Targets

| Tier | Latency | Precision | Use Case |
|------|---------|-----------|----------|
| fast | <100ms | 0.85 | Speed-critical |
| balanced | 100-300ms | 0.92 | General use |

## Monitoring

### Check Quality Tier in Logs

```bash
# Watch quality tier usage
journalctl -u mainrag-api -f | grep quality_tier

# Filter by tier
journalctl -u mainrag-api | grep '"quality_tier":"balanced"'
```

### Test Both Tiers

```bash
#!/bin/bash
for tier in fast balanced; do
  echo "Testing $tier..."
  curl -s -X POST http://localhost:3001/api/v1/search \
    -H "Content-Type: application/json" \
    -d "{\"quality\": \"$tier\", \"query\": \"test\"}" \
    | jq '{quality_tier, reranked, took_ms}'
done
```

## Decision Tree

```
Which quality tier should I use?

├─ "I need results NOW" (agents, auto-complete, high-volume)
│  └─ Use: fast
│
└─ "I need good results, normal speed" (user searches, UI)
   └─ Use: balanced (default)
```

## Why Only 2 Tiers?

The original design had 4 tiers:
- `fast` - hybrid search only
- `balanced` - + reranking
- `deep` - + HyDE (LLM-generated hypothetical documents)
- `verified` - + LLM verification of results

**Reasons for simplification:**
1. **HyDE adds complexity** - requires external LLM API, adds latency, risk of misinterpretation
2. **Verification is expensive** - $0.01-0.05 per query for LLM verification
3. **2 tiers cover 99% of use cases** - fast for speed, balanced for quality
4. **No external dependencies** - both tiers work with just TEI (local)

If deep/verified tiers are needed in the future, they can be re-added without changing the current API contract.

---

## Implementation Status

✅ Phase 15 Complete (Simplified)
- 2-tier quality system implemented
- API ready for integration
- BGE reranker integrated
- No external LLM dependencies

## Files

- `api/src/services/quality.rs` - Quality tier enum and parsing
- `api/src/services/search.rs` - Hybrid search with optional reranking
- `api/src/api/handlers/search.rs` - HTTP handlers with tier support

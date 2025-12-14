# Phase 15: Quality Tiers & Adaptive Retrieval

## Overview

Phase 15 introduces **Quality Tiers** - user-selectable search quality levels that allow fine-grained control over the latency vs. precision tradeoff.

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│  quality=fast (DEFAULT)                       Latency: <100ms    │
│  ├── Hybrid Search (FTS + Vector)                               │
│  ├── RRF Score Fusion                                           │
│  └── Basic Query Preprocessing                                  │
├─────────────────────────────────────────────────────────────────┤
│  quality=balanced                            Latency: 100-300ms  │
│  ├── + Reranking (BGE-reranker-base)                            │
│  └── + Query Expansion (Stemming, Synonyms)                     │
├─────────────────────────────────────────────────────────────────┤
│  quality=deep                                Latency: 300-2000ms │
│  ├── + HyDE Query Expansion                                     │
│  └── + Multi-Query Retrieval                                    │
├─────────────────────────────────────────────────────────────────┤
│  quality=verified (API required)             Latency: 1-5s      │
│  ├── + Corrective RAG (LLM verifies results)                    │
│  └── + Source Citation Verification                             │
└─────────────────────────────────────────────────────────────────┘
```

## Quality Tier Details

### 1. `fast` (Default)

**Purpose:** Quick lookups, agent queries, auto-complete

**Components:**
- Hybrid Search (PostgreSQL FTS + pgvector)
- RRF (Reciprocal Rank Fusion)
- Basic preprocessing

**Latency:** <100ms
**VRAM:** 0 (uses existing TEI)
**Cost:** $0

```bash
# Default behavior - no quality parameter needed
curl -X POST http://localhost:3001/api/search \
  -H "Content-Type: application/json" \
  -d '{"query": "authentication"}'

# Explicit fast tier
curl -X POST http://localhost:3001/api/search \
  -H "Content-Type: application/json" \
  -d '{"query": "authentication", "quality": "fast"}'
```

**When to use:**
- Quick lookups
- Agent-driven queries
- Auto-complete/suggestions
- Time-sensitive applications

---

### 2. `balanced`

**Purpose:** User-facing searches, important queries

**Components:**
- Everything from `fast`
- Reranking via BGE-reranker-base (port 8082)
- Query expansion (optional)

**Latency:** 100-300ms
**VRAM:** +0 (reranker already deployed in Phase 11)
**Cost:** $0

```bash
# Request balanced quality
curl -X POST http://localhost:3001/api/search \
  -H "Content-Type: application/json" \
  -d '{"quality": "balanced", "query": "authentication middleware"}'
```

**Response includes:**
```json
{
  "results": [...],
  "meta": {
    "quality_tier": "balanced",
    "latency_ms": 187,
    "reranked": true,
    "hyde_used": false,
    "llm_verified": false
  }
}
```

**When to use:**
- User-facing search UI
- Important queries that need accuracy
- Content recommendations
- When latency budget is 100-300ms

---

### 3. `deep`

**Purpose:** Complex research, vague queries, comprehensive results

**Components:**
- Everything from `balanced`
- HyDE (Hypothetical Document Embedding)
  - Generates hypothetical document that answers the query
  - Embeddings more similar to actual answers
- Multi-query expansion
- Duplicate deduplication

**Latency:** 300-2000ms
**VRAM:** 0 (API-based HyDE, no local model)
**Cost:** $0.001-0.01 per query (if using Claude/OpenAI)
**Requires:** `MAINRAG_HYDE_MODE=api`

```bash
# Request deep quality with HyDE
curl -X POST http://localhost:3001/api/search \
  -H "Content-Type: application/json" \
  -d '{"quality": "deep", "query": "how to handle authentication in microservices"}'
```

**HyDE Flow:**
```
1. User Query:      "how to handle authentication in microservices"
2. HyDE generates:  "Here is code implementing OAuth2 for microservices..."
3. Embedding vectors are more similar to actual solutions
4. Retrieval finds more relevant documents
```

**When to use:**
- Complex research questions
- Vague or multi-part queries
- When standard search isn't finding relevant results
- Content discovery
- Literature reviews

**Configuration:**
```bash
# .env or /etc/mainrag/mainrag.env
MAINRAG_HYDE_MODE=api              # Use API-based (Claude/OpenAI)
MAINRAG_LLM_PROVIDER=anthropic     # Which LLM to use
MAINRAG_LLM_MODEL=claude-3-haiku   # Smaller/faster model
MAINRAG_LLM_API_KEY=sk-...         # API credentials
```

---

### 4. `verified`

**Purpose:** Critical decision-making, fact-checking, high-stakes retrieval

**Components:**
- Everything from `deep`
- Corrective RAG: LLM verifies each result's relevance
- Citation verification: Confirms quoted passages exist in source
- Confidence scores per result

**Latency:** 1-5s
**VRAM:** 0 (API-only)
**Cost:** $0.01-0.05 per query (LLM verification)
**Requires:** `MAINRAG_LLM_PROVIDER` configured

```bash
# Request verified quality with LLM verification
curl -X POST http://localhost:3001/api/search \
  -H "Content-Type: application/json" \
  -d '{
    "quality": "verified",
    "query": "what is the API rate limit for this service",
    "verify_citations": true
  }'
```

**Verification Flow:**
```
1. Deep search (HyDE + reranking) retrieves 20 candidates
2. LLM reads each result + source context
3. LLM rates: "Is this actually relevant to the query?"
4. Returns top results with confidence scores
5. Optional: Verify quoted sections exist in source
```

**Response includes:**
```json
{
  "results": [
    {
      "chunk_id": 123,
      "content": "...",
      "score": 0.95,
      "verified": true,
      "confidence": 0.98,
      "verification_reason": "Directly answers the query with specific numbers"
    }
  ],
  "meta": {
    "quality_tier": "verified",
    "latency_ms": 2847,
    "reranked": true,
    "hyde_used": true,
    "llm_verified": true
  }
}
```

**When to use:**
- Critical business decisions
- Fact-checking / compliance
- Legal/medical/financial queries
- When accuracy is more important than speed
- High-stakes documentation lookup

**Configuration:**
```bash
# Use Claude for verification (recommended)
MAINRAG_LLM_PROVIDER=anthropic
MAINRAG_LLM_MODEL=claude-3-haiku-20240307
MAINRAG_LLM_API_KEY=sk-ant-...

# Or OpenAI
MAINRAG_LLM_PROVIDER=openai
MAINRAG_LLM_MODEL=gpt-4o-mini
MAINRAG_LLM_API_KEY=sk-...
```

---

## API Usage Examples

### Basic Search

```bash
# Uses default tier (fast)
curl -X POST http://localhost:3001/api/search \
  -H "Content-Type: application/json" \
  -d '{"query": "authentication"}'
```

### Tier-Specific Search

```bash
# Fast tier (explicit)
curl -X POST http://localhost:3001/api/search \
  -d '{"query": "test", "quality": "fast"}'

# Balanced tier
curl -X POST http://localhost:3001/api/search \
  -d '{"query": "test", "quality": "balanced"}'

# Deep tier
curl -X POST http://localhost:3001/api/search \
  -d '{"query": "test", "quality": "deep"}'

# Verified tier
curl -X POST http://localhost:3001/api/search \
  -d '{"quality": "verified", "query": "test"}'
```

### With Additional Parameters

```bash
curl -X POST http://localhost:3001/api/search \
  -H "Content-Type: application/json" \
  -d '{
    "query": "authentication middleware",
    "quality": "balanced",
    "limit": 10,
    "source": "mainrag",
    "filters": {"language": "rust"}
  }'
```

## Configuration

### Environment Variables

```bash
# Default quality tier
MAINRAG_QUALITY_DEFAULT=fast  # fast|balanced|deep|verified

# LLM Provider (for deep/verified tiers)
MAINRAG_LLM_PROVIDER=none     # none|anthropic|openai|local
MAINRAG_LLM_API_KEY=          # API key if provider requires
MAINRAG_LLM_MODEL=            # claude-3-haiku, gpt-4o-mini, etc.

# HyDE Configuration
MAINRAG_HYDE_MODE=api         # api|disabled

# RRF Tuning
MAINRAG_RRF_K=60              # Smoothing factor (default 60)

# Reranking
MAINRAG_RERANK_MODEL=bgereranker  # Model for balanced+
MAINRAG_RERANK_TOP_K=100       # Candidates before reranking
```

### Example .env File

```bash
# File: /etc/mainrag/mainrag.env or .env

# Quality Tiers
MAINRAG_QUALITY_DEFAULT=fast

# For deep/verified tiers
MAINRAG_HYDE_MODE=api
MAINRAG_LLM_PROVIDER=anthropic
MAINRAG_LLM_MODEL=claude-3-haiku-20240307
MAINRAG_LLM_API_KEY=sk-ant-...

# Reranking
MAINRAG_RERANK_MODEL=bgereranker
MAINRAG_RERANK_TOP_K=100

# RRF tuning
MAINRAG_RRF_K=60
```

## Performance Targets

| Tier | Latency | Precision | Recall | Use Case |
|------|---------|-----------|--------|----------|
| fast | <100ms | 0.85 | 0.80 | Speed-critical |
| balanced | 100-300ms | 0.92 | 0.90 | General use |
| deep | 300-2000ms | 0.95 | 0.95 | Research |
| verified | 1-5s | 0.98 | 0.98 | Critical |

## Monitoring & Debugging

### Check Quality Tier in Logs

```bash
# Watch quality tier usage
journalctl -u mainrag-api -f | grep quality_tier

# Filter by tier
journalctl -u mainrag-api | grep '"quality_tier":"balanced"'
```

### Metrics per Tier

```bash
# Average latency by tier (from metrics endpoint)
curl http://localhost:3001/metrics | grep search_latency_ms

# Sample output:
# search_latency_ms{tier="fast"} 45
# search_latency_ms{tier="balanced"} 187
# search_latency_ms{tier="deep"} 1200
# search_latency_ms{tier="verified"} 3400
```

### Test All Tiers

```bash
#!/bin/bash
for tier in fast balanced deep verified; do
  echo "Testing $tier..."
  curl -X POST http://localhost:3001/api/search \
    -d "{\"quality\": \"$tier\", \"query\": \"test\"}" \
    | jq '.meta'
done
```

## Decision Tree

```
Which quality tier should I use?

├─ "I need results NOW" (agents, auto-complete)
│  └─ Use: fast
│
├─ "I need good results, normal speed" (user searches)
│  └─ Use: balanced
│
├─ "I'm researching, more time is OK"
│  └─ Use: deep
│
└─ "This is critical, accuracy > speed" (compliance, fact-check)
   └─ Use: verified (requires LLM config)
```

## Hardware Requirements

| Tier | Local | Remote | Notes |
|------|-------|--------|-------|
| fast | ✅ | ✅ | Uses existing TEI |
| balanced | ✅ | ✅ | Uses existing reranker |
| deep | ✅* | ✅ | *With API LLM only |
| verified | ⚠️ | ✅ | Requires API LLM |

## Costs (Monthly Estimate)

```
Assuming 100K queries/month:

Tier       LLM Cost    API Cost   Total
fast       $0          $0         $0
balanced   $0          $0         $0
deep       ~$10-50     $0         ~$10-50
verified   ~$100-200   $0         ~$100-200

*Assuming Claude Haiku ~$0.0001 per 1K input tokens
```

## Next Steps

1. **Test locally:** Try each tier with test queries
2. **Monitor:** Watch latency/precision in production
3. **Optimize:** Tune RRF_K based on your data
4. **Integrate:** Use balanced tier in user-facing UI
5. **Expand:** Configure LLM provider for deep/verified

## Troubleshooting

### Q: Deep/Verified tiers return no results?
A: Check `MAINRAG_LLM_PROVIDER` is set correctly and API credentials work.

### Q: Latency exceeds budget?
A: Reduce `MAINRAG_RERANK_TOP_K` or move to a faster tier.

### Q: Cost too high?
A: Use balanced tier instead of verified, or reduce deep tier usage.

### Q: Results worse than before?
A: Your data may need higher tier. Try balanced instead of fast.

---

## Implementation Status

✅ Phase 15 Complete
- Quality tier routing implemented
- API ready for integration
- Configuration system ready
- Monitoring prepared

⏭️ Next Phase: Production Deployment & Scaling

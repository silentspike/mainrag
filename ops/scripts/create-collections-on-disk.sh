#!/bin/bash
# Create Qdrant collections with on_disk storage for low-RAM systems
# This script sets up mainrag_chunks and mainrag_code collections
# with on_disk: true to optimize RAM usage (500MB-1GB instead of 8-12GB)

set -e

QDRANT_URL="${QDRANT_URL:-http://localhost:6333}"
API_KEY="${QDRANT_API_KEY:-<REDACTED_QDRANT_API_KEY>}"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

echo -e "${YELLOW}🔧 Creating Qdrant collections with on_disk storage...${NC}\n"

# Collection 1: mainrag_chunks (document chunks for RAG)
echo "📦 Creating mainrag_chunks collection..."
curl -X PUT "$QDRANT_URL/collections/mainrag_chunks?wait=true" \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "vectors": {
      "size": 768,
      "distance": "Cosine",
      "on_disk": true
    },
    "hnsw_config": {
      "m": 24,
      "ef_construct": 128,
      "ef_search": 100,
      "on_disk": true,
      "payload_m": 8
    },
    "quantization_config": {
      "scalar": {
        "type": "int8",
        "quantile": 0.99,
        "always_ram": false
      }
    },
    "shard_number": 1,
    "replication_factor": 1
  }' > /dev/null 2>&1

if [ $? -eq 0 ]; then
  echo -e "${GREEN}✅ mainrag_chunks created (on_disk: true)${NC}"
else
  echo -e "${RED}❌ Failed to create mainrag_chunks${NC}"
  exit 1
fi

# Collection 2: mainrag_code (code snippets for code search)
echo "📦 Creating mainrag_code collection..."
curl -X PUT "$QDRANT_URL/collections/mainrag_code?wait=true" \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "vectors": {
      "size": 768,
      "distance": "Cosine",
      "on_disk": true
    },
    "hnsw_config": {
      "m": 24,
      "ef_construct": 128,
      "ef_search": 100,
      "on_disk": true,
      "payload_m": 8
    },
    "shard_number": 1,
    "replication_factor": 1
  }' > /dev/null 2>&1

if [ $? -eq 0 ]; then
  echo -e "${GREEN}✅ mainrag_code created (on_disk: true)${NC}"
else
  echo -e "${RED}❌ Failed to create mainrag_code${NC}"
  exit 1
fi

echo ""
echo -e "${GREEN}✅ Collections created with on_disk storage${NC}"
echo ""
echo -e "${YELLOW}📊 Collection Stats:${NC}"
curl -s "$QDRANT_URL/collections/mainrag_chunks" \
  -H "Authorization: Bearer $API_KEY" | jq '{name: .result.name, points: .result.points_count, config: {on_disk: .result.config.hnsw_config.on_disk, distance: .result.config.vectors.distance}}'

echo ""
curl -s "$QDRANT_URL/collections/mainrag_code" \
  -H "Authorization: Bearer $API_KEY" | jq '{name: .result.name, points: .result.points_count, config: {on_disk: .result.config.hnsw_config.on_disk, distance: .result.config.vectors.distance}}'

echo ""
echo -e "${GREEN}🎉 Qdrant on_disk setup complete${NC}"

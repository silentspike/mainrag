#!/usr/bin/env python3
"""
Load synonyms from YAML into Qdrant for MainRAG Query Expansion.

Generates embeddings using TEI and upserts to Qdrant collection.

Usage:
    python load_synonyms_to_qdrant.py synonyms.yaml
    python load_synonyms_to_qdrant.py synonyms.yaml --collection synonyms_v2
    python load_synonyms_to_qdrant.py synonyms.yaml --dry-run

Environment variables:
    QDRANT_URL: Qdrant server URL (default: http://localhost:6333)
    QDRANT_API_KEY: Qdrant API key (default: none)
    TEI_URL: TEI embedding server URL (default: http://localhost:8080)
"""

import argparse
import hashlib
import os
import sys
from pathlib import Path
from typing import Dict, List, Optional
import uuid

import requests
import yaml


# === CONFIGURATION ===

QDRANT_URL = os.environ.get('QDRANT_URL', 'http://localhost:6333')
QDRANT_API_KEY = os.environ.get('QDRANT_API_KEY', '')
TEI_URL = os.environ.get('TEI_URL', 'http://localhost:8080')

# Embedding dimension (must match TEI model)
EMBEDDING_DIM = 384  # all-MiniLM-L6-v2 default

# Batch size for embedding generation
EMBED_BATCH_SIZE = 32


# === TEI CLIENT ===

def embed_texts(texts: List[str], tei_url: str = TEI_URL) -> List[List[float]]:
    """Generate embeddings for a batch of texts using TEI."""
    response = requests.post(
        f"{tei_url}/embed",
        json={"inputs": texts},
        headers={"Content-Type": "application/json"},
        timeout=60
    )
    response.raise_for_status()
    return response.json()


def embed_single(text: str, tei_url: str = TEI_URL) -> List[float]:
    """Generate embedding for a single text."""
    result = embed_texts([text], tei_url)
    return result[0]


# === QDRANT CLIENT ===

def create_collection(
    name: str,
    vector_size: int = EMBEDDING_DIM,
    qdrant_url: str = QDRANT_URL,
    api_key: str = QDRANT_API_KEY
) -> bool:
    """Create Qdrant collection if it doesn't exist."""
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["api-key"] = api_key

    # Check if exists
    check_response = requests.get(
        f"{qdrant_url}/collections/{name}",
        headers=headers
    )

    if check_response.status_code == 200:
        print(f"Collection '{name}' already exists")
        return False

    # Create collection
    create_response = requests.put(
        f"{qdrant_url}/collections/{name}",
        headers=headers,
        json={
            "vectors": {
                "size": vector_size,
                "distance": "Cosine"
            }
        }
    )

    if create_response.status_code in (200, 201):
        print(f"Created collection '{name}'")
        return True
    else:
        print(f"Failed to create collection: {create_response.text}")
        raise RuntimeError(f"Failed to create collection: {create_response.status_code}")


def upsert_points(
    collection: str,
    points: List[Dict],
    qdrant_url: str = QDRANT_URL,
    api_key: str = QDRANT_API_KEY
):
    """Upsert points to Qdrant collection."""
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["api-key"] = api_key

    response = requests.put(
        f"{qdrant_url}/collections/{collection}/points",
        headers=headers,
        json={"points": points}
    )

    if response.status_code not in (200, 201):
        print(f"Failed to upsert: {response.text}")
        raise RuntimeError(f"Upsert failed: {response.status_code}")


def delete_collection(
    name: str,
    qdrant_url: str = QDRANT_URL,
    api_key: str = QDRANT_API_KEY
):
    """Delete Qdrant collection."""
    headers = {}
    if api_key:
        headers["api-key"] = api_key

    response = requests.delete(
        f"{qdrant_url}/collections/{name}",
        headers=headers
    )

    if response.status_code in (200, 204):
        print(f"Deleted collection '{name}'")
    else:
        print(f"Failed to delete collection (may not exist): {response.status_code}")


# === SYNONYM PROCESSING ===

def generate_point_id(term: str) -> str:
    """Generate deterministic UUID from term for idempotent upserts."""
    # Create UUID from hash of term
    hash_bytes = hashlib.md5(term.lower().encode()).digest()
    return str(uuid.UUID(bytes=hash_bytes))


def synonym_to_embedding_text(entry: Dict) -> str:
    """
    Convert synonym entry to text for embedding.

    Strategy: Embed the primary term + all aliases as a single string.
    This creates a vector that represents the semantic space of all related terms.
    """
    term = entry['term']
    aliases = entry.get('aliases', [])
    category = entry.get('category', '')

    # Combine term and aliases
    all_terms = [term] + aliases
    text = ' '.join(all_terms)

    # Optionally add category for context
    if category and category not in ('harvested', 'unknown'):
        text = f"{category}: {text}"

    return text


def load_synonyms(path: Path) -> List[Dict]:
    """Load synonyms from YAML file."""
    with open(path) as f:
        data = yaml.safe_load(f)
    return data.get('synonyms', [])


def process_synonyms(
    synonyms: List[Dict],
    collection: str,
    tei_url: str = TEI_URL,
    qdrant_url: str = QDRANT_URL,
    api_key: str = QDRANT_API_KEY,
    dry_run: bool = False
):
    """Process synonyms and upload to Qdrant."""
    total = len(synonyms)
    print(f"Processing {total} synonyms...")

    # Prepare texts for batch embedding
    texts = [synonym_to_embedding_text(s) for s in synonyms]

    # Generate embeddings in batches
    all_embeddings: List[List[float]] = []

    for i in range(0, len(texts), EMBED_BATCH_SIZE):
        batch = texts[i:i + EMBED_BATCH_SIZE]
        print(f"  Embedding batch {i // EMBED_BATCH_SIZE + 1}/{(len(texts) - 1) // EMBED_BATCH_SIZE + 1}...")

        if not dry_run:
            embeddings = embed_texts(batch, tei_url)
            all_embeddings.extend(embeddings)
        else:
            # Dry run: fake embeddings
            all_embeddings.extend([[0.0] * EMBEDDING_DIM for _ in batch])

    # Prepare Qdrant points
    points = []
    for i, (syn, embedding) in enumerate(zip(synonyms, all_embeddings)):
        point_id = generate_point_id(syn['term'])
        points.append({
            "id": point_id,
            "vector": embedding,
            "payload": {
                "term": syn['term'],
                "aliases": syn.get('aliases', []),
                "category": syn.get('category', 'unknown'),
                "language": syn.get('language', 'mixed'),
            }
        })

    # Upload in batches
    batch_size = 100
    for i in range(0, len(points), batch_size):
        batch = points[i:i + batch_size]
        print(f"  Uploading batch {i // batch_size + 1}/{(len(points) - 1) // batch_size + 1}...")

        if not dry_run:
            upsert_points(collection, batch, qdrant_url, api_key)

    print(f"Done! Uploaded {len(points)} synonyms to '{collection}'")


# === MAIN ===

def main():
    parser = argparse.ArgumentParser(
        description='Load synonyms to Qdrant for MainRAG query expansion'
    )
    parser.add_argument('file', type=Path, help='Synonym YAML file to load')
    parser.add_argument('--collection', '-c', default='synonyms_v1',
                       help='Qdrant collection name (default: synonyms_v1)')
    parser.add_argument('--qdrant-url', default=QDRANT_URL,
                       help=f'Qdrant URL (default: {QDRANT_URL})')
    parser.add_argument('--tei-url', default=TEI_URL,
                       help=f'TEI URL (default: {TEI_URL})')
    parser.add_argument('--recreate', action='store_true',
                       help='Delete and recreate collection')
    parser.add_argument('--dry-run', action='store_true',
                       help='Dry run (no actual uploads)')
    parser.add_argument('--dim', type=int, default=EMBEDDING_DIM,
                       help=f'Embedding dimension (default: {EMBEDDING_DIM})')

    args = parser.parse_args()

    if not args.file.exists():
        print(f"Error: File not found: {args.file}")
        sys.exit(1)

    print(f"Loading synonyms from {args.file}")
    print(f"Qdrant: {args.qdrant_url}")
    print(f"TEI: {args.tei_url}")
    print(f"Collection: {args.collection}")
    print(f"Embedding dim: {args.dim}")
    print()

    # Load synonyms
    synonyms = load_synonyms(args.file)
    print(f"Loaded {len(synonyms)} synonyms")

    if args.dry_run:
        print("\n=== DRY RUN MODE ===\n")

    # Create/recreate collection
    if args.recreate and not args.dry_run:
        delete_collection(args.collection, args.qdrant_url)

    if not args.dry_run:
        create_collection(args.collection, args.dim, args.qdrant_url)

    # Process and upload
    process_synonyms(
        synonyms,
        args.collection,
        args.tei_url,
        args.qdrant_url,
        dry_run=args.dry_run
    )


if __name__ == '__main__':
    main()

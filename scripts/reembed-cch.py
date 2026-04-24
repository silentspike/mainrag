#!/usr/bin/env python3
"""Re-embed all chunks for a source with CCH (Contextual Chunk Header) prefix.

This script reads existing chunks, prepends context_prefix to content_text,
generates new embeddings via TEI, and updates both PostgreSQL and Qdrant.

Usage: python3 scripts/reembed-cch.py [--source-id 144] [--batch-size 32] [--dry-run]
"""
import argparse
import json
import os
import sys
import time
import urllib.request
import urllib.error

import psycopg2  # pip install psycopg2-binary

TEI_URL = os.environ.get("TEI_REST_URL", "http://localhost:8091")
QDRANT_URL = os.environ.get("QDRANT_REST_URL", "http://localhost:6333")
DB_URL = os.environ.get("DATABASE_URL")
if not DB_URL:
    sys.exit("ERROR: Set DATABASE_URL env var (postgresql://user:pw@host:port/db)")
COLLECTION = os.environ.get("QDRANT_CHUNK_COLLECTION", "mainrag_chunks_gte")
MODEL_NAME = os.environ.get("EMBEDDING_MODEL_ID", "Alibaba-NLP/gte-modernbert-base") + "+cch"


def embed_batch(texts: list[str]) -> list[list[float]]:
    """Call TEI embed endpoint."""
    payload = json.dumps({"inputs": texts}).encode()
    req = urllib.request.Request(
        f"{TEI_URL}/embed",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read())


def qdrant_upsert(points: list[dict]):
    """Upsert points to Qdrant."""
    payload = json.dumps({"points": points}).encode()
    req = urllib.request.Request(
        f"{QDRANT_URL}/collections/{COLLECTION}/points?wait=true",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="PUT",
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        result = json.loads(resp.read())
        if result.get("status") != "ok":
            raise RuntimeError(f"Qdrant upsert failed: {result}")


def main():
    parser = argparse.ArgumentParser(description="Re-embed chunks with CCH prefix")
    parser.add_argument("--source-id", type=int, default=144, help="Source ID to re-embed")
    parser.add_argument("--batch-size", type=int, default=32, help="TEI batch size")
    parser.add_argument("--dry-run", action="store_true", help="Don't write, just show stats")
    args = parser.parse_args()

    conn = psycopg2.connect(DB_URL)
    cur = conn.cursor()

    # Get all chunks with context_prefix for this source
    cur.execute("""
        SELECT c.id, c.context_prefix, c.content_text, f.path,
               ce.model as current_model
        FROM chunks c
        JOIN files f ON c.file_id = f.id
        LEFT JOIN chunk_embeddings ce ON ce.chunk_id = c.id
        WHERE f.source_id = %s
        ORDER BY c.id
    """, (args.source_id,))

    rows = cur.fetchall()
    print(f"Found {len(rows)} chunks for source_id={args.source_id}")

    # Filter: only re-embed chunks that don't already have CCH embeddings
    to_embed = []
    already_cch = 0
    for chunk_id, context_prefix, content_text, file_path, current_model in rows:
        if current_model == MODEL_NAME:
            already_cch += 1
            continue
        # Build CCH-enriched text
        cch_text = f"{context_prefix or ''}\n\n{content_text or ''}"
        to_embed.append((chunk_id, cch_text, file_path))

    print(f"  Already CCH-embedded: {already_cch}")
    print(f"  Need re-embedding: {len(to_embed)}")

    if args.dry_run:
        print("Dry run — exiting.")
        return

    if not to_embed:
        print("Nothing to do!")
        return

    # Process in batches
    total_embedded = 0
    start_time = time.monotonic()

    for batch_start in range(0, len(to_embed), args.batch_size):
        batch = to_embed[batch_start : batch_start + args.batch_size]
        batch_ids = [b[0] for b in batch]
        batch_texts = [b[1] for b in batch]

        # Generate embeddings via TEI
        try:
            vectors = embed_batch(batch_texts)
        except Exception as e:
            print(f"  ERROR embedding batch at {batch_start}: {e}")
            continue

        # Update PostgreSQL chunk_embeddings
        for i, (chunk_id, _, _) in enumerate(batch):
            vector = vectors[i]
            # Upsert: delete old embedding, insert new one
            cur.execute(
                "DELETE FROM chunk_embeddings WHERE chunk_id = %s",
                (chunk_id,),
            )
            cur.execute(
                "INSERT INTO chunk_embeddings (chunk_id, model, vector) VALUES (%s, %s, %s)",
                (chunk_id, MODEL_NAME, vector),
            )

        conn.commit()

        # Build Qdrant points with same payload structure as the API
        # Need to get file_id and user_id for each chunk
        chunk_ids_str = ",".join(str(b[0]) for b in batch)
        cur.execute(f"""
            SELECT c.id, c.file_id, f.source_id, s.user_id
            FROM chunks c
            JOIN files f ON c.file_id = f.id
            JOIN sources s ON f.source_id = s.id
            WHERE c.id IN ({chunk_ids_str})
        """)
        meta = {row[0]: row for row in cur.fetchall()}

        qdrant_points = []
        for i, (chunk_id, _, _) in enumerate(batch):
            m = meta.get(chunk_id)
            if not m:
                continue
            _, file_id, source_id, user_id = m
            qdrant_points.append({
                "id": chunk_id,
                "vector": vectors[i],
                "payload": {
                    "chunk_id": chunk_id,
                    "file_id": file_id,
                    "source_id": source_id,
                    "user_id": str(user_id) if user_id else None,
                },
            })

        try:
            qdrant_upsert(qdrant_points)
        except Exception as e:
            print(f"  ERROR upserting to Qdrant at {batch_start}: {e}")
            continue

        total_embedded += len(batch)
        elapsed = time.monotonic() - start_time
        rate = total_embedded / elapsed if elapsed > 0 else 0
        eta = (len(to_embed) - total_embedded) / rate if rate > 0 else 0

        if (batch_start // args.batch_size + 1) % 10 == 0 or batch_start + args.batch_size >= len(to_embed):
            print(
                f"  Progress: {total_embedded}/{len(to_embed)} "
                f"({total_embedded/len(to_embed)*100:.1f}%) "
                f"Rate: {rate:.1f} chunks/s  ETA: {eta:.0f}s"
            )

    elapsed = time.monotonic() - start_time
    print(f"\nDone! Re-embedded {total_embedded} chunks in {elapsed:.1f}s")
    print(f"Model: {MODEL_NAME}")

    cur.close()
    conn.close()


if __name__ == "__main__":
    main()

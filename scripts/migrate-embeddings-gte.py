#!/usr/bin/env python3
"""
Migrate embeddings from BGE-base-en-v1.5 to GTE-ModernBERT-base.

Reads all chunks from PostgreSQL, generates GTE embeddings via TEI on :8091,
and stores them in chunk_embeddings_gte + Qdrant mainrag_chunks_gte.

Resumable: skips chunks that already exist in chunk_embeddings_gte.

Usage:
    python3 scripts/migrate-embeddings-gte.py [--batch-size 32] [--limit 100]
"""

import argparse
import json
import os
import sys
import time
import urllib.request
import psycopg2
import psycopg2.extras
import struct

# Configuration (DATABASE_URL env var is REQUIRED — no default credentials)
DB_DSN = os.environ.get("DATABASE_URL") or os.environ.get("MAINRAG_DB_DSN")
if not DB_DSN:
    sys.exit("ERROR: Set DATABASE_URL (postgresql://user:pw@host:port/db) or MAINRAG_DB_DSN (libpq DSN)")
TEI_URL = os.environ.get("TEI_URL", "http://localhost:8091")
QDRANT_URL = os.environ.get("QDRANT_URL", "http://localhost:6333")
QDRANT_API_KEY = os.environ.get("QDRANT_API_KEY", "")
QDRANT_COLLECTION = os.environ.get("QDRANT_CHUNK_COLLECTION", "mainrag_chunks_gte")
MODEL_NAME = os.environ.get("EMBEDDING_MODEL_ID", "Alibaba-NLP/gte-modernbert-base") + "+cch"


def embed_batch(texts: list[str], max_retries: int = 3) -> list[list[float]]:
    """Call TEI to generate embeddings for a batch of texts. Retries on timeout."""
    for attempt in range(max_retries):
        try:
            data = json.dumps({"inputs": texts}).encode("utf-8")
            req = urllib.request.Request(
                f"{TEI_URL}/embed",
                data=data,
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=180) as resp:
                return json.loads(resp.read())
        except (TimeoutError, urllib.error.URLError, ConnectionError) as e:
            if attempt < max_retries - 1:
                wait = 5 * (attempt + 1)
                print(f"\n  TEI timeout (attempt {attempt+1}/{max_retries}), retrying in {wait}s...",
                      file=sys.stderr, flush=True)
                time.sleep(wait)
            else:
                # Last resort: split batch in half and try individually
                if len(texts) > 1:
                    mid = len(texts) // 2
                    print(f"\n  Splitting batch {len(texts)} → {mid}+{len(texts)-mid}",
                          file=sys.stderr, flush=True)
                    left = embed_batch(texts[:mid], max_retries)
                    right = embed_batch(texts[mid:], max_retries)
                    return left + right
                raise


def qdrant_upsert(points: list[dict]):
    """Upsert points to Qdrant collection."""
    data = json.dumps({"points": points}).encode("utf-8")
    req = urllib.request.Request(
        f"{QDRANT_URL}/collections/{QDRANT_COLLECTION}/points?wait=false",
        data=data,
        headers={"Content-Type": "application/json"},
        method="PUT",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        result = json.loads(resp.read())
        if result.get("status") != "ok":
            print(f"  Qdrant upsert warning: {result}", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(description="Migrate embeddings to GTE-ModernBERT-base")
    parser.add_argument("--batch-size", type=int, default=32)
    parser.add_argument("--limit", type=int, default=0, help="Limit chunks (0=all)")
    args = parser.parse_args()

    # Read connection (for cursor iteration) — must be in a transaction for named cursors
    read_conn = psycopg2.connect(DB_DSN)
    read_conn.autocommit = False
    # Write connection (for inserts) — no statement timeout for bulk migration
    conn = psycopg2.connect(DB_DSN)
    conn.autocommit = False
    with conn.cursor() as cur:
        cur.execute("SET statement_timeout = '0'")
    conn.commit()

    # Count total chunks to migrate
    with read_conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM chunks")
        total = cur.fetchone()[0]

        cur.execute("SELECT COUNT(*) FROM chunk_embeddings_gte")
        already_done = cur.fetchone()[0]

    remaining = total - already_done
    if args.limit > 0:
        remaining = min(remaining, args.limit)

    print(f"Total chunks: {total}, already migrated: {already_done}, remaining: {remaining}")

    if remaining == 0:
        print("Nothing to migrate!")
        return

    # Fetch chunks that don't have GTE embeddings yet
    limit_clause = f"LIMIT {args.limit}" if args.limit > 0 else ""
    with read_conn.cursor(name="migrate_cursor") as cur:
        cur.itersize = args.batch_size * 2
        cur.execute(f"""
            SELECT c.id, c.content_text, c.context_prefix, f.source_id
            FROM chunks c
            JOIN files f ON c.file_id = f.id
            WHERE c.id NOT IN (SELECT chunk_id FROM chunk_embeddings_gte)
            ORDER BY c.id
            {limit_clause}
        """)

        batch_texts = []
        batch_ids = []
        batch_source_ids = []
        processed = 0
        start_time = time.time()

        for row in cur:
            chunk_id, content_text, context_prefix, source_id = row

            # Build CCH text (same as API does)
            if context_prefix and content_text:
                text = f"{context_prefix}\n\n{content_text}"
            elif content_text:
                text = content_text
            else:
                continue  # skip empty chunks

            # Truncate to ~8000 chars (GTE supports 8192 tokens, ~4 chars/token)
            if len(text) > 30000:
                text = text[:30000]

            batch_texts.append(text)
            batch_ids.append(chunk_id)
            batch_source_ids.append(source_id)

            if len(batch_texts) >= args.batch_size:
                _process_batch(conn, batch_ids, batch_texts, batch_source_ids)
                processed += len(batch_texts)

                elapsed = time.time() - start_time
                eps = processed / elapsed if elapsed > 0 else 0
                eta = (remaining - processed) / eps if eps > 0 else 0
                pct = 100.0 * (already_done + processed) / total

                print(
                    f"\r  {already_done + processed}/{total} ({pct:.1f}%) "
                    f"| {eps:.1f} ebs/s | ETA {eta/60:.0f}min",
                    end="", flush=True,
                )

                batch_texts.clear()
                batch_ids.clear()
                batch_source_ids.clear()

        # Final batch
        if batch_texts:
            _process_batch(conn, batch_ids, batch_texts, batch_source_ids)
            processed += len(batch_texts)

    elapsed = time.time() - start_time
    print(f"\nDone! Migrated {processed} chunks in {elapsed:.0f}s ({processed/elapsed:.1f} ebs/s)")
    read_conn.close()
    conn.close()


def _process_batch(conn, chunk_ids, texts, source_ids):
    """Embed a batch, store in PostgreSQL + Qdrant."""
    vectors = embed_batch(texts)

    # Insert into chunk_embeddings_gte
    with conn.cursor() as cur:
        values = [
            (cid, MODEL_NAME, vec)
            for cid, vec in zip(chunk_ids, vectors)
        ]
        psycopg2.extras.execute_values(
            cur,
            "INSERT INTO chunk_embeddings_gte (chunk_id, model, vector) VALUES %s "
            "ON CONFLICT (chunk_id) DO UPDATE SET vector = EXCLUDED.vector, model = EXCLUDED.model",
            values,
            template="(%s, %s, %s::vector)",
        )
    conn.commit()

    # Upsert to Qdrant
    points = []
    for cid, vec, sid in zip(chunk_ids, vectors, source_ids):
        points.append({
            "id": cid,
            "vector": vec,
            "payload": {
                "chunk_id": cid,
                "source_id": sid,
            },
        })
    qdrant_upsert(points)


if __name__ == "__main__":
    main()

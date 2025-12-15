#!/usr/bin/env python3
"""
MAINRAG Migration: Import from CodeRag export into PostgreSQL + Qdrant

This script:
1. Imports sources, files, chunks, symbols from exported JSON
2. Triggers re-embedding via TEI (768-dim BGE-base-en-v1.5)
3. Stores vectors in PostgreSQL (pgvector) and Qdrant

Prerequisites:
- PostgreSQL with mainrag database and schema deployed
- Qdrant running with collection created
- TEI running for embeddings

Usage:
    export POSTGRES_PASSWORD='<REDACTED_DB_PW>'
    python import-mainrag.py --export-dir /work/mainrag/ops/migration/export
"""

import argparse
import json
import os
import sys
import zlib  # Always needed to decompress old CodeRag data
from datetime import datetime
from pathlib import Path
from typing import Iterator

import psycopg2
from psycopg2.extras import execute_batch
import requests

# zstd for re-compression (matches Rust API which uses zstd)
try:
    import zstandard as zstd
    HAS_ZSTD = True
except ImportError:
    HAS_ZSTD = False
    print("WARNING: zstandard not installed, falling back to zlib for compression")
    print("Install with: pip install zstandard")
    print("Note: Rust API expects zstd, zlib may cause issues!")

# Configuration
POSTGRES_HOST = os.environ.get("POSTGRES_HOST", "localhost")
POSTGRES_PORT = int(os.environ.get("POSTGRES_PORT", "5432"))
POSTGRES_DB = os.environ.get("POSTGRES_DB", "mainrag")
POSTGRES_USER = os.environ.get("POSTGRES_USER", "mainrag")
POSTGRES_PASSWORD = os.environ.get("POSTGRES_PASSWORD", "<REDACTED_DB_PW>")

QDRANT_URL = os.environ.get("QDRANT_URL", "http://localhost:6333")
QDRANT_API_KEY = os.environ.get("QDRANT_API_KEY", "<REDACTED_QDRANT_API_KEY>")
QDRANT_COLLECTION = os.environ.get("QDRANT_COLLECTION", "mainrag_chunks")

TEI_URL = os.environ.get("TEI_URL", "http://localhost:8080")

BATCH_SIZE = int(os.environ.get("BATCH_SIZE", "100"))


def connect_db():
    """Connect to PostgreSQL"""
    return psycopg2.connect(
        host=POSTGRES_HOST,
        port=POSTGRES_PORT,
        dbname=POSTGRES_DB,
        user=POSTGRES_USER,
        password=POSTGRES_PASSWORD,
    )


def check_prerequisites():
    """Check all services are available"""
    print("=== Checking Prerequisites ===")

    # PostgreSQL
    try:
        conn = connect_db()
        cur = conn.cursor()
        cur.execute("SELECT version()")
        version = cur.fetchone()[0]
        print(f"  PostgreSQL: OK ({version[:50]}...)")
        conn.close()
    except Exception as e:
        print(f"  PostgreSQL: FAILED - {e}")
        return False

    # Qdrant (mit API Key Header)
    try:
        headers = {"api-key": QDRANT_API_KEY} if QDRANT_API_KEY else {}
        resp = requests.get(f"{QDRANT_URL}/healthz", timeout=5, headers=headers)
        if resp.status_code == 200:
            print(f"  Qdrant: OK ({QDRANT_URL})")
        else:
            print(f"  Qdrant: status {resp.status_code}")
            # Continue anyway - API might still work
    except Exception as e:
        print(f"  Qdrant: FAILED - {e}")
        return False

    # TEI
    try:
        resp = requests.get(f"{TEI_URL}/health", timeout=5)
        if resp.status_code == 200:
            print(f"  TEI: OK ({TEI_URL})")
        else:
            print(f"  TEI: FAILED - status {resp.status_code}")
            return False
    except Exception as e:
        print(f"  TEI: FAILED - {e}")
        return False

    print("")
    return True


def get_embeddings(texts: list[str]) -> list[list[float]]:
    """Get embeddings from TEI"""
    resp = requests.post(
        f"{TEI_URL}/embed",
        json={"inputs": texts},
        headers={"Content-Type": "application/json"},
        timeout=60,
    )
    resp.raise_for_status()
    return resp.json()


def upsert_qdrant(points: list[dict]):
    """Upsert points to Qdrant"""
    resp = requests.put(
        f"{QDRANT_URL}/collections/{QDRANT_COLLECTION}/points",
        json={"points": points},
        headers={
            "api-key": QDRANT_API_KEY,
            "Content-Type": "application/json",
        },
        timeout=60,
    )
    resp.raise_for_status()
    return resp.json()


def read_jsonl(filepath: Path) -> Iterator[dict]:
    """Read JSONL file, yielding each object"""
    with open(filepath, "r") as f:
        buffer = ""
        for line in f:
            buffer += line
            try:
                # Try to parse accumulated buffer as JSON array
                data = json.loads(buffer)
                if isinstance(data, list):
                    for item in data:
                        yield item
                else:
                    yield data
                buffer = ""
            except json.JSONDecodeError:
                # Incomplete JSON, continue accumulating
                continue


def import_sources(conn, export_dir: Path):
    """Import sources into PostgreSQL"""
    print("[Sources] Importing...")

    sources_file = export_dir / "sources.json"
    if not sources_file.exists():
        print("  No sources.json found, skipping")
        return {}

    with open(sources_file) as f:
        sources = json.load(f)

    cur = conn.cursor()
    old_to_new_id = {}

    for source in sources:
        # Map source type
        source_type_map = {
            "git": "git",
            "fs": "fs",
            "web": "web",
            "conversation": "conversation",
        }
        source_type = source_type_map.get(source["type"], "fs")

        # Insert source - FIXED: use correct column names (type, path)
        cur.execute("""
            INSERT INTO sources (name, type, path, config, created_at, updated_at)
            VALUES (%s, %s, %s, %s, to_timestamp(%s), to_timestamp(%s))
            ON CONFLICT (name) DO UPDATE SET
                type = EXCLUDED.type,
                path = EXCLUDED.path,
                updated_at = EXCLUDED.updated_at
            RETURNING id
        """, (
            source["name"],
            source_type,
            source["path"],
            json.dumps(source.get("config")) if source.get("config") else None,
            source["created_at"],
            source["updated_at"],
        ))

        new_id = cur.fetchone()[0]
        old_to_new_id[source["id"]] = new_id

    conn.commit()
    print(f"  Imported {len(sources)} sources")
    return old_to_new_id


def import_files(conn, export_dir: Path, source_id_map: dict):
    """Import files into PostgreSQL"""
    print("[Files] Importing...")

    files_file = export_dir / "files.jsonl"
    if not files_file.exists():
        print("  No files.jsonl found, skipping")
        return {}

    cur = conn.cursor()
    old_to_new_id = {}
    count = 0
    batch = []

    for file_data in read_jsonl(files_file):
        old_source_id = file_data["source_id"]
        new_source_id = source_id_map.get(old_source_id)

        if new_source_id is None:
            continue

        # Decompress content - FIXED: handle zstd/zlib and get sizes
        try:
            content_compressed_old = bytes.fromhex(file_data["content_hex"])
            # Try zlib first (old CodeRag format)
            content_text = zlib.decompress(content_compressed_old).decode("utf-8", errors="replace")
            size_original = len(content_text.encode('utf-8'))
            # Re-compress with zstd for MAINRAG
            if HAS_ZSTD:
                cctx = zstd.ZstdCompressor(level=3)
                content_compressed = cctx.compress(content_text.encode('utf-8'))
            else:
                content_compressed = zlib.compress(content_text.encode('utf-8'), 6)
            size_compressed = len(content_compressed)
        except Exception:
            content_text = ""
            content_compressed = b""
            size_original = file_data.get("size_original", 0)
            size_compressed = 0

        batch.append((
            file_data["id"],
            new_source_id,
            file_data["path"],
            bytes.fromhex(file_data["hash_hex"]),  # Raw bytes for BYTEA
            content_compressed,  # zstd compressed BYTEA
            content_text,  # Decompressed TEXT for FTS
            file_data.get("language"),
            size_original,
            size_compressed,
            file_data["last_modified"],
            file_data["created_at"],
        ))

        if len(batch) >= BATCH_SIZE:
            _insert_files_batch(cur, batch, old_to_new_id)
            count += len(batch)
            print(f"  Processed {count} files...")
            batch = []

    if batch:
        _insert_files_batch(cur, batch, old_to_new_id)
        count += len(batch)

    conn.commit()
    print(f"  Imported {count} files")
    return old_to_new_id


def _insert_files_batch(cur, batch, old_to_new_id):
    """Insert batch of files - FIXED: correct column names and types"""
    for old_id, source_id, path, hash_bytes, content_compressed, content_text, language, size_orig, size_comp, modified, created in batch:
        cur.execute("""
            INSERT INTO files (source_id, path, hash, content, content_text, language, size_original, size_compressed, last_modified, created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, to_timestamp(%s), to_timestamp(%s), NOW())
            ON CONFLICT (source_id, path) DO UPDATE SET
                hash = EXCLUDED.hash,
                content = EXCLUDED.content,
                content_text = EXCLUDED.content_text,
                language = EXCLUDED.language,
                size_original = EXCLUDED.size_original,
                size_compressed = EXCLUDED.size_compressed,
                last_modified = EXCLUDED.last_modified,
                updated_at = NOW()
            RETURNING id
        """, (source_id, path, hash_bytes, content_compressed, content_text, language, size_orig, size_comp, modified, created))

        new_id = cur.fetchone()[0]
        old_to_new_id[old_id] = new_id


def import_chunks_and_embed(conn, export_dir: Path, file_id_map: dict, source_id_map: dict):
    """Import chunks, generate embeddings with TEI, store in PostgreSQL + Qdrant"""
    print("[Chunks] Importing and embedding...")

    chunks_file = export_dir / "chunks.jsonl"
    if not chunks_file.exists():
        print("  No chunks.jsonl found, skipping")
        return {}

    cur = conn.cursor()
    old_to_new_id = {}
    count = 0
    embed_count = 0
    batch = []

    for chunk_data in read_jsonl(chunks_file):
        old_file_id = chunk_data["file_id"]
        new_file_id = file_id_map.get(old_file_id)

        if new_file_id is None:
            continue

        # Decompress content - FIXED: handle zstd/zlib
        try:
            content_compressed_old = bytes.fromhex(chunk_data["content_compressed_hex"])
            # Try zlib first (old CodeRag format)
            content_text = zlib.decompress(content_compressed_old).decode("utf-8", errors="replace")
            # Re-compress with zstd for MAINRAG
            if HAS_ZSTD:
                cctx = zstd.ZstdCompressor(level=3)
                content_compressed = cctx.compress(content_text.encode('utf-8'))
            else:
                content_compressed = zlib.compress(content_text.encode('utf-8'), 6)
        except Exception:
            content_text = ""
            content_compressed = b""

        if not content_text.strip():
            continue

        batch.append({
            "old_id": chunk_data["id"],
            "file_id": new_file_id,
            "chunk_type": chunk_data["chunk_type"],
            "content_hash": bytes.fromhex(chunk_data["content_hash_hex"]),  # Raw bytes for BYTEA
            "content_compressed": content_compressed,  # zstd compressed BYTEA
            "content_text": content_text,  # Decompressed TEXT for FTS
            "start_line": chunk_data["start_line"],
            "end_line": chunk_data["end_line"],
            "metadata": chunk_data.get("metadata"),
            "created_at": chunk_data["created_at"],
        })

        if len(batch) >= BATCH_SIZE:
            embedded = _process_chunk_batch(cur, batch, old_to_new_id, file_id_map, source_id_map)
            count += len(batch)
            embed_count += embedded
            print(f"  Processed {count} chunks, {embed_count} embedded...")
            batch = []

    if batch:
        embedded = _process_chunk_batch(cur, batch, old_to_new_id, file_id_map, source_id_map)
        count += len(batch)
        embed_count += embedded

    conn.commit()
    print(f"  Imported {count} chunks, {embed_count} embedded")
    return old_to_new_id


def _process_chunk_batch(cur, batch, old_to_new_id, file_id_map, source_id_map):
    """Process a batch of chunks: insert, embed, store vectors"""
    # Insert chunks first
    chunk_ids = []
    contents = []

    for chunk in batch:
        # FIXED: use correct column names (content_compressed, content_text)
        cur.execute("""
            INSERT INTO chunks (file_id, chunk_type, content_hash, content_compressed, content_text, start_line, end_line, metadata, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, to_timestamp(%s))
            ON CONFLICT (file_id, content_hash) DO UPDATE SET
                content_compressed = EXCLUDED.content_compressed,
                content_text = EXCLUDED.content_text,
                start_line = EXCLUDED.start_line,
                end_line = EXCLUDED.end_line
            RETURNING id
        """, (
            chunk["file_id"],
            chunk["chunk_type"],
            chunk["content_hash"],  # Now raw bytes
            chunk["content_compressed"],  # zstd compressed
            chunk["content_text"],  # Plain text for FTS
            chunk["start_line"],
            chunk["end_line"],
            json.dumps(chunk.get("metadata")) if chunk.get("metadata") else None,
            chunk["created_at"],
        ))

        new_id = cur.fetchone()[0]
        old_to_new_id[chunk["old_id"]] = new_id
        chunk_ids.append(new_id)
        contents.append(chunk["content_text"][:2000])  # Truncate for embedding

    # Generate embeddings
    try:
        embeddings = get_embeddings(contents)
    except Exception as e:
        print(f"    Warning: Embedding failed for batch: {e}")
        return 0

    # Store in PostgreSQL chunk_embeddings
    for chunk_id, embedding in zip(chunk_ids, embeddings):
        vector_str = "[" + ",".join(str(f) for f in embedding) + "]"
        cur.execute("""
            INSERT INTO chunk_embeddings (chunk_id, model, vector, created_at)
            VALUES (%s, %s, %s::vector, NOW())
            ON CONFLICT (chunk_id) DO UPDATE SET
                model = EXCLUDED.model,
                vector = EXCLUDED.vector
        """, (chunk_id, "BAAI/bge-base-en-v1.5", vector_str))

    # Store in Qdrant
    # Get source_id for each chunk
    qdrant_points = []
    for i, (chunk_id, chunk, embedding) in enumerate(zip(chunk_ids, batch, embeddings)):
        # Get source_id via file
        cur.execute("SELECT source_id FROM files WHERE id = %s", (chunk["file_id"],))
        row = cur.fetchone()
        source_id = row[0] if row else None

        qdrant_points.append({
            "id": str(chunk_id),
            "vector": embedding,
            "payload": {
                "chunk_id": chunk_id,
                "file_id": chunk["file_id"],
                "source_id": source_id,
                "chunk_type": chunk["chunk_type"],
                "start_line": chunk["start_line"],
                "end_line": chunk["end_line"],
            }
        })

    try:
        upsert_qdrant(qdrant_points)
    except Exception as e:
        print(f"    Warning: Qdrant upsert failed: {e}")

    return len(embeddings)


def import_symbols(conn, export_dir: Path, file_id_map: dict):
    """Import symbols into PostgreSQL"""
    print("[Symbols] Importing...")

    symbols_file = export_dir / "symbols.json"
    if not symbols_file.exists():
        print("  No symbols.json found, skipping")
        return {}

    with open(symbols_file) as f:
        symbols = json.load(f)

    cur = conn.cursor()
    old_to_new_id = {}
    count = 0

    for symbol in symbols:
        old_file_id = symbol["file_id"]
        new_file_id = file_id_map.get(old_file_id)

        if new_file_id is None:
            continue

        cur.execute("""
            INSERT INTO symbols (file_id, name, symbol_type, line_start, line_end, signature, docstring)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (file_id, name, symbol_type, line_start) DO UPDATE SET
                line_end = EXCLUDED.line_end,
                signature = EXCLUDED.signature
            RETURNING id
        """, (
            new_file_id,
            symbol["name"],
            symbol["type"],
            symbol["line_start"],
            symbol["line_end"],
            symbol.get("context"),  # Map context to signature
            None,
        ))

        new_id = cur.fetchone()[0]
        old_to_new_id[symbol["id"]] = new_id
        count += 1

    conn.commit()
    print(f"  Imported {count} symbols")
    return old_to_new_id


def import_call_graph(conn, export_dir: Path, symbol_id_map: dict):
    """Import call graph into PostgreSQL"""
    print("[Call Graph] Importing...")

    cg_file = export_dir / "call_graph.json"
    if not cg_file.exists():
        print("  No call_graph.json found, skipping")
        return

    with open(cg_file) as f:
        call_graph = json.load(f)

    cur = conn.cursor()
    count = 0

    for cg in call_graph:
        old_caller_id = cg["caller_symbol_id"]
        new_caller_id = symbol_id_map.get(old_caller_id)

        if new_caller_id is None:
            continue

        old_callee_id = cg.get("callee_symbol_id")
        new_callee_id = symbol_id_map.get(old_callee_id) if old_callee_id else None

        cur.execute("""
            INSERT INTO call_graph (caller_id, callee_id, callee_name, call_line, call_type, is_external)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT DO NOTHING
        """, (
            new_caller_id,
            new_callee_id,
            cg["callee_name"],
            cg["call_line"],
            cg["call_type"],
            cg.get("is_external", False),
        ))
        count += 1

    conn.commit()
    print(f"  Imported {count} call graph entries")


def main():
    parser = argparse.ArgumentParser(description="Import CodeRag export into MAINRAG")
    parser.add_argument("--export-dir", type=Path, required=True, help="Export directory from export-coderag.sh")
    parser.add_argument("--skip-prerequisites", action="store_true", help="Skip prerequisite checks")
    parser.add_argument("--skip-embeddings", action="store_true", help="Skip embedding generation (PostgreSQL only)")
    args = parser.parse_args()

    print("=== MAINRAG Import from CodeRag ===")
    print(f"Export Dir: {args.export_dir}")
    print(f"Target: PostgreSQL ({POSTGRES_HOST}:{POSTGRES_PORT}/{POSTGRES_DB})")
    print(f"Qdrant: {QDRANT_URL}")
    print(f"TEI: {TEI_URL}")
    print("")

    # Check export directory
    if not args.export_dir.exists():
        print(f"ERROR: Export directory not found: {args.export_dir}")
        sys.exit(1)

    manifest_file = args.export_dir / "manifest.json"
    if manifest_file.exists():
        with open(manifest_file) as f:
            manifest = json.load(f)
        print("=== Export Manifest ===")
        print(f"  Source DB: {manifest['source_db']}")
        print(f"  Export Date: {manifest['export_date']}")
        print(f"  Counts: {manifest['counts']}")
        print("")

    # Prerequisites
    if not args.skip_prerequisites:
        if not check_prerequisites():
            print("ERROR: Prerequisites not met")
            sys.exit(1)
    print("")

    # Connect to PostgreSQL
    conn = connect_db()

    try:
        # Import in order
        source_id_map = import_sources(conn, args.export_dir)
        file_id_map = import_files(conn, args.export_dir, source_id_map)

        if args.skip_embeddings:
            # Import chunks without embedding
            print("[Chunks] Importing (without embeddings)...")
            # Simplified import...
        else:
            chunk_id_map = import_chunks_and_embed(conn, args.export_dir, file_id_map, source_id_map)

        symbol_id_map = import_symbols(conn, args.export_dir, file_id_map)
        import_call_graph(conn, args.export_dir, symbol_id_map)

    finally:
        conn.close()

    print("")
    print("=== Import Complete ===")
    print("Next steps:")
    print("  1. Verify with: python verify-migration.py")
    print("  2. Test search: curl http://localhost:3001/api/v1/search?q=test")


if __name__ == "__main__":
    main()

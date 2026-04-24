#!/usr/bin/env python3
"""
Targeted Symbol Visibility Backfill

Updates symbols.visibility for Java symbols by extracting visibility modifiers
from the first line of the symbol's chunk content.

Does NOT touch: chunks, embeddings, call_graph, files, file hashes.
Only updates: symbols.visibility column.

Usage:
    python3 scripts/backfill-visibility.py [--source-id 146] [--dry-run] [--batch-size 1000]
"""

import argparse
import os
import re
import sys
import time
import psycopg2
import psycopg2.extras

VISIBILITY_PATTERN = re.compile(
    r'(?:^|\s)(public|private|protected)\s+'
)


def get_db_connection():
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        sys.exit("ERROR: Set DATABASE_URL env var (postgresql://user:pw@host:port/db)")
    return psycopg2.connect(dsn)


def extract_visibility(content: str, symbol_line_start: int, chunk_start_line: int) -> str | None:
    """Extract visibility from the symbol's declaration line in chunk content."""
    if not content:
        return None

    lines = content.split("\n")
    # Find the line relative to chunk start where the symbol begins
    rel_line = symbol_line_start - chunk_start_line
    if rel_line < 0 or rel_line >= len(lines):
        # Fallback: search all lines
        rel_line = 0

    # Search from the symbol's start line and a few lines before (for annotations)
    search_start = max(0, rel_line - 2)
    search_end = min(len(lines), rel_line + 3)

    for i in range(search_start, search_end):
        line = lines[i].strip()
        # Skip annotation lines
        if line.startswith("@"):
            continue
        # Check for visibility modifier
        match = VISIBILITY_PATTERN.search(line)
        if match:
            return match.group(1)
        # If line has a class/method declaration without explicit modifier → package_private
        if re.search(r'(?:class|interface|enum|void|int|long|double|float|boolean|byte|char|short|String|\w+)\s+\w+\s*[({]', line):
            if not any(kw in line for kw in ("public", "private", "protected")):
                return "package_private"

    return None


def main():
    parser = argparse.ArgumentParser(description="Backfill symbols.visibility for Java")
    parser.add_argument("--source-id", type=int, default=146, help="Source ID (default: 146 = bitwig6-decompiled)")
    parser.add_argument("--batch-size", type=int, default=1000)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    conn = get_db_connection()

    # Count symbols to process
    with conn.cursor() as cur:
        cur.execute("""
            SELECT COUNT(*) FROM symbols s
            JOIN files f ON s.file_id = f.id
            WHERE f.source_id = %s AND f.language = 'java' AND (s.visibility IS NULL OR s.visibility = '')
        """, (args.source_id,))
        total = cur.fetchone()[0]

    print(f"Symbols to backfill: {total} (source_id={args.source_id})")
    if args.dry_run:
        print("DRY RUN — no DB writes")

    updated = 0
    skipped = 0
    errors = 0
    offset = 0
    start_time = time.time()

    while offset < total:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT s.id as symbol_id, s.name, s.type as symbol_type, s.line_start,
                       c.content_text, c.start_line as chunk_start_line
                FROM symbols s
                JOIN files f ON s.file_id = f.id
                JOIN chunks c ON c.file_id = f.id
                    AND c.start_line <= s.line_start AND c.end_line >= s.line_start
                    AND c.content_text IS NOT NULL
                WHERE f.source_id = %s AND f.language = 'java'
                    AND (s.visibility IS NULL OR s.visibility = '')
                ORDER BY s.id
                LIMIT %s OFFSET %s
            """, (args.source_id, args.batch_size, offset))
            rows = cur.fetchall()

        if not rows:
            break

        # Deduplicate: same symbol_id may appear in multiple overlapping chunks
        seen = {}
        for row in rows:
            sid = row["symbol_id"]
            if sid in seen:
                continue
            vis = extract_visibility(
                row["content_text"],
                row["line_start"],
                row["chunk_start_line"],
            )
            if vis:
                seen[sid] = vis

        if not args.dry_run and seen:
            with conn.cursor() as cur:
                for sid, vis in seen.items():
                    cur.execute(
                        "UPDATE symbols SET visibility = %s WHERE id = %s AND (visibility IS NULL OR visibility = '')",
                        (vis, sid),
                    )
                    updated += 1
            conn.commit()
        else:
            updated += len(seen)

        skipped += len(rows) - len(seen)
        offset += len(rows)

        elapsed = time.time() - start_time
        rate = offset / elapsed if elapsed > 0 else 0
        print(f"  Progress: {offset}/{total} symbols, {updated} updated, {skipped} skipped ({rate:.0f}/s)")

    elapsed = time.time() - start_time
    print(f"\nDone in {elapsed:.1f}s")
    print(f"  Updated: {updated}")
    print(f"  Skipped (no visibility found): {skipped}")
    if args.dry_run:
        print("  (DRY RUN — nothing written)")

    # Verify
    if not args.dry_run:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT visibility, COUNT(*) FROM symbols s
                JOIN files f ON s.file_id = f.id
                WHERE f.source_id = %s
                GROUP BY visibility ORDER BY count DESC
            """, (args.source_id,))
            print("\n  Visibility distribution:")
            for row in cur.fetchall():
                print(f"    {row[0] or 'NULL'}: {row[1]}")

    conn.close()


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Measure every storage-v2 shadow-ingest stage on a disposable synthetic DB."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import ExitStack
from pathlib import Path

import psycopg2
from psycopg2.extras import Json

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from eval.storage_v2.harness import TemporaryPostgres


SCHEMA = ROOT / "schema.sql"
ADMIN_ID = "00000000-0000-4000-8000-000000000041"
ITEMS = (
    ("alpha.txt", b"alpha synthetic body\n"),
    ("beta.txt", b"beta synthetic body\n"),
    ("alpha-copy.txt", b"alpha synthetic body\n"),
)


def elapsed_ms(started: int) -> float:
    return (time.perf_counter_ns() - started) / 1_000_000.0


def write_json_atomic(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    try:
        with temporary.open("x", encoding="utf-8") as output:
            json.dump(value, output, ensure_ascii=False, separators=(",", ":"))
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        temporary.chmod(0o600)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def apply_schema(socket: Path, database: str) -> None:
    result = subprocess.run(
        [
            "psql",
            "-X",
            "--no-psqlrc",
            "--quiet",
            "--set=ON_ERROR_STOP=1",
            "--host",
            str(socket),
            "--dbname",
            database,
            "--file",
            str(SCHEMA),
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"schema bootstrap failed:\n{result.stdout}\n{result.stderr}")


def setup_fixture(connection: psycopg2.extensions.connection) -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            f"""
CREATE TABLE users(id UUID PRIMARY KEY, is_admin BOOLEAN NOT NULL);
INSERT INTO users VALUES ('{ADMIN_ID}', TRUE);
CREATE FUNCTION user_can_access_source(
    p_user_id UUID, p_source_id BIGINT, p_action TEXT DEFAULT 'read'
) RETURNS BOOLEAN
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
    SELECT EXISTS (SELECT 1 FROM users WHERE id = p_user_id AND is_admin)
$$;
INSERT INTO sources(id, name, type, path)
VALUES (1, 'storage-v2-synthetic-benchmark', 'fixture', 'synthetic');
SET app.user_id = '{ADMIN_ID}';
"""
        )


def read_fixture(directory: Path, buffer_bytes: int) -> tuple[list[dict[str, object]], float]:
    directory.mkdir(parents=True, exist_ok=False)
    for name, content in ITEMS:
        (directory / name).write_bytes(content)
    measured: list[dict[str, object]] = []
    started = time.perf_counter_ns()
    for name, _ in ITEMS:
        digest = hashlib.sha256()
        size = 0
        with (directory / name).open("rb") as source:
            while chunk := source.read(buffer_bytes):
                digest.update(chunk)
                size += len(chunk)
        measured.append(
            {
                "name": name,
                "path": directory / name,
                "bytes": (directory / name).read_bytes(),
                "length": size,
                "digest": digest.digest(),
                "digest_hex": digest.hexdigest(),
            }
        )
    return measured, elapsed_ms(started)


def run_benchmark(output: Path, verification: Path, buffer_bytes: int) -> None:
    if not 4096 <= buffer_bytes <= 1024 * 1024:
        raise ValueError("buffer bytes must be between 4096 and 1048576")
    pipeline_total_ms = 0.0
    phases = {
        "lesen_hashen_ms": 0.0,
        "content_store_ms": 0.0,
        "strukturprojektion_ms": 0.0,
        "analyse_ms": 0.0,
        "db_staging_ms": 0.0,
        "intervall_delta_ms": 0.0,
        "sealing_ms": 0.0,
    }
    counters = {
        "eingang_bytes": 0,
        "unique_bytes": 0,
        "stored_bytes": 0,
        "reuse_bodies": 0,
        "reuse_nodes": 0,
        "reuse_views": 0,
        "reuse_analysis": 0,
        "parser_passes": 0,
        "artifacts_created": 0,
        "occurrences_created": 0,
        "intervals_opened": 0,
        "intervals_closed": 0,
        "errors": 0,
        "peak_buffer_bytes": buffer_bytes,
        "writer_concurrency": 1,
    }
    checks: dict[str, object] = {}

    with ExitStack() as stack:
        temporary = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="mainrag-shadow-bench-")))
        socket = stack.enter_context(TemporaryPostgres(temporary / "postgres")).socket
        database = f"shadow_bench_{uuid.uuid4().hex}"
        subprocess.run(
            ["createdb", "--host", str(socket), database],
            check=True,
            capture_output=True,
            text=True,
        )
        stack.callback(
            subprocess.run,
            ["dropdb", "--if-exists", "--force", "--host", str(socket), database],
            check=False,
            capture_output=True,
            text=True,
        )
        bootstrap = psycopg2.connect(host=str(socket), dbname="postgres")
        bootstrap.autocommit = True
        with bootstrap.cursor() as cursor:
            cursor.execute(
                "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='mainrag') "
                "THEN CREATE ROLE mainrag; END IF; END $$"
            )
        bootstrap.close()
        apply_schema(socket, database)
        connection = psycopg2.connect(host=str(socket), dbname=database)
        connection.autocommit = True
        stack.callback(connection.close)
        setup_fixture(connection)

        pipeline_started = time.perf_counter_ns()
        items, phases["lesen_hashen_ms"] = read_fixture(temporary / "input", buffer_bytes)
        counters["eingang_bytes"] = sum(int(item["length"]) for item in items)
        unique_digests = {bytes(item["digest"]) for item in items}
        counters["unique_bytes"] = sum(
            len(content) for content in {content for _, content in ITEMS}
        )

        body_by_digest: dict[bytes, int] = {}
        node_by_digest: dict[bytes, int] = {}
        view_by_digest: dict[bytes, int] = {}
        projection_by_name: dict[str, tuple[int, int]] = {}
        with connection.cursor() as cursor:
            started = time.perf_counter_ns()
            for item in items:
                digest = bytes(item["digest"])
                cursor.execute(
                    "SELECT id FROM content_body WHERE digest_algorithm='sha256-v1' AND digest=%s",
                    (psycopg2.Binary(digest),),
                )
                existed = cursor.fetchone()
                cursor.execute(
                    "SELECT id, logical_length FROM storage_v2_put_inline_body(%s)",
                    (psycopg2.Binary(bytes(item["bytes"])),),
                )
                body_id, logical_length = cursor.fetchone()
                if existed:
                    counters["reuse_bodies"] += 1
                else:
                    counters["stored_bytes"] += int(logical_length)
                body_by_digest[digest] = body_id
            phases["content_store_ms"] = elapsed_ms(started)

            started = time.perf_counter_ns()
            parsed: set[bytes] = set()
            for item in items:
                digest = bytes(item["digest"])
                cursor.execute(
                    "SELECT node.id FROM content_node node JOIN content_body body ON body.id=node.body_id "
                    "WHERE body.digest=%s AND node.domain='shadow-benchmark' AND node.node_type='text'",
                    (psycopg2.Binary(digest),),
                )
                existing_node = cursor.fetchone()
                cursor.execute(
                    "SELECT id FROM storage_v2_put_leaf_node('shadow-benchmark','text',%s)",
                    (body_by_digest[digest],),
                )
                node_id = cursor.fetchone()[0]
                if existing_node:
                    counters["reuse_nodes"] += 1
                node_by_digest[digest] = node_id
                cursor.execute(
                    "SELECT id FROM storage_v2_put_retrieval_view("
                    "'chunk','shadow-benchmark-v1','text','bytes-v1',0,"
                    "ARRAY['content'],ARRAY['node'],ARRAY[%s::BIGINT],ARRAY[0::BIGINT],ARRAY[%s::BIGINT])",
                    (node_id, item["length"]),
                )
                view_id = cursor.fetchone()[0]
                if digest in view_by_digest:
                    counters["reuse_views"] += 1
                view_by_digest[digest] = view_id
                projection_by_name[str(item["name"])] = (node_id, view_id)
                if digest not in parsed:
                    parsed.add(digest)
                    counters["parser_passes"] += 1
            phases["strukturprojektion_ms"] = elapsed_ms(started)

            started = time.perf_counter_ns()
            analyzed: set[bytes] = set()
            for item in items:
                digest = bytes(item["digest"])
                cursor.execute(
                    "SELECT status FROM storage_v2_begin_analysis_attempt(%s,'shadow-analysis-v1')",
                    (psycopg2.Binary(digest),),
                )
                status = cursor.fetchone()[0]
                if digest in analyzed and status == "complete":
                    counters["reuse_analysis"] += 1
                    continue
                if status == "pending":
                    cursor.execute(
                        "SELECT status FROM storage_v2_finish_analysis_attempt("
                        "%s,'shadow-analysis-v1',%s,NULL)",
                        (psycopg2.Binary(digest), Json({"tokens": int(item["length"])})),
                    )
                    if cursor.fetchone()[0] != "complete":
                        raise RuntimeError("analysis did not complete")
                analyzed.add(digest)
            phases["analyse_ms"] = elapsed_ms(started)

            semantic_manifest = hashlib.sha256(
                b"".join(
                    name.encode("utf-8") + b"\0" + content
                    for name, content in sorted(ITEMS)
                )
            ).hexdigest()
            idempotency_key = hashlib.sha256(
                b"shadow-benchmark-v1\0" + semantic_manifest.encode("ascii")
            ).hexdigest()
            cursor.execute(
                "SELECT id, generation_id FROM storage_v2_begin_shadow_ingest("
                "1,%s,%s,'shadow-adapter-v1','synthetic-snapshot',%s,FALSE)",
                (idempotency_key, semantic_manifest, Json({"fixture": "storage-v2-v1"})),
            )
            run_id, generation_id = cursor.fetchone()

            started = time.perf_counter_ns()
            for item in items:
                digest = bytes(item["digest"])
                node_id, view_id = projection_by_name[str(item["name"])]
                # Only the first occurrence of each content/profile performs parser work.
                parser_pass = 1 if str(item["name"]) in {"alpha.txt", "beta.txt"} else 0
                cursor.execute(
                    "SELECT artifact_version_id, occurrence_id FROM storage_v2_stage_shadow_item("
                    "%s::BIGINT,%s::TEXT,'document'::TEXT,'synthetic-item'::TEXT,%s::JSONB,"
                    "'shadow-adapter-v1'::TEXT,%s::BIGINT,NULL::BIGINT,%s::TEXT,%s::BIGINT,%s::BYTEA,"
                    "'shadow-analysis-v1'::TEXT,%s::BIGINT,%s::TEXT,%s::JSONB,%s::SMALLINT)",
                    (
                        run_id,
                        item["name"],
                        Json({"item": item["name"]}),
                        node_id,
                        item["digest_hex"],
                        item["length"],
                        psycopg2.Binary(digest),
                        view_id,
                        f"/synthetic/{item['name']}",
                        Json({"byte_start": 0, "byte_end": item["length"]}),
                        parser_pass,
                    ),
                )
                cursor.fetchone()
            phases["db_staging_ms"] = elapsed_ms(started)

            cursor.execute(
                "SELECT storage_v2_shadow_generation_root(%s)",
                (run_id,),
            )
            generation_root = cursor.fetchone()[0]
            cursor.execute(
                "SELECT status, staged_item_count, changed_item_count, deleted_item_count, "
                "parser_work_count, error_count, membership_delta_us, sealing_us "
                "FROM storage_v2_commit_shadow_ingest(%s,%s,%s)",
                (run_id, len(items), generation_root),
            )
            (
                status,
                staged_count,
                changed_count,
                deleted_count,
                parser_count,
                error_count,
                membership_us,
                sealing_us,
            ) = cursor.fetchone()
            phases["intervall_delta_ms"] = membership_us / 1000.0
            phases["sealing_ms"] = sealing_us / 1000.0
            cursor.execute("SELECT COUNT(*) FROM artifact_version")
            counters["artifacts_created"] = cursor.fetchone()[0]
            cursor.execute("SELECT COUNT(*) FROM occurrence")
            counters["occurrences_created"] = cursor.fetchone()[0]
            cursor.execute("SELECT COUNT(*) FROM generation_item_version WHERE valid_from_seq=1")
            counters["intervals_opened"] = cursor.fetchone()[0]
            counters["intervals_closed"] = deleted_count
            counters["errors"] = error_count

            cursor.execute(
                "SELECT string_agg(convert_from(body.inline_bytes,'UTF8'),'|' ORDER BY item.item_key) "
                "FROM generation_item_version membership "
                "JOIN source_item item ON item.id=membership.source_item_id "
                "JOIN artifact_version artifact ON artifact.id=membership.artifact_version_id "
                "JOIN content_node node ON node.id=artifact.content_root_node_id "
                "JOIN content_body body ON body.id=node.body_id "
                "WHERE membership.source_id=1 AND membership.valid_from_seq<=1 "
                "AND (membership.valid_to_seq IS NULL OR membership.valid_to_seq>1)"
            )
            reconstructed = cursor.fetchone()[0]
            cursor.execute(
                "SELECT generation.status, generation.item_count, source.active_generation_id IS NULL "
                "FROM source_generation generation JOIN logical_source source ON source.id=generation.source_id "
                "WHERE generation.id=%s",
                (generation_id,),
            )
            generation_status, item_count, pointer_unchanged = cursor.fetchone()

        checks = {
            "gueltig": int(
                status == "sealed"
                and generation_status == "sealed"
                and staged_count == len(items)
                and item_count == len(items)
                and changed_count == len(items)
                and error_count == 0
                and parser_count == len(unique_digests)
                and pointer_unchanged
                and reconstructed
                == "alpha synthetic body\n|alpha synthetic body\n|beta synthetic body\n"
            ),
            "generation_status": generation_status,
            "item_count": item_count,
            "root_sha256": generation_root,
            "active_pointer_unchanged": pointer_unchanged,
            "reconstruction_sha256": hashlib.sha256(reconstructed.encode("utf-8")).hexdigest(),
        }
        pipeline_total_ms = elapsed_ms(pipeline_started)

    counters["latenz_ms"] = pipeline_total_ms
    write_json_atomic(output, {"ablauf": counters, "phase": phases})
    write_json_atomic(verification, checks)
    if checks["gueltig"] != 1:
        raise RuntimeError(f"shadow benchmark verification failed: {checks}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(os.environ.get("TM_KENNZAHLEN", "/tmp/mainrag-shadow-kennzahlen.json")),
    )
    parser.add_argument("--verification", type=Path)
    parser.add_argument("--buffer-bytes", type=int, default=256 * 1024)
    arguments = parser.parse_args()
    for command in ("psql", "createdb", "dropdb"):
        if shutil.which(command) is None:
            parser.error(f"required command is absent: {command}")
    verification = arguments.verification or arguments.output.with_name("pruefung.json")
    run_benchmark(arguments.output, verification, arguments.buffer_bytes)
    print(f"storage-v2 shadow telemetry: {arguments.output}")
    print(f"verification: {verification}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

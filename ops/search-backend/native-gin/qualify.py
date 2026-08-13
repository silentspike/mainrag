#!/usr/bin/env python3
"""Qualify MainRAG's built-in PostgreSQL GIN backend in disposable clusters."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[3]
HERE = Path(__file__).resolve().parent
LOCK_PATH = HERE / "backend.lock.json"
EVIDENCE_SCHEMA = HERE / "evidence.schema.json"
PROTOTYPE = ROOT / "eval" / "storage_v2" / "topk" / "prototype.py"
SHADOW_TEST = (
    "eval.storage_v2.schema.test_shadow_ingest_schema."
    "ShadowIngestSchemaTests.test_exact_retrieval_composes_views_and_fails_closed"
)


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def run(
    arguments: list[str],
    *,
    env: dict[str, str] | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        arguments,
        cwd=ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    if check and result.returncode != 0:
        command = Path(arguments[0]).name
        raise RuntimeError(f"{command} failed with exit code {result.returncode}")
    return result


def require_file_hash(path: Path, expected: str) -> None:
    actual = sha256_file(path)
    if actual != expected:
        raise RuntimeError(f"locked input changed: {path.relative_to(ROOT)}")


def plan_uses_index(plan: Any, index_name: str) -> bool:
    if isinstance(plan, dict):
        if plan.get("Index Name") == index_name:
            return True
        return any(plan_uses_index(value, index_name) for value in plan.values())
    if isinstance(plan, list):
        return any(plan_uses_index(value, index_name) for value in plan)
    return False


class DisposableCluster:
    def __init__(self, bindir: Path, root: Path) -> None:
        self.bindir = bindir
        self.root = root
        self.data = root / "data"
        self.socket = root / "socket"
        self.log = root / "server.log"
        self.started = False

    def binary(self, name: str) -> str:
        return str(self.bindir / name)

    def initialize(self) -> None:
        self.socket.mkdir(parents=True)
        run(
            [
                self.binary("initdb"),
                "--pgdata",
                str(self.data),
                "--auth=trust",
                "--no-locale",
                "--encoding=UTF8",
                "--data-checksums",
            ]
        )
        socket_value = str(self.socket).replace("'", "''")
        with (self.data / "postgresql.conf").open("a", encoding="utf-8") as handle:
            handle.write("\n# MainRAG disposable native-GIN qualification\n")
            handle.write("listen_addresses = ''\n")
            handle.write(f"unix_socket_directories = '{socket_value}'\n")
            handle.write("shared_preload_libraries = ''\n")
            handle.write("fsync = on\n")
            handle.write("synchronous_commit = on\n")
            handle.write("full_page_writes = on\n")

    def start(self) -> None:
        run(
            [
                self.binary("pg_ctl"),
                "--pgdata",
                str(self.data),
                "--log",
                str(self.log),
                "--wait",
                "start",
            ]
        )
        self.started = True

    def stop(self, mode: str = "fast") -> None:
        if not self.started:
            return
        run(
            [
                self.binary("pg_ctl"),
                "--pgdata",
                str(self.data),
                "--wait",
                "--mode",
                mode,
                "stop",
            ]
        )
        self.started = False

    def sql(self, statement: str) -> str:
        return run(
            [
                self.binary("psql"),
                "-X",
                "--no-psqlrc",
                "--quiet",
                "--set=ON_ERROR_STOP=1",
                "--tuples-only",
                "--no-align",
                "--host",
                str(self.socket),
                "--dbname",
                "postgres",
                "--command",
                statement,
            ]
        ).stdout.strip()

    def environment(self) -> dict[str, str]:
        environment = os.environ.copy()
        environment["PATH"] = str(self.bindir) + os.pathsep + environment.get("PATH", "")
        environment["STORAGE_V2_TEST_SOCKET"] = str(self.socket)
        return environment


def qualify_runtime(bindir: Path, commit_sha: str) -> tuple[dict[str, Any], str]:
    gates: dict[str, Any] = {}
    with tempfile.TemporaryDirectory(prefix="mainrag-native-gin-") as temporary:
        temporary_root = Path(temporary)
        cluster = DisposableCluster(bindir, temporary_root / "cluster")
        cluster.root.mkdir()
        cluster.initialize()
        cluster.start()
        try:
            preload = cluster.sql("SHOW shared_preload_libraries;")
            if preload:
                raise RuntimeError("native GIN qualification unexpectedly loaded a preload library")
            gates["preload"] = {
                "status": "N/A",
                "detail": "built-in GIN needs no extension or preload library; empty setting verified",
            }

            cluster.sql(
                """
CREATE TABLE qualification_docs(
    id INTEGER PRIMARY KEY,
    body TEXT NOT NULL,
    fts TSVECTOR GENERATED ALWAYS AS (to_tsvector('simple', body)) STORED
);
INSERT INTO qualification_docs(id, body) VALUES
    (1, 'alpha beta'), (2, 'alpha gamma'),
    (3, 'beta alpha exact'), (4, 'decoy only');
CREATE INDEX qualification_docs_fts_gin ON qualification_docs USING GIN (fts);
"""
            )
            expected = "1,3"
            exact = cluster.sql(
                "SELECT string_agg(id::TEXT, ',' ORDER BY id) FROM qualification_docs "
                "WHERE fts @@ to_tsquery('simple', 'alpha & beta');"
            )
            if exact != expected:
                raise RuntimeError("native GIN fixture returned a non-reference result")
            plan = json.loads(
                cluster.sql(
                    "SET enable_seqscan = off; EXPLAIN (FORMAT JSON) "
                    "SELECT id FROM qualification_docs "
                    "WHERE fts @@ to_tsquery('simple', 'alpha & beta');"
                )
            )
            if not plan_uses_index(plan, "qualification_docs_fts_gin"):
                raise RuntimeError("forced query plan did not use the qualified GIN index")
            gates["final_index_shape"] = {
                "status": "PASS",
                "detail": "generated tsvector plus built-in GIN returned the exact synthetic identity set",
                "count": 2,
                "sha256": sha256_bytes(exact.encode()),
            }

            cluster.sql(
                """
CREATE TABLE qualification_interrupt(
    id BIGINT PRIMARY KEY,
    body TEXT NOT NULL,
    fts TSVECTOR GENERATED ALWAYS AS (to_tsvector('simple', body)) STORED
);
INSERT INTO qualification_interrupt(id, body)
SELECT value, 'alpha ' || md5(value::TEXT) || ' beta ' || md5((value + 1)::TEXT)
  FROM generate_series(1, 10000) AS value;
CREATE FUNCTION qualification_slow_fts(value TSVECTOR)
RETURNS TSVECTOR
LANGUAGE plpgsql
IMMUTABLE
STRICT
AS $$
BEGIN
    PERFORM pg_sleep(0.001);
    RETURN value;
END;
$$;
"""
            )
            interrupt = subprocess.Popen(
                [
                    cluster.binary("psql"),
                    "-X",
                    "--no-psqlrc",
                    "--set=ON_ERROR_STOP=1",
                    "--host",
                    str(cluster.socket),
                    "--dbname",
                    "postgres",
                    "--command",
                    "SET application_name='mainrag_qualification_interrupt'; "
                    "CREATE INDEX CONCURRENTLY qualification_interrupted_gin "
                    "ON qualification_interrupt USING GIN (qualification_slow_fts(fts));",
                ],
                cwd=ROOT,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                text=True,
            )
            backend_pid: int | None = None
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline and interrupt.poll() is None:
                value = cluster.sql(
                    "SELECT pid FROM pg_stat_activity "
                    "WHERE application_name = 'mainrag_qualification_interrupt' "
                    "AND query LIKE '%CREATE INDEX%' ORDER BY pid LIMIT 1;"
                )
                if value:
                    backend_pid = int(value)
                    progress = cluster.sql(
                        f"SELECT COUNT(*) FROM pg_stat_progress_create_index WHERE pid = {backend_pid};"
                    )
                    if progress == "1":
                        break
                time.sleep(0.02)
            if backend_pid is None or interrupt.poll() is not None:
                interrupt.kill()
                interrupt.wait(timeout=5)
                raise RuntimeError("concurrent GIN build completed before interruption gate")
            if cluster.sql(f"SELECT pg_cancel_backend({backend_pid});") != "t":
                raise RuntimeError("failed to cancel the concurrent GIN build")
            interrupt.wait(timeout=30)
            if interrupt.returncode == 0:
                raise RuntimeError("interrupted GIN build unexpectedly succeeded")
            invalid_count = int(
                cluster.sql(
                    "SELECT COUNT(*) FROM pg_index index_row "
                    "JOIN pg_class relation ON relation.oid = index_row.indexrelid "
                    "WHERE relation.relname = 'qualification_interrupted_gin' "
                    "AND (NOT index_row.indisvalid OR NOT index_row.indisready);"
                )
            )
            if invalid_count != 1:
                raise RuntimeError("canceled concurrent GIN build left no single invalid artifact")
            gates["interrupted_build"] = {
                "status": "PASS",
                "detail": "throttled concurrent build was canceled and one invalid catalog artifact was detected",
                "count": invalid_count,
            }
            cluster.sql(
                "DROP INDEX IF EXISTS qualification_interrupted_gin; "
                "DROP FUNCTION qualification_slow_fts(TSVECTOR); "
                "CREATE INDEX qualification_interrupted_gin "
                "ON qualification_interrupt USING GIN (fts);"
            )
            remaining_invalid = cluster.sql(
                "SELECT COUNT(*) FROM pg_index index_row "
                "JOIN pg_class relation ON relation.oid = index_row.indexrelid "
                "WHERE relation.relname LIKE 'qualification_interrupt%gin%' "
                "AND (NOT index_row.indisvalid OR NOT index_row.indisready OR NOT index_row.indislive);"
            )
            if remaining_invalid != "0":
                raise RuntimeError("partial or invalid GIN artifact remained after rebuild")
            gates["orphan_cleanup"] = {
                "status": "PASS",
                "detail": "controlled drop and rebuild left no invalid, unready, or dead index",
                "count": 0,
            }

            cluster.sql(
                "CREATE TABLE qualification_wal(marker TEXT PRIMARY KEY); "
                "INSERT INTO qualification_wal VALUES ('committed-before-immediate-stop'); "
                "CHECKPOINT;"
            )
            cluster.stop("immediate")
            cluster.start()
            marker = cluster.sql("SELECT marker FROM qualification_wal;")
            if marker != "committed-before-immediate-stop":
                raise RuntimeError("committed WAL marker was absent after immediate restart")
            post_restart = cluster.sql(
                "SELECT string_agg(id::TEXT, ',' ORDER BY id) FROM qualification_docs "
                "WHERE fts @@ to_tsquery('simple', 'alpha & beta');"
            )
            if post_restart != expected:
                raise RuntimeError("GIN result changed after immediate restart")
            flags = cluster.sql(
                "SELECT index_row.indisvalid::INT || index_row.indisready::INT || "
                "index_row.indislive::INT FROM pg_index index_row "
                "JOIN pg_class relation ON relation.oid = index_row.indexrelid "
                "WHERE relation.relname = 'qualification_docs_fts_gin';"
            )
            if flags != "111":
                raise RuntimeError("qualified GIN index is not valid, ready, and live after restart")
            gates["wal_crash_restart"] = {
                "status": "PASS",
                "detail": "immediate stop recovered committed data and identical GIN results",
                "sha256": sha256_bytes(post_restart.encode()),
            }

            cluster.stop("fast")
            checksum = run(
                [
                    cluster.binary("pg_checksums"),
                    "--check",
                    "--pgdata",
                    str(cluster.data),
                ]
            )
            if "Checksum operation completed" not in checksum.stdout:
                raise RuntimeError("offline page checksum verification did not complete")
            gates["page_integrity"] = {
                "status": "PASS",
                "detail": "offline checksums covered the checksummed disposable data directory",
            }
            cluster.start()
            cluster.sql("REINDEX INDEX qualification_docs_fts_gin;")
            reindexed = cluster.sql(
                "SELECT string_agg(id::TEXT, ',' ORDER BY id) FROM qualification_docs "
                "WHERE fts @@ to_tsquery('simple', 'alpha & beta');"
            )
            if reindexed != expected:
                raise RuntimeError("reference result changed after native REINDEX")
            gates["native_reindex"] = {
                "status": "PASS",
                "detail": "native REINDEX completed and preserved the exact result identity",
            }

            shadow = run(
                [sys.executable, "-m", "unittest", SHADOW_TEST],
                env=cluster.environment(),
            )
            if "Ran 1 test" not in shadow.stderr or "OK" not in shadow.stderr:
                raise RuntimeError("final storage-v2 schema retrieval integration did not pass")
            gates["final_storage_v2_schema"] = {
                "status": "PASS",
                "detail": "final migration and occurrence-scoped retrieval test passed on PostgreSQL 18.4",
                "count": 1,
            }

            prototype_output = temporary_root / "prototype.json"
            run(
                [
                    sys.executable,
                    str(PROTOTYPE),
                    "--commit-sha",
                    commit_sha,
                    "--output",
                    str(prototype_output),
                ],
                env=cluster.environment(),
            )
            prototype = json.loads(prototype_output.read_text(encoding="utf-8"))
            if prototype["status"] != "PASS" or prototype["backend"]["version"] != "18.4":
                raise RuntimeError("frozen Top-K prototype did not pass on PostgreSQL 18.4")
            gates["frozen_exact_topk"] = {
                "status": "PASS",
                "detail": "all frozen queries matched exhaustive Top-10 without candidate truncation",
                "count": prototype["inputs"]["queries"],
                "sha256": prototype["aggregate"]["result_identity_sha256"],
            }
        finally:
            cluster.stop("fast")
    return gates, "temporary cluster and temporary prototype evidence removed"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bindir", type=Path, required=True)
    parser.add_argument("--commit-sha", required=True)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()

    if not re.fullmatch(r"[0-9a-f]{40}", arguments.commit_sha):
        parser.error("--commit-sha must be a full lowercase Git SHA")
    bindir = arguments.bindir.resolve()
    required_binaries = [
        "postgres",
        "pg_config",
        "initdb",
        "pg_ctl",
        "psql",
        "createdb",
        "dropdb",
        "pg_checksums",
    ]
    for name in required_binaries:
        if not (bindir / name).is_file():
            parser.error(f"target bindir lacks {name}")

    lock = json.loads(LOCK_PATH.read_text(encoding="utf-8"))
    inputs = lock["inputs"]
    locked_paths = {
        "cargo_lock_sha256": ROOT / "Cargo.lock",
        "schema_sha256": ROOT / "schema.sql",
        "retrieval_migration_sha256": ROOT / "migrations" / "034_storage_v2_retrieval.sql",
        "prototype_fixtures_sha256": ROOT / "eval" / "storage_v2" / "topk" / "fixtures.json",
        "prototype_queries_sha256": ROOT / "eval" / "storage_v2" / "topk" / "queries.jsonl",
        "prototype_result_sha256": ROOT / "eval" / "storage_v2" / "topk" / "results" / "native-gin.json",
        "prototype_schema_sha256": ROOT / "eval" / "storage_v2" / "topk" / "artifact.schema.json",
    }
    for key, path in locked_paths.items():
        require_file_hash(path, inputs[key])

    version_output = run([str(bindir / "postgres"), "--version"]).stdout.strip()
    match = re.fullmatch(r"postgres \(PostgreSQL\) ([0-9]+\.[0-9]+)", version_output)
    if match is None or match.group(1) != lock["postgresql"]["version"]:
        raise RuntimeError("qualification requires the locked PostgreSQL 18.4 binary")
    dependencies = run(["ldd", str(bindir / "postgres")]).stdout
    if "not found" in dependencies:
        raise RuntimeError("postgres binary has an unresolved shared-library dependency")
    symbols = run(["nm", str(bindir / "postgres")]).stdout
    if not re.search(r"\bginhandler\b", symbols):
        raise RuntimeError("postgres binary does not expose the built-in GIN handler symbol")
    configure = run([str(bindir / "pg_config"), "--configure"]).stdout.strip()
    for flag in lock["postgresql"]["configure_flags"]:
        if flag not in configure:
            raise RuntimeError(f"target PostgreSQL build lacks locked configure flag {flag}")
    compiler = run([str(bindir / "pg_config"), "--cc"]).stdout.strip()

    gates, cleanup_detail = qualify_runtime(bindir, arguments.commit_sha)
    gates = {
        "locked_inputs": {
            "status": "PASS",
            "detail": "all public schema, lock, prototype, and dependency inputs matched SHA-256",
            "count": len(locked_paths),
        },
        "binary_dependencies": {
            "status": "PASS",
            "detail": "postgres had no unresolved dynamic dependency and contained ginhandler",
        },
        "package_file_ownership": {
            "status": "N/A",
            "detail": "built-in GIN installs no extension package or backend-owned shared object",
        },
        "extension_create_remove": {
            "status": "N/A",
            "detail": "built-in GIN has no CREATE EXTENSION, DROP EXTENSION, or preload lifecycle",
        },
        **gates,
    }
    artifact_digest = sha256_bytes(
        bytes.fromhex(sha256_file(bindir / "postgres"))
        + bytes.fromhex(inputs["retrieval_migration_sha256"])
    )
    evidence = {
        "schema_version": "mainrag-native-gin-evidence/v1",
        "status": "PASS",
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "subject": {
            "commit_sha": arguments.commit_sha,
            "backend": "PostgreSQL built-in GIN",
            "backend_version": "18.4",
            "postgresql_version": "18.4",
        },
        "inputs": inputs,
        "toolchain": {
            "postgres_binary_sha256": sha256_file(bindir / "postgres"),
            "configure_sha256": sha256_bytes(configure.encode()),
            "compiler": compiler,
        },
        "package": {
            "format": "none-built-in",
            "artifact_digest": artifact_digest,
            "extension": None,
            "preload_required": False,
            "reproducibility": (
                "N/A: built-in backend; locked PostgreSQL source and schema inputs are qualified"
            ),
        },
        "gates": gates,
        "cleanup": {
            "status": "PASS",
            "temporary_cluster_removed": True,
            "temporary_evidence_removed": True,
        },
        "limitations": [
            "Disposable crash/restart evidence is not backup restore, PITR, HA, or production availability evidence.",
            "Built-in GIN has no amcheck operator class; checksums, catalog flags, forced plans, exact reads, and REINDEX form the native integrity gate.",
            "No production install, preload change, restart, reindex, deployment, or active-pointer change was performed.",
            cleanup_detail,
        ],
    }
    try:
        import jsonschema

        jsonschema.validate(evidence, json.loads(EVIDENCE_SCHEMA.read_text(encoding="utf-8")))
    except ImportError as error:
        raise RuntimeError("jsonschema is required to validate qualification evidence") from error
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"PASS: {len(gates)} native-GIN qualification gates")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

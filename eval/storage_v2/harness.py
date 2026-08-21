#!/usr/bin/env python3
"""Run the public MainRAG current-path storage-v2 fixture baseline."""

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
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
EVAL_ROOT = ROOT / "eval"
sys.path.insert(0, str(EVAL_ROOT))

from eval_common import percentile, recall_at_k, reciprocal_rank  # noqa: E402
from storage_v2.check_writers import check as check_writers  # noqa: E402


HERE = Path(__file__).resolve().parent
CORPUS = HERE / "fixtures" / "corpus"
QUERIES = HERE / "fixtures" / "queries.jsonl"
SCHEMA = HERE / "manifest.schema.json"
STATUS_VALUES = {"PASS", "FAIL", "BLOCKED", "SKIP", "NOT_RUN"}
QUERY_CONSTRUCTS = {
    "and",
    "or",
    "not",
    "phrase",
    "group",
    "exact_identifier",
    "adverse",
    "common_term",
    "negative",
}


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def git_output(*args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def canonical_corpus_hash(documents: list[tuple[str, bytes]]) -> str:
    digest = hashlib.sha256()
    for path, content in documents:
        encoded_path = path.encode("utf-8")
        digest.update(len(encoded_path).to_bytes(8, "big"))
        digest.update(encoded_path)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def load_documents(corpus: Path = CORPUS) -> list[tuple[str, bytes]]:
    documents = [
        (path.relative_to(corpus).as_posix(), path.read_bytes())
        for path in sorted(corpus.rglob("*"))
        if path.is_file()
    ]
    if not documents:
        raise ValueError("fixture corpus contains zero documents")
    if len({path for path, _ in documents}) != len(documents):
        raise ValueError("fixture corpus contains duplicate paths")
    return documents


def load_queries(path: Path = QUERIES) -> list[dict[str, Any]]:
    queries: list[dict[str, Any]] = []
    seen: set[str] = set()
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        try:
            query = json.loads(line)
        except json.JSONDecodeError as error:
            raise ValueError(f"invalid query JSON on line {line_number}") from error
        required = {"id", "construct", "query", "phrase", "k", "expected"}
        missing = required - set(query)
        if missing:
            raise ValueError(f"query line {line_number} lacks {sorted(missing)}")
        if query["id"] in seen:
            raise ValueError(f"duplicate query id: {query['id']}")
        if query["construct"] not in QUERY_CONSTRUCTS:
            raise ValueError(f"unsupported query construct: {query['construct']}")
        if not isinstance(query["query"], str) or not query["query"].strip():
            raise ValueError(f"query {query['id']} is empty")
        if query["k"] != 10:
            raise ValueError(f"query {query['id']} must use exact Top-10")
        if not isinstance(query["expected"], list):
            raise ValueError(f"query {query['id']} has invalid expected results")
        seen.add(query["id"])
        queries.append(query)
    if not queries:
        raise ValueError("query suite contains zero queries")
    return queries


def sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def latency_summary(samples: list[float]) -> dict[str, float | int]:
    if not samples:
        raise ValueError("latency sample is empty")
    return {
        "samples": len(samples),
        "p50_ms": round(percentile(samples, 50), 3),
        "p95_ms": round(percentile(samples, 95), 3),
        "p99_ms": round(percentile(samples, 99), 3),
        "min_ms": round(min(samples), 3),
        "max_ms": round(max(samples), 3),
    }


@dataclass
class TemporaryPostgres:
    root: Path

    def __post_init__(self) -> None:
        self.data = self.root / "data"
        self.socket = self.root / "socket"
        self.log = self.root / "postgres.log"
        self.started = False

    def __enter__(self) -> "TemporaryPostgres":
        self.socket.mkdir(parents=True)
        subprocess.run(
            [
                "initdb",
                "--pgdata",
                str(self.data),
                "--auth=trust",
                "--no-locale",
                "--encoding=UTF8",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            [
                "pg_ctl",
                "--pgdata",
                str(self.data),
                "--log",
                str(self.log),
                "--options",
                f"-F -h '' -k {self.socket}",
                "--wait",
                "start",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        self.started = True
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        if self.started:
            subprocess.run(
                ["pg_ctl", "--pgdata", str(self.data), "--wait", "stop", "--mode=fast"],
                check=False,
                capture_output=True,
                text=True,
            )
            self.started = False

    def sql(self, statement: str) -> str:
        result = subprocess.run(
            [
                "psql",
                "-X",
                "--no-psqlrc",
                "--set=ON_ERROR_STOP=1",
                "--tuples-only",
                "--no-align",
                "--host",
                str(self.socket),
                "--dbname",
                "postgres",
                "--command",
                statement,
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout.strip()

    def session(self) -> "PsqlSession":
        return PsqlSession(self.socket)


class PsqlSession:
    """One persistent psql connection with a marker-delimited request protocol."""

    def __init__(self, socket: Path) -> None:
        self.socket = socket
        self.process: subprocess.Popen[str] | None = None

    def __enter__(self) -> "PsqlSession":
        self.process = subprocess.Popen(
            [
                "psql",
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
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        if self.process is None:
            return
        if self.process.stdin is not None:
            self.process.stdin.write("\\quit\n")
            self.process.stdin.flush()
            self.process.stdin.close()
        self.process.wait(timeout=5)
        self.process = None

    def sql(self, statement: str) -> tuple[str, float]:
        if self.process is None or self.process.stdin is None or self.process.stdout is None:
            raise RuntimeError("psql session is not running")
        marker = uuid.uuid4().hex
        start_marker = f"__MAINRAG_START_{marker}__"
        end_marker = f"__MAINRAG_END_{marker}__"
        self.process.stdin.write(f"\\echo {start_marker}\n")
        self.process.stdin.flush()

        while True:
            line = self.process.stdout.readline()
            if line == "":
                raise RuntimeError("psql session ended before completing a query")
            if line.rstrip("\n") == start_marker:
                break

        started_ns = time.monotonic_ns()
        self.process.stdin.write(statement.rstrip() + "\n")
        self.process.stdin.write(f"\\echo {end_marker}\n")
        self.process.stdin.flush()
        lines: list[str] = []
        while True:
            line = self.process.stdout.readline()
            if line == "":
                raise RuntimeError("psql session ended before completing a query")
            value = line.rstrip("\n")
            if value == end_marker:
                elapsed_ms = (time.monotonic_ns() - started_ns) / 1_000_000
                return "\n".join(lines).strip(), elapsed_ms
            lines.append(value)


def setup_database(database: TemporaryPostgres, documents: list[tuple[str, bytes]]) -> dict[str, Any]:
    database.sql(
        """
        CREATE TABLE documents (
            id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            path TEXT NOT NULL,
            content_sha256 TEXT NOT NULL,
            body TEXT NOT NULL,
            fts_simple TSVECTOR GENERATED ALWAYS AS
                (to_tsvector('simple', body)) STORED,
            fts_english TSVECTOR GENERATED ALWAYS AS
                (to_tsvector('english', body)) STORED,
            UNIQUE (path, content_sha256)
        );
        CREATE INDEX documents_fts_simple ON documents USING GIN (fts_simple);
        CREATE INDEX documents_fts_english ON documents USING GIN (fts_english);
        """
    )
    values = []
    source_bytes = 0
    for path, content_bytes in documents:
        content = content_bytes.decode("utf-8")
        source_bytes += len(content_bytes)
        values.append(
            f"({sql_literal(path)}, {sql_literal(sha256_bytes(content_bytes))}, {sql_literal(content)})"
        )
    insert = (
        "INSERT INTO documents (path, content_sha256, body) VALUES "
        + ",".join(values)
        + " ON CONFLICT (path, content_sha256) DO NOTHING;"
    )
    started = time.monotonic_ns()
    database.sql(insert)
    parsed_items = int(database.sql("SELECT COUNT(*) FROM documents;"))
    database.sql(insert)
    elapsed_ms = (time.monotonic_ns() - started) / 1_000_000
    unchanged_items = parsed_items
    database_bytes = int(database.sql("SELECT pg_total_relation_size('documents');"))
    database.sql("ANALYZE documents;")
    return {
        "status": "PASS",
        "source_bytes_read": source_bytes,
        "content_bytes_stored": source_bytes,
        "parsed_items": parsed_items,
        "unchanged_items_reused": unchanged_items,
        "errors": 0,
        "elapsed_ms": round(elapsed_ms, 3),
        "database_bytes_after_ingest": database_bytes,
    }


def query_sql(query_text: str, phrase: bool) -> str:
    constructor = "phraseto_tsquery" if phrase else "websearch_to_tsquery"
    query = sql_literal(query_text)
    return f"""
        WITH q AS (
            SELECT
                {constructor}('simple', {query}) AS simple_q,
                {constructor}('english', {query}) AS english_q
        ),
        channel AS (
            SELECT d.id, d.path,
                   ts_rank_cd(d.fts_simple, q.simple_q, 1)::double precision AS score
            FROM documents d CROSS JOIN q
            WHERE d.fts_simple @@ q.simple_q
            UNION ALL
            SELECT d.id, d.path,
                   (ts_rank_cd(d.fts_english, q.english_q, 1) * 0.8)::double precision AS score
            FROM documents d CROSS JOIN q
            WHERE d.fts_english @@ q.english_q
        ),
        grouped AS (
            SELECT id, path, MAX(score) AS score
            FROM channel
            GROUP BY id, path
        ),
        top_results AS (
            SELECT id, path, score
            FROM grouped
            ORDER BY score DESC, path ASC, id ASC
            LIMIT 10
        )
        SELECT json_build_object(
            'matched_documents', (SELECT COUNT(*) FROM grouped),
            'scored_channel_rows', (SELECT COUNT(*) FROM channel),
            'results', COALESCE(
                (SELECT json_agg(path ORDER BY score DESC, path ASC, id ASC) FROM top_results),
                '[]'::json
            )
        );
    """


def execute_query(database: PsqlSession, query: dict[str, Any]) -> tuple[dict[str, Any], float]:
    raw, elapsed_ms = database.sql(query_sql(query["query"], query["phrase"]))
    return json.loads(raw), elapsed_ms


def evaluate_queries(
    database: PsqlSession,
    queries: list[dict[str, Any]],
    warmups: int,
    iterations: int,
) -> dict[str, Any]:
    if warmups < 1:
        raise ValueError("at least one warmup is required")
    if iterations < 30:
        raise ValueError("at least 30 measured iterations per query are required")

    query_results: list[dict[str, Any]] = []
    aggregate_cold: list[float] = []
    aggregate_warm: list[float] = []
    identity: list[dict[str, Any]] = []

    for query in queries:
        first, cold_ms = execute_query(database, query)
        aggregate_cold.append(cold_ms)
        for _ in range(warmups):
            execute_query(database, query)
        warm_samples: list[float] = []
        measured_result = first
        deterministic = True
        for _ in range(iterations):
            current, elapsed_ms = execute_query(database, query)
            warm_samples.append(elapsed_ms)
            aggregate_warm.append(elapsed_ms)
            if current != first:
                deterministic = False
            measured_result = current

        results = measured_result["results"]
        recall = recall_at_k(results, query["expected"], 10)
        rr = reciprocal_rank(results, query["expected"], 10)
        status = "PASS" if deterministic and recall == 1.0 else "FAIL"
        result = {
            "id": query["id"],
            "construct": query["construct"],
            "query_sha256": sha256_bytes(query["query"].encode("utf-8")),
            "status": status,
            "expected": query["expected"],
            "exact_top_10": results,
            "recall_at_10": round(recall, 6),
            "reciprocal_rank": round(rr, 6),
            "matched_documents": int(measured_result["matched_documents"]),
            "scored_channel_rows": int(measured_result["scored_channel_rows"]),
            "returned_shortlist": len(results),
            "cold_first_ms": round(cold_ms, 3),
            "warm_latency": latency_summary(warm_samples),
        }
        query_results.append(result)
        identity.append({"id": query["id"], "results": results})

    query_count = len(query_results)
    return {
        "status": "PASS" if all(item["status"] == "PASS" for item in query_results) else "FAIL",
        "query_count": query_count,
        "recall_at_10": round(sum(item["recall_at_10"] for item in query_results) / query_count, 6),
        "mrr_at_10": round(sum(item["reciprocal_rank"] for item in query_results) / query_count, 6),
        "result_identity_sha256": sha256_bytes(
            json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ),
        "matched_documents_total": sum(item["matched_documents"] for item in query_results),
        "scored_channel_rows_total": sum(item["scored_channel_rows"] for item in query_results),
        "returned_shortlist_total": sum(item["returned_shortlist"] for item in query_results),
        "cold_first_latency": latency_summary(aggregate_cold),
        "warm_latency": latency_summary(aggregate_warm),
        "queries": query_results,
    }


def ensure_public_manifest(value: Any, path: str = "$") -> None:
    """Reject local paths, endpoints, or credential-shaped keys in output."""
    if isinstance(value, dict):
        for key, child in value.items():
            lowered = key.lower()
            if any(token in lowered for token in ("token", "password", "secret", "hostname", "address")):
                raise ValueError(f"private field name in manifest: {path}.{key}")
            ensure_public_manifest(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            ensure_public_manifest(child, f"{path}[{index}]")
    elif isinstance(value, str):
        if Path(value).is_absolute() or "localhost" in value or "127.0.0.1" in value:
            raise ValueError(f"local operational value in manifest: {path}")


def validate_manifest(manifest: dict[str, Any], schema_path: Path = SCHEMA) -> None:
    try:
        import jsonschema
    except ImportError as error:
        raise RuntimeError("jsonschema is required to validate the baseline manifest") from error
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker()).validate(manifest)
    ensure_public_manifest(manifest)
    if manifest["status"] not in STATUS_VALUES:
        raise ValueError("invalid aggregate status")


def build_manifest(
    documents: list[tuple[str, bytes]],
    queries: list[dict[str, Any]],
    maintenance_gate: dict[str, Any],
    ingest: dict[str, Any],
    search: dict[str, Any],
    backend_version: str,
    code_sha: str,
    harness_commit: str,
    warmups: int,
    iterations: int,
) -> dict[str, Any]:
    corpus_hash = canonical_corpus_hash(documents)
    query_hash = sha256_file(QUERIES)
    run_identity = sha256_bytes(
        f"{code_sha}\0{harness_commit}\0{corpus_hash}\0{query_hash}".encode("utf-8")
    )[:16]
    overall = "PASS"
    if any(section.get("status") != "PASS" for section in (maintenance_gate, ingest, search)):
        overall = "FAIL"
    return {
        "schema_version": "storage-v2-baseline/v1",
        "run_id": f"fixture-current-{run_identity}",
        "status": overall,
        "recorded_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "subject": {
            "adapter": "current-postgresql-fts-fixture-v1",
            "code_sha": code_sha,
            "harness_commit": harness_commit,
            "schema_sha256": sha256_file(ROOT / "schema.sql"),
        },
        "inputs": {
            "corpus_sha256": corpus_hash,
            "corpus_items": len(documents),
            "query_set_sha256": query_hash,
            "query_count": len(queries),
        },
        "configuration": {
            "backend": "PostgreSQL native FTS",
            "backend_version": backend_version,
            "query_semantics": "dual simple/english websearch_or_phrase_tsquery with ts_rank_cd",
            "cache_profile": "first-before-query-warmups plus explicit warmups",
            "concurrency": 1,
            "warmups_per_query": warmups,
            "measured_iterations_per_query": iterations,
            "result_limit": 10,
            "tie_break": "score DESC, repository-relative path ASC, document id ASC",
        },
        "maintenance_gate": maintenance_gate,
        "ingest": ingest,
        "search": search,
        "cleanup": {"status": "PASS", "temporary_cluster_removed": True},
        "limitations": [
            "The public fixture baseline is not a production performance guarantee.",
            "First-before-query-warmups latency does not claim an operating-system cold page cache.",
            "The read-only writer inventory cannot prove that unknown external writers do not exist.",
            "This current-path fixture does not select a storage-v2 search backend.",
        ],
    }


def write_manifest(path: Path, manifest: dict[str, Any]) -> None:
    if path.is_absolute() and not str(path.resolve()).startswith(str(ROOT.resolve())):
        raise ValueError("output must remain inside the repository")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--code-sha", default=git_output("rev-parse", "origin/main"))
    parser.add_argument("--harness-commit", default=git_output("rev-parse", "HEAD"))
    parser.add_argument("--warmups", type=int, default=3)
    parser.add_argument("--iterations", type=int, default=30)
    args = parser.parse_args()

    for name, value in (("code SHA", args.code_sha), ("harness commit", args.harness_commit)):
        if not re.fullmatch(r"[0-9a-f]{40}", value):
            parser.error(f"{name} must be a full Git commit SHA")
    for command in ("initdb", "pg_ctl", "psql"):
        if shutil.which(command) is None:
            print(f"BLOCKED: required command is unavailable: {command}", file=sys.stderr)
            return 2

    documents = load_documents()
    queries = load_queries()
    maintenance_gate = check_writers()
    if maintenance_gate["status"] != "PASS":
        print("BLOCKED: writer inventory is incomplete", file=sys.stderr)
        return 2

    with tempfile.TemporaryDirectory(prefix="mainrag-storage-v2-fixture-") as temporary:
        with TemporaryPostgres(Path(temporary)) as database:
            backend_version = database.sql("SHOW server_version;")
            ingest = setup_database(database, documents)
            with database.session() as session:
                search = evaluate_queries(session, queries, args.warmups, args.iterations)

    manifest = build_manifest(
        documents,
        queries,
        maintenance_gate,
        ingest,
        search,
        backend_version,
        args.code_sha,
        args.harness_commit,
        args.warmups,
        args.iterations,
    )
    validate_manifest(manifest)
    write_manifest(args.output, manifest)
    print(
        f"{manifest['status']}: {manifest['search']['query_count']} queries, "
        f"Recall@10={manifest['search']['recall_at_10']:.3f}, "
        f"MRR@10={manifest['search']['mrr_at_10']:.3f}, "
        f"result_identity={manifest['search']['result_identity_sha256']}"
    )
    print(f"Manifest: {args.output.as_posix()}")
    return 0 if manifest["status"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())

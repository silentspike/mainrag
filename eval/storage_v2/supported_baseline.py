#!/usr/bin/env python3
"""Validate actual-ingest observations and compare two frozen-corpus runs."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import re
from datetime import datetime, timezone
from pathlib import Path

try:
    from . import harness
    from .compare_manifests import relative_delta
except ImportError:
    import harness
    from compare_manifests import relative_delta


CONFIGURATION = {
    "cpu_mode": True,
    "chunker": "semantic",
    "chunker_version": "semantic-v1",
    "active_lexical_profile": "hf_bge_wordpiece",
    "indexed_lexical_version": "tiktoken-cl100k",
    "embedding_model_id": "BAAI/bge-base-en-v1.5",
    "lexical_asset_sha256": "d241a60d5e8f04cc1b2b3e9ef7a4921b27bf526d9f6050ab90f9267a1f9e5c66",
    "warmups_per_query": 3,
    "measured_iterations_per_query": 30,
    "concurrency": 1,
}


class ObservationError(ValueError):
    """Static public validation reason, never raw input or operational details."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ObservationError(message)


def expected_configuration(execution_profile: str) -> dict:
    configuration = CONFIGURATION.copy()
    if execution_profile == "hosted-ci-local-postgres":
        configuration["indexed_lexical_version"] = "hf_bge_wordpiece"
    return configuration


def number(value: object, *, integer: bool = False) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, int if integer else (int, float))
        and math.isfinite(value)
        and value >= 0
    )


def from_log(text: str) -> dict:
    rows = [line.removeprefix("corpus baseline: ") for line in text.splitlines()
            if line.startswith("corpus baseline: ")]
    require(len(rows) == 1, "exactly one corpus observation is required")
    require("1 passed; 0 failed; 0 ignored;" in text, "a nonzero successful fixture test is required")
    result = json.loads(rows[0])
    require(isinstance(result, dict), "observation must be an object")
    return result


def fixture_definition_sha256() -> str:
    root = harness.ROOT / "api/src/services/index"
    digest = hashlib.sha256()
    for name in ("baseline_tests.rs", "intelligence_retry_fixture.sql", "corpus_baseline.rs"):
        digest.update((root / name).read_bytes())
    return digest.hexdigest()


def summarize(raw: dict, code_sha: str, execution_profile: str) -> dict:
    require(bool(re.fullmatch(r"[0-9a-f]{40}", code_sha)), "exact code SHA required")
    require(execution_profile in {"hosted-ci-local-postgres", "remote-test-fixture-tunnel"}, "unknown execution profile")
    documents = harness.load_documents()
    queries = harness.load_queries()
    names = {name for name, _ in documents}
    logical = sum(len(content) for _, content in documents)
    reads = sum(len(content) + min(len(content), 512) for _, content in documents)
    require(raw.get("schema_version") == "supported-ingest-observation/v1", "unsupported observation version")
    require(raw.get("scope") == "supported_eager_filesystem_cpu_ingest_and_chunk_fts", "unsupported scope")
    require(raw.get("corpus_sha256") == harness.canonical_corpus_hash(documents), "corpus identity mismatch")
    require(raw.get("corpus_items") == len(documents), "corpus count mismatch")
    require(raw.get("query_set_sha256") == harness.sha256_file(harness.QUERIES), "query suite identity mismatch")
    require(raw.get("query_sql_sha256") == harness.sha256_file(harness.HERE / "current_path_query.sql"), "query implementation mismatch")
    require(raw.get("configuration") == expected_configuration(execution_profile), "fixture configuration drift")
    require(raw.get("fixture_definition_sha256") == fixture_definition_sha256(), "fixture schema or scaffold definition mismatch")
    require(bool(re.fullmatch(r"[0-9a-f]{64}", raw.get("schema_columns_sha256", ""))), "schema identity missing")
    require(bool(re.fullmatch(r"[0-9]+(?:\.[0-9]+)*(?: \([^\n]+\))?", raw.get("backend_version", ""))), "backend version missing")
    require(number(raw.get("stable_chunk_count"), integer=True) and raw["stable_chunk_count"] > 0, "nonzero chunks required")
    for key, expected in (("vector_connection_attempts", 0), ("outbox_rows", 0), ("ledger_rows", 2)):
        require(type(raw.get(key)) is int and raw[key] == expected, "runtime isolation or ledger mismatch")
    ingest = raw.get("ingest")
    require(isinstance(ingest, list) and len(ingest) == 2, "both ingestion phases required")
    for index, phase in enumerate(("initial", "unchanged")):
        item = ingest[index]
        require(set(item) == {"phase", "status", "logical_input_bytes", "source_io", "work", "files_processed", "files_skipped", "chunks_created", "chunk_compressed_bytes", "elapsed_ms", "errors"}, "unknown or missing ingestion field")
        require(item.get("phase") == phase and item.get("status") == "PASS", "ingestion phase did not pass")
        for key in ("logical_input_bytes", "files_processed", "files_skipped", "chunks_created", "chunk_compressed_bytes", "errors"):
            require(number(item.get(key), integer=True), "missing measured ingestion counter")
        require(item["logical_input_bytes"] == logical and item["errors"] == 0, "input or error mismatch")
        require(item["files_processed"] == (len(documents) if index == 0 else 0), "processed count mismatch")
        require(item["files_skipped"] == (0 if index == 0 else len(documents)), "skip count mismatch")
        require(item["chunks_created"] == (raw["stable_chunk_count"] if index == 0 else 0), "created chunk count mismatch")
        require(item["chunk_compressed_bytes"] > 0, "stored chunk measurement missing")
        require(number(item.get("elapsed_ms")), "invalid ingestion duration")
        calls = len(documents) if index == 0 else 0
        require(item.get("work") == {"chunker_calls": calls, "intelligence_parser_calls": calls}, "actual parser observations required")
        require(all(type(value) is int for value in item["work"].values()), "parser counters must be integers")
        io = item.get("source_io", {})
        require(set(io) == {"adapter_read_bytes", "deferred_read_bytes", "total_content_read_bytes", "device_read_bytes", "content_read_coverage", "excluded"}, "unknown or missing I/O field")
        require(io["excluded"] == ["filesystem_metadata", "walker_configuration", "pack_reads", "device_io"], "I/O exclusions mismatch")
        require(io.get("content_read_coverage") == "COMPLETE", "partial source reads cannot pass")
        require(type(io.get("adapter_read_bytes")) is int and io["adapter_read_bytes"] == reads, "adapter reads must include the binary probe")
        require(type(io.get("total_content_read_bytes")) is int and io["total_content_read_bytes"] == reads, "total read mismatch")
        require(type(io.get("deferred_read_bytes")) is int and io["deferred_read_bytes"] == 0, "unexpected deferred reads")
        require("device_read_bytes" in io and io["device_read_bytes"] is None, "unmeasured device I/O must remain null")
    require(ingest[0]["chunk_compressed_bytes"] == ingest[1]["chunk_compressed_bytes"], "stored bytes changed on repeat")
    observed = raw.get("queries")
    require(isinstance(observed, list) and len(observed) == len(queries), "nonzero complete query suite required")
    evaluated = []
    all_samples = []
    for query, actual in zip(queries, observed):
        require(actual.get("id") == query["id"], "query order or identity mismatch")
        samples = actual.get("warm_samples_ms")
        require(isinstance(samples, list) and len(samples) == 30 and all(number(n) for n in samples), "30 finite measured samples per query required")
        require(number(actual.get("first_ms")), "first-roundtrip observation missing")
        result = actual.get("observation", {})
        paths = result.get("results")
        require(isinstance(paths, list) and len(paths) <= 10 and all(p in names for p in paths), "invalid or non-public result identity")
        require(number(result.get("matched_documents"), integer=True) and number(result.get("scored_channel_rows"), integer=True), "evaluated work counters missing")
        require(result["scored_channel_rows"] >= result["matched_documents"] >= len(paths), "evaluated work cannot be a shortlist alias")
        recall = harness.recall_at_k(paths, query["expected"], 10)
        evaluated.append({"id": query["id"], "results": paths, "matched_chunks": result["matched_documents"],
                          "scored_channel_rows": result["scored_channel_rows"], "recall_at_10": recall,
                          "mrr_at_10": harness.reciprocal_rank(paths, query["expected"], 10),
                          "status": "PASS" if recall == 1.0 else "FAIL",
                          "first_ms": actual["first_ms"], "warm_latency": harness.latency_summary(samples)})
        all_samples.extend(samples)
    identities = [{key: value for key, value in row.items() if key not in {"first_ms", "warm_latency"}} for row in evaluated]
    output = {
        "schema_version": "storage-v2-supported-baseline/v1",
        "status": "PASS" if all(row["status"] == "PASS" for row in evaluated) else "FAIL",
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "code_sha": code_sha, "execution_profile": execution_profile,
        "subject": {key: raw[key] for key in ("corpus_sha256", "corpus_items", "query_set_sha256", "query_sql_sha256", "schema_columns_sha256", "fixture_definition_sha256", "backend_version")},
        "configuration": copy.deepcopy(raw["configuration"]), "ingest": copy.deepcopy(ingest),
        "stable_chunk_count": raw["stable_chunk_count"], "queries": evaluated,
        "warm_latency": harness.latency_summary(all_samples),
        "result_identity_sha256": hashlib.sha256(json.dumps(identities, sort_keys=True).encode()).hexdigest(),
        "limitations": ["Minimal fixture schema; no production migration or RLS acceptance.",
                        "Measured FTS query shape over actual persisted chunks; not full hybrid API search.",
                        "Eager filesystem CPU-mode only; no streaming or other-adapter acceptance.",
                        "First query roundtrip is not an operating-system cold-cache proof.",
                        "Content reads exclude metadata, walker configuration, pack reads and device I/O."],
    }
    harness.ensure_public_manifest(output)
    return output


def compare(left: dict, right: dict, tolerance: float) -> list[str]:
    require(number(tolerance) and tolerance <= 5, "invalid timing tolerance")
    errors = []
    if left["status"] != "PASS" or right["status"] != "PASS":
        errors.append("both runs must pass")
    for key in ("code_sha", "execution_profile", "subject", "configuration", "stable_chunk_count", "result_identity_sha256"):
        if left[key] != right[key]:
            errors.append(f"identity differs: {key}")
    for a, b in zip(left["ingest"], right["ingest"]):
        if {k:v for k,v in a.items() if k != "elapsed_ms"} != {k:v for k,v in b.items() if k != "elapsed_ms"}:
            errors.append("measured ingestion work differs")
    for key in ("p50_ms", "p95_ms", "p99_ms"):
        if relative_delta(left["warm_latency"][key], right["warm_latency"][key]) > tolerance:
            errors.append(f"warm latency tolerance exceeded: {key}")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--log", type=Path, action="append", required=True)
    parser.add_argument("--code-sha", required=True)
    parser.add_argument("--execution-profile", required=True)
    parser.add_argument("--timing-tolerance", type=float, default=0.5)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = collect_report(args.log, args.code_sha, args.execution_profile, args.timing_tolerance)
    validate_report(report)
    with args.output.open("x", encoding="utf-8") as output:
        json.dump(report, output, indent=2, allow_nan=False)
        output.write("\n")
    print(json.dumps({"status": report["status"], "runs": len(report["runs"]), "differences": report["differences"]}))
    return 0 if report["status"] == "PASS" else 1


def collect_report(paths: list[Path], code_sha: str, execution_profile: str, tolerance: float) -> dict:
    require(number(tolerance) and tolerance <= 5, "invalid timing tolerance")
    runs = []
    try:
        require(len(paths) == 2, "two independent fixture runs required")
        for path in paths:
            runs.append(summarize(from_log(path.read_text()), code_sha, execution_profile))
        errors = compare(*runs, tolerance)
        status = "FAIL" if errors else "PASS"
    except FileNotFoundError:
        status, errors = "NOT_RUN", ["required_fixture_log_missing"]
    except ObservationError as error:
        status, errors = "FAIL", [str(error)]
    except (ValueError, KeyError, TypeError):
        status, errors = "FAIL", ["invalid_or_failed_fixture_observation"]
    return {"status": status, "timing_tolerance": tolerance, "differences": errors, "runs": runs}


def validate_report(report: dict) -> None:
    import jsonschema
    schema = json.loads((harness.HERE / "supported-baseline.schema.json").read_text())
    jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker()).validate(report)
    harness.ensure_public_manifest(report)
    for run in report["runs"]:
        require(run["configuration"] == expected_configuration(run["execution_profile"]), "report configuration contradicts execution profile")
    if len(report["runs"]) != 2:
        require(report["status"] != "PASS" and bool(report["differences"]), "incomplete runs cannot pass")
        return
    errors = compare(*report["runs"], report["timing_tolerance"])
    require(report["differences"] == errors, "comparison differences were altered")
    require(report["status"] == ("FAIL" if errors else "PASS"), "comparison status contradicts observations")


if __name__ == "__main__":
    raise SystemExit(main())

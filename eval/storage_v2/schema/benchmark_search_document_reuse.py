#!/usr/bin/env python3
"""Reproduce a collision-safe SQL reuse comparison on disposable synthetic data."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import statistics
import subprocess
from pathlib import Path

from eval.storage_v2.schema import test_search_document_reuse as reuse
from eval.storage_v2.shadow_slice import write_json_atomic


def implementation_identity() -> dict[str, object]:
    paths = ["schema.sql", "migrations", "eval/storage_v2"]
    dirty = subprocess.check_output(
        ["git", "status", "--porcelain", "--untracked-files=all", "--", *paths],
        cwd=reuse.schema.ROOT, text=True,
    ).strip()
    if dirty:
        raise RuntimeError("commit the benchmark implementation before recording evidence")
    commit = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=reuse.schema.ROOT, text=True,
    ).strip()
    return {
        "commit_sha": commit,
        "migration_sha256": hashlib.sha256(reuse.MIGRATION.read_bytes()).hexdigest(),
        "benchmark_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    }


def corpus_identity(case: reuse.SearchDocumentReuseTests) -> dict[str, object]:
    return json.loads(case.sql("""
SELECT json_build_object(
    'documents', (SELECT count(*) FROM storage_v2_search_document),
    'postings', (SELECT count(*) FROM storage_v2_search_posting),
    'document_rows_sha256', (SELECT encode(sha256(convert_to(
        string_agg(row_to_json(d)::TEXT, E'\n' ORDER BY id), 'UTF8')), 'hex')
        FROM storage_v2_search_document d),
    'posting_rows_sha256', (SELECT encode(sha256(convert_to(
        string_agg(row_to_json(p)::TEXT, E'\n' ORDER BY document_id,term), 'UTF8')), 'hex')
        FROM storage_v2_search_posting p));
"""))


def nodes(plan: dict[str, object]):
    yield plan
    for child in plan.get("Plans", []):
        yield from nodes(child)


def measure(case: reuse.SearchDocumentReuseTests, kind: str, calls: int) -> dict[str, object]:
    plan = json.loads(case.sql(
        f"SET app.user_id='{reuse.schema.ADMIN_ID}'; SET plan_cache_mode=force_generic_plan; "
        "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) "
        "SELECT count(document.id) FROM ("
        f"SELECT {kind}_id AS id FROM fixture_indexed_reuse_components "
        f"ORDER BY md5({kind}_id::TEXT) LIMIT {calls}) component "
        "CROSS JOIN LATERAL storage_v2_put_search_document("
        f"'{reuse.PROFILE}', '{kind}', component.id, '{kind} fixture', "
        "ARRAY[]::TEXT[]) document;"
    ))[0]
    functions = [node for node in nodes(plan["Plan"]) if node["Node Type"] == "Function Scan"]
    if len(functions) != 1 or functions[0]["Actual Loops"] != calls \
            or functions[0]["Actual Rows"] != 1:
        raise RuntimeError("benchmark did not execute the expected nonempty reuse workload")
    return {
        "kind": kind, "function_calls": calls,
        "execution_ms": plan["Execution Time"],
        "shared_hit_blocks": plan["Plan"].get("Shared Hit Blocks", 0),
        "shared_read_blocks": plan["Plan"].get("Shared Read Blocks", 0),
    }


def run_benchmark(repetitions: int = 3, calls: int = 500) -> dict[str, object]:
    if not 3 <= repetitions <= 20 or not 1 <= calls <= 5000:
        raise ValueError("use 3..20 repetitions and 1..5000 calls")
    if os.environ.get("STORAGE_V2_TEST_SOCKET"):
        raise RuntimeError("the benchmark requires its own disposable PostgreSQL server")
    identity = implementation_identity()
    case = reuse.SearchDocumentReuseTests(
        "test_reuse_and_conflict_lookups_use_component_index_with_generic_plans"
    )
    initialized = False
    try:
        case.setUpClass()
        initialized = True
        # Reuse the exact regression fixture and require all four generic-plan
        # index assertions before measuring either implementation.
        case.test_reuse_and_conflict_lookups_use_component_index_with_generic_plans()
        before = corpus_identity(case)
        if (before["documents"], before["postings"]) != (10000, 20000):
            raise RuntimeError("benchmark corpus is not the complete declared fixture")
        timings = []
        for repeat in range(repetitions):
            order = [("before", reuse.PREVIOUS), ("after", reuse.MIGRATION)]
            if repeat % 2:
                order.reverse()
            for version, migration in order:
                case.file(migration)
                for kind in ("body", "node"):
                    timings.append({"repeat": repeat + 1, "version": version,
                                    **measure(case, kind, calls)})
        if corpus_identity(case) != before:
            raise RuntimeError("reuse changed document/posting rows")
        medians = {}
        for kind in ("body", "node"):
            baseline = statistics.median(x["execution_ms"] for x in timings
                                         if x["kind"] == kind and x["version"] == "before")
            candidate = statistics.median(x["execution_ms"] for x in timings
                                          if x["kind"] == kind and x["version"] == "after")
            medians[kind] = {"before_ms": baseline, "after_ms": candidate,
                             "speedup_ratio": baseline / candidate}
        result = {
            "schema_version": "mainrag.storage-v2.sql-reuse-comparison.v1",
            "status": "PASS", **identity, "fixture": reuse.PROFILE, "corpus": before,
            "postgres_version_num": int(case.sql("SHOW server_version_num")),
            "generic_plan_index_regressions": 4, "semantic_rows_unchanged": True,
            "repetitions_per_variant": repetitions, "calls_per_repetition": calls,
            "timings": timings, "medians": medians,
            "limitations": [
                "Synthetic SQL reuse-only comparison; not whole-ingest production performance.",
                "Warm-cache measurements with alternating variant order; no host isolation claim.",
                "Fixture selection overhead is included equally in both variants.",
                "Optional telemetry contains SQL metrics only, not API or kernel resource costs.",
            ],
        }
    finally:
        if initialized:
            case.tearDownClass()
        elif hasattr(case, "stack"):
            case.stack.close()
    if implementation_identity() != identity:
        raise RuntimeError("implementation identity changed during the benchmark")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--calls", type=int, default=500)
    parser.add_argument("--output", type=Path)
    arguments = parser.parse_args()
    destination = os.environ.get("TM_KENNZAHLEN")
    if arguments.output is not None and arguments.output.exists():
        parser.error("output already exists; retain prior evidence and choose a new file")
    if arguments.output is not None and destination \
            and arguments.output.resolve() == Path(destination).resolve():
        parser.error("evidence and telemetry require different output paths")
    result = run_benchmark(arguments.repetitions, arguments.calls)
    if arguments.output is not None:
        write_json_atomic(arguments.output, result)
    if destination:
        write_json_atomic(Path(destination), {"sql_reuse": {
            **{f"{kind}_{key}": value for kind, values in result["medians"].items()
               for key, value in values.items()},
            "documents": result["corpus"]["documents"],
            "postings": result["corpus"]["postings"],
            "calls_per_repetition": arguments.calls,
            "repetitions_per_variant": arguments.repetitions,
        }})
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()

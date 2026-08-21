#!/usr/bin/env python3
"""Two-phase supported-API harness for the storage-v2 public shadow slice."""

from __future__ import annotations

import argparse
import collections
import hashlib
import json
import math
import os
import subprocess
import struct
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


EVAL_ROOT = Path(__file__).resolve().parents[1]
if str(EVAL_ROOT) not in sys.path:
    sys.path.insert(0, str(EVAL_ROOT))

from storage_v2.topk.prototype import (  # noqa: E402
    QueryParser,
    exact_identifiers,
    leaves,
    matches,
    phrase_present,
    tokenize,
)


TELEMETRY_PHASES = {
    "lesen_hashen_ms",
    "content_store_ms",
    "strukturprojektion_ms",
    "analyse_ms",
    "db_staging_ms",
    "intervall_delta_ms",
    "sealing_ms",
}
TELEMETRY_COUNTERS = {
    "latenz_ms",
    "eingang_bytes",
    "unique_bytes",
    "stored_bytes",
    "reuse_bodies",
    "reuse_nodes",
    "reuse_views",
    "reuse_analysis",
    "reuse_generation",
    "parser_passes",
    "analysis_retries",
    "artifacts_created",
    "occurrences_created",
    "intervals_opened",
    "intervals_closed",
    "errors",
    "io_buffer_bytes",
    "peak_buffer_bytes",
    "writer_concurrency",
}


def request(api_url: str, token: str, method: str, path: str, body: Any | None = None) -> Any:
    data = None if body is None else json.dumps(body).encode("utf-8")
    headers = {"Authorization": f"Bearer {token}"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    call = urllib.request.Request(api_url.rstrip("/") + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(call, timeout=180) as response:
            payload = response.read()
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", "replace")
        raise RuntimeError(f"{method} {path} failed with HTTP {error.code}: {detail}") from error
    return json.loads(payload) if payload else None


def write_json_atomic(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(value, output, indent=2, sort_keys=True)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary_name, path)
        sudo_uid = os.environ.get("SUDO_UID")
        sudo_gid = os.environ.get("SUDO_GID")
        if (
            os.geteuid() == 0
            and sudo_uid is not None
            and sudo_gid is not None
            and sudo_uid.isdecimal()
            and sudo_gid.isdecimal()
        ):
            os.chown(path, int(sudo_uid), int(sudo_gid))
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)


def publish_telemetry(telemetry: Any, output_name: str | None) -> Path | None:
    """Validate and publish the API metrics consumed by run.sh and the HTML viewer."""
    if not isinstance(telemetry, dict):
        raise RuntimeError("shadow ingest response has no telemetry object")
    phases = telemetry.get("phase")
    counters = telemetry.get("ablauf")
    if not isinstance(phases, dict) or set(phases) != TELEMETRY_PHASES:
        raise RuntimeError("shadow ingest telemetry has incomplete or unknown phase keys")
    if not isinstance(counters, dict) or not TELEMETRY_COUNTERS.issubset(counters):
        raise RuntimeError("shadow ingest telemetry has incomplete optimization counters")
    values = [*phases.values(), *(counters[key] for key in TELEMETRY_COUNTERS)]
    if any(isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0 for value in values):
        raise RuntimeError("shadow ingest telemetry values must be non-negative numbers")
    if output_name is None:
        return None
    output = Path(output_name)
    write_json_atomic(output, {"ablauf": counters, "phase": phases})
    return output


def validate_fixture_ingest_result(result: Any) -> None:
    """Reconcile the public fixture result with its optimization counters."""
    if not isinstance(result, dict) or not isinstance(result.get("telemetry"), dict):
        raise RuntimeError("shadow ingest response is incomplete")
    counters = result["telemetry"].get("ablauf")
    integer_fields = {
        "item_count": result.get("item_count"),
        "generation_seq": result.get("generation_seq"),
        "controlled_retry_count": result.get("controlled_retry_count"),
        **(
            {key: counters.get(key) for key in TELEMETRY_COUNTERS - {"latenz_ms"}}
            if isinstance(counters, dict)
            else {}
        ),
    }
    if any(
        isinstance(value, bool) or not isinstance(value, int) or value < 0
        for value in integer_fields.values()
    ):
        raise RuntimeError("shadow ingest result counters must be non-negative integers")
    item_count = integer_fields["item_count"]
    generation_seq = integer_fields["generation_seq"]
    retry_count = integer_fields["controlled_retry_count"]
    if item_count <= 0 or generation_seq <= 0:
        raise RuntimeError("shadow ingest result has no fixture items or generation")
    assert isinstance(counters, dict)
    if counters["io_buffer_bytes"] <= 0:
        raise RuntimeError("shadow ingest result has no configured I/O buffer")
    reused_generation = result.get("reused_generation")
    if not isinstance(reused_generation, bool):
        raise RuntimeError("shadow ingest result has no generation reuse decision")

    creation_fields = (
        "unique_bytes",
        "stored_bytes",
        "parser_passes",
        "analysis_retries",
        "artifacts_created",
        "occurrences_created",
        "intervals_opened",
        "intervals_closed",
        "errors",
        "peak_buffer_bytes",
        "writer_concurrency",
    )
    reuse_fields = ("reuse_bodies", "reuse_nodes", "reuse_views", "reuse_analysis")
    if reused_generation:
        if retry_count != 0 or counters["reuse_generation"] != 1:
            raise RuntimeError("reused generation has inconsistent retry or reuse counters")
        if any(counters[key] != 0 for key in creation_fields):
            raise RuntimeError("reused generation reports write or error work")
        if any(counters[key] != item_count for key in reuse_fields):
            raise RuntimeError("reused generation does not account for every fixture item")
        return

    if counters["reuse_generation"] != 0 or counters["errors"] != 0:
        raise RuntimeError("new generation has inconsistent reuse or error counters")
    if (
        counters["writer_concurrency"] != 1
        or counters["peak_buffer_bytes"] <= 0
        or counters["peak_buffer_bytes"] > counters["io_buffer_bytes"]
    ):
        raise RuntimeError("new generation has no bounded writer configuration")
    if len({counters[key] for key in reuse_fields}) != 1:
        raise RuntimeError("fixture body, node, view, and analysis reuse diverged")
    if counters["reuse_bodies"] + counters["artifacts_created"] != item_count:
        raise RuntimeError("fixture creation and reuse counters do not reconcile")
    if counters["artifacts_created"] != counters["occurrences_created"]:
        raise RuntimeError("fixture artifact and occurrence creation counters diverged")
    if counters["reuse_analysis"] + counters["parser_passes"] != item_count:
        raise RuntimeError("fixture parser and analysis reuse counters do not reconcile")
    if retry_count != (1 if counters["parser_passes"] > 0 else 0):
        raise RuntimeError("controlled analysis retry counter does not match parser work")
    if counters["analysis_retries"] != retry_count:
        raise RuntimeError("telemetry analysis retry count does not match the ingest result")
    if (
        counters["intervals_opened"] <= 0
        or counters["intervals_closed"] > counters["intervals_opened"]
    ):
        raise RuntimeError("fixture membership interval counters do not reconcile")
    if generation_seq == 1 and (
        counters["intervals_opened"] != item_count or counters["intervals_closed"] != 0
    ):
        raise RuntimeError("initial fixture generation has invalid interval counters")
    if (counters["unique_bytes"] == 0) != (counters["stored_bytes"] == 0):
        raise RuntimeError("fixture unique and stored byte counters disagree")


def run_cli(binary: Path, api_url: str, config_home: Path, arguments: list[str]) -> Any:
    command = [str(binary.resolve()), "--api-url", api_url, "--json", *arguments]
    environment = os.environ.copy()
    environment["XDG_CONFIG_HOME"] = str(config_home)
    result = subprocess.run(
        command,
        check=False,
        capture_output=True,
        env=environment,
        text=True,
        timeout=180,
    )
    if result.returncode != 0:
        diagnostic = result.stderr.strip().splitlines()[-1:] or ["no diagnostic"]
        raise RuntimeError(f"CLI command {arguments[0]} failed: {diagnostic[0]}")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise RuntimeError(f"CLI command {arguments[0]} returned invalid JSON") from error


def load_queries(path: Path) -> list[dict[str, Any]]:
    queries = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if not queries or any(not item.get("query") or int(item.get("k", 0)) != 10 for item in queries):
        raise ValueError("query fixture must be non-empty and use exact Top-10")
    return queries


def query_set_sha256(queries: list[dict[str, Any]]) -> str:
    digest = hashlib.sha256()
    identities = [item.get("id") for item in queries]
    if any(not identity for identity in identities) or len(identities) != len(set(identities)):
        raise ValueError("query fixture requires unique non-empty ids")
    encoded_queries = sorted(
        json.dumps(item, sort_keys=True, separators=(",", ":")).encode("utf-8")
        for item in queries
    )
    for encoded in encoded_queries:
        digest.update(struct.pack(">Q", len(encoded)))
        digest.update(encoded)
    return digest.hexdigest()


def storage_v2_query(fixture: dict[str, Any]) -> str:
    """Translate the #55 query metadata into the explicit storage-v2 syntax."""
    query = fixture["query"].strip()
    if fixture.get("phrase"):
        return f'"{query}"'
    if fixture.get("construct") == "exact_identifier":
        return f"id:{query}"
    return query


def exhaustive_fixture_scores(source_path: Path, query: str) -> dict[str, float]:
    """Reuse the #56 exhaustive semantics for the fixture's one-document views."""
    documents: dict[str, str] = {}
    for path in sorted(source_path.rglob("*")):
        if not path.is_file() or path.name == ".mainrag-shadow-fixture":
            continue
        relative = path.relative_to(source_path).as_posix()
        documents[relative] = path.read_text(encoding="utf-8")
    if not documents:
        raise RuntimeError("shadow reference evaluator found no fixture documents")

    ast = QueryParser(query).parse()
    document_tokens = {path: tokenize(content) for path, content in documents.items()}
    document_exact = {path: exact_identifiers(content) for path, content in documents.items()}
    average_length = sum(map(len, document_tokens.values())) / len(document_tokens)
    frequency: collections.Counter[str] = collections.Counter()
    for tokens in document_tokens.values():
        frequency.update(set(tokens))
    all_terms = {leaf.value for leaf, _ in leaves(ast) if leaf.kind == "term"}
    score_terms = {
        leaf.value
        for leaf, negated in leaves(ast)
        if leaf.kind == "term" and not negated
    }
    phrases = {leaf.value for leaf, _ in leaves(ast) if leaf.kind == "phrase"}
    exact_values = {leaf.value for leaf, _ in leaves(ast) if leaf.kind == "exact"}
    view_count = len(documents)
    scores: dict[str, float] = {}
    for path, tokens in document_tokens.items():
        counts = collections.Counter(tokens)
        matched_terms = {term for term in all_terms if counts[term]}
        matched_phrases = {
            phrase for phrase in phrases if phrase_present(tokens, phrase)
        }
        matched_exact = exact_values.intersection(document_exact[path])
        if not matches(ast, matched_terms, matched_phrases, matched_exact):
            continue
        lexical = 0.0
        for term in score_terms:
            term_frequency = counts[term]
            if not term_frequency:
                continue
            inverse_frequency = math.log(
                1 + (view_count + 1.0) / (frequency[term] + 1.0)
            )
            normalized = term_frequency / (
                term_frequency + 0.5 + 0.5 * (len(tokens) / average_length)
            )
            lexical += inverse_frequency * normalized
        scores[path] = lexical + 1.5 * len(matched_phrases) + 2.0 * len(matched_exact)
    return scores


def matches_exhaustive_reference(
    results: list[dict[str, Any]], reference_scores: dict[str, float], limit: int
) -> tuple[bool, list[str]]:
    """Check membership, scores, and deterministic identity tie-breaking."""
    if len(reference_scores) > limit:
        raise RuntimeError("fixture reference exceeds limit without identities for cutoff ties")
    by_path = {result["file_path"]: result for result in results}
    if len(by_path) != len(results) or set(by_path) != set(reference_scores):
        return False, sorted(reference_scores)
    for path, expected_score in reference_scores.items():
        if not math.isclose(float(by_path[path]["score"]), expected_score, rel_tol=1e-5, abs_tol=1e-6):
            return False, sorted(reference_scores)
    expected = sorted(
        reference_scores,
        key=lambda path: (
            -reference_scores[path],
            by_path[path]["external_hit_id"],
            int(by_path[path]["chunk_id"]),
        ),
    )
    actual = [result["file_path"] for result in results]
    return actual == expected[:limit], expected[:limit]


def source_id(api_url: str, token: str, source_name: str) -> int | None:
    response = request(api_url, token, "GET", "/api/v1/sources")
    for source in response["sources"]:
        if source["name"] == source_name:
            return int(source["id"])
    return None


def source_state(api_url: str, token: str, source: int, generation: int) -> dict[str, Any]:
    query = urllib.parse.urlencode({"generation": generation, "include_test": "true"})
    return request(api_url, token, "GET", f"/api/v1/sources/{source}/shadow-state?{query}")


def comparable(result: dict[str, Any], current: bool) -> dict[str, Any]:
    hit_id = result.get("external_hit_id") or f"legacy:{int(result['chunk_id'])}"
    mapped = []
    successor = result.get("successor_metadata")
    if isinstance(successor, list):
        mapped = [str(item.get("external_hit_id")) for item in successor if item.get("external_hit_id")]
    return {
        "hit_id": str(hit_id),
        "rank": 0,
        "score": float(result["score"]),
        "mapped_hit_ids": mapped,
        "authorized": True,
        "_current": current,
    }


def ranked(results: list[dict[str, Any]], current: bool) -> list[dict[str, Any]]:
    output = []
    for rank, result in enumerate(results, 1):
        hit = comparable(result, current)
        hit["rank"] = rank
        hit.pop("_current")
        output.append(hit)
    return output


def ingest(arguments: argparse.Namespace, token: str) -> None:
    existing = source_id(arguments.api_url, token, arguments.source_name)
    if existing is None:
        created = request(
            arguments.api_url,
            token,
            "POST",
            "/api/v1/admin/sources",
            {
                "name": arguments.source_name,
                "source_type": "fs",
                "path": str(arguments.source_path.resolve()),
                "config": {"fixture": "storage-v2-public-v1"},
                "is_test": True,
            },
        )
        existing = int(created["id"])
    result = request(
        arguments.api_url,
        token,
        "POST",
        f"/api/v1/admin/sources/{existing}/storage-v2-shadow-slice",
        {"commit_sha": arguments.commit_sha},
    )
    validate_fixture_ingest_result(result)
    telemetry_output = publish_telemetry(
        result.get("telemetry"), os.environ.get("TM_KENNZAHLEN")
    )
    state = source_state(arguments.api_url, token, existing, int(result["generation_seq"]))
    checkpoint = {
        "schema_version": 1,
        "source_id": existing,
        "generation": int(result["generation_seq"]),
        "generation_id": int(result["generation_id"]),
        "fixture_sha256": result["fixture_sha256"],
        "commit_sha": arguments.commit_sha,
        "query_set_sha256": query_set_sha256(load_queries(arguments.queries)),
        "ingest_server_instance_id": state["server_instance_id"],
        "controlled_retry_count": int(result["controlled_retry_count"]),
    }
    write_json_atomic(arguments.checkpoint, checkpoint)
    print(json.dumps({"status": "INGESTED", **checkpoint}, sort_keys=True))
    if telemetry_output is not None:
        print(f"Telemetry: {telemetry_output}")
    print("Restart the API, then run this harness with --phase verify.")


def verify(arguments: argparse.Namespace, token: str) -> None:
    checkpoint = json.loads(arguments.checkpoint.read_text(encoding="utf-8"))
    queries = load_queries(arguments.queries)
    if query_set_sha256(queries) != checkpoint["query_set_sha256"]:
        raise RuntimeError("query fixture changed after ingest")
    state = source_state(
        arguments.api_url, token, int(checkpoint["source_id"]), int(checkpoint["generation"])
    )
    restart_passed = state["server_instance_id"] != checkpoint["ingest_server_instance_id"]
    if not restart_passed:
        raise RuntimeError("API restart not observed: server instance identity is unchanged")
    state_passed = (
        state["status"] == "verified"
        and not state["is_active"]
        and state["active_generation_id"] is None
        and state["declared_item_count"] == state["item_count"]
        and state["item_count"] == state["occurrence_count"]
        and state["view_count"] == state["search_document_count"]
        and state["analysis_incomplete_count"] == 0
        and state["packed_body_count"] == state["item_count"]
        and state["published_pack_count"] >= 1
        and state["source_watermark_sha256"] == checkpoint["fixture_sha256"]
    )
    if not state_passed:
        raise RuntimeError(f"shadow source-state reconciliation failed: {state}")

    comparisons = []
    top10_passed = True
    performance_passed = True
    degradation_passed = True
    test_scope_passed = True
    query_evidence = []
    for fixture in queries:
        normalized_query = storage_v2_query(fixture)
        common = {
            "query": normalized_query,
            "source_id": checkpoint["source_id"],
            "limit": 10,
        }
        current = request(arguments.api_url, token, "POST", "/api/v1/search/keyword", common)
        storage = request(
            arguments.api_url,
            token,
            "POST",
            "/api/v1/search/keyword",
            {
                **common,
                "read_path": "storage_v2",
                "generation": str(checkpoint["generation"]),
                "include_test": True,
                "graph_profile": "fixture-unavailable-v1",
                "semantic_profile": "fixture-unavailable-v1",
                "rerank_profile": "fixture-unavailable-v1",
            },
        )
        current_paths = [result["file_path"] for result in current["results"]]
        storage_paths = [result["file_path"] for result in storage["results"]]
        expected = fixture["expected"]
        query_test_scope = not current["results"]
        reference_matches, reference_paths = matches_exhaustive_reference(
            storage["results"],
            exhaustive_fixture_scores(arguments.source_path, normalized_query),
            10,
        )
        query_top10 = (
            reference_matches
            and set(expected).issubset(storage_paths)
            and int(storage["fully_scored_views"]) >= int(storage["total"])
        )
        query_performance = int(storage["took_ms"]) <= arguments.max_query_ms
        query_degradation = all(
            result.get("degradation", {}).get(stage) in {"available", "unavailable"}
            for result in storage["results"]
            for stage in ("graph", "semantic", "rerank")
        )
        top10_passed &= query_top10
        performance_passed &= query_performance
        degradation_passed &= query_degradation
        test_scope_passed &= query_test_scope
        comparisons.append(
            {
                "fixture": fixture,
                "normalized_query": normalized_query,
                "current": ranked(current["results"], True),
                "storage_v2": ranked(storage["results"], False),
            }
        )
        query_evidence.append(
            {
                "id": fixture["id"],
                "current_result_count": len(current_paths),
                "test_scope_passed": query_test_scope,
                "storage_v2_identity_sha256": hashlib.sha256(
                    json.dumps(storage_paths, separators=(",", ":")).encode("utf-8")
                ).hexdigest(),
                "reference_identity_sha256": hashlib.sha256(
                    json.dumps(reference_paths, separators=(",", ":")).encode("utf-8")
                ).hexdigest(),
                "top10_passed": query_top10,
                "took_ms": int(storage["took_ms"]),
            }
        )
    if not (top10_passed and performance_passed and degradation_passed and test_scope_passed):
        raise RuntimeError("Top-10, performance, degradation, or test-source scope gate failed")

    intelligence_query = urllib.parse.urlencode(
        {
            "source_id": checkpoint["source_id"],
            "generation": checkpoint["generation"],
            "command": "layers",
            "include_test": "true",
        }
    )
    layers = request(arguments.api_url, token, "GET", f"/api/v1/intelligence/shadow?{intelligence_query}")
    symbol_name = None
    if isinstance(layers, list) and layers:
        generic = layers[0].get("generic_card", {})
        symbol_name = generic.get("name") or layers[0].get("qualified_name")
    if not symbol_name:
        raise RuntimeError("shadow fixture produced no symbol for intelligence API/CLI gates")
    for command in ("card", "explain", "ownership"):
        command_query = urllib.parse.urlencode(
            {
                "source_id": checkpoint["source_id"],
                "generation": checkpoint["generation"],
                "command": command,
                "name": symbol_name,
                "include_test": "true",
            }
        )
        request(arguments.api_url, token, "GET", f"/api/v1/intelligence/shadow?{command_query}")

    if not arguments.cli_binary.is_file():
        raise RuntimeError(f"CLI binary does not exist: {arguments.cli_binary}")
    with tempfile.TemporaryDirectory(prefix="mainrag-shadow-cli-") as temporary:
        config_home = Path(temporary)
        token_directory = config_home / "mainrag"
        token_directory.mkdir(mode=0o700)
        token_path = token_directory / "token"
        token_path.write_text(token + "\n", encoding="utf-8")
        token_path.chmod(0o600)
        generation = str(checkpoint["generation"])
        scope = ["--source", arguments.source_name, "--generation", generation, "--include-test"]
        cli_commands = [
            ["source", "state", arguments.source_name, "--generation", generation, "--include-test"],
            [
                "search",
                queries[0]["query"],
                "--mode",
                "keyword",
                "--limit",
                "10",
                "--source",
                arguments.source_name,
                "--read-path",
                "storage_v2",
                "--generation",
                generation,
                "--include-test",
            ],
            ["layers", *scope],
            ["card", symbol_name, *scope],
            ["explain", symbol_name, *scope],
            ["ownership", symbol_name, *scope],
        ]
        for cli_command in cli_commands:
            run_cli(arguments.cli_binary, arguments.api_url, config_home, cli_command)

    evidence = request(
        arguments.api_url,
        token,
        "POST",
        f"/api/v1/admin/sources/{checkpoint['source_id']}/storage-v2-dual-read",
        {
            "generation": checkpoint["generation"],
            "commit_sha": checkpoint["commit_sha"],
            "fixture_sha256": checkpoint["fixture_sha256"],
            "query_set_sha256": checkpoint["query_set_sha256"],
            "queries": comparisons,
            "exact_top10_passed": top10_passed,
            "performance_envelope_passed": performance_passed,
            "restart_passed": restart_passed,
            "optional_degradation_passed": degradation_passed,
        },
    )
    manifest = {
        "schema_version": 1,
        "status": "PASS" if evidence["status"] == "PASS" else "FAIL",
        "commit_sha": checkpoint["commit_sha"],
        "fixture_sha256": checkpoint["fixture_sha256"],
        "query_set_sha256": checkpoint["query_set_sha256"],
        "generation_id": checkpoint["generation_id"],
        "generation_seq": checkpoint["generation"],
        "evidence_id": evidence["evidence_id"],
        "evidence_sha256": evidence["artifact_sha256"],
        "checks": {
            "restart": restart_passed,
            "source_state": state_passed,
            "exact_top10": top10_passed,
            "performance_envelope": performance_passed,
            "optional_degradation": degradation_passed,
            "test_source_scope": test_scope_passed,
            "cli_named_generation": True,
            "active_pointer_unchanged": state["active_generation_id"] is None,
        },
        "queries": query_evidence,
        "recorded_at_unix": int(time.time()),
    }
    write_json_atomic(arguments.output, manifest)
    print(f"PASS: {len(queries)} supported-API queries; evidence={evidence['evidence_id']}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", choices=("ingest", "verify"), required=True)
    parser.add_argument("--api-url", default="http://localhost:3001")
    parser.add_argument("--token-env", default="MAINRAG_TOKEN")
    parser.add_argument("--source-name", default="storage-v2-public-fixture")
    parser.add_argument("--source-path", type=Path, default=Path("eval/storage_v2/fixtures/corpus"))
    parser.add_argument("--commit-sha", default="")
    parser.add_argument("--queries", type=Path, default=Path("eval/storage_v2/fixtures/queries.jsonl"))
    parser.add_argument("--checkpoint", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=Path("eval/storage_v2/shadow-result.json"))
    parser.add_argument("--max-query-ms", type=int, default=500)
    parser.add_argument("--cli-binary", type=Path, default=Path("target/debug/mainrag"))
    arguments = parser.parse_args()
    token = os.environ.get(arguments.token_env)
    if not token:
        parser.error(f"token environment variable {arguments.token_env} is empty")
    if arguments.phase == "ingest":
        if len(arguments.commit_sha) != 40 or any(character not in "0123456789abcdef" for character in arguments.commit_sha):
            parser.error("--commit-sha must be a full lowercase Git SHA during ingest")
        ingest(arguments, token)
    else:
        verify(arguments, token)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

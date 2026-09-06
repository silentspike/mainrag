#!/usr/bin/env python3
"""Build and qualify one protected, pointer-neutral storage-v2 candidate."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import shutil
import struct
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any


CHECKS = (
    "artifact_root",
    "authorization",
    "body_pack_integrity",
    "dual_read",
    "intelligence",
    "intervals",
    "legacy_intelligence_export",
    "resource_budget",
    "restart_resume",
    "search_quality",
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
    "fragments_created",
    "largest_item_bytes",
}


def request(api_url: str, token: str, method: str, path: str, body: object | None = None) -> Any:
    data = None if body is None else json.dumps(body, separators=(",", ":")).encode()
    call = urllib.request.Request(
        api_url.rstrip("/") + path,
        data=data,
        method=method,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(call, timeout=24 * 3600) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        detail = error.read(4096).decode("utf-8", "replace")
        raise RuntimeError(f"API request failed with HTTP {error.code}: {detail}") from error


def atomic_private_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(value, output, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary_name, path)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)


def source_state(api_url: str, token: str, source_id: int, generation: int) -> dict[str, Any]:
    query = urllib.parse.urlencode({"generation": generation, "include_test": "true"})
    return request(api_url, token, "GET", f"/api/v1/sources/{source_id}/shadow-state?{query}")


def publish_telemetry(value: object) -> None:
    destination = os.environ.get("TM_KENNZAHLEN")
    if destination:
        atomic_private_json(Path(destination), value)


def validate_telemetry(value: object, item_count: int) -> None:
    if not isinstance(value, dict):
        raise RuntimeError("release-candidate response has no telemetry object")
    phases = value.get("phase")
    counters = value.get("ablauf")
    if not isinstance(phases, dict) or set(phases) != TELEMETRY_PHASES:
        raise RuntimeError("release-candidate telemetry has incomplete or unknown phase keys")
    if not isinstance(counters, dict) or not TELEMETRY_COUNTERS.issubset(counters):
        raise RuntimeError("release-candidate telemetry has incomplete optimization counters")
    values = [*phases.values(), *(counters[key] for key in TELEMETRY_COUNTERS)]
    if any(
        isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0
        for value in values
    ):
        raise RuntimeError("release-candidate telemetry values must be non-negative numbers")
    if any(
        isinstance(counters[key], bool) or not isinstance(counters[key], int)
        for key in TELEMETRY_COUNTERS - {"latenz_ms"}
    ):
        raise RuntimeError("release-candidate telemetry counters must be integers")
    if counters["errors"] != 0 or counters["io_buffer_bytes"] <= 0:
        raise RuntimeError("release-candidate telemetry reports errors or no I/O buffer")
    if counters["fragments_created"] > item_count:
        raise RuntimeError("release-candidate fragment count exceeds its item count")
    if item_count > 0 and not 0 < counters["largest_item_bytes"] <= counters["eingang_bytes"]:
        raise RuntimeError("release-candidate telemetry has invalid source item bounds")


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def build(arguments: argparse.Namespace, token: str) -> None:
    result = request(
        arguments.api_url,
        token,
        "POST",
        f"/api/v1/admin/sources/{arguments.source_id}/storage-v2-release-candidate-build",
        {"commit_sha": arguments.commit_sha},
    )
    if result["active_generation_before"] != result["active_generation_after"]:
        raise RuntimeError("candidate construction changed the active pointer")
    validate_telemetry(result.get("telemetry"), int(result["item_count"]))
    state = source_state(arguments.api_url, token, arguments.source_id, int(result["generation_seq"]))
    checkpoint = {
        "schema_version": 1,
        "source_ref": sha256_text(f"mainrag.issue-66.source:{arguments.source_id}"),
        "source_id": arguments.source_id,
        "commit_sha": arguments.commit_sha,
        "generation_id": int(result["generation_id"]),
        "generation_seq": int(result["generation_seq"]),
        "source_watermark_sha256": result["source_watermark_sha256"],
        "item_count": int(result["item_count"]),
        "server_instance_id": state["server_instance_id"],
        "active_generation_id": state["active_generation_id"],
        "build": result,
        "captured_at_unix": int(time.time()),
    }
    atomic_private_json(arguments.checkpoint, checkpoint)
    publish_telemetry(result["telemetry"])
    print(json.dumps({
        "status": "VERIFIED",
        "source_ref": checkpoint["source_ref"],
        "generation_seq": checkpoint["generation_seq"],
        "item_count": checkpoint["item_count"],
        "reused_generation": bool(result["reused_generation"]),
    }, sort_keys=True))


def ranked(results: list[dict[str, Any]], mappings: dict[str, list[str]] | None = None) -> list[dict[str, Any]]:
    output = []
    mappings = mappings or {}
    for rank, result in enumerate(results, 1):
        hit_id = str(result.get("external_hit_id") or f"legacy:{int(result['chunk_id'])}")
        output.append({
            "hit_id": hit_id,
            "rank": rank,
            "score": float(result["score"]),
            "mapped_hit_ids": mappings.get(sha256_text(result["file_path"]), []),
            "authorized": True,
        })
    return output


def path_identity(results: list[dict[str, Any]]) -> list[str]:
    """Return the stable, de-duplicated path identity in result-rank order."""
    identity = []
    seen = set()
    for result in results:
        path_sha256 = sha256_text(result["file_path"])
        if path_sha256 not in seen:
            identity.append(path_sha256)
            seen.add(path_sha256)
    return identity


def query_set_sha256(comparisons: list[dict[str, Any]]) -> str:
    fixtures = sorted(
        json.dumps(item["fixture"], sort_keys=True, separators=(",", ":")).encode()
        for item in comparisons
    )
    digest = hashlib.sha256()
    for fixture in fixtures:
        digest.update(struct.pack(">Q", len(fixture)))
        digest.update(fixture)
    return digest.hexdigest()


def query_seed_summary(seeds: list[dict[str, Any]]) -> dict[str, Any]:
    """Describe suite diversity without changing cases or acceptance policy.

    Equality is exact query-text equality, not inferred semantic equivalence.
    Multiple expected paths for one query are cases, not independent queries.
    Counts alone do not prove representative gold coverage.
    """
    query_counts: dict[str, int] = {}
    for seed in seeds:
        query = seed["query"]
        query_counts[query] = query_counts.get(query, 0) + 1
    return {
        "schema_version": "mainrag.storage-v2.query-seed-summary.v1",
        "case_count": len(seeds),
        "distinct_query_count": len(query_counts),
        "repeated_query_case_count": len(seeds) - len(query_counts),
        "largest_query_group": max(query_counts.values(), default=0),
        "positive_case_count": sum(seed["expects_match"] is True for seed in seeds),
        "negative_case_count": sum(seed["expects_match"] is False for seed in seeds),
        "representative_gold_coverage": "NOT_ESTABLISHED",
    }


def query_difference_diagnostics(seed: dict[str, Any], current: dict[str, Any],
                                 storage: dict[str, Any]) -> dict[str, Any]:
    """Describe observed top-k differences, never infer corpus loss or relevance."""
    baseline, candidate = path_identity(current["results"]), path_identity(storage["results"])
    baseline_set, candidate_set = set(baseline), set(candidate)
    expected = seed["expected_path_sha256"]
    reasons = []
    if seed["expects_match"]:
        presence = (expected in baseline_set, expected in candidate_set)
        expected_location = {(True, True): "both", (True, False): "current_only",
                             (False, True): "storage_v2_only", (False, False): "neither"}[presence]
        if not presence[0]:
            reasons.append("expected_not_in_current_top_k")
        if not presence[1]:
            reasons.append("expected_not_in_storage_v2_top_k")
    else:
        expected_location = "not_applicable"
        if baseline or candidate:
            reasons.append("unexpected_negative_case_hits")
    missing = len(baseline_set - candidate_set)
    common_order_equal = ([path for path in baseline if path in candidate_set]
                          == [path for path in candidate if path in baseline_set])
    if missing:
        reasons.append("baseline_paths_missing_from_top_k")
    if not common_order_equal:
        reasons.append("retained_baseline_order_changed")
    return {"schema_version": "mainrag.storage-v2.query-difference.v1",
            "expected_location": expected_location, "observations": reasons,
            "baseline_paths_missing": missing,
            "candidate_paths_added": len(candidate_set - baseline_set),
            "common_path_order_equal": common_order_equal,
            "current_repeated_path_hits": len(current["results"]) - len(baseline),
            "storage_v2_repeated_path_hits": len(storage["results"]) - len(candidate),
            "corpus_presence": "NOT_ESTABLISHED", "ranking_cause": "NOT_ESTABLISHED",
            "acceptance_effect": "NONE"}


def repeated_result_diagnostics(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    """Classify repeated reads of one path; never use across search engines.

    Timing/other envelope metadata is excluded. Every returned hit field and the
    total remain part of identity. A tie classification is not an acceptance.
    """
    def valid(response):
        if not isinstance(response, dict) or not isinstance(response.get("results"), list):
            return False
        if "total" in response and (type(response["total"]) is not int or response["total"] < 0):
            return False
        rows = response["results"]
        if any(not isinstance(row, dict) or type(row.get("chunk_id")) is not int
               or row["chunk_id"] <= 0 or type(row.get("score")) not in (int, float)
               or (type(row["score"]) is float and not math.isfinite(row["score"])) for row in rows):
            return False
        try:
            json.dumps(rows, sort_keys=True, allow_nan=False)
        except (TypeError, ValueError):
            return False
        return len({row["chunk_id"] for row in rows}) == len(rows)

    classification = "UNCLASSIFIED_VARIATION"
    if not valid(left) or not valid(right):
        classification = "INVALID_RESULT_IDENTITY"
    elif ("total" in left) == ("total" in right) and left.get("total") == right.get("total"):
        a, b = left["results"], right["results"]
        encoded_a = [json.dumps(row, sort_keys=True, allow_nan=False) for row in a]
        encoded_b = [json.dumps(row, sort_keys=True, allow_nan=False) for row in b]
        if encoded_a == encoded_b:
            classification = "ORDERED_RESULTS_IDENTICAL"
        elif (dict(zip((row["chunk_id"] for row in a), encoded_a))
              == dict(zip((row["chunk_id"] for row in b), encoded_b))
              and [row["score"] for row in a] == [row["score"] for row in b]):
            classification = "IDENTICAL_HITS_EQUAL_SCORE_TIE_PERMUTATION"
    return {"schema_version": "mainrag.storage-v2.repeated-result.v1",
            "classification": classification, "acceptance_effect": "NONE"}


def search_query_gates(seed: dict[str, Any], current: dict[str, Any],
                       storage: dict[str, Any], max_query_ms: int,
                       coverage: dict[str, Any] | None = None,
                       checkpoint: dict[str, Any] | None = None) -> dict[str, Any]:
    """Classify observable failures without accepting a plausible difference."""
    current_paths = path_identity(current["results"])
    storage_paths = path_identity(storage["results"])
    expected = seed["expected_path_sha256"]
    quality = (
        expected in current_paths and expected in storage_paths and current_paths == storage_paths
        if seed["expects_match"] else not current["results"] and not storage["results"]
    )
    coverage_check = None
    if coverage is not None:
        coverage_check = query_coverage_gates(seed, current, storage, coverage, checkpoint or {})
        quality = coverage_check["passed"]
    took_ms = storage.get("took_ms")
    performance = (type(took_ms) is int and 0 <= took_ms <= max_query_ms)
    degradation = all(
        result.get("degradation", {}).get(stage) in {"available", "unavailable"}
        for result in storage["results"] for stage in ("graph", "semantic", "rerank")
    )
    return {
        "id": seed["id"],
        "quality_passed": quality,
        "performance_passed": performance,
        "degradation_passed": degradation,
        "expected_in_current": expected in current_paths,
        "expected_in_storage_v2": expected in storage_paths,
        "missing_current_paths": len(set(storage_paths) - set(current_paths)),
        "missing_storage_v2_paths": len(set(current_paths) - set(storage_paths)),
        "same_path_order": current_paths == storage_paths,
        "current_count": len(current["results"]),
        "storage_v2_count": len(storage["results"]),
        "current_took_ms": current.get("took_ms"),
        "storage_v2_took_ms": took_ms,
        "max_query_ms": max_query_ms,
        "current_identity_sha256": sha256_text(json.dumps(current_paths, separators=(",", ":"))),
        "storage_v2_identity_sha256": sha256_text(json.dumps(storage_paths, separators=(",", ":"))),
        "coverage": coverage_check,
        "diagnostics": query_difference_diagnostics(seed, current, storage),
    }


def query_coverage_gates(seed: dict[str, Any], current: dict[str, Any], storage: dict[str, Any],
                         evidence: dict[str, Any], checkpoint: dict[str, Any]) -> dict[str, Any]:
    """Require complete legacy path recall and independent support for every new hit."""
    failed = {"passed": False, "policy": "literal-coverage-non-inferiority-v1"}
    if evidence.get("schema_version") != "mainrag.storage-v2.query-coverage.v1" \
            or evidence.get("query_sha256") != sha256_text(seed["query"]) \
            or any(type(evidence.get(key)) is not int or evidence[key] <= 0
                   for key in ("source_id", "generation_id", "generation_seq")) \
            or any(key not in checkpoint or evidence.get(key) != checkpoint[key]
                   for key in ("source_id", "generation_id", "generation_seq", "commit_sha")):
        return failed
    candidate = evidence.get("candidate")
    baseline = evidence.get("current")
    legacy = evidence.get("legacy_paths")
    if not all(isinstance(rows, list) for rows in (candidate, baseline, legacy)):
        return failed
    for rows, results, identity in ((candidate, storage["results"], "occurrence_id"),
                                    (baseline, current["results"], "chunk_id")):
        if len(rows) != len(results) or len(rows) > 10 or any(not isinstance(row, dict) for row in rows):
            return failed
        if any(type(row.get(identity)) is not int or row[identity] <= 0 for row in rows) \
                or any(type(hit.get("chunk_id")) is not int or hit["chunk_id"] <= 0
                       or not isinstance(hit.get("file_path"), str) for hit in results):
            return failed
        indexed = {row[identity]: row for row in rows}
        if len(indexed) != len(rows) or set(indexed) != {hit["chunk_id"] for hit in results}:
            return failed
        for hit in results:
            row = indexed[hit["chunk_id"]]
            if row.get("path_sha256") != sha256_text(hit["file_path"]):
                return failed
            if identity == "chunk_id":
                if row.get("indexed_match") is not True:
                    return failed
            elif row.get("external_hit_id") != hit.get("external_hit_id") \
                    or not isinstance(row.get("external_hit_id"), str) \
                    or row.get("body_text_matches") is not True \
                    or not isinstance(row.get("body_sha256"), str) \
                    or len(row["body_sha256"]) != 64 \
                    or any(c not in "0123456789abcdef" for c in row["body_sha256"]) \
                    or type(row.get("reference_frequency")) is not int \
                    or row["reference_frequency"] <= 0 \
                    or type(row.get("posting_frequency")) is not int \
                    or row["posting_frequency"] != row["reference_frequency"]:
                return failed
    current_paths = path_identity(current["results"])
    storage_paths = path_identity(storage["results"])
    if any(not isinstance(row, dict) or not isinstance(row.get("path_sha256"), str) for row in legacy):
        return failed
    by_path = {row["path_sha256"]: row for row in legacy}
    if len(by_path) != len(legacy) or set(by_path) != set(current_paths) | set(storage_paths):
        return failed
    for row in legacy:
        if any(type(row.get(key)) is not int or row[key] < 0
               for key in ("chunk_count", "indexed_matches", "literal_matches")) \
                or row["indexed_matches"] > row["chunk_count"] \
                or row["literal_matches"] > row["chunk_count"]:
            return failed
    # Added, independently supported paths may not displace a baseline path or
    # reorder the retained baseline. They are relevant gold hits, not negatives
    # merely because the legacy index omitted their document or lexical text.
    retained = [path for path in storage_paths if path in set(current_paths)]
    positive = (seed["expected_path_sha256"] in storage_paths and retained == current_paths)
    negative = not current["results"] and not storage["results"]
    classes: dict[str, int] = {}
    for path in set(storage_paths) - set(current_paths):
        row = by_path[path]
        if row["chunk_count"] == 0:
            reason = "legacy_not_indexed"
        elif row["indexed_matches"] == 0:
            reason = "legacy_lexical_projection_gap" if row["literal_matches"] else "legacy_content_gap"
        else:
            reason = "ranking_expansion"
        classes[reason] = classes.get(reason, 0) + 1
    return {"passed": positive if seed["expects_match"] else negative,
            "policy": "literal-coverage-non-inferiority-v1",
            "all_candidate_hits_supported": True,
            "all_current_hits_supported": True,
            "baseline_paths_retained_in_order": retained == current_paths,
            "additional_path_classes": classes}


def verify_intelligence(api_url: str, token: str, source_id: int, generation: int,
                        export: dict[str, Any], progress: dict[str, Any] | None = None) -> dict[str, Any]:
    hashes: dict[str, str] = {}
    if progress is not None:
        progress["intelligence_result_sha256"] = hashes
        progress["phase"] = "intelligence_layers"
    record_counts = export["payload"]["record_counts"]
    if int(record_counts["cards"]) == 0:
        return {"applicability": "unknown_not_applicable", "commands": []}
    common = {"source_id": source_id, "generation": generation, "include_test": "true"}
    layers_query = urllib.parse.urlencode({**common, "command": "layers"})
    layers = request(api_url, token, "GET", f"/api/v1/intelligence/shadow?{layers_query}")
    if not isinstance(layers, list) or not layers:
        raise RuntimeError("candidate intelligence layers returned no applicable symbol")
    generic = layers[0].get("generic_card", {})
    name = generic.get("name") or layers[0].get("qualified_name")
    if not name:
        raise RuntimeError("candidate intelligence symbol omitted its name")
    hashes["layers"] = sha256_text(json.dumps(layers, sort_keys=True))
    for command in ("card", "explain", "ownership"):
        if progress is not None:
            progress["phase"] = "intelligence_" + command
        query = urllib.parse.urlencode({**common, "command": command, "name": name})
        value = request(api_url, token, "GET", f"/api/v1/intelligence/shadow?{query}")
        hashes[command] = sha256_text(json.dumps(value, sort_keys=True))
    return {"applicability": "applicable", "commands": sorted(hashes), "result_sha256": hashes}


def verify(arguments: argparse.Namespace, token: str) -> None:
    if arguments.output.exists():
        raise RuntimeError("verification output already exists; retain it and choose a new attempt")
    progress: dict[str, Any] = {
        "phase": "checkpoint", "query_results": [], "comparisons": [], "query_coverage": [],
        "qualification_attempted": False, "qualification_outcome": "NOT_ATTEMPTED",
    }
    try:
        verify_candidate(arguments, token, progress)
    except Exception as error:
        # Preserve completed proof even when a later HTTP request or local check
        # fails. Never copy exception messages, request headers, or response bodies.
        if not arguments.output.exists():
            failure = {"type": type(error).__name__}
            cause = error
            seen: set[int] = set()
            while cause is not None and id(cause) not in seen:
                seen.add(id(cause))
                if isinstance(cause, urllib.error.HTTPError):
                    failure["http_status"] = cause.code
                    break
                cause = cause.__cause__ or cause.__context__
            atomic_private_json(arguments.output, {
                **progress, "status": "FAIL", "failed_gate": progress["phase"],
                "error": failure,
            })
        raise


def verify_candidate(arguments: argparse.Namespace, token: str, progress: dict[str, Any]) -> None:
    checkpoint = json.loads(arguments.checkpoint.read_text(encoding="utf-8"))
    progress["checkpoint"] = checkpoint
    if checkpoint["source_id"] != arguments.source_id or checkpoint["commit_sha"] != arguments.commit_sha:
        raise RuntimeError("checkpoint source or commit identity differs")
    progress["phase"] = "restart_state"
    state = source_state(arguments.api_url, token, arguments.source_id, checkpoint["generation_seq"])
    if state["server_instance_id"] == checkpoint["server_instance_id"]:
        raise RuntimeError("API restart was not observed after candidate construction")
    progress["phase"] = "restart_resume"
    repeated = request(
        arguments.api_url,
        token,
        "POST",
        f"/api/v1/admin/sources/{arguments.source_id}/storage-v2-release-candidate-build",
        {"commit_sha": arguments.commit_sha},
    )
    if (
        not repeated["reused_generation"]
        or int(repeated["generation_id"]) != checkpoint["generation_id"]
        or int(repeated["generation_seq"]) != checkpoint["generation_seq"]
        or repeated["source_watermark_sha256"] != checkpoint["source_watermark_sha256"]
        or repeated["active_generation_before"] != checkpoint["active_generation_id"]
        or repeated["active_generation_after"] != checkpoint["active_generation_id"]
    ):
        raise RuntimeError("restart/resume did not reproduce the completed candidate identity")
    validate_telemetry(repeated.get("telemetry"), int(repeated["item_count"]))
    progress["restart_resume"] = {"server_instance_changed": True, "generation_reused": True}
    progress["phase"] = "integrity"
    verified = request(
        arguments.api_url,
        token,
        "POST",
        f"/api/v1/admin/sources/{arguments.source_id}/storage-v2-release-candidate-verify",
        {"generation_id": checkpoint["generation_id"]},
    )
    progress["verification"] = verified
    progress["query_seed_summary"] = query_seed_summary(verified["query_seeds"])
    if (
        int(verified["source_id"]) != arguments.source_id
        or int(verified["generation_id"]) != checkpoint["generation_id"]
        or int(verified["generation_seq"]) != checkpoint["generation_seq"]
        or verified["source_watermark_sha256"] != checkpoint["source_watermark_sha256"]
        or verified["active_generation_id"] != checkpoint["active_generation_id"]
        or verified["status"] not in {"verified", "release_candidate"}
    ):
        raise RuntimeError("server verification returned a different candidate identity")
    intelligence = verify_intelligence(
        arguments.api_url, token, arguments.source_id, checkpoint["generation_seq"],
        verified["intelligence_export"], progress,
    )
    progress["intelligence"] = intelligence

    comparisons = progress["comparisons"]
    query_results = progress["query_results"]
    query_coverage = progress["query_coverage"]
    quality_passed = True
    performance_passed = True
    degradation_passed = True
    for ordinal, seed in enumerate(verified["query_seeds"], 1):
        pending = {"ordinal": ordinal, "id": seed["id"], "query_sha256": sha256_text(seed["query"])}
        progress["pending_query"] = pending
        common = {"query": seed["query"], "source_id": arguments.source_id, "limit": 10}
        progress["phase"] = "search_current"
        current = request(arguments.api_url, token, "POST", "/api/v1/search/keyword", common)
        pending["current"] = ranked(current["results"])
        pending["current_path_sha256"] = path_identity(current["results"])
        pending["current_ms"] = current.get("took_ms")
        progress["phase"] = "search_storage_v2"
        storage = request(arguments.api_url, token, "POST", "/api/v1/search/keyword", {
            **common,
            "read_path": "storage_v2",
            "generation": str(checkpoint["generation_seq"]),
            "include_test": True,
            "graph_profile": "candidate-unavailable-v1",
            "semantic_profile": "candidate-unavailable-v1",
            "rerank_profile": "candidate-unavailable-v1",
        })
        pending["storage_v2"] = ranked(storage["results"])
        pending["storage_v2_path_sha256"] = path_identity(storage["results"])
        pending["storage_v2_ms"] = storage.get("took_ms")
        progress["phase"] = "query_coverage"
        current_by_path: dict[str, list[str]] = {}
        for result in current["results"]:
            current_by_path.setdefault(sha256_text(result["file_path"]), []).append(
                f"legacy:{int(result['chunk_id'])}"
            )
        coverage = request(
            arguments.api_url, token, "POST",
            f"/api/v1/admin/sources/{arguments.source_id}/storage-v2-candidate-query-evidence",
            {"generation_id": checkpoint["generation_id"], "commit_sha": arguments.commit_sha,
             "query": seed["query"],
             "candidate_occurrence_ids": [hit["chunk_id"] for hit in storage["results"]],
             "current_chunk_ids": [hit["chunk_id"] for hit in current["results"]]},
        )
        query_coverage.append(coverage)
        gates = search_query_gates(seed, current, storage, arguments.max_query_ms, coverage, checkpoint)
        quality_passed &= gates["quality_passed"]
        performance_passed &= gates["performance_passed"]
        degradation_passed &= gates["degradation_passed"]
        fixture = {"id": seed["id"], "query": seed["query"], "phrase": False, "k": 10,
                   "coverage_evidence_sha256": sha256_text(json.dumps(coverage, sort_keys=True))}
        comparisons.append({
            "fixture": fixture,
            "normalized_query": seed["query"],
            "current": ranked(current["results"]),
            "storage_v2": ranked(storage["results"], current_by_path),
        })
        query_results.append(gates)
        progress.pop("pending_query")
    progress["phase"] = "candidate_search"
    if not query_results or not (quality_passed and performance_passed and degradation_passed):
        atomic_private_json(arguments.output, {
            "status": "FAIL", "failed_gate": "candidate_search",
            "checkpoint": checkpoint, "verification": verified,
            "intelligence": intelligence,
            "query_results": query_results, "comparisons": comparisons,
            "query_seed_summary": progress["query_seed_summary"],
            "query_coverage": query_coverage,
            "checks": {"quality": quality_passed and bool(query_results),
                       "performance": performance_passed and bool(query_results),
                       "degradation": degradation_passed and bool(query_results)},
            "qualification_submitted": False,
        })
        raise RuntimeError("candidate search quality, latency, or degradation gate failed")
    dual_request = {
        "generation": checkpoint["generation_seq"],
        "commit_sha": arguments.commit_sha,
        "fixture_sha256": checkpoint["build"]["fixture_sha256"],
        "query_set_sha256": query_set_sha256(comparisons),
        "queries": comparisons,
        "exact_top10_passed": quality_passed,
        "performance_envelope_passed": performance_passed,
        "restart_passed": True,
        "optional_degradation_passed": degradation_passed,
    }
    progress["phase"] = "dual_read"
    dual = request(
        arguments.api_url,
        token,
        "POST",
        f"/api/v1/admin/sources/{arguments.source_id}/storage-v2-dual-read",
        dual_request,
    )
    if dual.get("status") != "PASS" or dual.get("artifact", {}).get("unexplained_count") != 0:
        raise RuntimeError("server rejected the dual-read evidence")
    progress["dual_read"] = dual
    progress["phase"] = "resource_budget"
    free_bytes = shutil.disk_usage(arguments.pack_root).free
    if free_bytes < arguments.minimum_free_bytes:
        raise RuntimeError("resource reserve is below the approved minimum")
    progress["phase"] = "server_checks"
    checks = {name: "PASS" for name in CHECKS}
    for name in (
        "artifact_root", "authorization", "body_pack_integrity", "intelligence",
        "intervals", "legacy_intelligence_export",
    ):
        if verified["checks"].get(name) != "PASS":
            raise RuntimeError(f"server verification did not pass {name}")
    evidence_id = str(uuid.uuid5(
        uuid.NAMESPACE_URL,
        f"mainrag:storage-v2:rc:{arguments.source_id}:{checkpoint['generation_id']}:{arguments.commit_sha}",
    ))
    qualification = {
        "evidence_id": evidence_id,
        "generation_id": checkpoint["generation_id"],
        "commit_sha": arguments.commit_sha,
        "source_watermark_sha256": checkpoint["source_watermark_sha256"],
        "adapter_profile_id": verified["adapter_profile_id"],
        "analysis_profile_id": verified["analysis_profile_id"],
        "search_profile_id": verified["search_profile_id"],
        "manifest": {
            "status": "PASS",
            "checks": checks,
            "server_verification_sha256": sha256_text(json.dumps(verified, sort_keys=True)),
            "dual_read_evidence_id": dual["evidence_id"],
            "dual_read_artifact_sha256": dual["artifact_sha256"],
            "query_results": query_results,
            "query_seed_summary": progress["query_seed_summary"],
            "query_coverage_sha256": sha256_text(json.dumps(query_coverage, sort_keys=True)),
            "intelligence": intelligence,
            "resource": {"free_bytes": free_bytes, "minimum_free_bytes": arguments.minimum_free_bytes},
            "restart": {"server_instance_changed": True, "generation_reused": True},
        },
    }
    progress["phase"] = "qualification"
    progress["qualification"] = qualification
    progress["qualification_attempted"] = True
    # A lost response does not prove the server rejected or never received a POST.
    progress["qualification_outcome"] = "UNKNOWN"
    result = request(
        arguments.api_url,
        token,
        "POST",
        f"/api/v1/admin/sources/{arguments.source_id}/storage-v2-release-candidate-qualify",
        qualification,
    )
    progress["qualification_outcome"] = "RESPONSE_RECEIVED"
    progress["result"] = result
    progress["phase"] = "evidence_write"
    artifact = {"checkpoint": checkpoint, "verification": verified, "dual_read": dual,
                "query_coverage": query_coverage,
                "qualification": qualification, "result": result}
    atomic_private_json(arguments.output, artifact)
    publish_telemetry(repeated["telemetry"])
    print(json.dumps({
        "status": result["status"], "source_ref": checkpoint["source_ref"],
        "generation_seq": result["generation_seq"], "evidence_id": result["evidence_id"],
        "active_generation_id": result["active_generation_id"],
    }, sort_keys=True))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("build", "verify"))
    parser.add_argument("--api-url", default="http://127.0.0.1:3001")
    parser.add_argument("--token-env", default="MAINRAG_TOKEN")
    parser.add_argument("--source-id", type=int, required=True)
    parser.add_argument("--commit-sha", required=True)
    parser.add_argument("--checkpoint", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--pack-root", type=Path, default=Path("/data/mainrag/storage-v2-66/packs"))
    parser.add_argument("--minimum-free-bytes", type=int, default=40 * 1024**3)
    parser.add_argument("--max-query-ms", type=int, default=2000)
    arguments = parser.parse_args()
    if len(arguments.commit_sha) != 40 or any(c not in "0123456789abcdef" for c in arguments.commit_sha):
        parser.error("--commit-sha must be a full lowercase Git SHA")
    if arguments.phase == "verify" and arguments.output is None:
        parser.error("verify requires --output")
    token = os.environ.get(arguments.token_env)
    if not token:
        parser.error(f"token environment variable {arguments.token_env} is empty")
    (build if arguments.phase == "build" else verify)(arguments, token)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Build and qualify one protected, pointer-neutral storage-v2 candidate."""

from __future__ import annotations

import argparse
import hashlib
import json
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


def verify_intelligence(api_url: str, token: str, source_id: int, generation: int,
                        export: dict[str, Any]) -> dict[str, Any]:
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
    hashes = {"layers": sha256_text(json.dumps(layers, sort_keys=True))}
    for command in ("card", "explain", "ownership"):
        query = urllib.parse.urlencode({**common, "command": command, "name": name})
        value = request(api_url, token, "GET", f"/api/v1/intelligence/shadow?{query}")
        hashes[command] = sha256_text(json.dumps(value, sort_keys=True))
    return {"applicability": "applicable", "commands": sorted(hashes), "result_sha256": hashes}


def verify(arguments: argparse.Namespace, token: str) -> None:
    checkpoint = json.loads(arguments.checkpoint.read_text(encoding="utf-8"))
    if checkpoint["source_id"] != arguments.source_id or checkpoint["commit_sha"] != arguments.commit_sha:
        raise RuntimeError("checkpoint source or commit identity differs")
    state = source_state(arguments.api_url, token, arguments.source_id, checkpoint["generation_seq"])
    if state["server_instance_id"] == checkpoint["server_instance_id"]:
        raise RuntimeError("API restart was not observed after candidate construction")
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
    verified = request(
        arguments.api_url,
        token,
        "POST",
        f"/api/v1/admin/sources/{arguments.source_id}/storage-v2-release-candidate-verify",
        {"generation_id": checkpoint["generation_id"]},
    )
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
        verified["intelligence_export"],
    )

    comparisons = []
    query_results = []
    quality_passed = True
    performance_passed = True
    degradation_passed = True
    for seed in verified["query_seeds"]:
        common = {"query": seed["query"], "source_id": arguments.source_id, "limit": 10}
        current = request(arguments.api_url, token, "POST", "/api/v1/search/keyword", common)
        storage = request(arguments.api_url, token, "POST", "/api/v1/search/keyword", {
            **common,
            "read_path": "storage_v2",
            "generation": str(checkpoint["generation_seq"]),
            "include_test": True,
            "graph_profile": "candidate-unavailable-v1",
            "semantic_profile": "candidate-unavailable-v1",
            "rerank_profile": "candidate-unavailable-v1",
        })
        current_by_path: dict[str, list[str]] = {}
        for result in current["results"]:
            current_by_path.setdefault(sha256_text(result["file_path"]), []).append(
                f"legacy:{int(result['chunk_id'])}"
            )
        current_paths = path_identity(current["results"])
        storage_paths = path_identity(storage["results"])
        expected = seed["expected_path_sha256"]
        query_quality = (
            expected in current_paths
            and expected in storage_paths
            and current_paths == storage_paths
            if seed["expects_match"]
            else not current["results"] and not storage["results"]
        )
        query_performance = int(storage["took_ms"]) <= arguments.max_query_ms
        query_degradation = all(
            result.get("degradation", {}).get(stage) in {"available", "unavailable"}
            for result in storage["results"] for stage in ("graph", "semantic", "rerank")
        )
        quality_passed &= query_quality
        performance_passed &= query_performance
        degradation_passed &= query_degradation
        fixture = {"id": seed["id"], "query": seed["query"], "phrase": False, "k": 10}
        comparisons.append({
            "fixture": fixture,
            "normalized_query": seed["query"],
            "current": ranked(current["results"]),
            "storage_v2": ranked(storage["results"], current_by_path),
        })
        query_results.append({
            "id": seed["id"],
            "quality_passed": query_quality,
            "performance_passed": query_performance,
            "current_count": len(current["results"]),
            "storage_v2_count": len(storage["results"]),
            "current_identity_sha256": sha256_text(json.dumps(current_paths, separators=(",", ":"))),
            "storage_v2_identity_sha256": sha256_text(json.dumps(storage_paths, separators=(",", ":"))),
        })
    if not (quality_passed and performance_passed and degradation_passed):
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
    dual = request(
        arguments.api_url,
        token,
        "POST",
        f"/api/v1/admin/sources/{arguments.source_id}/storage-v2-dual-read",
        dual_request,
    )
    if dual.get("status") != "PASS" or dual.get("artifact", {}).get("unexplained_count") != 0:
        raise RuntimeError("server rejected the dual-read evidence")
    free_bytes = shutil.disk_usage(arguments.pack_root).free
    if free_bytes < arguments.minimum_free_bytes:
        raise RuntimeError("resource reserve is below the approved minimum")
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
            "intelligence": intelligence,
            "resource": {"free_bytes": free_bytes, "minimum_free_bytes": arguments.minimum_free_bytes},
            "restart": {"server_instance_changed": True, "generation_reused": True},
        },
    }
    result = request(
        arguments.api_url,
        token,
        "POST",
        f"/api/v1/admin/sources/{arguments.source_id}/storage-v2-release-candidate-qualify",
        qualification,
    )
    artifact = {"checkpoint": checkpoint, "verification": verified, "dual_read": dual,
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

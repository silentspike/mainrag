#!/usr/bin/env python3
"""Audit persisted release-candidate evidence without accepting live gates."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
import tempfile
import uuid
from collections import Counter
from pathlib import Path


CHECKS = (
    "artifact_root", "authorization", "body_pack_integrity", "dual_read",
    "intelligence", "intervals", "legacy_intelligence_export",
    "resource_budget", "restart_resume", "search_quality",
)
EXTERNAL_GATES = (
    "live_adapter_watermarks", "writer_and_maintenance_inventory",
    "current_package_and_schema", "representative_gold_review",
    "per_class_and_aggregate_quality", "cumulative_resource_and_recovery_budget",
    "benchmark_result", "legacy_state_readback",
)
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
COMMIT = re.compile(r"[0-9a-f]{40}\Z")


def digest_identity(value: object) -> bool:
    return isinstance(value, str) and SHA256.fullmatch(value) is not None


def candidate_proof(manifest: object) -> tuple[list[str], dict | None]:
    """Inspect measured qualification fields, beyond its persisted PASS labels."""
    failures: list[str] = []
    if not isinstance(manifest, dict) or manifest.get("status") != "PASS":
        return ["qualification_manifest_not_pass"], None
    checks = manifest.get("checks")
    if not isinstance(checks, dict) or any(checks.get(name) != "PASS" for name in CHECKS):
        failures.append("qualification_checks_incomplete")
    gold = manifest.get("gold_suite_summary")
    if not gold_summary_valid(gold):
        failures.append("gold_suite_binding_missing")
    automatic = manifest.get("query_seed_summary")
    automatic_count = automatic.get("case_count") if isinstance(automatic, dict) else None
    if type(automatic_count) is not int or automatic_count < 0:
        failures.append("automatic_query_summary_invalid")
    queries = manifest.get("query_results")
    expected_count = (automatic_count + gold["case_count"]
                      if type(automatic_count) is int and automatic_count >= 0
                      and gold_summary_valid(gold) else None)
    if (not isinstance(queries, list) or expected_count is None
            or len(queries) != expected_count or not queries):
        failures.append("query_result_set_incomplete")
    else:
        seen: set[str] = set()
        for query in queries:
            if not isinstance(query, dict) or not isinstance(query.get("id"), str) \
                    or not query["id"] or query["id"] in seen \
                    or any(query.get(key) is not True for key in (
                        "quality_passed", "performance_passed", "degradation_passed"
                    )) or type(query.get("storage_v2_took_ms")) is not int \
                    or type(query.get("max_query_ms")) is not int \
                    or not 0 <= query["storage_v2_took_ms"] <= query["max_query_ms"] \
                    or (query.get("coverage") is not None
                        and (not isinstance(query["coverage"], dict)
                             or query["coverage"].get("passed") is not True)):
                failures.append("query_result_gate_failed")
                break
            seen.add(query["id"])
    if any(not digest_identity(manifest.get(key)) for key in (
        "server_verification_sha256", "dual_read_artifact_sha256",
        "query_coverage_sha256",
    )) or not isinstance(manifest.get("dual_read_evidence_id"), str):
        failures.append("verification_identity_incomplete")
    else:
        try:
            uuid.UUID(manifest["dual_read_evidence_id"])
        except ValueError:
            failures.append("verification_identity_incomplete")
    resource = manifest.get("resource")
    if not isinstance(resource, dict) or any(
        type(resource.get(key)) is not int for key in ("free_bytes", "minimum_free_bytes")
    ) or not 0 < resource["minimum_free_bytes"] <= resource["free_bytes"]:
        failures.append("resource_receipt_invalid")
    restart = manifest.get("restart")
    if not isinstance(restart, dict) or restart.get("server_instance_changed") is not True \
            or restart.get("generation_reused") is not True:
        failures.append("restart_receipt_invalid")
    intelligence = manifest.get("intelligence")
    if not isinstance(intelligence, dict):
        failures.append("intelligence_receipt_invalid")
    elif intelligence.get("applicability") == "applicable":
        expected_commands = {"card", "explain", "layers", "ownership"}
        hashes = intelligence.get("result_sha256")
        commands = intelligence.get("commands")
        if not isinstance(commands, list) or len(commands) != 4 \
                or any(not isinstance(command, str) for command in commands) \
                or set(commands) != expected_commands \
                or not isinstance(hashes, dict) or set(hashes) != expected_commands \
                or not all(digest_identity(value) for value in hashes.values()):
            failures.append("intelligence_receipt_invalid")
    elif intelligence.get("applicability") != "unknown_not_applicable" \
            or intelligence.get("commands") != []:
        failures.append("intelligence_receipt_invalid")
    if failures:
        return failures, None
    return [], {"source_class": gold["source_class"],
                "gold_case_count": gold["case_count"],
                "query_count": len(queries), "suite_sha256": gold["suite_sha256"]}


def canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def read_protected_inventory(path: Path, expected_sha256: str) -> tuple[dict, str]:
    if not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
        raise RuntimeError("exact inventory digest is required")
    try:
        metadata = path.lstat()
    except OSError as error:
        raise RuntimeError("protected inventory is unavailable") from error
    if not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) & 0o077:
        raise RuntimeError("inventory must be a private regular file")
    try:
        raw = path.read_bytes()
    except OSError as error:
        raise RuntimeError("protected inventory could not be read") from error
    if len(raw) > 32 * 1024 * 1024:
        raise RuntimeError("protected inventory exceeds the bounded input size")
    actual = hashlib.sha256(raw).hexdigest()
    if actual != expected_sha256:
        raise RuntimeError("protected inventory digest differs")
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeError("protected inventory is invalid JSON") from error
    if not isinstance(value, dict) or value.get("schema_version") != "mainrag.storage-v2.candidate-inventory.v1":
        raise RuntimeError("protected inventory schema differs")
    if value.get("capture_status") != "OBSERVED_ONLY":
        raise RuntimeError("protected inventory status differs")
    return value, actual


def gold_summary_valid(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    if value.get("schema_version") != "mainrag.storage-v2.gold-suite-summary.v1":
        return False
    if not isinstance(value.get("suite_sha256"), str) or not re.fullmatch(
        r"[0-9a-f]{64}", value["suite_sha256"]
    ) or not isinstance(value.get("source_class"), str) or not value["source_class"]:
        return False
    counts = [value.get(key) for key in (
        "case_count", "positive_case_count", "negative_case_count", "distinct_query_count"
    )]
    return (all(type(count) is int and count > 0 for count in counts)
            and counts[0] == counts[1] + counts[2]
            and 2 <= counts[3] <= counts[0]
            and value.get("representative_coverage") == "SUITE_DIGEST_BOUND_REVIEW_EXTERNAL")


def audit(inventory: dict, inventory_sha256: str) -> tuple[dict, dict]:
    sources = inventory.get("sources")
    if not isinstance(sources, list) or not sources:
        raise RuntimeError("protected inventory has no source set")
    seen: set[int] = set()
    protected_sources = []
    blockers: Counter[str] = Counter()
    benchmark_count = 0
    benchmark_candidate_count = 0
    candidate_count = 0
    candidate_set = []
    quality_by_class: dict[str, dict[str, int]] = {}
    expected_commit = inventory.get("operator_commit_sha")
    for source in sources:
        if not isinstance(source, dict) or type(source.get("source_id")) is not int \
                or source["source_id"] <= 0 or source["source_id"] in seen \
                or not isinstance(source.get("source_ref"), str) \
                or not re.fullmatch(r"[0-9a-f]{64}", source["source_ref"]):
            raise RuntimeError("protected inventory source identity differs")
        seen.add(source["source_id"])
        if type(source.get("is_test")) is not bool:
            raise RuntimeError("protected inventory benchmark classification differs")
        benchmark_count += source["is_test"]
        generations = source.get("generations")
        if not isinstance(generations, list) or any(not isinstance(item, dict) for item in generations):
            raise RuntimeError("protected inventory generation set differs")
        candidates = [item for item in generations if item.get("status") == "release_candidate"]
        if len(candidates) > 1:
            raise RuntimeError("protected inventory has duplicate current candidates")
        if candidates and (type(candidates[0].get("generation_id")) is not int
                           or candidates[0]["generation_id"] <= 0):
            raise RuntimeError("protected inventory candidate identity differs")
        failures = []
        if source.get("active_generation_id") is not None:
            failures.append("active_pointer_changed")
        if not candidates:
            failures.append("missing_release_candidate")
        else:
            candidate_count += 1
            benchmark_candidate_count += source["is_test"]
            candidate = candidates[0]
            if candidate.get("qualification_manifest_digest_matches") is not True:
                failures.append("qualification_manifest_digest_mismatch")
            proof_failures, proof_summary = candidate_proof(
                candidate.get("qualification_manifest"))
            failures.extend(proof_failures)
            if not isinstance(expected_commit, str) or not COMMIT.fullmatch(expected_commit) \
                    or candidate.get("commit_sha") != expected_commit:
                failures.append("candidate_package_identity_mismatch")
            if not all(candidate.get(key) for key in (
                "evidence_id", "adapter_profile_id", "analysis_profile_id",
                "search_profile_id",
            )) or not all(digest_identity(candidate.get(key)) for key in (
                "source_watermark_sha256", "qualification_manifest_sha256",
            )) or not digest_identity(candidate.get("verification_manifest_sha256")) \
                    or type(candidate.get("item_count")) is not int \
                    or candidate["item_count"] < 0:
                failures.append("candidate_identity_incomplete")
            if not failures and proof_summary is not None:
                source_class = proof_summary["source_class"]
                group = quality_by_class.setdefault(source_class, {
                    "source_count": 0, "gold_case_count": 0, "query_count": 0,
                })
                group["source_count"] += 1
                group["gold_case_count"] += proof_summary["gold_case_count"]
                group["query_count"] += proof_summary["query_count"]
                candidate_set.append({
                    "source_id": source["source_id"],
                    "candidate_generation_id": candidate["generation_id"],
                    "expected_active_generation_id": None,
                    "evidence_id": candidate["evidence_id"],
                    "evidence_manifest_sha256": candidate["qualification_manifest_sha256"],
                    "source_watermark_sha256": candidate["source_watermark_sha256"],
                    "verification_manifest_sha256": candidate["verification_manifest_sha256"],
                    "adapter_profile_id": candidate["adapter_profile_id"],
                    "analysis_profile_id": candidate["analysis_profile_id"],
                    "search_profile_id": candidate["search_profile_id"],
                    "gold_suite_sha256": proof_summary["suite_sha256"],
                })
        for failure in failures:
            blockers[failure] += 1
        protected_sources.append({
            "source_id": source["source_id"],
            "source_ref": source["source_ref"],
            "is_test": source["is_test"],
            "candidate_generation_id": candidates[0]["generation_id"] if candidates else None,
            "blockers": failures,
        })
    if benchmark_count != 1:
        blockers["benchmark_classification_invalid"] = 1
    candidate_set_complete = not blockers and len(candidate_set) == len(sources)
    candidate_set.sort(key=lambda item: item["source_id"])
    candidate_set_sha256 = (hashlib.sha256(canonical(candidate_set)).hexdigest()
                            if candidate_set_complete else None)
    protected = {
        "schema_version": "mainrag.storage-v2.candidate-aggregate-audit.v1",
        "status": "BLOCKED",
        "inventory_id": inventory.get("inventory_id"),
        "inventory_sha256": inventory_sha256,
        "source_count": len(sources),
        "release_candidate_source_count": candidate_count,
        "benchmark_source_count": benchmark_count,
        "benchmark_candidate_count": benchmark_candidate_count,
        "persisted_gate_blockers": dict(sorted(blockers.items())),
        "persisted_candidate_set_complete": candidate_set_complete,
        "candidate_set_sha256": candidate_set_sha256,
        "candidate_set": candidate_set if candidate_set_complete else [],
        "quality_by_class": dict(sorted(quality_by_class.items())),
        "external_gates_not_proven_by_inventory": list(EXTERNAL_GATES),
        "sources": protected_sources,
    }
    public = {
        "schema_version": "mainrag.storage-v2.candidate-aggregate-audit-summary.v1",
        "status": "BLOCKED",
        "audit_id": str(uuid.uuid4()),
        "inventory_sha256": inventory_sha256,
        "source_count": len(sources),
        "release_candidate_source_count": candidate_count,
        "benchmark_source_count": benchmark_count,
        "benchmark_candidate_count": benchmark_candidate_count,
        "persisted_gate_blockers": protected["persisted_gate_blockers"],
        "persisted_candidate_set_complete": candidate_set_complete,
        "candidate_set_sha256": candidate_set_sha256,
        "validated_source_class_count": len(quality_by_class),
        "validated_query_count": sum(value["query_count"] for value in quality_by_class.values()),
        "external_gate_count": len(EXTERNAL_GATES),
        "limitations": [
            "Protected database snapshot only; current watermarks, writers, resources, package and acceptance are not proven.",
            "No build, qualification, activation or cleanup was performed.",
        ],
    }
    protected["audit_id"] = public["audit_id"]
    public["protected_sha256"] = hashlib.sha256(canonical(protected) + b"\n").hexdigest()
    return protected, public


def private_create(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as output:
            output.write(canonical(value) + b"\n")
            output.flush()
            os.fsync(output.fileno())
        os.link(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inventory", required=True, type=Path)
    parser.add_argument("--expected-inventory-sha256", required=True)
    parser.add_argument("--protected-output", required=True, type=Path)
    arguments = parser.parse_args()
    if arguments.protected_output.exists() or arguments.protected_output.is_symlink():
        parser.error("protected audit output already exists")
    try:
        inventory, digest = read_protected_inventory(
            arguments.inventory, arguments.expected_inventory_sha256)
        protected, public = audit(inventory, digest)
        private_create(arguments.protected_output, protected)
    except FileExistsError:
        parser.error("protected audit output appeared during capture")
    except RuntimeError as error:
        parser.error(str(error))
    except OSError:
        parser.error("protected audit output could not be written")
    print(json.dumps(public, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())

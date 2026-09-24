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
            manifest = candidate.get("qualification_manifest")
            if not isinstance(manifest, dict) or manifest.get("status") != "PASS":
                failures.append("qualification_manifest_not_pass")
            else:
                checks = manifest.get("checks")
                if not isinstance(checks, dict) or any(checks.get(name) != "PASS" for name in CHECKS):
                    failures.append("qualification_checks_incomplete")
                if not gold_summary_valid(manifest.get("gold_suite_summary")):
                    failures.append("gold_suite_binding_missing")
            if not all(candidate.get(key) for key in (
                "evidence_id", "commit_sha", "source_watermark_sha256",
                "qualification_manifest_sha256"
            )):
                failures.append("candidate_identity_incomplete")
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

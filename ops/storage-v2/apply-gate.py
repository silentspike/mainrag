#!/usr/bin/env python3
"""Execute one explicitly approved storage-v2 preparation gate."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import preflight


GATE_REQUIREMENTS = {
    "postgresql-minor-upgrade": {
        "target": "postgresql_version",
        "prerequisites": (
            "candidate_identity", "capacity", "backup", "maintenance",
            "active_operations", "backend_lock",
        ),
    },
    "postgresql-configuration": {
        "target": "configuration",
        "prerequisites": (
            "candidate_identity", "postgresql_version", "capacity", "backup",
            "maintenance", "active_operations", "backend_lock",
        ),
    },
    "schema-extension-upgrade": {
        "target": "extensions",
        "prerequisites": (
            "candidate_identity", "postgresql_version", "configuration",
            "capacity", "backup", "maintenance", "active_operations",
            "backend_lock",
        ),
    },
    "collation-refresh": {
        "target": "collation",
        "prerequisites": (
            "candidate_identity", "postgresql_version", "extensions",
            "configuration", "capacity", "backup", "maintenance",
            "active_operations", "backend_lock",
        ),
    },
    "backend-index": {
        "target": "backend_index",
        "prerequisites": (
            "candidate_identity", "postgresql_version", "extensions",
            "configuration", "collation", "capacity", "backup",
            "maintenance", "active_operations", "backend_lock",
        ),
    },
}


def validate_bindings(
    gate_name: str,
    manifest_digest: str,
    expected_manifest_digest: str,
    adapter_digest: str,
    expected_adapter_digest: str,
    approval: str,
) -> None:
    if manifest_digest != expected_manifest_digest:
        raise ValueError("checked manifest digest differs from the expected digest")
    if adapter_digest != expected_adapter_digest:
        raise ValueError("gate adapter digest differs from the expected digest")
    expected_approval = f"APPLY:{gate_name}:{manifest_digest}:{adapter_digest}"
    if approval != expected_approval:
        raise ValueError("operator approval does not bind the exact gate, manifest, and adapter")


def collect(args: argparse.Namespace, policy: dict) -> dict:
    snapshot, client_version, schema_sha256 = preflight.collect_database(
        args.database,
        policy["allowed_database_applications"],
        ["sudo", "-n", "-u", "postgres"] if args.local_postgres else None,
    )
    units = preflight.collect_units(policy)
    backup = preflight.load_backup(
        args.backup_evidence, int(policy["backup_max_age_seconds"]), int(time.time())
    )
    commit, tracked_clean = preflight.git_state()
    return preflight.evaluate(
        snapshot, client_version, schema_sha256, units, backup, policy,
        commit_sha=commit, tracked_clean=tracked_clean,
    )


def validate_before(checked: dict, before: dict, gate_name: str) -> None:
    if before["state_sha256"] != checked.get("state_sha256"):
        raise ValueError("live state drifted from the checked manifest")
    gate = GATE_REQUIREMENTS[gate_name]
    failed_prerequisites = [
        name for name in gate["prerequisites"] if before["checks"].get(name) != "PASS"
    ]
    if failed_prerequisites:
        raise ValueError("gate prerequisites are not PASS: " + ", ".join(failed_prerequisites))
    if before["checks"].get(gate["target"]) == "PASS":
        raise ValueError("the selected gate is already PASS")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", choices=sorted(GATE_REQUIREMENTS), required=True)
    parser.add_argument("--checked-manifest", type=Path, required=True)
    parser.add_argument("--expected-manifest-sha256", required=True)
    parser.add_argument("--adapter", type=Path, required=True)
    parser.add_argument("--expected-adapter-sha256", required=True)
    parser.add_argument("--operator-approval", required=True)
    parser.add_argument("--database", default=os.environ.get("MAINRAG_PREFLIGHT_DATABASE", "mainrag"))
    parser.add_argument("--policy", type=Path, default=preflight.DEFAULT_POLICY)
    parser.add_argument("--backup-evidence", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--local-postgres", action="store_true")
    args = parser.parse_args()

    manifest_digest = preflight.sha256_file(args.checked_manifest)
    adapter_digest = preflight.sha256_file(args.adapter)
    try:
        validate_bindings(
            args.apply,
            manifest_digest,
            args.expected_manifest_sha256,
            adapter_digest,
            args.expected_adapter_sha256,
            args.operator_approval,
        )
    except ValueError as error:
        parser.error(str(error))

    checked = json.loads(args.checked_manifest.read_text(encoding="utf-8"))
    if checked.get("schema_version") != preflight.SCHEMA_VERSION:
        parser.error("checked manifest has an unsupported schema")
    policy = json.loads(args.policy.read_text(encoding="utf-8"))
    before = collect(args, policy)
    gate = GATE_REQUIREMENTS[args.apply]
    try:
        validate_before(checked, before, args.apply)
    except ValueError as error:
        parser.error(str(error))

    environment = os.environ.copy()
    environment["MAINRAG_APPROVED_GATE"] = args.apply
    environment["MAINRAG_BEFORE_STATE_SHA256"] = before["state_sha256"]
    result = subprocess.run(
        [str(args.adapter.resolve())],
        cwd=preflight.ROOT,
        env=environment,
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if result.returncode != 0:
        raise RuntimeError(f"approved gate adapter failed with exit {result.returncode}")

    after = collect(args, policy)
    if after["checks"].get(gate["target"]) != "PASS":
        raise RuntimeError("gate target is not PASS after the adapter completed")
    regressed = [
        name for name, status in before["checks"].items()
        if status == "PASS" and after["checks"].get(name) != "PASS"
    ]
    if regressed:
        raise RuntimeError("previously passing checks regressed: " + ", ".join(regressed))

    evidence = {
        "schema_version": "mainrag-storage-v2-apply-evidence/v1",
        "status": "PASS",
        "gate": args.apply,
        "checked_manifest_sha256": manifest_digest,
        "adapter_sha256": adapter_digest,
        "before_state_sha256": before["state_sha256"],
        "after_state_sha256": after["state_sha256"],
        "target_check": gate["target"],
        "recorded_at_unix": int(time.time()),
        "limitations": [
            "This record proves one gate adapter and immediate readback only.",
            "Restore, recovery, activation, backfill, cleanup, and release are not implied.",
        ],
    }
    preflight.write_manifest(args.output, evidence)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as error:
        print(f"BLOCKED: {error}", file=sys.stderr)
        raise SystemExit(3)

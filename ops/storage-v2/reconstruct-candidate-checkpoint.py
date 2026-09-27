#!/usr/bin/env python3
"""Recover a build checkpoint from sealed database witness and live adapter state.

This is a read-only reconciliation step for a verified generation whose local
build checkpoint was lost. It does not change the generation or its build identity.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import runpy
import shutil
import stat
import subprocess
import time


operator = runpy.run_path(str(Path(__file__).with_name("release-candidate.py")))
load_token = runpy.run_path(str(Path(__file__).with_name("operator_token.py")))["load_token"]
SHA = re.compile(r"[0-9a-f]{64}\Z")
COMMIT = re.compile(r"[0-9a-f]{40}\Z")


def database_generation(database: str, source_id: int, generation_id: int) -> dict:
    statement = f"""
BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY;
SET LOCAL row_security = off;
SELECT jsonb_build_object(
    'source_id', generation.source_id,
    'generation_id', generation.id,
    'generation_seq', generation.generation_seq,
    'status', generation.status::TEXT,
    'item_count', generation.item_count,
    'created_at_unix', extract(epoch from generation.created_at)::BIGINT,
    'witness', generation.witness,
    'run_status', run.status,
    'run_adapter_profile_id', run.adapter_profile_id,
    'run_expected_item_count', run.expected_item_count,
    'active_generation_id', logical.active_generation_id,
    'source_type', source.type
)
FROM source_generation generation
JOIN logical_source logical ON logical.id = generation.source_id
JOIN sources source ON source.id = generation.source_id
JOIN storage_v2_ingest_run run ON run.generation_id = generation.id
WHERE generation.source_id = {source_id} AND generation.id = {generation_id};
COMMIT;
"""
    environment = os.environ.copy()
    environment["PGOPTIONS"] = (
        environment.get("PGOPTIONS", "")
        + " -c default_transaction_read_only=on -c row_security=off"
    ).strip()
    result = subprocess.run(
        ["sudo", "-n", "-u", "postgres", "psql", "-X", "--no-psqlrc", "-qAt",
         "--set=ON_ERROR_STOP=1", "--dbname", database, "--command", statement],
        env=environment, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise RuntimeError("read-only generation reconciliation query failed")
    lines = [line for line in result.stdout.splitlines() if line.startswith("{")]
    if len(lines) != 1:
        raise RuntimeError("exactly one persisted generation is required")
    return json.loads(lines[0])


def api_service_start() -> tuple[int, int]:
    result = subprocess.run(
        ["systemctl", "show", "mainrag-api.service", "-p", "ExecMainPID", "--value"],
        capture_output=True, text=True, check=False)
    if result.returncode != 0 or not result.stdout.strip().isdigit():
        raise RuntimeError("API process identity is unavailable")
    pid = int(result.stdout.strip())
    if pid <= 0:
        raise RuntimeError("API process is inactive")
    age = subprocess.run(["ps", "-o", "etimes=", "-p", str(pid)],
                         capture_output=True, text=True, check=False)
    if age.returncode != 0 or not age.stdout.strip().isdigit():
        raise RuntimeError("API process start age is unavailable")
    return pid, int(time.time()) - int(age.stdout.strip())


def reconstruct(args: argparse.Namespace) -> dict:
    generation = database_generation(args.database, args.source_id, args.generation_id)
    witness = generation["witness"]
    if generation["status"] != "verified" or generation["run_status"] != "sealed" \
            or generation["source_type"] not in {"fs", "git", "managed_append"} \
            or generation["item_count"] != generation["run_expected_item_count"] \
            or not isinstance(witness, dict) \
            or witness.get("kind") != "release-candidate-build" \
            or not COMMIT.fullmatch(witness.get("commit_sha", "")) \
            or not all(SHA.fullmatch(witness.get(key, "")) for key in (
                "fixture_sha256", "source_watermark_sha256")) \
            or witness.get("adapter_profile_id") != generation["run_adapter_profile_id"]:
        raise RuntimeError("persisted build or verification witness is incomplete")
    token = load_token(args.token_file, args.token_env)
    review_sha256 = None
    if args.source_snapshot_review is not None:
        path = args.source_snapshot_review
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) & 0o077:
            raise RuntimeError("source snapshot review is not a private regular file")
        raw = path.read_bytes()
        review_sha256 = hashlib.sha256(raw).hexdigest()
        if review_sha256 != args.source_snapshot_review_sha256:
            raise RuntimeError("source snapshot review digest differs")
        observed = json.loads(raw)
        if observed.get("source_type") != generation["source_type"]:
            raise RuntimeError("source snapshot review adapter differs")
    else:
        observed = operator["request"](
            args.api_url, token, "GET",
            f"/api/v1/admin/sources/{args.source_id}/storage-v2-release-watermark",
            timeout_seconds=600)
    if observed.get("source_id") != args.source_id \
            or observed.get("source_watermark_sha256") != witness["source_watermark_sha256"] \
            or observed.get("adapter_profile_id") != witness["adapter_profile_id"] \
            or observed.get("item_count") != generation["item_count"]:
        raise RuntimeError("adapter observation differs from the persisted build witness")
    state = operator["source_state"](
        args.api_url, token, args.source_id, generation["generation_seq"])
    if state.get("active_generation_id") != generation["active_generation_id"]:
        raise RuntimeError("active pointer changed during reconciliation")
    pid, service_start_unix = api_service_start()
    if service_start_unix <= generation["created_at_unix"] + 2:
        raise RuntimeError("API restart after original build is not established")
    thin_pool = operator["thin_pool_capacity"](args.pack_root, 0, require_estimate=False)
    if thin_pool is None:
        raise RuntimeError("current pack pool identity is unavailable")
    return {
        "schema_version": 1,
        "reconstructed_from_persisted_witness": True,
        "reconstruction_evidence": {
            "generation_created_at_unix": generation["created_at_unix"],
            "api_service_start_unix": service_start_unix,
            "api_service_pid": pid,
            "source_type": generation["source_type"],
            "run_status": generation["run_status"],
            "generation_status": generation["status"],
            "source_snapshot_review_sha256": review_sha256,
            "current_watermark_observed": review_sha256 is None,
        },
        "source_ref": os.urandom(32).hex(),
        "source_id": args.source_id,
        "commit_sha": witness["commit_sha"],
        "generation_id": args.generation_id,
        "generation_seq": generation["generation_seq"],
        "source_watermark_sha256": witness["source_watermark_sha256"],
        "item_count": generation["item_count"],
        "server_instance_id": state["server_instance_id"],
        "active_generation_id": generation["active_generation_id"],
        "pack_capacity_before_build": {"thin_pool": thin_pool},
        "pack_free_bytes_after_build": shutil.disk_usage(args.pack_root).free,
        "resource_gate_after_build": "RECONSTRUCTED_CURRENT_RESERVE",
        "build": {"fixture_sha256": witness["fixture_sha256"]},
        "captured_at_unix": int(time.time()),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True)
    parser.add_argument("--api-url", required=True)
    parser.add_argument("--token-env", default="MAINRAG_TOKEN")
    parser.add_argument("--token-file", type=Path)
    parser.add_argument("--source-id", type=int, required=True)
    parser.add_argument("--generation-id", type=int, required=True)
    parser.add_argument("--pack-root", type=Path, required=True)
    parser.add_argument("--checkpoint", type=Path, required=True)
    parser.add_argument("--source-snapshot-review", type=Path)
    parser.add_argument("--source-snapshot-review-sha256")
    args = parser.parse_args()
    if args.source_id <= 0 or args.generation_id <= 0 or not args.pack_root.is_dir():
        parser.error("positive source/generation and existing pack root required")
    if (args.source_snapshot_review is None) != (args.source_snapshot_review_sha256 is None):
        parser.error("source review requires its exact digest")
    checkpoint = reconstruct(args)
    operator["atomic_private_json"](args.checkpoint, checkpoint, replace=False)
    print(json.dumps({"status": "RECONSTRUCTED", "generation_seq": checkpoint["generation_seq"],
                      "item_count": checkpoint["item_count"]}, sort_keys=True))


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError, ValueError, KeyError, TypeError) as error:
        print(json.dumps({"status": "BLOCKED", "reason": str(error)}))
        raise SystemExit(1) from error

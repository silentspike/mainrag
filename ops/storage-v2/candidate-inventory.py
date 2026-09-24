#!/usr/bin/env python3
"""Capture a protected, read-only source and candidate inventory for issue 66."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


INVENTORY_SQL = r"""
BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY;
SET LOCAL row_security = off;
SELECT COALESCE(jsonb_agg(jsonb_build_object(
    'source_id', source.id,
    'name', source.name,
    'source_type', source.type,
    'path', source.path,
    'config', source.config,
    'is_test', source.is_test,
    'last_synced', source.last_synced,
    'updated_at', source.updated_at,
    'file_count', COALESCE(source.file_count, 0),
    'total_size', COALESCE(source.total_size, 0),
    'active_generation_id', logical.active_generation_id,
    'next_generation_seq', logical.next_generation_seq,
    'generations', COALESCE(generations.value, '[]'::jsonb)
) ORDER BY source.id), '[]'::jsonb)
FROM sources AS source
LEFT JOIN logical_source AS logical ON logical.id = source.id
LEFT JOIN LATERAL (
    SELECT jsonb_agg(jsonb_build_object(
        'generation_id', generation.id,
        'generation_seq', generation.generation_seq,
        'status', generation.status::text,
        'item_count', generation.item_count,
        'verification_manifest_sha256', generation.verification_manifest_sha256,
        'evidence_id', evidence.id,
        'commit_sha', evidence.commit_sha,
        'source_watermark_sha256', evidence.source_watermark_sha256
    ) ORDER BY generation.generation_seq) AS value
    FROM source_generation AS generation
    LEFT JOIN storage_v2_release_candidate_evidence AS evidence
      ON evidence.generation_id = generation.id
    WHERE generation.source_id = source.id
) AS generations ON TRUE;
COMMIT;
"""


def canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


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


def read_sources(database: str, local_postgres: bool = False) -> list[dict]:
    command = (["sudo", "-n", "-u", "postgres"] if local_postgres else []) + [
        "psql", "-X", "--no-psqlrc", "-qAt", "--set=ON_ERROR_STOP=1",
        "--dbname", database, "--command", INVENTORY_SQL,
    ]
    environment = os.environ.copy()
    environment["PGAPPNAME"] = "mainrag-storage-v2-candidate-inventory"
    environment["PGOPTIONS"] = (
        environment.get("PGOPTIONS", "") + " -c default_transaction_read_only=on -c row_security=off"
    ).strip()
    try:
        result = subprocess.run(command, capture_output=True, text=True, check=False, env=environment)
    except OSError as error:
        raise RuntimeError("read-only source inventory query could not start") from error
    if result.returncode != 0:
        raise RuntimeError("read-only source inventory query failed")
    try:
        rows = json.loads(result.stdout.strip())
    except (ValueError, TypeError) as error:
        raise RuntimeError("source inventory query returned invalid JSON") from error
    if not isinstance(rows, list):
        raise RuntimeError("source inventory query did not return an array")
    return rows


def capture(rows: list[dict], operator_commit_sha: str) -> tuple[dict, dict]:
    if not rows:
        raise RuntimeError("source inventory is empty")
    if len(operator_commit_sha) != 40 or any(
        character not in "0123456789abcdef" for character in operator_commit_sha
    ):
        raise RuntimeError("exact lowercase operator commit SHA is required")
    seen: set[int] = set()
    protected = []
    type_counts: Counter[str] = Counter()
    generation_counts: Counter[str] = Counter()
    test_count = 0
    candidate_source_count = 0
    for row in rows:
        if not isinstance(row, dict) or type(row.get("source_id")) is not int or row["source_id"] <= 0:
            raise RuntimeError("source inventory has an invalid identity")
        source_id = row["source_id"]
        if source_id in seen:
            raise RuntimeError("source inventory has duplicate identities")
        seen.add(source_id)
        if not all(isinstance(row.get(key), str) and row[key]
                   for key in ("name", "source_type", "path", "updated_at")):
            raise RuntimeError("source inventory has incomplete registration data")
        if type(row.get("is_test")) is not bool:
            raise RuntimeError("source inventory is missing benchmark classification")
        if any(type(row.get(key)) is not int or row[key] < 0
               for key in ("file_count", "total_size")):
            raise RuntimeError("source inventory has invalid current-path counts")
        generations = row.get("generations")
        if not isinstance(generations, list):
            raise RuntimeError("source inventory is missing generation state")
        if row.get("active_generation_id") is not None and not any(
            generation.get("generation_id") == row["active_generation_id"]
            and generation.get("status") == "active" for generation in generations
        ):
            raise RuntimeError("active pointer does not match generation state")
        candidate_count = sum(generation.get("status") == "release_candidate" for generation in generations)
        if candidate_count > 1:
            raise RuntimeError("source has multiple release candidates")
        generation_ids: set[int] = set()
        generation_sequences: set[int] = set()
        for generation in generations:
            if not isinstance(generation, dict) or generation.get("status") not in {
                "building", "sealed", "verified", "release_candidate", "active", "superseded"
            }:
                raise RuntimeError("source inventory has an invalid generation state")
            generation_id = generation.get("generation_id")
            generation_seq = generation.get("generation_seq")
            if (type(generation_id) is not int or generation_id <= 0
                or type(generation_seq) is not int or generation_seq <= 0
                or generation_id in generation_ids or generation_seq in generation_sequences):
                raise RuntimeError("source inventory has duplicate or invalid generations")
            generation_ids.add(generation_id)
            generation_sequences.add(generation_seq)
            if generation["status"] == "release_candidate" and not all(
                generation.get(key) for key in ("evidence_id", "commit_sha", "source_watermark_sha256")
            ):
                raise RuntimeError("release candidate is missing qualification evidence")
            generation_counts[generation["status"]] += 1
        reference = os.urandom(32).hex()
        protected.append({**row, "source_ref": reference,
                          "config_sha256": hashlib.sha256(canonical(row.get("config"))).hexdigest()})
        public_type = row["source_type"] if row["source_type"] in {
            "fs", "git", "web", "pdf", "export"
        } else "other"
        type_counts[public_type] += 1
        test_count += row["is_test"]
        candidate_source_count += bool(candidate_count)
    inventory = {
        "schema_version": "mainrag.storage-v2.candidate-inventory.v1",
        "capture_status": "OBSERVED_ONLY",
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "operator_commit_sha": operator_commit_sha,
        "inventory_id": str(uuid.uuid4()),
        "sources": protected,
    }
    public = {
        "schema_version": "mainrag.storage-v2.candidate-inventory-summary.v1",
        "capture_status": "OBSERVED_ONLY",
        "inventory_id": inventory["inventory_id"],
        "protected_sha256": hashlib.sha256(canonical(inventory) + b"\n").hexdigest(),
        "source_count": len(protected),
        "test_source_count": test_count,
        "release_candidate_source_count": candidate_source_count,
        "sources_without_candidate_count": len(protected) - candidate_source_count,
        "source_type_counts": dict(sorted(type_counts.items())),
        "generation_status_counts": dict(sorted(generation_counts.items())),
        "source_refs": sorted(item["source_ref"] for item in protected),
        "limitations": [
            "Database registration snapshot only; live adapter watermarks and writers require fresh per-source gates.",
            "No candidate build, qualification, activation, or cleanup was performed.",
        ],
    }
    return inventory, public


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True)
    parser.add_argument("--operator-commit-sha", required=True)
    parser.add_argument("--protected-output", type=Path, required=True)
    parser.add_argument("--local-postgres", action="store_true")
    arguments = parser.parse_args()
    try:
        actual_commit = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=ROOT,
            capture_output=True, text=True, check=True
        ).stdout.strip()
    except subprocess.CalledProcessError:
        parser.error("operator checkout identity is unavailable")
    if actual_commit != arguments.operator_commit_sha:
        parser.error("operator checkout differs from the named commit")
    tracked = subprocess.run(
        ["git", "diff", "--quiet", "HEAD", "--"], cwd=ROOT,
        capture_output=True, check=False
    )
    if tracked.returncode != 0:
        parser.error("operator checkout has tracked changes")
    owned = subprocess.run(
        ["git", "ls-files", "--error-unmatch", "ops/storage-v2/candidate-inventory.py"],
        cwd=ROOT, capture_output=True, check=False
    )
    if owned.returncode != 0:
        parser.error("operator script is not part of the named commit")
    if arguments.protected_output.exists() or arguments.protected_output.is_symlink():
        parser.error("protected inventory already exists")
    try:
        inventory, public = capture(
            read_sources(arguments.database, arguments.local_postgres), arguments.operator_commit_sha
        )
        private_create(arguments.protected_output, inventory)
    except FileExistsError:
        parser.error("protected inventory appeared during capture")
    except RuntimeError as error:
        parser.error(str(error))
    print(json.dumps(public, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())

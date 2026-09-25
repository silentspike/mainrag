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
import re
import stat
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
        'source_watermark_sha256', evidence.source_watermark_sha256,
        'adapter_profile_id', evidence.adapter_profile_id,
        'analysis_profile_id', evidence.analysis_profile_id,
        'search_profile_id', evidence.search_profile_id,
        'qualification_manifest', evidence.manifest,
        'qualification_manifest_sha256', encode(evidence.manifest_sha256, 'hex'),
        'qualification_manifest_digest_matches',
            evidence.manifest_sha256 = digest(convert_to(evidence.manifest::TEXT, 'UTF8'), 'sha256')
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


def capture(rows: list[dict], operator_commit_sha: str,
            candidate_commit_sha: str | None,
            candidate_commit_map: dict | None = None,
            candidate_commit_map_sha256: str | None = None) -> tuple[dict, dict]:
    if not rows:
        raise RuntimeError("source inventory is empty")
    if len(operator_commit_sha) != 40 or any(
        character not in "0123456789abcdef" for character in operator_commit_sha
    ):
        raise RuntimeError("exact lowercase operator commit SHA is required")
    if (candidate_commit_sha is None) == (candidate_commit_map is None):
        raise RuntimeError("exactly one candidate package identity mode is required")
    if candidate_commit_sha is not None and not re.fullmatch(r"[0-9a-f]{40}", candidate_commit_sha):
        raise RuntimeError("exact lowercase candidate package commit SHA is required")
    expected_by_source: dict[int, tuple[int, str]] = {}
    if candidate_commit_map is not None:
        if not isinstance(candidate_commit_map, dict) \
                or candidate_commit_map.get("schema_version") != "mainrag.storage-v2.final-candidate-commit-map.v1" \
                or not re.fullmatch(r"[0-9a-f]{64}", candidate_commit_map_sha256 or "") \
                or not isinstance(candidate_commit_map.get("sources"), list):
            raise RuntimeError("protected final candidate commit map differs")
        for item in candidate_commit_map["sources"]:
            if not isinstance(item, dict) or type(item.get("source_id")) is not int \
                    or item["source_id"] <= 0 or item["source_id"] in expected_by_source \
                    or type(item.get("candidate_generation_id")) is not int \
                    or item["candidate_generation_id"] <= 0 \
                    or not isinstance(item.get("candidate_commit_sha"), str) \
                    or not re.fullmatch(r"[0-9a-f]{40}", item["candidate_commit_sha"]):
                raise RuntimeError("final candidate commit map has invalid source identity")
            expected_by_source[item["source_id"]] = (
                item["candidate_generation_id"], item["candidate_commit_sha"])
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
        if candidate_commit_map is not None:
            expected = expected_by_source.get(source_id)
            candidates = [generation for generation in generations
                          if generation.get("status") == "release_candidate"]
            if expected is None or len(candidates) != 1 or (
                candidates[0].get("generation_id"), candidates[0].get("commit_sha")
            ) != expected:
                raise RuntimeError("final candidate commit map differs from live inventory")
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
                generation.get(key) for key in (
                    "evidence_id", "commit_sha", "source_watermark_sha256",
                    "adapter_profile_id", "analysis_profile_id", "search_profile_id",
                    "qualification_manifest", "qualification_manifest_sha256",
                )
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
    if candidate_commit_map is not None and set(expected_by_source) != seen:
        raise RuntimeError("final candidate commit map does not cover every source")
    inventory = {
        "schema_version": ("mainrag.storage-v2.candidate-inventory.v2"
                           if candidate_commit_map is not None else
                           "mainrag.storage-v2.candidate-inventory.v1"),
        "capture_status": "OBSERVED_ONLY",
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "operator_commit_sha": operator_commit_sha,
        "candidate_commit_sha": candidate_commit_sha,
        "candidate_commit_map_sha256": candidate_commit_map_sha256,
        "candidate_commit_map": candidate_commit_map,
        "inventory_id": str(uuid.uuid4()),
        "sources": protected,
    }
    public = {
        "schema_version": ("mainrag.storage-v2.candidate-inventory-summary.v2"
                           if candidate_commit_map is not None else
                           "mainrag.storage-v2.candidate-inventory-summary.v1"),
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


def read_commit_map(path: Path, expected_sha256: str) -> dict:
    if not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
        raise RuntimeError("exact protected commit-map digest is required")
    try:
        metadata = path.lstat()
    except OSError as error:
        raise RuntimeError("protected final candidate commit map is unavailable") from error
    if not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) & 0o077 \
            or metadata.st_size > 2 * 1024 * 1024:
        raise RuntimeError("final candidate commit map must be a bounded private file")
    try:
        raw = path.read_bytes()
    except OSError as error:
        raise RuntimeError("protected final candidate commit map is unavailable") from error
    if hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise RuntimeError("final candidate commit map digest differs")
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("duplicate JSON key")
            value[key] = item
        return value
    try:
        value = json.loads(raw, object_pairs_hook=unique)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise RuntimeError("final candidate commit map is invalid JSON") from error
    if not isinstance(value, dict):
        raise RuntimeError("final candidate commit map must be an object")
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True)
    parser.add_argument("--operator-commit-sha", required=True)
    identity = parser.add_mutually_exclusive_group(required=True)
    identity.add_argument("--candidate-commit-sha")
    identity.add_argument("--candidate-commit-map", type=Path)
    parser.add_argument("--candidate-commit-map-sha256")
    parser.add_argument("--protected-output", type=Path, required=True)
    parser.add_argument("--local-postgres", action="store_true")
    arguments = parser.parse_args()
    if (arguments.candidate_commit_map is None) != (arguments.candidate_commit_map_sha256 is None):
        parser.error("commit map requires its exact protected SHA-256")
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
            read_sources(arguments.database, arguments.local_postgres),
            arguments.operator_commit_sha, arguments.candidate_commit_sha,
            read_commit_map(arguments.candidate_commit_map,
                            arguments.candidate_commit_map_sha256)
            if arguments.candidate_commit_map is not None else None,
            arguments.candidate_commit_map_sha256,
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

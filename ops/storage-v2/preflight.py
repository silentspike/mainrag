#!/usr/bin/env python3
"""Fail-closed, redacted storage-v2 PostgreSQL preparation preflight."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
DEFAULT_POLICY = HERE / "preflight-policy.json"
SCHEMA_VERSION = "mainrag-storage-v2-preflight/v1"


DATABASE_SNAPSHOT_SQL = r"""
WITH settings AS (
    SELECT jsonb_object_agg(name, current_setting(name) ORDER BY name) AS value
      FROM pg_settings
     WHERE name = ANY (ARRAY[
        'shared_preload_libraries', 'shared_buffers', 'effective_cache_size',
        'work_mem', 'maintenance_work_mem', 'max_connections', 'max_wal_size',
        'max_worker_processes', 'max_parallel_workers',
        'max_parallel_maintenance_workers', 'io_method', 'track_io_timing',
        'autovacuum', 'statement_timeout'
     ])
), extensions AS (
    SELECT COALESCE(
        jsonb_agg(jsonb_build_object('name', extname, 'version', extversion)
                  ORDER BY extname), '[]'::jsonb
    ) AS value
      FROM pg_extension
), collation_state AS (
    SELECT jsonb_build_object(
        'database_stored_version', d.datcollversion,
        'database_actual_version', pg_database_collation_actual_version(d.oid),
        'database_mismatch', d.datcollversion IS DISTINCT FROM
            pg_database_collation_actual_version(d.oid),
        'explicit_mismatch_count', (
            SELECT count(*)
              FROM pg_collation c
             WHERE c.collversion IS NOT NULL
               AND c.collversion IS DISTINCT FROM
                   pg_collation_actual_version(c.oid)
        ),
        'potentially_affected_index_count', (
            SELECT count(DISTINCT i.indexrelid)
              FROM pg_index i
              JOIN pg_class t ON t.oid = i.indrelid
              JOIN pg_namespace n ON n.oid = t.relnamespace
              JOIN pg_attribute a ON a.attrelid = i.indrelid
                                  AND a.attnum = ANY(i.indkey)
             WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')
               AND a.attcollation <> 0
        )
    ) AS value
      FROM pg_database d
     WHERE d.datname = current_database()
), activity AS (
    SELECT jsonb_build_object(
        'active_writer_count', count(*) FILTER (
            WHERE pid <> pg_backend_pid()
              AND state <> 'idle'
              AND query ~* '(insert|update|delete|merge|copy|create[[:space:]]+index|reindex|vacuum|cluster|refresh[[:space:]]+materialized)'
        ),
        'unknown_application_count', count(*) FILTER (
            WHERE pid <> pg_backend_pid()
              AND state <> 'idle'
              AND application_name <> ALL(
                  string_to_array(current_setting('mainrag.preflight_allowed_apps', true), ',')
              )
        ),
        'active_index_build_count', (SELECT count(*) FROM pg_stat_progress_create_index),
        'active_vacuum_count', (SELECT count(*) FROM pg_stat_progress_vacuum),
        'active_backfill_count', count(*) FILTER (
            WHERE pid <> pg_backend_pid()
              AND state <> 'idle'
              AND query ~* '(backfill|storage[_-]v2.*(build|ingest|candidate))'
        )
    ) AS value
      FROM pg_stat_activity
     WHERE datname = current_database()
), capacity AS (
    SELECT jsonb_build_object(
        'database_bytes', pg_database_size(current_database()),
        'index_bytes', COALESCE((
            SELECT sum(pg_relation_size(indexrelid)) FROM pg_index
        ), 0),
        'wal_bytes', COALESCE((
            SELECT sum(size) FROM pg_ls_waldir()
        ), 0),
        'data_directory', current_setting('data_directory')
    ) AS value
), backend AS (
    SELECT jsonb_build_object(
        'required_index_count', 2,
        'valid_required_index_count', count(*) FILTER (
            WHERE c.relname IN (
                'idx_storage_v2_search_document_fts',
                'idx_storage_v2_search_document_exact'
            )
              AND i.indisvalid
              AND i.indisready
              AND am.amname = 'gin'
        )
    ) AS value
      FROM pg_class c
      JOIN pg_index i ON i.indexrelid = c.oid
      JOIN pg_am am ON am.oid = c.relam
)
SELECT jsonb_build_object(
    'server_version', current_setting('server_version'),
    'server_version_num', current_setting('server_version_num')::integer,
    'settings', settings.value,
    'extensions', extensions.value,
    'collation', collation_state.value,
    'activity', activity.value,
    'capacity', capacity.value,
    'backend', backend.value
)
FROM settings, extensions, collation_state, activity, capacity, backend;
"""


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def combined_hash(paths: list[Path]) -> str:
    digest = hashlib.sha256()
    for path in paths:
        relative = path.relative_to(ROOT).as_posix().encode("utf-8")
        content = path.read_bytes()
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def run(command: list[str], *, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        command,
        cwd=ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"command failed with exit {result.returncode}: {command[0]}")
    return result


def parse_version(output: str) -> str:
    match = re.search(r"([0-9]+\.[0-9]+)(?:\.[0-9]+)?", output)
    if match is None:
        raise RuntimeError("PostgreSQL client version is not parseable")
    return match.group(1)


def tuning_expectations(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or "=" not in line:
            continue
        name, value = line.split("=", 1)
        values[name.strip()] = value.strip().strip("'")
    return values


def collect_database(
    database: str,
    allowed_apps: list[str],
    command_prefix: list[str] | None = None,
) -> tuple[dict[str, Any], str, str]:
    prefix = command_prefix or []
    client = run([*prefix, "psql", "--version"]).stdout
    environment = os.environ.copy()
    environment["PGAPPNAME"] = "mainrag-storage-v2-preflight"
    apps = ",".join(allowed_apps)
    environment["PGOPTIONS"] = (
        environment.get("PGOPTIONS", "")
        + f" -c mainrag.preflight_allowed_apps={apps}"
    ).strip()
    snapshot_result = run(
        [
            *prefix, "psql", "-X", "--no-psqlrc", "--tuples-only", "--no-align",
            "--set=ON_ERROR_STOP=1", "--dbname", database,
            "--command", DATABASE_SNAPSHOT_SQL,
        ],
        env=environment,
    )
    snapshot = json.loads(snapshot_result.stdout.strip())
    schema = run(
        [
            *prefix, "pg_dump", "--schema-only", "--no-owner", "--no-privileges",
            "--dbname", database,
        ],
        env=environment,
    ).stdout.encode("utf-8")
    data_directory = Path(snapshot["capacity"].pop("data_directory"))
    if prefix:
        free_output = run(
            [*prefix, "df", "--block-size=1", "--output=avail", str(data_directory)]
        ).stdout.splitlines()
        snapshot["capacity"]["filesystem_free_bytes"] = int(free_output[-1].strip())
    else:
        usage = shutil.disk_usage(data_directory)
        snapshot["capacity"]["filesystem_free_bytes"] = usage.free
    return snapshot, parse_version(client), sha256_bytes(schema)


def unit_state(name: str) -> dict[str, str]:
    result = subprocess.run(
        ["systemctl", "show", name, "--property=LoadState,ActiveState,SubState,UnitFileState", "--value"],
        check=False,
        capture_output=True,
        text=True,
    )
    values = result.stdout.splitlines()
    while len(values) < 4:
        values.append("unknown")
    return {
        "load": values[0] or "unknown",
        "active": values[1] or "unknown",
        "sub": values[2] or "unknown",
        "enabled": values[3] or "unknown",
    }


def collect_units(policy: dict[str, Any]) -> dict[str, Any]:
    known = {entry["name"]: unit_state(entry["name"]) for entry in policy["managed_units"]}
    listing = subprocess.run(
        ["systemctl", "list-units", "--type=service", "--state=running", "--plain", "--no-legend"],
        check=False,
        capture_output=True,
        text=True,
    )
    if listing.returncode != 0:
        running = []
        discovery_failed = True
    else:
        running = [line.split()[0] for line in listing.stdout.splitlines() if line.strip()]
        discovery_failed = False
    declared = set(known)
    unknown = sorted(name for name in running if "mainrag" in name.lower() and name not in declared)
    active_writers = sorted(
        entry["name"] for entry in policy["managed_units"]
        if entry["writes"] and known[entry["name"]]["active"] == "active"
    )
    timers = sorted(name for name in known if name.endswith(".timer"))
    active_timers = sorted(name for name in timers if known[name]["active"] == "active")
    read_behavior_counts: dict[str, int] = {}
    for entry in policy["managed_units"]:
        behavior = entry["read_behavior"]
        read_behavior_counts[behavior] = read_behavior_counts.get(behavior, 0) + 1
    return {
        "known_unit_count": len(known),
        "loaded_unit_count": sum(value["load"] == "loaded" for value in known.values()),
        "active_writer_count": len(active_writers),
        "active_writer_identity_sha256": sha256_bytes(canonical(active_writers)),
        "unknown_runtime_count": len(unknown),
        "unknown_runtime_identity_sha256": sha256_bytes(canonical(unknown)),
        "timer_count": len(timers),
        "active_timer_count": len(active_timers),
        "active_timer_identity_sha256": sha256_bytes(canonical(active_timers)),
        "read_behavior_counts": read_behavior_counts,
        "discovery_failed": discovery_failed,
        "state_sha256": sha256_bytes(canonical(known)),
    }


def load_backup(path: Path | None, max_age_seconds: int, now: int) -> dict[str, Any]:
    if path is None:
        return {
            "status": "BLOCKED",
            "evidence_level": "none",
            "restore_tested": False,
            "reason": "backup evidence was not supplied",
        }
    value = json.loads(path.read_text(encoding="utf-8"))
    required = {"schema_version", "status", "completed_at_unix", "artifact_sha256", "restore_tested"}
    if not required.issubset(value) or value["schema_version"] != 1:
        raise RuntimeError("backup evidence has an unsupported schema")
    age = max(0, now - int(value["completed_at_unix"]))
    passed = value["status"] == "PASS" and age <= max_age_seconds
    restore_tested = bool(value["restore_tested"])
    return {
        "status": "PASS" if passed else "BLOCKED",
        "evidence_level": "restore-exercised" if restore_tested else "backup-command-only",
        "restore_tested": restore_tested,
        "age_seconds": age,
        "artifact_sha256": value["artifact_sha256"],
        "evidence_sha256": sha256_file(path),
    }


def evaluate(
    snapshot: dict[str, Any],
    client_version: str,
    schema_sha256: str,
    units: dict[str, Any],
    backup: dict[str, Any],
    policy: dict[str, Any],
    *,
    commit_sha: str,
    tracked_clean: bool,
) -> dict[str, Any]:
    backend_path = ROOT / policy["accepted_backend_lock"]
    backend = json.loads(backend_path.read_text(encoding="utf-8"))
    backend_evidence_path = ROOT / policy["accepted_backend_evidence"]
    backend_evidence = json.loads(backend_evidence_path.read_text(encoding="utf-8"))
    backend_digests_passed = (
        sha256_file(backend_path) == policy["accepted_backend_lock_sha256"]
        and sha256_file(backend_evidence_path) == policy["accepted_backend_evidence_sha256"]
        and backend_evidence.get("status") == "PASS"
    )
    expected_version = backend["postgresql"]["version"]
    expected_extensions = policy["required_extensions"]
    observed_extensions = {entry["name"]: entry["version"] for entry in snapshot["extensions"]}
    extension_mismatches = sorted(
        name for name, version in expected_extensions.items()
        if observed_extensions.get(name) != version
    )

    expected_settings = tuning_expectations(ROOT / policy["tuning_config"])
    required_settings = set(policy["required_settings"])
    missing_settings = sorted(required_settings - set(snapshot["settings"]))
    compared_settings = sorted(required_settings & set(snapshot["settings"]))
    setting_mismatches = sorted(
        name for name in compared_settings
        if snapshot["settings"][name] != expected_settings[name]
    )

    capacity = snapshot["capacity"]
    capacity_policy = policy["capacity"]
    required_free = max(
        int(capacity_policy["minimum_free_bytes"]),
        int(capacity["database_bytes"] * float(capacity_policy["database_multiplier"]))
        + int(capacity["index_bytes"] * float(capacity_policy["index_multiplier"]))
        + int(capacity_policy["wal_reserve_bytes"]),
    )
    capacity_passed = capacity["filesystem_free_bytes"] >= required_free
    collation = snapshot["collation"]
    collation_passed = not collation["database_mismatch"] and collation["explicit_mismatch_count"] == 0
    activity = snapshot["activity"]
    maintenance_passed = (
        not units["discovery_failed"]
        and units["active_writer_count"] == 0
        and units["unknown_runtime_count"] == 0
        and activity["active_writer_count"] == 0
        and activity["unknown_application_count"] == 0
        and activity["active_index_build_count"] == 0
        and activity["active_vacuum_count"] == 0
        and activity["active_backfill_count"] == 0
    )

    migration_paths = [ROOT / relative for relative in policy["required_migrations"]]
    version_passed = snapshot["server_version"] == expected_version and client_version == expected_version
    backend_index_passed = (
        snapshot["backend"]["valid_required_index_count"]
        == snapshot["backend"]["required_index_count"]
    )
    config_passed = not setting_mismatches and not missing_settings
    checks = {
        "candidate_identity": "PASS" if tracked_clean and len(commit_sha) == 40 else "BLOCKED",
        "postgresql_version": "PASS" if version_passed else "BLOCKED",
        "extensions": "PASS" if not extension_mismatches else "BLOCKED",
        "configuration": "PASS" if config_passed else "BLOCKED",
        "collation": "PASS" if collation_passed else "BLOCKED",
        "capacity": "PASS" if capacity_passed else "BLOCKED",
        "backup": backup["status"],
        "maintenance": "PASS" if maintenance_passed else "BLOCKED",
        "active_operations": "PASS" if (
            activity["active_index_build_count"] == 0 and activity["active_vacuum_count"] == 0
            and activity["active_backfill_count"] == 0
        ) else "BLOCKED",
        "backend_lock": "PASS" if (
            backend_digests_passed
            and
            backend["backend"]["package_format"] == "none-built-in"
            and backend["backend"]["version"] == expected_version
        ) else "FAIL",
        "backend_index": "PASS" if backend_index_passed else "BLOCKED",
    }
    overall = "FAIL" if "FAIL" in checks.values() else (
        "PASS" if set(checks.values()) == {"PASS"} else "BLOCKED"
    )
    state = {
        "commit_sha": commit_sha,
        "schema_sha256": schema_sha256,
        "server_version": snapshot["server_version"],
        "client_version": client_version,
        "settings": snapshot["settings"],
        "extensions": snapshot["extensions"],
        "collation": collation,
        "activity": activity,
        "capacity": capacity,
        "backend": snapshot["backend"],
        "units_state_sha256": units["state_sha256"],
        "backup_evidence_sha256": backup.get("evidence_sha256"),
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "mode": "check",
        "overall_status": overall,
        "state_sha256": sha256_bytes(canonical(state)),
        "candidate": {
            "commit_sha": commit_sha,
            "tracked_clean": tracked_clean,
            "schema_sha256": schema_sha256,
            "migration_set_sha256": combined_hash(migration_paths),
            "tuning_config_sha256": sha256_file(ROOT / policy["tuning_config"]),
            "writer_inventory_sha256": sha256_file(ROOT / policy["writer_inventory"]),
            "backend_lock_sha256": sha256_file(backend_path),
            "backend_evidence_sha256": sha256_file(backend_evidence_path),
        },
        "postgresql": {
            "server_version": snapshot["server_version"],
            "client_version": client_version,
            "required_version": expected_version,
            "extension_versions": observed_extensions,
            "extension_mismatches": extension_mismatches,
            "shared_preload_libraries": sorted(
                value.strip() for value in snapshot["settings"]["shared_preload_libraries"].split(",")
                if value.strip()
            ),
            "resource_limits": {
                name: snapshot["settings"][name] for name in (
                    "shared_buffers", "effective_cache_size", "work_mem",
                    "maintenance_work_mem", "max_connections", "max_wal_size",
                    "max_worker_processes", "max_parallel_workers",
                    "max_parallel_maintenance_workers",
                )
            },
            "configuration_identity_sha256": sha256_bytes(canonical(snapshot["settings"])),
            "configuration_mismatches": setting_mismatches,
            "configuration_missing": missing_settings,
            "collation": collation,
            "backend": snapshot["backend"],
        },
        "capacity": {
            "database_bytes": capacity["database_bytes"],
            "index_bytes": capacity["index_bytes"],
            "wal_bytes": capacity["wal_bytes"],
            "filesystem_free_bytes": capacity["filesystem_free_bytes"],
            "required_free_bytes": required_free,
        },
        "backup": backup,
        "maintenance": {
            **units,
            "database_active_writer_count": activity["active_writer_count"],
            "database_unknown_application_count": activity["unknown_application_count"],
            "active_index_build_count": activity["active_index_build_count"],
            "active_vacuum_count": activity["active_vacuum_count"],
            "active_backfill_count": activity["active_backfill_count"],
            "search_read_behavior": "classified-by-unit-policy",
        },
        "checks": checks,
        "limitations": [
            "Backup status does not prove restore or recovery unless restore_tested is true.",
            "The check observes repository-known units and current database activity; dormant external writers require operator inventory.",
            "No live change, activation, backfill, service action, or legacy-state mutation is performed.",
        ],
    }


def git_state() -> tuple[str, bool]:
    commit = run(["git", "rev-parse", "HEAD"]).stdout.strip()
    tracked = run(["git", "status", "--porcelain", "--untracked-files=no"]).stdout.strip()
    return commit, not tracked


def write_manifest(path: Path | None, manifest: dict[str, Any]) -> None:
    encoded = json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    if path is None:
        sys.stdout.write(encoded)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".tmp-{os.getpid()}")
    temporary.write_text(encoded, encoding="utf-8")
    os.replace(temporary, path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", required=True)
    parser.add_argument("--database", default=os.environ.get("MAINRAG_PREFLIGHT_DATABASE", "mainrag"))
    parser.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    parser.add_argument("--backup-evidence", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--local-postgres",
        action="store_true",
        help="run read-only database commands as the standard local postgres OS account",
    )
    args = parser.parse_args()

    policy = json.loads(args.policy.read_text(encoding="utf-8"))
    if policy.get("schema_version") != "mainrag-storage-v2-preflight-policy/v1":
        parser.error("unsupported policy schema")
    now = int(time.time())
    snapshot, client_version, schema_sha256 = collect_database(
        args.database,
        policy["allowed_database_applications"],
        ["sudo", "-n", "-u", "postgres"] if args.local_postgres else None,
    )
    units = collect_units(policy)
    backup = load_backup(args.backup_evidence, int(policy["backup_max_age_seconds"]), now)
    commit, tracked_clean = git_state()
    manifest = evaluate(
        snapshot, client_version, schema_sha256, units, backup, policy,
        commit_sha=commit, tracked_clean=tracked_clean,
    )
    manifest["generated_at_unix"] = now
    write_manifest(args.output, manifest)
    return 0 if manifest["overall_status"] == "PASS" else 3


if __name__ == "__main__":
    raise SystemExit(main())

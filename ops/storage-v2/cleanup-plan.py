#!/usr/bin/env python3
"""Capture a protected, read-only catalog baseline for storage-v2 cleanup.

This is an inventory input, not a cleanup decision or deletion command. A
reviewed cleanup manifest must also bind runtime callers, Qdrant metadata,
exports, retention, pack reachability, and the accepted post-activation state.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path


CATALOG_SQL = """
BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY;
SET LOCAL row_security = off;
SELECT jsonb_build_object(
  'database_oid', (SELECT oid FROM pg_database WHERE datname = current_database()),
  'relations', (
    SELECT COALESCE(jsonb_agg(jsonb_build_object(
      'oid', relation.oid,
      'name', relation.relname,
      'kind', relation.relkind,
      'owner', pg_get_userbyid(relation.relowner),
      'total_bytes', pg_total_relation_size(relation.oid),
      'estimated_rows', relation.reltuples
    ) ORDER BY relation.oid), '[]'::jsonb)
    FROM pg_class AS relation
    WHERE relation.relnamespace = 'public'::regnamespace
      AND relation.relkind IN ('r', 'p', 'm', 'S', 'v', 'f')
  ),
  'columns', (
    SELECT COALESCE(jsonb_agg(jsonb_build_object(
      'relation_oid', attribute.attrelid,
      'number', attribute.attnum,
      'name', attribute.attname,
      'type', format_type(attribute.atttypid, attribute.atttypmod),
      'not_null', attribute.attnotnull
    ) ORDER BY attribute.attrelid, attribute.attnum), '[]'::jsonb)
    FROM pg_attribute AS attribute
    JOIN pg_class AS relation ON relation.oid = attribute.attrelid
    WHERE relation.relnamespace = 'public'::regnamespace
      AND attribute.attnum > 0 AND NOT attribute.attisdropped
  ),
  'constraints', (
    SELECT COALESCE(jsonb_agg(jsonb_build_object(
      'oid', con.oid,
      'name', con.conname,
      'kind', con.contype,
      'relation_oid', con.conrelid,
      'referenced_relation_oid', con.confrelid,
      'definition_sha256', encode(digest(pg_get_constraintdef(con.oid), 'sha256'), 'hex')
    ) ORDER BY con.oid), '[]'::jsonb)
    FROM pg_constraint AS con
    WHERE con.connamespace = 'public'::regnamespace
  ),
  'policies', (
    SELECT COALESCE(jsonb_agg(jsonb_build_object(
      'oid', policy.oid,
      'name', policy.polname,
      'relation_oid', policy.polrelid,
      'command', policy.polcmd,
      'permissive', policy.polpermissive
    ) ORDER BY policy.oid), '[]'::jsonb)
    FROM pg_policy AS policy
    JOIN pg_class AS relation ON relation.oid = policy.polrelid
    WHERE relation.relnamespace = 'public'::regnamespace
  ),
  'triggers', (
    SELECT COALESCE(jsonb_agg(jsonb_build_object(
      'oid', trigger.oid,
      'name', trigger.tgname,
      'relation_oid', trigger.tgrelid,
      'function_oid', trigger.tgfoid,
      'enabled', trigger.tgenabled
    ) ORDER BY trigger.oid), '[]'::jsonb)
    FROM pg_trigger AS trigger
    JOIN pg_class AS relation ON relation.oid = trigger.tgrelid
    WHERE relation.relnamespace = 'public'::regnamespace
      AND NOT trigger.tgisinternal
  ),
  'functions', (
    SELECT COALESCE(jsonb_agg(jsonb_build_object(
      'oid', routine.oid,
      'name', routine.proname,
      'arguments', pg_get_function_identity_arguments(routine.oid),
      'kind', routine.prokind,
      'definition_sha256', CASE WHEN routine.prokind IN ('f', 'p')
        THEN encode(digest(pg_get_functiondef(routine.oid), 'sha256'), 'hex')
        ELSE NULL END
    ) ORDER BY routine.oid), '[]'::jsonb)
    FROM pg_proc AS routine
    WHERE routine.pronamespace = 'public'::regnamespace
  ),
  'indexes', (
    SELECT COALESCE(jsonb_agg(jsonb_build_object(
      'oid', index_class.oid,
      'name', index_class.relname,
      'relation_oid', index_relation.indrelid,
      'valid', index_relation.indisvalid,
      'ready', index_relation.indisready,
      'total_bytes', pg_total_relation_size(index_class.oid)
    ) ORDER BY index_class.oid), '[]'::jsonb)
    FROM pg_index AS index_relation
    JOIN pg_class AS index_class ON index_class.oid = index_relation.indexrelid
    WHERE index_class.relnamespace = 'public'::regnamespace
  ),
  'dependencies', (
    SELECT COALESCE(jsonb_agg(jsonb_build_object(
      'class_oid', dependency.classid,
      'object_oid', dependency.objid,
      'object_subid', dependency.objsubid,
      'referenced_class_oid', dependency.refclassid,
      'referenced_object_oid', dependency.refobjid,
      'referenced_object_subid', dependency.refobjsubid,
      'kind', dependency.deptype
    ) ORDER BY dependency.classid, dependency.objid, dependency.objsubid,
               dependency.refclassid, dependency.refobjid, dependency.refobjsubid), '[]'::jsonb)
    FROM pg_depend AS dependency
    WHERE dependency.refclassid = 'pg_class'::regclass
      AND dependency.refobjid IN (
        SELECT oid FROM pg_class WHERE relnamespace = 'public'::regnamespace
      )
  ),
  'active_pointer_count', (
    SELECT count(*) FROM logical_source WHERE active_generation_id IS NOT NULL
  ),
  'activation_receipt_relation_oid', (
    SELECT to_regclass('public.storage_v2_activation_set_evidence')::oid
  )
)::text;
COMMIT;
"""


def canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode()


def unique_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def catalog(database: str, local_postgres: bool) -> dict:
    command = (["sudo", "-n", "-u", "postgres"] if local_postgres else []) + [
        "psql", "-X", "--no-psqlrc", "-qAt", "--set=ON_ERROR_STOP=1",
        "--dbname", database,
    ]
    environment = os.environ.copy()
    environment["PGAPPNAME"] = "mainrag-storage-v2-cleanup-inventory"
    environment["PGOPTIONS"] = (
        environment.get("PGOPTIONS", "") + " -c default_transaction_read_only=on"
    ).strip()
    completed = subprocess.run(command, input=CATALOG_SQL, text=True,
                               capture_output=True, env=environment, check=False)
    if completed.returncode:
        raise RuntimeError("read-only cleanup catalog capture failed")
    if len(completed.stdout) > 64 * 1024 * 1024:
        raise RuntimeError("cleanup catalog exceeds protected output bound")
    try:
        value = json.loads(completed.stdout, object_pairs_hook=unique_keys)
    except (ValueError, UnicodeError) as error:
        raise RuntimeError("cleanup catalog response is invalid") from error
    required = {"database_oid", "relations", "columns", "constraints", "policies",
                "triggers", "functions", "indexes", "dependencies",
                "active_pointer_count", "activation_receipt_relation_oid"}
    if not isinstance(value, dict) or set(value) != required \
            or any(not isinstance(value[key], list) for key in required - {
                "database_oid", "active_pointer_count", "activation_receipt_relation_oid"
            }) or not (isinstance(value["database_oid"], str)
                       and value["database_oid"].isdecimal()) \
            or type(value["active_pointer_count"]) is not int \
            or (value["activation_receipt_relation_oid"] is not None
                and not (isinstance(value["activation_receipt_relation_oid"], str)
                         and value["activation_receipt_relation_oid"].isdecimal())):
        raise RuntimeError("cleanup catalog response is incomplete")
    return value


def private_create(path: Path, value: object) -> str:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if stat.S_IMODE(path.parent.stat().st_mode) & 0o077:
        raise RuntimeError("protected output directory is accessible to others")
    raw = canonical(value) + b"\n"
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as output:
            output.write(raw)
            output.flush()
            os.fsync(output.fileno())
        os.link(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return hashlib.sha256(raw).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True)
    parser.add_argument("--local-postgres", action="store_true")
    parser.add_argument("--output", required=True, type=Path)
    arguments = parser.parse_args()
    if re.fullmatch(r"[A-Za-z0-9_-]+", arguments.database) is None:
        parser.error("database must be a local database name")
    if arguments.output.exists() or arguments.output.is_symlink():
        parser.error("protected output already exists")
    try:
        observed = catalog(arguments.database, arguments.local_postgres)
        artifact = {"schema_version": "mainrag.storage-v2.cleanup-catalog.v1",
                    "status": "OBSERVED_ONLY", "catalog": observed,
                    "observed_at_unix": int(time.time()),
                    "before_state_sha256": hashlib.sha256(canonical(observed)).hexdigest(),
                    "operator_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                    "limitations": ["No exact row counts or reviewed dispositions.",
                                    "No Qdrant, runtime caller, export or pack reachability inventory.",
                                    "No post-activation acceptance or deletion authority."]}
        digest = private_create(arguments.output, artifact)
    except RuntimeError as error:
        parser.error(str(error))
    except OSError:
        parser.error("protected cleanup catalog output is unavailable")
    print(json.dumps({"status": "OBSERVED_ONLY", "sha256": digest,
                      "relation_count": len(observed["relations"]),
                      "dependency_count": len(observed["dependencies"])}, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())

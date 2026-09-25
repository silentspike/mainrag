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
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


RUNTIME_TERMS = (
    "files", "chunks", "embeddings", "chunk_embeddings", "indexing_outbox",
    "legacy_hit_mapping", "qdrant", "outbox",
)
RUNTIME_PATHS = ("api/src", "cli/src", "ops")


CATALOG_SQL = """
BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY;
SET LOCAL row_security = off;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '120s';
%TARGET_LOCK_SQL%
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
  'pointer_set_sha256', (
    SELECT encode(digest(convert_to(COALESCE(jsonb_agg(jsonb_build_object(
      'source_id', source.id,
      'active_generation_id', source.active_generation_id
    ) ORDER BY source.id), '[]'::jsonb)::text, 'UTF8'), 'sha256'), 'hex')
    FROM logical_source AS source
  ),
  'open_reader_count', (
    SELECT count(*) FROM content_reader_epoch WHERE finished_at IS NULL
  ),
  'building_run_count', (
    SELECT count(*) FROM storage_v2_ingest_run WHERE status = 'building'
  ),
  'generations', (
    SELECT COALESCE(jsonb_agg(jsonb_build_object(
      'id', generation.id,
      'source_id', generation.source_id,
      'sequence', generation.generation_seq,
      'status', generation.status::text,
      'item_count', generation.item_count,
      'verification_manifest_sha256', generation.verification_manifest_sha256
    ) ORDER BY generation.id), '[]'::jsonb)
    FROM source_generation AS generation
  ),
  'packs', (
    SELECT COALESCE(jsonb_agg(jsonb_build_object(
      'id', pack.id,
      'status', pack.status::text,
      'stored_bytes', pack.stored_bytes,
      'live_bytes', pack.live_bytes,
      'entry_count', pack.entry_count,
      'manifest_sha256', encode(pack.manifest_sha256, 'hex')
    ) ORDER BY pack.id), '[]'::jsonb)
    FROM content_pack AS pack
  ),
  'activation_receipt_relation_oid', (
    SELECT to_regclass('public.storage_v2_activation_set_evidence')::oid
  ),
  'exact_rows', (%EXACT_ROWS_SQL%),
  'reachability', (%REACHABILITY_SQL%)
)::text;
COMMIT;
"""


REACHABILITY_SQL = """
WITH RECURSIVE
requested(id) AS (%RETAINED_QUERY%),
retained AS (
  SELECT generation.id, generation.source_id, generation.generation_seq
  FROM source_generation AS generation JOIN requested ON requested.id = generation.id
),
visible_artifact AS (
  SELECT DISTINCT membership.artifact_version_id AS id
  FROM retained AS generation
  JOIN generation_item_version AS membership
    ON membership.source_id = generation.source_id
   AND membership.valid_from_seq <= generation.generation_seq
   AND (membership.valid_to_seq IS NULL
        OR membership.valid_to_seq > generation.generation_seq)
),
protected_occurrence AS (
  SELECT occurrence_id AS id FROM legacy_hit_mapping
  UNION
  SELECT occurrence_id FROM storage_v2_symbol_occurrence
  UNION
  SELECT item.occurrence_id FROM storage_v2_ingest_run_item AS item
  JOIN storage_v2_ingest_run AS run ON run.id = item.run_id
  WHERE run.status = 'building'
),
root_artifact AS (
  SELECT id FROM visible_artifact
  UNION
  SELECT occurrence.artifact_version_id
  FROM protected_occurrence AS protected
  JOIN occurrence ON occurrence.id = protected.id
),
root_occurrence AS (
  SELECT occurrence.id, occurrence.view_id
  FROM occurrence JOIN root_artifact ON root_artifact.id = occurrence.artifact_version_id
),
root_view AS (SELECT DISTINCT view_id AS id FROM root_occurrence),
view_document AS (
  SELECT DISTINCT document.id, document.body_id, document.node_id
  FROM root_view AS root_v
  JOIN storage_v2_search_view_document AS binding ON binding.view_id = root_v.id
  JOIN storage_v2_search_document AS document ON document.id = binding.document_id
),
seed_node AS (
  SELECT artifact.content_root_node_id AS id
  FROM root_artifact AS root
  JOIN artifact_version AS artifact ON artifact.id = root.id
  WHERE artifact.content_root_node_id IS NOT NULL
  UNION
  SELECT component.node_id
  FROM root_view AS root_v JOIN view_component AS component ON component.view_id = root_v.id
  WHERE component.node_id IS NOT NULL
  UNION
  SELECT node_id FROM view_document WHERE node_id IS NOT NULL
),
walk_node(id) AS (
  SELECT id FROM seed_node
  UNION
  SELECT edge.child_node_id
  FROM walk_node AS parent
  JOIN content_node_edge AS edge ON edge.parent_node_id = parent.id
),
root_body AS (
  SELECT artifact.raw_body_id AS id
  FROM root_artifact AS root
  JOIN artifact_version AS artifact ON artifact.id = root.id
  WHERE artifact.raw_body_id IS NOT NULL
  UNION
  SELECT node.body_id FROM walk_node AS walk
  JOIN content_node AS node ON node.id = walk.id WHERE node.body_id IS NOT NULL
  UNION
  SELECT component.body_id
  FROM root_view AS root_v JOIN view_component AS component ON component.view_id = root_v.id
  WHERE component.body_id IS NOT NULL
  UNION
  SELECT body_id FROM view_document WHERE body_id IS NOT NULL
),
pack_assignment AS (
  SELECT pack.id,
         count(body.id) AS assigned_body_count,
         count(root.id) AS reachable_assigned_body_count
  FROM content_pack AS pack
  LEFT JOIN content_body AS body ON body.pack_id = pack.id
  LEFT JOIN root_body AS root ON root.id = body.id
  GROUP BY pack.id
),
pack_entries AS (
  SELECT entry.pack_id AS id,
         count(*) AS entry_count,
         COALESCE(sum(entry.stored_length), 0) AS entry_stored_bytes,
         COALESCE(sum(entry.stored_length) FILTER (WHERE root.id IS NOT NULL), 0)
           AS reachable_body_entry_bytes
  FROM content_pack_entry AS entry
  LEFT JOIN root_body AS root ON root.id = entry.body_id
  GROUP BY entry.pack_id
)
SELECT jsonb_build_object(
  'status', 'REACHABILITY_ONLY_NOT_GC_AUTHORITY',
  'root_scope', 'SELECTED_GENERATIONS_MAPPINGS_INTELLIGENCE_BUILDING_RUNS',
  'root_coverage', 'PARTIAL_EXTERNAL_RETENTION_UNVERIFIED',
  'requested_generation_count', (SELECT count(*) FROM requested),
  'found_generation_count', (SELECT count(*) FROM retained),
  'retained_generation_ids', (
    SELECT COALESCE(jsonb_agg(id ORDER BY id), '[]'::jsonb) FROM retained
  ),
  'active_generation_count', (SELECT count(*) FROM source_generation WHERE status = 'active'),
  'active_generation_included_count', (
    SELECT count(*) FROM source_generation AS generation
    JOIN retained ON retained.id = generation.id WHERE generation.status = 'active'
  ),
  'active_pointer_count', (
    SELECT count(*) FROM logical_source WHERE active_generation_id IS NOT NULL
  ),
  'active_pointer_included_count', (
    SELECT count(*) FROM logical_source AS source
    JOIN retained ON retained.id = source.active_generation_id
  ),
  'mapped_hit_count', (SELECT count(*) FROM legacy_hit_mapping),
  'intelligence_occurrence_count', (SELECT count(*) FROM storage_v2_symbol_occurrence),
  'building_run_item_count', (
    SELECT count(*) FROM storage_v2_ingest_run_item AS item
    JOIN storage_v2_ingest_run AS run ON run.id = item.run_id
    WHERE run.status = 'building'
  ),
  'artifact_count', (SELECT count(*) FROM root_artifact),
  'occurrence_count', (SELECT count(*) FROM root_occurrence),
  'view_count', (SELECT count(*) FROM root_view),
  'node_count', (SELECT count(*) FROM walk_node),
  'body_count', (SELECT count(*) FROM root_body),
  'outside_selected_roots_body_count', (
    SELECT count(*) FROM content_body AS body
    LEFT JOIN root_body AS root ON root.id = body.id WHERE root.id IS NULL
  ),
  'body_set_sha256', (
    SELECT encode(digest(convert_to(COALESCE(string_agg(id::text, ',' ORDER BY id), ''),
                                   'UTF8'), 'sha256'), 'hex') FROM root_body
  ),
  'view_set_sha256', (
    SELECT encode(digest(convert_to(COALESCE(string_agg(id::text, ',' ORDER BY id), ''),
                                   'UTF8'), 'sha256'), 'hex') FROM root_view
  ),
  'packs', (
    SELECT COALESCE(jsonb_agg(jsonb_build_object(
      'id', pack.id, 'status', pack.status::text,
      'stored_bytes', pack.stored_bytes,
      'assigned_body_count', assignment.assigned_body_count,
      'reachable_assigned_body_count', assignment.reachable_assigned_body_count,
      'entry_count', COALESCE(entries.entry_count, 0),
      'entry_stored_bytes', COALESCE(entries.entry_stored_bytes, 0),
      'reachable_body_entry_bytes', COALESCE(entries.reachable_body_entry_bytes, 0)
    ) ORDER BY pack.id), '[]'::jsonb)
    FROM content_pack AS pack
    JOIN pack_assignment AS assignment ON assignment.id = pack.id
    LEFT JOIN pack_entries AS entries ON entries.id = pack.id
  )
)
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


def validate_relation_names(relation_names: tuple[str, ...]) -> None:
    if len(relation_names) > 64 or len(set(relation_names)) != len(relation_names) \
            or any(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) is None
                   for name in relation_names):
        raise RuntimeError("exact-count relation list is invalid or exceeds its bound")


def exact_rows_sql(relation_names: tuple[str, ...]) -> str:
    validate_relation_names(relation_names)
    if not relation_names:
        return "'{}'::jsonb"
    rows = ",\n".join(
        f"('{name}', (SELECT count(*) FROM public.\"{name}\"))"
        for name in relation_names
    )
    return ("SELECT jsonb_object_agg(name, row_count) FROM (VALUES\n"
            + rows + "\n) AS counted(name, row_count)")


def target_lock_sql(relation_names: tuple[str, ...]) -> str:
    validate_relation_names(relation_names)
    return "\n".join(
        f'LOCK TABLE public."{name}" IN ACCESS SHARE MODE;'
        for name in relation_names
    )


def reachability_sql(generation_ids: tuple[int, ...], retain_all: bool) -> str:
    if retain_all and generation_ids:
        raise RuntimeError("explicit retention and retain-all are mutually exclusive")
    if len(generation_ids) > 4096 or len(set(generation_ids)) != len(generation_ids) \
            or any(type(identifier) is not int or identifier <= 0 for identifier in generation_ids):
        raise RuntimeError("retained generation identity list is invalid")
    if not retain_all and not generation_ids:
        return "NULL::jsonb"
    requested = ("SELECT id FROM source_generation" if retain_all else
                 "SELECT unnest(ARRAY[" + ",".join(map(str, generation_ids))
                 + "]::bigint[]) AS id")
    return REACHABILITY_SQL.replace("%RETAINED_QUERY%", requested)


def catalog(database: str, local_postgres: bool,
            relation_names: tuple[str, ...] = (),
            generation_ids: tuple[int, ...] = (), retain_all: bool = False) -> dict:
    count_sql = exact_rows_sql(relation_names)
    statement = CATALOG_SQL.replace("%EXACT_ROWS_SQL%", count_sql).replace(
        "%TARGET_LOCK_SQL%", target_lock_sql(relation_names)).replace(
        "%REACHABILITY_SQL%", reachability_sql(generation_ids, retain_all))
    command = (["sudo", "-n", "-u", "postgres"] if local_postgres else []) + [
        "psql", "-X", "--no-psqlrc", "-qAt", "--set=ON_ERROR_STOP=1",
        "--dbname", database,
    ]
    environment = os.environ.copy()
    environment["PGAPPNAME"] = "mainrag-storage-v2-cleanup-inventory"
    environment["PGOPTIONS"] = (
        environment.get("PGOPTIONS", "") + " -c default_transaction_read_only=on"
    ).strip()
    completed = subprocess.run(command, input=statement, text=True,
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
                "active_pointer_count", "pointer_set_sha256", "open_reader_count",
                "building_run_count", "generations", "packs",
                "activation_receipt_relation_oid", "exact_rows", "reachability"}
    if not isinstance(value, dict) or set(value) != required \
            or any(not isinstance(value[key], list) for key in required - {
                "database_oid", "active_pointer_count", "activation_receipt_relation_oid",
                "exact_rows", "reachability", "pointer_set_sha256", "open_reader_count",
                "building_run_count"
            }) or not (isinstance(value["database_oid"], str)
                       and value["database_oid"].isdecimal()) \
            or any(type(value[key]) is not int or value[key] < 0 for key in (
                "active_pointer_count", "open_reader_count", "building_run_count"
            )) or not (isinstance(value["pointer_set_sha256"], str)
                       and re.fullmatch(r"[0-9a-f]{64}", value["pointer_set_sha256"])) \
            or (value["activation_receipt_relation_oid"] is not None
                and not (isinstance(value["activation_receipt_relation_oid"], str)
                         and value["activation_receipt_relation_oid"].isdecimal())) \
            or not isinstance(value["exact_rows"], dict) \
            or set(value["exact_rows"]) != set(relation_names) \
            or any(type(count) is not int or count < 0
                   for count in value["exact_rows"].values()):
        raise RuntimeError("cleanup catalog response is incomplete")
    if len(value["generations"]) > 100000 or len(value["packs"]) > 100000:
        raise RuntimeError("cleanup catalog generation or pack inventory exceeds its bound")
    reachable = value["reachability"]
    if (reachable is not None) != bool(generation_ids or retain_all):
        raise RuntimeError("retained generation reachability is incomplete")
    if reachable is not None:
        counts = ("requested_generation_count", "found_generation_count",
                  "active_generation_count", "active_generation_included_count",
                  "active_pointer_count", "active_pointer_included_count",
                  "mapped_hit_count", "intelligence_occurrence_count",
                  "building_run_item_count", "artifact_count", "occurrence_count",
                  "view_count", "node_count", "body_count",
                  "outside_selected_roots_body_count")
        if not isinstance(reachable, dict) \
                or reachable.get("status") != "REACHABILITY_ONLY_NOT_GC_AUTHORITY" \
                or reachable.get("root_scope") != \
                    "SELECTED_GENERATIONS_MAPPINGS_INTELLIGENCE_BUILDING_RUNS" \
                or reachable.get("root_coverage") != \
                    "PARTIAL_EXTERNAL_RETENTION_UNVERIFIED" \
                or any(type(reachable.get(key)) is not int or reachable[key] < 0
                       for key in counts) \
                or reachable["requested_generation_count"] != reachable["found_generation_count"] \
                or reachable["active_generation_count"] != reachable["active_generation_included_count"] \
                or reachable["active_pointer_count"] != reachable["active_pointer_included_count"] \
                or reachable["active_pointer_count"] != value["active_pointer_count"] \
                or not isinstance(reachable.get("retained_generation_ids"), list) \
                or len(reachable["retained_generation_ids"]) != reachable["found_generation_count"] \
                or any(type(identifier) is not int or identifier <= 0
                       for identifier in reachable["retained_generation_ids"]) \
                or reachable["retained_generation_ids"] != sorted(set(reachable["retained_generation_ids"])) \
                or (not retain_all and
                    reachable["retained_generation_ids"] != sorted(generation_ids)) \
                or (retain_all and reachable["found_generation_count"] != len(value["generations"])) \
                or any(not isinstance(reachable.get(key), str)
                       or re.fullmatch(r"[0-9a-f]{64}", reachable[key]) is None
                       for key in ("body_set_sha256", "view_set_sha256")) \
                or not isinstance(reachable.get("packs"), list) \
                or len(reachable["packs"]) != len(value["packs"]):
            raise RuntimeError("retained generation reachability is incomplete")
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


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        raise RuntimeError("Qdrant inventory request redirected")


def qdrant_origin(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    try:
        port = parsed.port
    except ValueError as error:
        raise RuntimeError("Qdrant origin is invalid") from error
    if parsed.scheme != "http" or parsed.hostname not in ("127.0.0.1", "::1") \
            or port is None or parsed.username or parsed.password \
            or parsed.path not in ("", "/") or parsed.params or parsed.query \
            or parsed.fragment:
        raise RuntimeError("Qdrant inventory requires a loopback HTTP origin")
    return url.rstrip("/")


def qdrant_key(path: Path | None) -> str | None:
    if path is None:
        return None
    try:
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) & 0o077 \
                or metadata.st_size > 4096:
            raise RuntimeError("Qdrant key file is not private")
        key = path.read_text().strip()
    except OSError as error:
        raise RuntimeError("Qdrant key file is unavailable") from error
    if not key or "\r" in key or "\n" in key:
        raise RuntimeError("Qdrant key file is invalid")
    return key


def qdrant_response(origin: str, key: str | None, path: str,
                    *, exact_count: bool = False) -> dict:
    data = b'{"exact":true}' if exact_count else None
    headers = {"Content-Type": "application/json"}
    if key is not None:
        headers["api-key"] = key
    request = urllib.request.Request(origin + path, data=data, headers=headers,
                                     method="POST" if exact_count else "GET")
    try:
        with urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect).open(
            request, timeout=300 if exact_count else 15
        ) as response:
            raw = response.read(2 * 1024 * 1024 + 1)
    except (OSError, urllib.error.HTTPError) as error:
        raise RuntimeError("Qdrant inventory request failed") from error
    if len(raw) > 2 * 1024 * 1024:
        raise RuntimeError("Qdrant inventory response exceeds its bound")
    try:
        value = json.loads(raw, object_pairs_hook=unique_keys)
    except (UnicodeError, ValueError) as error:
        raise RuntimeError("Qdrant inventory response is invalid JSON") from error
    if not isinstance(value, dict) or value.get("status") != "ok" \
            or not isinstance(value.get("result"), dict):
        raise RuntimeError("Qdrant inventory response is incomplete")
    return value["result"]


def qdrant_names(origin: str, key: str | None) -> tuple[list[str], list[dict]]:
    collections = qdrant_response(origin, key, "/collections").get("collections")
    aliases = qdrant_response(origin, key, "/aliases").get("aliases")
    if not isinstance(collections, list) or not isinstance(aliases, list) \
            or len(collections) > 128 or len(aliases) > 256:
        raise RuntimeError("Qdrant collection or alias inventory is invalid")
    names = [item.get("name") for item in collections if isinstance(item, dict)]
    if len(names) != len(collections) or any(not isinstance(name, str)
                                             or not name or len(name) > 255 for name in names) \
            or len(names) != len(set(names)):
        raise RuntimeError("Qdrant collection identities are invalid")
    normalized_aliases = []
    for item in aliases:
        if not isinstance(item, dict) or not isinstance(item.get("alias_name"), str) \
                or not isinstance(item.get("collection_name"), str) \
                or item["collection_name"] not in names:
            raise RuntimeError("Qdrant alias identity is invalid")
        normalized_aliases.append({"alias_name": item["alias_name"],
                                   "collection_name": item["collection_name"]})
    if len({item["alias_name"] for item in normalized_aliases}) != len(normalized_aliases):
        raise RuntimeError("Qdrant alias identities are duplicated")
    return sorted(names), sorted(normalized_aliases,
                                 key=lambda item: item["alias_name"])


def qdrant_inventory(url: str, key_file: Path | None) -> dict:
    origin = qdrant_origin(url)
    key = qdrant_key(key_file)
    names, aliases = qdrant_names(origin, key)
    collections = []
    for name in names:
        segment = urllib.parse.quote(name, safe="")
        details = qdrant_response(origin, key, f"/collections/{segment}")
        count = qdrant_response(origin, key,
                                f"/collections/{segment}/points/count",
                                exact_count=True).get("count")
        if type(count) is not int or count < 0 \
                or not isinstance(details.get("status"), str) \
                or not isinstance(details.get("config"), dict):
            raise RuntimeError("Qdrant collection detail or exact count is invalid")
        collections.append({
            "name": name, "exact_point_count": count,
            "status": details["status"],
            "config_sha256": hashlib.sha256(canonical(details["config"])).hexdigest(),
            "detail_sha256": hashlib.sha256(canonical(details)).hexdigest(),
        })
    repeated_names, repeated_aliases = qdrant_names(origin, key)
    if (names, aliases) != (repeated_names, repeated_aliases):
        raise RuntimeError("Qdrant collection or alias set drifted during inventory")
    return {"schema_version": "mainrag.storage-v2.cleanup-qdrant.v1",
            "consistency": "TWO_LIST_READBACKS_NOT_ATOMIC",
            "collections": collections, "aliases": aliases}


def git_read(root: Path, *arguments: str) -> bytes:
    completed = subprocess.run(["git", "-C", str(root), *arguments],
                               capture_output=True, check=False)
    if completed.returncode or len(completed.stdout) > 16 * 1024 * 1024:
        raise RuntimeError("tracked runtime source inventory failed")
    return completed.stdout


def runtime_inventory(root: Path, relation_names: tuple[str, ...]) -> dict:
    root = root.resolve(strict=True)
    actual_root = Path(os.fsdecode(git_read(root, "rev-parse", "--show-toplevel")).strip())
    if actual_root != root:
        raise RuntimeError("runtime inventory must use the repository root")
    commit = os.fsdecode(git_read(root, "rev-parse", "HEAD")).strip()
    if re.fullmatch(r"[0-9a-f]{40}", commit) is None:
        raise RuntimeError("runtime inventory commit identity is invalid")
    if git_read(root, "status", "--porcelain", "--untracked-files=no"):
        raise RuntimeError("runtime inventory requires a clean tracked tree")
    raw_paths = git_read(root, "ls-files", "-z", "--", *RUNTIME_PATHS)
    paths = [Path(os.fsdecode(item)) for item in raw_paths.split(b"\0") if item]
    if not paths or len(paths) > 10000:
        raise RuntimeError("runtime inventory tracked file set is invalid")
    terms = sorted(set((*RUNTIME_TERMS, *relation_names)))
    files = []
    matches = []
    for relative in paths:
        if relative.is_absolute() or ".." in relative.parts:
            raise RuntimeError("runtime inventory has an unsafe tracked path")
        path = root / relative
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > 16 * 1024 * 1024:
            raise RuntimeError("runtime inventory has a nonregular or oversized file")
        raw = path.read_bytes()
        try:
            lines = raw.decode("utf-8").splitlines()
        except UnicodeError as error:
            raise RuntimeError("runtime inventory has a non-UTF-8 tracked file") from error
        files.append({"path": relative.as_posix(),
                      "sha256": hashlib.sha256(raw).hexdigest()})
        for number, line in enumerate(lines, 1):
            found = [term for term in terms if term.lower() in line.lower()]
            if found:
                matches.append({"path": relative.as_posix(), "line": number,
                                "terms": found,
                                "line_sha256": hashlib.sha256(line.encode()).hexdigest()})
                if len(matches) > 20000:
                    raise RuntimeError("runtime caller candidate set exceeds its bound")
    return {"schema_version": "mainrag.storage-v2.cleanup-runtime-search.v1",
            "status": "SEARCH_CANDIDATES_NOT_CALLER_PROOF",
            "commit_sha": commit, "terms": terms,
            "files": files, "matches": matches}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True)
    parser.add_argument("--local-postgres", action="store_true")
    parser.add_argument("--count-relation", action="append", default=[])
    retention = parser.add_mutually_exclusive_group()
    retention.add_argument("--retained-generation-id", action="append", type=int, default=[])
    retention.add_argument("--retain-all-generations", action="store_true")
    parser.add_argument("--qdrant-url")
    parser.add_argument("--qdrant-api-key-file", type=Path)
    parser.add_argument("--runtime-root", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    arguments = parser.parse_args()
    if re.fullmatch(r"[A-Za-z0-9_-]+", arguments.database) is None:
        parser.error("database must be a local database name")
    if arguments.output.exists() or arguments.output.is_symlink():
        parser.error("protected output already exists")
    if arguments.qdrant_api_key_file is not None and arguments.qdrant_url is None:
        parser.error("Qdrant key requires a Qdrant origin")
    try:
        observed = catalog(arguments.database, arguments.local_postgres,
                           tuple(arguments.count_relation),
                           tuple(arguments.retained_generation_id),
                           arguments.retain_all_generations)
        qdrant = (qdrant_inventory(arguments.qdrant_url, arguments.qdrant_api_key_file)
                  if arguments.qdrant_url is not None else None)
        runtime = (runtime_inventory(arguments.runtime_root, tuple(arguments.count_relation))
                   if arguments.runtime_root is not None else None)
        artifact = {"schema_version": "mainrag.storage-v2.cleanup-catalog.v1",
                    "status": "OBSERVED_ONLY", "catalog": observed,
                    "qdrant": qdrant,
                    "runtime_search": runtime,
                    "observed_at_unix": int(time.time()),
                    "before_state_sha256": hashlib.sha256(canonical(observed)).hexdigest(),
                    "operator_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                    "limitations": ["Only explicitly requested relations have exact row counts; no reviewed dispositions.",
                                    "Qdrant is optional and cannot share a transaction with PostgreSQL.",
                                    "Runtime text matches require human caller classification and installed-binary binding.",
                                    "No export inventory; pack reachability is optional.",
                                    "Selected roots omit unreviewed external retention and historical run/identity records; outside-root counts are not deletion candidates.",
                                    "Reachability does not verify content integrity or authorize GC.",
                                    "No post-activation acceptance or deletion authority."]}
        digest = private_create(arguments.output, artifact)
    except RuntimeError as error:
        parser.error(str(error))
    except OSError:
        parser.error("protected cleanup catalog output is unavailable")
    print(json.dumps({"status": "OBSERVED_ONLY", "sha256": digest,
                      "relation_count": len(observed["relations"]),
                      "exact_count_relation_count": len(observed["exact_rows"]),
                      "qdrant_observed": qdrant is not None,
                      "runtime_search_observed": runtime is not None,
                      "reachability_observed": observed["reachability"] is not None,
                      "dependency_count": len(observed["dependencies"])}, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())

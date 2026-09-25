#!/usr/bin/env python3
"""Plan and execute one manifest-bound, atomic storage-v2 database activation.

This operator never switches the application default or accepts post-activation
health. Its committed receipt is an input to the separately reviewed coupled
default-switch and first ordinary ingest procedure.
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
import uuid
from pathlib import Path
from typing import Any


HEX64 = re.compile(r"[0-9a-f]{64}\Z")
HEX40 = re.compile(r"[0-9a-f]{40}\Z")
EXTERNAL_GATES = {
    "live_adapter_watermarks", "writer_and_maintenance_inventory",
    "current_package_and_schema", "representative_gold_review",
    "per_class_and_aggregate_quality", "cumulative_resource_and_recovery_budget",
    "benchmark_result", "legacy_state_readback",
}
VOLATILE_GATES = {
    "live_adapter_watermarks", "writer_and_maintenance_inventory",
    "current_package_and_schema", "cumulative_resource_and_recovery_budget",
    "benchmark_result", "legacy_state_readback",
}


def canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode()


def sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def valid_hash(value: object) -> bool:
    return isinstance(value, str) and HEX64.fullmatch(value) is not None


def unique_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON key")
        value[key] = item
    return value


def read_private(path: Path, expected: str, maximum: int = 32 * 1024 * 1024) -> dict:
    if not valid_hash(expected):
        raise RuntimeError("exact protected evidence digest is required")
    try:
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) & 0o077:
            raise RuntimeError("protected evidence must be a private regular file")
        if metadata.st_size > maximum:
            raise RuntimeError("protected evidence exceeds the input limit")
        raw = path.read_bytes()
    except OSError as error:
        raise RuntimeError("protected evidence is unavailable") from error
    if sha256(raw) != expected:
        raise RuntimeError("protected evidence digest differs")
    try:
        value = json.loads(raw, object_pairs_hook=unique_keys)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise RuntimeError("protected evidence is invalid JSON") from error
    if not isinstance(value, dict):
        raise RuntimeError("protected evidence must be an object")
    return value


def private_write(path: Path, value: object, *, replace: bool = False) -> None:
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as output:
            output.write(canonical(value) + b"\n")
            output.flush()
            os.fsync(output.fileno())
        if replace:
            os.replace(temporary, path)
        else:
            os.link(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def psql(database: str, sql: str, local_postgres: bool, *, readonly: bool) -> str:
    command = (["sudo", "-n", "-u", "postgres"] if local_postgres else []) + [
        "psql", "-X", "--no-psqlrc", "-qAt", "--set=ON_ERROR_STOP=1",
        "--dbname", database,
    ]
    environment = os.environ.copy()
    environment["PGAPPNAME"] = "mainrag-storage-v2-activation-operator"
    if readonly:
        environment["PGOPTIONS"] = (
            environment.get("PGOPTIONS", "") + " -c default_transaction_read_only=on"
        ).strip()
    result = subprocess.run(command, input=sql, capture_output=True,
                            text=True, env=environment, check=False)
    if result.returncode:
        raise RuntimeError("activation database operation failed")
    return result.stdout.strip()


LIVE_SQL = """
BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY;
SET LOCAL row_security = off;
SELECT COALESCE(jsonb_agg(jsonb_build_object(
    'source_id', source.id,
    'is_test', source.is_test,
    'active_generation_id', logical.active_generation_id,
    'candidates', COALESCE(candidate.value, '[]'::jsonb)
) ORDER BY source.id), '[]'::jsonb)
FROM sources AS source
JOIN logical_source AS logical ON logical.id = source.id
LEFT JOIN LATERAL (
    SELECT jsonb_agg(jsonb_build_object(
        'generation_id', generation.id,
        'status', generation.status::text,
        'evidence_id', evidence.id,
        'evidence_manifest_sha256', encode(evidence.manifest_sha256, 'hex'),
        'source_watermark_sha256', evidence.source_watermark_sha256,
        'commit_sha', evidence.commit_sha
    ) ORDER BY generation.id) AS value
    FROM source_generation AS generation
    LEFT JOIN storage_v2_release_candidate_evidence AS evidence
      ON evidence.source_id=source.id AND evidence.generation_id=generation.id
    WHERE generation.source_id=source.id AND generation.status='release_candidate'
) AS candidate ON TRUE;
COMMIT;
"""


def live_rows(database: str, local_postgres: bool) -> list[dict]:
    try:
        rows = json.loads(psql(database, LIVE_SQL, local_postgres, readonly=True))
    except (ValueError, TypeError) as error:
        raise RuntimeError("live activation inventory is invalid") from error
    if not isinstance(rows, list) or not rows:
        raise RuntimeError("live activation inventory is empty")
    return rows


def validate_preflight(value: dict, operator_commit: str, schema_hash: str,
                       now: int, *, maximum_age: int) -> None:
    if value.get("mode") != "check" or value.get("overall_status") != "PASS" \
            or not isinstance(value.get("checks"), dict) \
            or set(value["checks"].values()) != {"PASS"} \
            or value.get("candidate", {}).get("commit_sha") != operator_commit \
            or value.get("candidate", {}).get("schema_sha256") != schema_hash \
            or type(value.get("generated_at_unix")) is not int \
            or not 0 <= now - value["generated_at_unix"] <= maximum_age:
        raise RuntimeError("current exact-package preflight has not passed")


def validate_acceptance(audit: dict, audit_sha: str, acceptance: dict,
                        acceptance_sha: str, binary: Path,
                        preflight: dict, now: int) -> list[dict]:
    candidate_set = audit.get("candidate_set")
    if audit.get("schema_version") != "mainrag.storage-v2.candidate-aggregate-audit.v2" \
            or audit.get("persisted_candidate_set_complete") is not True \
            or not isinstance(candidate_set, list) or not candidate_set \
            or audit.get("candidate_set_sha256") != sha256(canonical(candidate_set)):
        raise RuntimeError("persisted candidate set is incomplete")
    if acceptance.get("schema_version") != "mainrag.storage-v2.aggregate-acceptance.v1" \
            or acceptance.get("status") != "PASS" \
            or acceptance.get("persisted_audit_sha256") != audit_sha \
            or acceptance.get("candidate_set_sha256") != audit["candidate_set_sha256"] \
            or not isinstance(acceptance.get("external_gates"), dict) \
            or set(acceptance["external_gates"]) != EXTERNAL_GATES:
        raise RuntimeError("exact accepted aggregate evidence is missing")
    if any(not isinstance(item, dict)
           or type(item.get("source_id")) is not int or item["source_id"] <= 0
           or type(item.get("item_count")) is not int or item["item_count"] < 0
           or not isinstance(item.get("adapter_profile_id"), str)
           or not item["adapter_profile_id"]
           or not isinstance(item.get("candidate_commit_sha"), str)
           or HEX40.fullmatch(item["candidate_commit_sha"]) is None
           for item in candidate_set):
        raise RuntimeError("per-source candidate package identities are incomplete")
    for name in EXTERNAL_GATES:
        gate = acceptance["external_gates"][name]
        if not isinstance(gate, dict) or gate.get("status") != "PASS" \
                or not valid_hash(gate.get("evidence_sha256")) \
                or (name in VOLATILE_GATES and (
                    type(gate.get("observed_at_unix")) is not int
                    or not 0 <= now - gate["observed_at_unix"] <= 300)):
            raise RuntimeError("an external aggregate gate is incomplete")
    if any(not valid_hash(acceptance.get(key)) for key in (
        "schema_sha256", "backend_package_sha256", "installed_binary_sha256",
    )) or not isinstance(acceptance.get("code_commit_sha"), str) \
            or not HEX40.fullmatch(acceptance["code_commit_sha"]) \
            or not isinstance(acceptance.get("preflight_operator_commit_sha"), str) \
            or not HEX40.fullmatch(acceptance["preflight_operator_commit_sha"]):
        raise RuntimeError("accepted code, schema or package identity differs")
    if sha256(binary.read_bytes()) != acceptance["installed_binary_sha256"]:
        raise RuntimeError("installed binary differs from accepted package")
    validate_preflight(preflight, acceptance["preflight_operator_commit_sha"],
                       acceptance["schema_sha256"], now, maximum_age=1800)
    watermarks = acceptance.get("current_source_watermarks")
    if not isinstance(watermarks, list) or len(watermarks) != len(candidate_set):
        raise RuntimeError("accepted source watermarks are incomplete")
    observed = {}
    for item in watermarks:
        if not isinstance(item, dict) or type(item.get("source_id")) is not int \
                or item["source_id"] in observed \
                or not valid_hash(item.get("watermark_sha256")) \
                or type(item.get("observed_at_unix")) is not int \
                or not 0 <= now - item["observed_at_unix"] <= 300:
            raise RuntimeError("accepted source watermark identity differs")
        observed[item["source_id"]] = item["watermark_sha256"]
    if {item["source_id"]: item["source_watermark_sha256"]
            for item in candidate_set} != observed:
        raise RuntimeError("final candidate watermarks are stale")
    if acceptance.get("source_count") != len(candidate_set) \
            or acceptance.get("candidate_set_sha256") != sha256(canonical(candidate_set)) \
            or not valid_hash(acceptance_sha):
        raise RuntimeError("accepted source set differs")
    return candidate_set


def bind_live(candidate_set: list[dict], rows: list[dict]) -> list[dict]:
    by_id = {row.get("source_id"): row for row in rows if isinstance(row, dict)}
    if len(by_id) != len(rows) or len(rows) != len(candidate_set) \
            or sum(row.get("is_test") is True for row in rows) != 1:
        raise RuntimeError("live registered source set differs")
    entries = []
    for item in candidate_set:
        row = by_id.get(item["source_id"])
        if row is None or type(row.get("is_test")) is not bool \
                or not isinstance(row.get("candidates"), list) \
                or len(row["candidates"]) != 1:
            raise RuntimeError("live release candidate set differs")
        candidate = row["candidates"][0]
        fields = {
            "generation_id": item["candidate_generation_id"],
            "evidence_id": item["evidence_id"],
            "evidence_manifest_sha256": item["evidence_manifest_sha256"],
            "source_watermark_sha256": item["source_watermark_sha256"],
            "commit_sha": item["candidate_commit_sha"],
        }
        if candidate.get("status") != "release_candidate" \
                or any(candidate.get(key) != value for key, value in fields.items()):
            raise RuntimeError("live candidate qualification identity differs")
        active = row.get("active_generation_id")
        if active is not None and (type(active) is not int or active <= 0):
            raise RuntimeError("live active pointer identity differs")
        entries.append({
            "source_id": item["source_id"],
            "candidate_generation_id": item["candidate_generation_id"],
            "candidate_commit_sha": item["candidate_commit_sha"],
            "expected_active_generation_id": active,
            "evidence_id": item["evidence_id"],
            "evidence_manifest_sha256": item["evidence_manifest_sha256"],
            "source_watermark_sha256": item["source_watermark_sha256"],
        })
    return sorted(entries, key=lambda item: item["source_id"])


def verify_current_api_watermarks(api_url: str, token: str,
                                  candidate_set: list[dict]) -> None:
    parsed = urllib.parse.urlparse(api_url)
    if parsed.scheme != "http" or parsed.hostname not in ("127.0.0.1", "::1") \
            or parsed.username or parsed.password or parsed.path not in ("", "/") \
            or parsed.query or parsed.fragment or not token:
        raise RuntimeError("activation watermark gate requires a local authenticated API")
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, request, fp, code, msg, headers, newurl):
            return None
    opener = urllib.request.build_opener(NoRedirect)
    for item in candidate_set:
        source_id = item["source_id"]
        request = urllib.request.Request(
            api_url.rstrip("/")
            + f"/api/v1/admin/sources/{source_id}/storage-v2-release-watermark",
            headers={"Authorization": "Bearer " + token},
        )
        try:
            with opener.open(request, timeout=120) as response:
                observed = json.load(response)
        except (OSError, ValueError) as error:
            raise RuntimeError("current release source watermark readback failed") from error
        if not isinstance(observed, dict) or observed.get("source_id") != source_id \
                or observed.get("source_watermark_sha256") != item["source_watermark_sha256"] \
                or observed.get("adapter_profile_id") != item["adapter_profile_id"] \
                or observed.get("item_count") != item["item_count"]:
            raise RuntimeError("final source watermark drifted before activation")


def sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def manifest_digest(database: str, local_postgres: bool, manifest: dict) -> str:
    literal = sql_literal(canonical(manifest).decode())
    query = ("SELECT encode(digest(convert_to(" + literal
             + "::jsonb::text,'UTF8'),'sha256'),'hex');\n")
    result = psql(database, query, local_postgres, readonly=True)
    if not valid_hash(result):
        raise RuntimeError("database manifest digest is invalid")
    return result


def make_plan(audit: dict, audit_sha: str, acceptance: dict,
              acceptance_sha: str, preflight: dict, preflight_sha: str,
              binary: Path, rows: list[dict], database: str,
              local_postgres: bool, now: int) -> dict:
    candidate_set = validate_acceptance(
        audit, audit_sha, acceptance, acceptance_sha, binary, preflight, now)
    entries = bind_live(candidate_set, rows)
    manifest = {
        "schema_version": "mainrag.storage-v2.activation-set.v1",
        "activation_id": str(uuid.uuid4()),
        "code_commit_sha": acceptance["code_commit_sha"],
        "schema_sha256": acceptance["schema_sha256"],
        "backend_package_sha256": acceptance["backend_package_sha256"],
        "aggregate_evidence_sha256": acceptance_sha,
        "sources": entries,
    }
    return {
        "schema_version": "mainrag.storage-v2.activation-plan.v1",
        "status": "READY_FOR_EXPLICIT_APPROVAL",
        "created_at_unix": now,
        "persisted_audit_sha256": audit_sha,
        "aggregate_acceptance_sha256": acceptance_sha,
        "preflight_sha256": preflight_sha,
        "installed_binary_sha256": acceptance["installed_binary_sha256"],
        "default_switch": {
            "unit": "mainrag-api.service",
            "api_binary": "/opt/mainrag/api/mainrag-api",
            "dropin": "/etc/systemd/system/mainrag-api.service.d/90-storage-v2-default-read.conf",
            "environment_file": "/etc/mainrag/storage-v2-default-read.env",
            "coupling_max_seconds": 300,
            "active_ingest_commit_env_name": "MAINRAG_STORAGE_V2_ACTIVE_INGEST_COMMIT_SHA",
        },
        "candidate_set_sha256": audit["candidate_set_sha256"],
        "before_pointer_set_sha256": sha256(canonical([
            {"source_id": item["source_id"],
             "active_generation_id": item["expected_active_generation_id"]}
            for item in entries])),
        "manifest_sha256": manifest_digest(database, local_postgres, manifest),
        "manifest": manifest,
        "limitations": [
            "This plan does not authorize activation or switch the application default.",
            "Fresh maintenance, watermark, package, schema and pointer checks are required at apply.",
        ],
    }


def plan_command(args: argparse.Namespace) -> None:
    now = int(time.time())
    audit = read_private(args.audit, args.audit_sha256)
    acceptance = read_private(args.acceptance, args.acceptance_sha256)
    preflight = read_private(args.preflight, args.preflight_sha256)
    plan = make_plan(audit, args.audit_sha256, acceptance, args.acceptance_sha256,
                     preflight, args.preflight_sha256, args.installed_binary,
                     live_rows(args.database, args.local_postgres), args.database,
                     args.local_postgres, now)
    verify_current_api_watermarks(args.api_url, os.environ.get(args.token_env, ""),
                                  audit["candidate_set"])
    private_write(args.output, plan)
    print(json.dumps({"status": plan["status"],
                      "plan_sha256": sha256(args.output.read_bytes()),
                      "manifest_sha256": plan["manifest_sha256"],
                      "source_count": len(plan["manifest"]["sources"])}, sort_keys=True))


def validate_approval(value: dict, plan: dict, plan_sha: str, now: int) -> None:
    manifest = plan["manifest"]
    required = {
        "plan_sha256": plan_sha,
        "manifest_sha256": plan["manifest_sha256"],
        "code_commit_sha": manifest["code_commit_sha"],
        "schema_sha256": manifest["schema_sha256"],
        "backend_package_sha256": manifest["backend_package_sha256"],
        "candidate_set_sha256": plan["candidate_set_sha256"],
        "before_pointer_set_sha256": plan["before_pointer_set_sha256"],
    }
    if value.get("schema_version") != "mainrag.storage-v2.activation-approval.v1" \
            or value.get("status") != "APPROVED" \
            or any(value.get(key) != expected for key, expected in required.items()) \
            or type(value.get("approved_at_unix")) is not int \
            or not 0 <= now - value["approved_at_unix"] <= 900:
        raise RuntimeError("fresh exact activation approval is missing")


def activation_sql(manifest: dict, manifest_sha: str, admin_user_id: str) -> str:
    try:
        admin_uuid = str(uuid.UUID(admin_user_id))
    except (ValueError, TypeError, AttributeError) as error:
        raise RuntimeError("administrator context identity is invalid") from error
    if admin_uuid != admin_user_id or not valid_hash(manifest_sha):
        raise RuntimeError("activation approval identity is invalid")
    body = sql_literal(canonical(manifest).decode())
    return ("BEGIN;\n"
            + "SET LOCAL app.user_id = " + sql_literal(admin_uuid) + ";\n"
            + "SELECT storage_v2_activate_candidate_set(" + body
            + "::jsonb," + sql_literal(manifest_sha) + ");\n"
            + "COMMIT;\n")


def committed_readback(database: str, local_postgres: bool,
                       activation_id: str) -> dict:
    try:
        identifier = str(uuid.UUID(activation_id))
    except (ValueError, TypeError, AttributeError) as error:
        raise RuntimeError("activation receipt identity is invalid") from error
    query = """
BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY;
SET LOCAL row_security = off;
SELECT jsonb_build_object(
    'receipt', (SELECT jsonb_build_object(
        'id', id, 'manifest_sha256', manifest_sha256,
        'source_count', source_count, 'pointer_set_sha256', pointer_set_sha256
    ) FROM storage_v2_activation_set_evidence WHERE id = '%s'::uuid),
    'sources', (SELECT COALESCE(jsonb_agg(jsonb_build_object(
        'source_id', source.id,
        'active_generation_id', source.active_generation_id,
        'active_status', generation.status::text,
        'active_generation_count', (SELECT count(*) FROM source_generation current
            WHERE current.source_id=source.id AND current.status='active'),
        'generation_statuses', (SELECT COALESCE(jsonb_agg(jsonb_build_object(
            'generation_id', prior.id, 'status', prior.status::text
        ) ORDER BY prior.id), '[]'::jsonb) FROM source_generation prior
            WHERE prior.source_id=source.id)
    ) ORDER BY source.id), '[]'::jsonb)
    FROM logical_source source
    LEFT JOIN source_generation generation ON generation.id=source.active_generation_id),
    'pointer_set_sha256', (SELECT encode(digest(convert_to(
        jsonb_agg(jsonb_build_object(
            'source_id', source.id,
            'active_generation_id', source.active_generation_id
        ) ORDER BY source.id)::text, 'UTF8'), 'sha256'), 'hex')
        FROM logical_source source)
);
COMMIT;
""" % identifier
    try:
        value = json.loads(psql(database, query, local_postgres, readonly=True))
    except (ValueError, TypeError) as error:
        raise RuntimeError("committed activation readback is invalid") from error
    if not isinstance(value, dict):
        raise RuntimeError("committed activation readback is invalid")
    return value


def verify_committed(plan: dict, readback: dict) -> None:
    manifest = plan["manifest"]
    receipt = readback.get("receipt")
    sources = readback.get("sources")
    expected = {item["source_id"]: item["candidate_generation_id"]
                for item in manifest["sources"]}
    if not isinstance(receipt, dict) or not isinstance(sources, list) \
            or receipt.get("id") != manifest["activation_id"] \
            or receipt.get("manifest_sha256") != plan["manifest_sha256"] \
            or receipt.get("source_count") != len(expected) \
            or not valid_hash(receipt.get("pointer_set_sha256")) \
            or receipt["pointer_set_sha256"] != readback.get("pointer_set_sha256") \
            or len(sources) != len(expected):
        raise RuntimeError("committed activation receipt differs")
    seen = set()
    before = {item["source_id"]: item["expected_active_generation_id"]
              for item in manifest["sources"]}
    for row in sources:
        if not isinstance(row, dict) or row.get("source_id") in seen \
                or row.get("active_generation_id") != expected.get(row.get("source_id")) \
                or row.get("active_status") != "active" \
                or row.get("active_generation_count") != 1:
            raise RuntimeError("committed activation pointer set differs")
        former = before[row["source_id"]]
        if former is not None:
            statuses = row.get("generation_statuses")
            if not isinstance(statuses, list) or not any(
                isinstance(item, dict) and item.get("generation_id") == former
                and item.get("status") == "superseded" for item in statuses
            ):
                raise RuntimeError("previous active generation was not superseded")
        seen.add(row["source_id"])
    if seen != set(expected):
        raise RuntimeError("committed activation source set differs")


def apply_command(args: argparse.Namespace) -> None:
    now = int(time.time())
    plan = read_private(args.plan, args.plan_sha256)
    if plan.get("schema_version") != "mainrag.storage-v2.activation-plan.v1" \
            or plan.get("status") != "READY_FOR_EXPLICIT_APPROVAL" \
            or type(plan.get("created_at_unix")) is not int \
            or not 0 <= now - plan["created_at_unix"] <= 1800 \
            or not isinstance(plan.get("manifest"), dict):
        raise RuntimeError("approved activation plan is stale or invalid")
    approval = read_private(args.approval, args.approval_sha256, 64 * 1024)
    validate_approval(approval, plan, args.plan_sha256, now)
    admin = read_private(args.admin_context, args.admin_context_sha256, 64 * 1024)
    if admin.get("schema_version") != "mainrag.storage-v2.admin-context.v1" \
            or not isinstance(admin.get("admin_user_id"), str):
        raise RuntimeError("protected administrator context differs")
    audit = read_private(args.audit, plan["persisted_audit_sha256"])
    acceptance = read_private(args.acceptance, plan["aggregate_acceptance_sha256"])
    fresh_preflight = read_private(args.preflight, args.preflight_sha256)
    candidate_set = validate_acceptance(
        audit, plan["persisted_audit_sha256"], acceptance,
        plan["aggregate_acceptance_sha256"], args.installed_binary,
        fresh_preflight, now)
    verify_current_api_watermarks(args.api_url, os.environ.get(args.token_env, ""),
                                  candidate_set)
    validate_preflight(fresh_preflight, acceptance["preflight_operator_commit_sha"],
                       acceptance["schema_sha256"], now, maximum_age=300)
    entries = bind_live(candidate_set, live_rows(args.database, args.local_postgres))
    if entries != plan["manifest"].get("sources") \
            or sha256(canonical([{
                "source_id": item["source_id"],
                "active_generation_id": item["expected_active_generation_id"]
            } for item in entries])) != plan["before_pointer_set_sha256"] \
            or manifest_digest(args.database, args.local_postgres,
                               plan["manifest"]) != plan["manifest_sha256"]:
        raise RuntimeError("activation plan drifted before commit")
    attempt = {
        "schema_version": "mainrag.storage-v2.activation-attempt.v1",
        "status": "CALL_PENDING",
        "plan_sha256": args.plan_sha256,
        "manifest_sha256": plan["manifest_sha256"],
        "activation_id": plan["manifest"]["activation_id"],
        "started_at_unix": now,
    }
    private_write(args.output, attempt)
    try:
        result = psql(args.database, activation_sql(
            plan["manifest"], plan["manifest_sha256"], admin["admin_user_id"]),
            args.local_postgres, readonly=False)
        response = json.loads(result)
        if response.get("status") != "ACTIVATION_STATEMENT_COMPLETE" \
                or response.get("manifest_sha256") != plan["manifest_sha256"]:
            raise RuntimeError("activation statement response differs")
    except (RuntimeError, ValueError, TypeError):
        private_write(args.output, {**attempt, "status": "COMMIT_OUTCOME_UNKNOWN"},
                      replace=True)
        raise RuntimeError("activation call outcome is unknown; reconcile receipt before retry")
    try:
        readback = committed_readback(args.database, args.local_postgres,
                                      plan["manifest"]["activation_id"])
        verify_committed(plan, readback)
    except RuntimeError:
        private_write(args.output, {**attempt, "status": "COMMITTED_READBACK_FAILED"},
                      replace=True)
        raise RuntimeError("activation committed but readback failed; stop default switch")
    private_write(args.output, {
        **attempt, "status": "DB_COMMITTED_DEFAULT_SWITCH_PENDING",
        "pointer_set_sha256": readback["pointer_set_sha256"],
        "committed_at_unix": int(time.time()),
    }, replace=True)
    print(json.dumps({"status": "DB_COMMITTED_DEFAULT_SWITCH_PENDING",
                      "manifest_sha256": plan["manifest_sha256"],
                      "source_count": len(entries),
                      "pointer_set_sha256": readback["pointer_set_sha256"]},
                     sort_keys=True))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("plan", "apply"))
    parser.add_argument("--database", required=True)
    parser.add_argument("--local-postgres", action="store_true")
    parser.add_argument("--api-url", default="http://127.0.0.1:3001")
    parser.add_argument("--token-env", default="MAINRAG_TOKEN")
    parser.add_argument("--audit", type=Path, required=True)
    parser.add_argument("--audit-sha256")
    parser.add_argument("--acceptance", type=Path, required=True)
    parser.add_argument("--acceptance-sha256")
    parser.add_argument("--preflight", type=Path, required=True)
    parser.add_argument("--preflight-sha256", required=True)
    parser.add_argument("--installed-binary", type=Path, required=True)
    parser.add_argument("--plan", type=Path)
    parser.add_argument("--plan-sha256")
    parser.add_argument("--approval", type=Path)
    parser.add_argument("--approval-sha256")
    parser.add_argument("--admin-context", type=Path)
    parser.add_argument("--admin-context-sha256")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if re.fullmatch(r"[A-Za-z0-9_-]+", args.database) is None:
        parser.error("database must be a local database name")
    if args.installed_binary != Path("/opt/mainrag/api/mainrag-api") \
            or args.installed_binary.is_symlink() \
            or not args.installed_binary.is_file():
        parser.error("the installed API binary path is required")
    if args.phase == "plan" and (not args.audit_sha256 or not args.acceptance_sha256):
        parser.error("plan requires exact audit and aggregate acceptance digests")
    if args.phase == "apply" and any(getattr(args, key) is None for key in (
        "plan", "plan_sha256", "approval", "approval_sha256",
        "admin_context", "admin_context_sha256",
    )):
        parser.error("apply requires the exact plan, approval and admin context")
    if args.output.exists() or args.output.is_symlink():
        parser.error("protected plan output already exists")
    try:
        (plan_command if args.phase == "plan" else apply_command)(args)
    except (RuntimeError, OSError, FileExistsError) as error:
        parser.error(str(error))
    return 0


if __name__ == "__main__":
    sys.exit(main())

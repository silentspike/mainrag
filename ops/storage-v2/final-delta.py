#!/usr/bin/env python3
"""Plan and verify source-local final deltas before candidate-set activation."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path


def sibling(name: str, module_name: str):
    path = Path(__file__).with_name(name)
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ACTIVATION = sibling("activation-set.py", "storage_v2_activation")
INVENTORY = sibling("candidate-inventory.py", "storage_v2_inventory")
COMMIT = re.compile(r"[0-9a-f]{40}\Z")


def observation(api_url: str, token: str, source_id: int) -> dict:
    parsed = urllib.parse.urlparse(api_url)
    if parsed.scheme != "http" or parsed.hostname not in ("127.0.0.1", "::1") \
            or parsed.username or parsed.password or parsed.path not in ("", "/") \
            or parsed.query or parsed.fragment or not token:
        raise RuntimeError("final delta observation requires a local authenticated API")
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, request, fp, code, msg, headers, newurl):
            return None
    request = urllib.request.Request(
        api_url.rstrip("/")
        + f"/api/v1/admin/sources/{source_id}/storage-v2-release-watermark",
        headers={"Authorization": "Bearer " + token},
    )
    try:
        with urllib.request.build_opener(NoRedirect).open(request, timeout=120) as response:
            value = json.load(response)
    except (OSError, ValueError) as error:
        raise RuntimeError("release source watermark readback failed") from error
    if not isinstance(value, dict) or value.get("source_id") != source_id \
            or not ACTIVATION.valid_hash(value.get("source_watermark_sha256")) \
            or not isinstance(value.get("adapter_profile_id"), str) \
            or not value["adapter_profile_id"] \
            or type(value.get("item_count")) is not int or value["item_count"] < 0 \
            or type(value.get("input_bytes")) is not int or value["input_bytes"] < 0 \
            or "application_read_bytes" not in value \
            or (value["application_read_bytes"] is not None
                and (type(value["application_read_bytes"]) is not int
                     or value["application_read_bytes"] < 0)):
        raise RuntimeError("release source watermark observation is invalid")
    return value


def candidate_rows(rows: list[dict], baseline: list[dict]) -> dict[int, tuple[dict, dict]]:
    if len(rows) != len(baseline):
        raise RuntimeError("registered final-delta source set differs")
    by_id = {row.get("source_id"): row for row in rows if isinstance(row, dict)}
    if len(by_id) != len(rows) or set(by_id) != {item["source_id"] for item in baseline}:
        raise RuntimeError("registered final-delta source set differs")
    bound = {}
    for item in baseline:
        row = by_id[item["source_id"]]
        generations = row.get("generations")
        if row.get("active_generation_id") != item["expected_active_generation_id"] \
                or not isinstance(generations, list):
            raise RuntimeError("active pointer or generation set drifted before final delta")
        previous = [generation for generation in generations
                    if generation.get("generation_id") == item["candidate_generation_id"]]
        candidates = [generation for generation in generations
                      if generation.get("status") == "release_candidate"]
        if len(previous) != 1 or len(candidates) != 1 \
                or candidates[0] != previous[0] \
                or previous[0].get("generation_seq") != item["candidate_generation_seq"] \
                or previous[0].get("commit_sha") != item["candidate_commit_sha"] \
                or previous[0].get("evidence_id") != item["evidence_id"] \
                or previous[0].get("source_watermark_sha256") != item["source_watermark_sha256"] \
                or previous[0].get("adapter_profile_id") != item["adapter_profile_id"] \
                or type(previous[0].get("generation_seq")) is not int:
            raise RuntimeError("baseline candidate identity drifted before final delta")
        bound[item["source_id"]] = (row, previous[0])
    return bound


def validate_audit(audit: dict) -> list[dict]:
    candidates = audit.get("candidate_set")
    if audit.get("schema_version") != "mainrag.storage-v2.candidate-aggregate-audit.v2" \
            or audit.get("persisted_candidate_set_complete") is not True \
            or not isinstance(candidates, list) or not candidates \
            or audit.get("candidate_set_sha256") != ACTIVATION.sha256(
                ACTIVATION.canonical(candidates)):
        raise RuntimeError("complete persisted baseline candidate set is required")
    if any(not isinstance(item, dict) or type(item.get("source_id")) is not int \
           or item["source_id"] <= 0 or not isinstance(item.get("candidate_commit_sha"), str) \
           or COMMIT.fullmatch(item["candidate_commit_sha"]) is None
           for item in candidates) or len({item["source_id"] for item in candidates}) != len(candidates):
        raise RuntimeError("baseline candidate identities are incomplete")
    return candidates


def make_plan(audit: dict, audit_sha: str, rows: list[dict], observed: dict[int, dict],
              runtime_commit: str, now: int, operator_commit: str,
              schema_hash: str, preflight_sha: str, binary_hash: str) -> dict:
    baseline = validate_audit(audit)
    bound = candidate_rows(rows, baseline)
    if not isinstance(runtime_commit, str) or COMMIT.fullmatch(runtime_commit) is None \
            or not isinstance(operator_commit, str) or COMMIT.fullmatch(operator_commit) is None \
            or not ACTIVATION.valid_hash(schema_hash) \
            or not ACTIVATION.valid_hash(preflight_sha) \
            or not ACTIVATION.valid_hash(binary_hash) \
            or set(observed) != set(bound):
        raise RuntimeError("runtime commit or observed source set differs")
    sources = []
    for item in baseline:
        source_id = item["source_id"]
        current = observed[source_id]
        if current.get("source_id") != source_id \
                or not ACTIVATION.valid_hash(current.get("source_watermark_sha256")) \
                or not isinstance(current.get("adapter_profile_id"), str):
            raise RuntimeError("current source watermark identity is invalid")
        _, old = bound[source_id]
        retained = (current["source_watermark_sha256"] == item["source_watermark_sha256"]
                    and current["adapter_profile_id"] == item["adapter_profile_id"])
        sources.append({
            "source_id": source_id,
            "action": "RETAIN" if retained else "REBUILD",
            "baseline_generation_id": item["candidate_generation_id"],
            "baseline_generation_seq": old["generation_seq"],
            "baseline_commit_sha": item["candidate_commit_sha"],
            "baseline_watermark_sha256": item["source_watermark_sha256"],
            "observed_watermark_sha256": current["source_watermark_sha256"],
            "observed_adapter_profile_id": current["adapter_profile_id"],
            "observed_item_count": current["item_count"],
            "observed_input_bytes": current["input_bytes"],
            "application_read_bytes": current["application_read_bytes"],
            "expected_active_generation_id": item["expected_active_generation_id"],
        })
    return {
        "schema_version": "mainrag.storage-v2.final-delta-plan.v1",
        "status": "BUILD_AND_REVERIFY_REQUIRED",
        "created_at_unix": now,
        "baseline_audit_sha256": audit_sha,
        "baseline_candidate_set_sha256": audit["candidate_set_sha256"],
        "runtime_commit_sha": runtime_commit,
        "operator_commit_sha": operator_commit,
        "schema_sha256": schema_hash,
        "plan_preflight_sha256": preflight_sha,
        "installed_binary_sha256": binary_hash,
        "sources": sources,
        "limitations": [
            "The plan changes no database pointer; Git observation can refresh its local cache.",
            "Rebuilt candidates need the existing protected build, restart, gold and qualification gates.",
        ],
    }


def finalize(plan: dict, baseline_audit: dict, receipt_set: dict,
             rows: list[dict], observed: dict[int, dict], artifact_reader) -> dict:
    baseline = validate_audit(baseline_audit)
    if plan.get("schema_version") != "mainrag.storage-v2.final-delta-plan.v1" \
            or plan.get("baseline_candidate_set_sha256") != baseline_audit["candidate_set_sha256"] \
            or not isinstance(plan.get("sources"), list) \
            or len(plan["sources"]) != len(baseline) \
            or receipt_set.get("schema_version") != "mainrag.storage-v2.final-delta-receipts.v1" \
            or not isinstance(receipt_set.get("sources"), list):
        raise RuntimeError("final delta plan or qualification receipts are invalid")
    entries = {item.get("source_id"): item for item in receipt_set["sources"]
               if isinstance(item, dict)}
    if len(entries) != len(receipt_set["sources"]):
        raise RuntimeError("final delta qualification receipts contain duplicates")
    planned = {item.get("source_id"): item for item in plan["sources"]
               if isinstance(item, dict)}
    by_id = {row.get("source_id"): row for row in rows if isinstance(row, dict)}
    if len(planned) != len(baseline) or len(by_id) != len(rows) \
            or set(planned) != set(by_id) or set(observed) != set(by_id) \
            or set(entries) != {item["source_id"] for item in plan["sources"]
                                if item.get("action") == "REBUILD"}:
        raise RuntimeError("final delta source or receipt set differs")
    if not isinstance(plan.get("runtime_commit_sha"), str) \
            or COMMIT.fullmatch(plan["runtime_commit_sha"]) is None:
        raise RuntimeError("final delta runtime identity is invalid")
    old_by_id = {item["source_id"]: item for item in baseline}
    final = []
    for source_id in sorted(planned):
        expected = planned[source_id]
        old = old_by_id[source_id]
        if expected.get("baseline_generation_id") != old["candidate_generation_id"] \
                or expected.get("baseline_generation_seq") != old["candidate_generation_seq"] \
                or expected.get("baseline_commit_sha") != old["candidate_commit_sha"] \
                or expected.get("baseline_watermark_sha256") != old["source_watermark_sha256"] \
                or expected.get("expected_active_generation_id") != old["expected_active_generation_id"]:
            raise RuntimeError("final delta baseline candidate binding differs")
        row = by_id[source_id]
        generations = row.get("generations")
        current = observed[source_id]
        if row.get("active_generation_id") != expected["expected_active_generation_id"] \
                or not isinstance(generations, list) \
                or current.get("source_watermark_sha256") != expected["observed_watermark_sha256"] \
                or current.get("adapter_profile_id") != expected["observed_adapter_profile_id"]:
            raise RuntimeError("final delta pointer or source watermark drifted")
        candidates = [item for item in generations if item.get("status") == "release_candidate"]
        previous = [item for item in generations
                    if item.get("generation_id") == expected["baseline_generation_id"]]
        if len(candidates) != 1 or len(previous) != 1 \
                or previous[0].get("generation_seq") != expected["baseline_generation_seq"]:
            raise RuntimeError("final delta candidate count or generation sequence differs")
        candidate = candidates[0]
        if candidate.get("source_watermark_sha256") != current["source_watermark_sha256"] \
                or candidate.get("adapter_profile_id") != current["adapter_profile_id"] \
                or candidate.get("item_count") != current.get("item_count"):
            raise RuntimeError("final candidate watermark or adapter profile differs")
        if expected["action"] == "RETAIN":
            if candidate.get("generation_id") != old["candidate_generation_id"] \
                    or candidate.get("commit_sha") != old["candidate_commit_sha"] \
                    or candidate.get("evidence_id") != old["evidence_id"]:
                raise RuntimeError("unchanged final candidate was rebuilt or replaced")
        elif expected["action"] == "REBUILD":
            receipt = entries[source_id]
            if not isinstance(receipt.get("artifact_path"), str) \
                    or not ACTIVATION.valid_hash(receipt.get("artifact_sha256")):
                raise RuntimeError("rebuilt final candidate qualification artifact is missing")
            artifact = artifact_reader(Path(receipt["artifact_path"]),
                                       receipt["artifact_sha256"])
            checkpoint = artifact.get("checkpoint")
            result = artifact.get("result")
            verified = artifact.get("verification")
            if not all(isinstance(value, dict) for value in (checkpoint, result, verified)) \
                    or previous[0].get("status") != "verified" \
                    or checkpoint.get("source_id") != source_id \
                    or result.get("source_id") != source_id \
                    or verified.get("source_id") != source_id \
                    or candidate.get("generation_id") != checkpoint.get("generation_id") \
                    or candidate.get("generation_seq") != checkpoint.get("generation_seq") \
                    or candidate.get("generation_seq") != expected["baseline_generation_seq"] + 1 \
                    or candidate.get("commit_sha") != plan["runtime_commit_sha"] \
                    or checkpoint.get("commit_sha") != plan["runtime_commit_sha"] \
                    or checkpoint.get("source_watermark_sha256") != current["source_watermark_sha256"] \
                    or result.get("status") != "release_candidate" \
                    or result.get("generation_id") != candidate.get("generation_id") \
                    or result.get("generation_seq") != candidate.get("generation_seq") \
                    or result.get("evidence_id") != candidate.get("evidence_id") \
                    or result.get("manifest_sha256") != candidate.get("qualification_manifest_sha256") \
                    or verified.get("status") not in ("verified", "release_candidate") \
                    or verified.get("generation_id") != candidate.get("generation_id") \
                    or verified.get("generation_seq") != candidate.get("generation_seq") \
                    or not isinstance(verified.get("checks"), dict) \
                    or set(verified["checks"].values()) != {"PASS"} \
                    or not {"artifact_root", "authorization", "body_pack_integrity",
                            "intelligence", "intervals", "legacy_intelligence_export"}.issubset(
                                verified["checks"]):
                raise RuntimeError("rebuilt final candidate qualification identity differs")
        else:
            raise RuntimeError("unknown final delta action")
        final.append({
            "source_id": source_id,
            "candidate_generation_id": candidate["generation_id"],
            "candidate_commit_sha": candidate["commit_sha"],
        })
    return {
        "schema_version": "mainrag.storage-v2.final-candidate-commit-map.v1",
        "baseline_candidate_set_sha256": baseline_audit["candidate_set_sha256"],
        "runtime_commit_sha": plan["runtime_commit_sha"],
        "sources": final,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("plan", "finalize"))
    parser.add_argument("--database", required=True)
    parser.add_argument("--local-postgres", action="store_true")
    parser.add_argument("--api-url", default="http://127.0.0.1:3001")
    parser.add_argument("--token-env", default="MAINRAG_TOKEN")
    parser.add_argument("--baseline-audit", type=Path, required=True)
    parser.add_argument("--baseline-audit-sha256", required=True)
    parser.add_argument("--preflight", type=Path, required=True)
    parser.add_argument("--preflight-sha256", required=True)
    parser.add_argument("--operator-commit-sha", required=True)
    parser.add_argument("--schema-sha256", required=True)
    parser.add_argument("--installed-binary-sha256", required=True)
    parser.add_argument("--runtime-commit-sha")
    parser.add_argument("--plan", type=Path)
    parser.add_argument("--plan-sha256")
    parser.add_argument("--receipts", type=Path)
    parser.add_argument("--receipts-sha256")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9_-]+", args.database):
        parser.error("database must be a local database name")
    if args.output.exists() or args.output.is_symlink():
        parser.error("protected final delta output already exists")
    if args.phase == "plan" and not args.runtime_commit_sha:
        parser.error("plan requires an exact installed runtime commit")
    if args.phase == "finalize" and any(getattr(args, name) is None for name in (
        "plan", "plan_sha256", "receipts", "receipts_sha256",
    )):
        parser.error("finalize requires exact plan and qualification receipt digests")
    token = os.environ.get(args.token_env)
    if not token:
        parser.error("API token environment variable is empty")
    try:
        binary = Path("/opt/mainrag/api/mainrag-api")
        if binary.is_symlink() or not binary.is_file() \
                or not ACTIVATION.valid_hash(args.installed_binary_sha256):
            raise RuntimeError("exact installed runtime package digest is required")
        digest = hashlib.sha256()
        with binary.open("rb") as stream:
            for part in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(part)
        if digest.hexdigest() != args.installed_binary_sha256:
            raise RuntimeError("installed runtime binary differs from approved package")
        audit = ACTIVATION.read_private(args.baseline_audit, args.baseline_audit_sha256)
        preflight = ACTIVATION.read_private(args.preflight, args.preflight_sha256)
        ACTIVATION.validate_preflight(
            preflight, args.operator_commit_sha, args.schema_sha256,
            int(time.time()), maximum_age=1800 if args.phase == "plan" else 300)
        candidates = validate_audit(audit)
        rows = INVENTORY.read_sources(args.database, args.local_postgres)
        observed = {item["source_id"]: observation(args.api_url, token, item["source_id"])
                    for item in candidates}
        if ACTIVATION.canonical(rows) != ACTIVATION.canonical(
            INVENTORY.read_sources(args.database, args.local_postgres)):
            raise RuntimeError("registered source or candidate state drifted during observation")
        if args.phase == "plan":
            value = make_plan(audit, args.baseline_audit_sha256, rows, observed,
                              args.runtime_commit_sha, int(time.time()),
                              args.operator_commit_sha, args.schema_sha256,
                              args.preflight_sha256, args.installed_binary_sha256)
        else:
            plan = ACTIVATION.read_private(args.plan, args.plan_sha256)
            receipts = ACTIVATION.read_private(args.receipts, args.receipts_sha256)
            if plan.get("baseline_audit_sha256") != args.baseline_audit_sha256 \
                    or plan.get("operator_commit_sha") != args.operator_commit_sha \
                    or plan.get("schema_sha256") != args.schema_sha256 \
                    or plan.get("installed_binary_sha256") != args.installed_binary_sha256 \
                    or (args.runtime_commit_sha is not None and
                        plan.get("runtime_commit_sha") != args.runtime_commit_sha):
                raise RuntimeError("final delta baseline audit differs")
            value = finalize(plan, audit, receipts, rows, observed, ACTIVATION.read_private)
        ACTIVATION.private_write(args.output, value)
    except (RuntimeError, OSError, FileExistsError, KeyError, TypeError) as error:
        parser.error(str(error))
    summary = {
        "status": value.get("status", "FINAL_CANDIDATE_MAP_OBSERVED_ONLY"),
        "source_count": len(value["sources"]),
        "protected_sha256": ACTIVATION.sha256(args.output.read_bytes()),
    }
    if args.phase == "plan":
        summary["rebuild_count"] = sum(item["action"] == "REBUILD" for item in value["sources"])
    print(json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())

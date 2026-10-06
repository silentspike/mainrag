#!/usr/bin/env python3
"""Build and qualify one protected, pointer-neutral storage-v2 candidate."""

from __future__ import annotations

import argparse
import codecs
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
import os
import re
import runpy
import shutil
import stat
import struct
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any

scope_matcher = runpy.run_path(str(Path(__file__).with_name("fs_scope.py")))["scope_matcher"]
cut_contract = runpy.run_path(str(Path(__file__).with_name("fs_cut.py")))
load_token = runpy.run_path(str(Path(__file__).with_name("operator_token.py")))["load_token"]
monitored_build = runpy.run_path(str(Path(__file__).with_name("build_progress.py")))["monitored_build"]
candidate_proof = runpy.run_path(
    str(Path(__file__).with_name("candidate-aggregate-audit.py"))
)["candidate_proof"]
complete_file_proof_valid = runpy.run_path(
    str(Path(__file__).with_name("candidate-aggregate-audit.py")))["complete_file_proof_valid"]
integrity_resume = runpy.run_path(str(Path(__file__).with_name("integrity_resume.py")))


CHECKS = (
    "artifact_root",
    "authorization",
    "body_pack_integrity",
    "dual_read",
    "intelligence",
    "intervals",
    "legacy_intelligence_export",
    "lexical_segment_integrity",
    "resource_budget",
    "restart_resume",
    "search_quality",
)
TELEMETRY_PHASES = {
    "lesen_hashen_ms",
    "content_store_ms",
    "strukturprojektion_ms",
    "analyse_ms",
    "db_staging_ms",
    "intervall_delta_ms",
    "sealing_ms",
}
TELEMETRY_COUNTERS = {
    "latenz_ms",
    "eingang_bytes",
    "unique_bytes",
    "stored_bytes",
    "reuse_bodies",
    "reuse_nodes",
    "reuse_views",
    "reuse_analysis",
    "reuse_generation",
    "parser_passes",
    "analysis_retries",
    "artifacts_created",
    "occurrences_created",
    "intervals_opened",
    "intervals_closed",
    "errors",
    "io_buffer_bytes",
    "peak_buffer_bytes",
    "writer_concurrency",
    "fragments_created",
    "largest_item_bytes",
    "lexical_segments_copied",
    "lexical_segments_generated",
}
THIN_POOL_MAX_DATA_PERCENT = Decimal(75)
THIN_POOL_MAX_METADATA_PERCENT = Decimal(70)


class CandidateRequestFailure(RuntimeError):
    """Transport status and validated public classification, without response text."""

    def __init__(self, status: int, classification: dict[str, Any]) -> None:
        super().__init__(f"API request failed with HTTP {status}")
        self.classification = classification


def candidate_failure_classification(raw: bytes) -> dict[str, Any]:
    if len(raw) > 4096:
        return {}
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError):
        return {}
    message = value.get("error") if isinstance(value, dict) else None
    if not isinstance(message, str):
        return {}
    match = re.fullmatch(
        r"storage-v2 release-candidate verification failed: "
        r"phase=(lexical_segment_integrity|intelligence_export|validation_or_reader_epoch|"
        r"test_scope|candidate_identity|generation_root|body_inventory|body_pack_integrity|"
        r"source_state|timeout_read|verification_timeout_set|verification_timeout_restore|query_seeds); "
        r"database_sqlstate=(none|[0-9A-Z]{5}); "
        r"reader_epoch_close_sqlstate=(none|[0-9A-Z]{5}); "
        r"retention_required=(true|false)", message,
    )
    if match is None:
        return {}
    return {"database_phase": match[1], "database_sqlstate": match[2],
            "reader_epoch_close_sqlstate": match[3], "retention_required": match[4] == "true"}


def request(api_url: str, token: str, method: str, path: str,
            body: object | None = None, *, timeout_seconds: float = 24 * 3600) -> Any:
    data = None if body is None else json.dumps(body, separators=(",", ":")).encode()
    call = urllib.request.Request(
        api_url.rstrip("/") + path,
        data=data,
        method=method,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(call, timeout=timeout_seconds) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        # Preserve only the API's exact public classification grammar. Never
        # retain arbitrary response text, headers, paths or exception messages.
        try:
            classification = candidate_failure_classification(error.read(4097))
        except (OSError, ValueError):
            classification = {}
        raise CandidateRequestFailure(error.code, classification) from error


def atomic_private_json(path: Path, value: object, *, replace: bool = True) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(value, output, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        if replace:
            os.replace(temporary_name, path)
        else:
            # Create-only publication keeps an earlier build checkpoint intact
            # even when another process creates it after the preflight check.
            os.link(temporary_name, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)


class VerificationProgress(dict[str, Any]):
    """Persist bounded phase timing while preserving the full failure evidence.

    A running journal describes the last observed phase, not process liveness.
    Retain it alongside the owned qualification attempt until evidence expires.
    """

    def __init__(self, output: Path) -> None:
        super().__init__(phase="checkpoint", query_results=[], comparisons=[], query_coverage=[],
                         qualification_attempted=False, qualification_outcome="NOT_ATTEMPTED")
        self.path = output.with_name(output.name + ".progress.json")
        self.owner = str(uuid.uuid4())
        self.started = self.phase_started = time.monotonic()
        self.phase_started_at_unix = time.time()
        self.completed: dict[str, dict[str, float | int]] = {}
        self.flush("RUNNING", replace=False)

    def __setitem__(self, key: str, value: Any) -> None:
        if key == "phase":
            self.close_phase()
            self.phase_started = time.monotonic()
            self.phase_started_at_unix = time.time()
        super().__setitem__(key, value)
        if key in {"phase", "qualification_attempted", "qualification_outcome"}:
            self.flush("RUNNING")

    def close_phase(self) -> None:
        phase = self.completed.setdefault(self["phase"], {"visits": 0, "elapsed_seconds": 0.0})
        phase["visits"] += 1
        phase["elapsed_seconds"] += time.monotonic() - self.phase_started

    def flush(self, status: str, *, replace: bool = True) -> None:
        pending = self.get("pending_query", {})
        atomic_private_json(self.path, {
            "schema_version": "mainrag.storage-v2.qualification-progress.v1",
            "owner": self.owner, "pid": os.getpid(), "status": status,
            "phase": self["phase"], "phase_started_at_unix": self.phase_started_at_unix,
            "updated_at_unix": time.time(), "elapsed_seconds": time.monotonic() - self.started,
            "completed_phase_timings": self.completed,
            "completed_queries": len(self["query_results"]),
            "pending_query": {key: pending[key] for key in ("ordinal", "kind", "id", "query_sha256")
                              if key in pending},
            "qualification_attempted": self["qualification_attempted"],
            "qualification_outcome": self["qualification_outcome"],
        }, replace=replace)

    def finish(self, status: str) -> None:
        self.close_phase()
        self.flush(status)


def source_state(api_url: str, token: str, source_id: int, generation: int) -> dict[str, Any]:
    query = urllib.parse.urlencode({"generation": generation, "include_test": "true"})
    return request(api_url, token, "GET", f"/api/v1/sources/{source_id}/shadow-state?{query}",
                   timeout_seconds=30)


def restarted_source_state(api_url: str, token: str, source_id: int,
                           generation: int, previous_instance: str) -> dict[str, Any]:
    """Wait for the authenticated API readback after an operator restart."""
    deadline = time.monotonic() + 90
    while True:
        try:
            state = source_state(api_url, token, source_id, generation)
        except urllib.error.URLError as error:
            if not isinstance(error.reason, (ConnectionRefusedError, TimeoutError)):
                raise
        except TimeoutError:
            pass
        else:
            if state["server_instance_id"] != previous_instance:
                return state
        if time.monotonic() >= deadline:
            raise RuntimeError("restarted API did not become ready with a new instance")
        time.sleep(0.5)


def reconstructed_source_state(api_url: str, token: str, checkpoint: dict[str, Any]) -> dict[str, Any]:
    """Recheck a restart proven by persisted generation and live service ages."""
    proof = checkpoint.get("reconstruction_evidence")
    if not isinstance(proof, dict) or proof.get("generation_status") not in {"verified", "release_candidate"} \
            or proof.get("run_status") != "sealed" \
            or type(proof.get("generation_created_at_unix")) is not int:
        raise RuntimeError("reconstructed checkpoint lacks persisted restart proof")
    review_sha256 = proof.get("source_snapshot_review_sha256")
    if review_sha256 is not None and (not isinstance(review_sha256, str)
                                     or not re.fullmatch(r"[0-9a-f]{64}", review_sha256)):
        raise RuntimeError("reconstructed source review digest is invalid")
    service = subprocess.run(
        ["systemctl", "show", "mainrag-api.service", "-p", "ExecMainPID", "--value"],
        capture_output=True, text=True, check=False)
    if service.returncode != 0 or not service.stdout.strip().isdigit():
        raise RuntimeError("API service process is unavailable for restart proof")
    pid = int(service.stdout.strip())
    if pid <= 0:
        raise RuntimeError("API service process is inactive")
    age = subprocess.run(["ps", "-o", "etimes=", "-p", str(pid)],
                         capture_output=True, text=True, check=False)
    if age.returncode != 0 or not age.stdout.strip().isdigit() \
            or int(time.time()) - int(age.stdout.strip()) <= proof["generation_created_at_unix"] + 2:
        raise RuntimeError("API restart after the persisted build is not established")
    state = source_state(api_url, token, checkpoint["source_id"], checkpoint["generation_seq"])
    if (state.get("generation_id") != checkpoint["generation_id"]
            or state.get("generation_seq") != checkpoint["generation_seq"]
            or state.get("status") not in {"verified", "release_candidate"}
            or state.get("active_generation_id") != checkpoint["active_generation_id"]):
        raise RuntimeError("reconstructed generation or active pointer changed")
    return state


def publish_telemetry(value: object) -> None:
    destination = os.environ.get("TM_KENNZAHLEN")
    if destination:
        atomic_private_json(Path(destination), value)


def validate_telemetry(value: object, item_count: int) -> None:
    if not isinstance(value, dict):
        raise RuntimeError("release-candidate response has no telemetry object")
    phases = value.get("phase")
    counters = value.get("ablauf")
    if not isinstance(phases, dict) or set(phases) != TELEMETRY_PHASES:
        raise RuntimeError("release-candidate telemetry has incomplete or unknown phase keys")
    if not isinstance(counters, dict) or not TELEMETRY_COUNTERS.issubset(counters):
        raise RuntimeError("release-candidate telemetry has incomplete optimization counters")
    values = [*phases.values(), *(counters[key] for key in TELEMETRY_COUNTERS)]
    if any(
        isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0
        for value in values
    ):
        raise RuntimeError("release-candidate telemetry values must be non-negative numbers")
    if any(
        isinstance(counters[key], bool) or not isinstance(counters[key], int)
        for key in TELEMETRY_COUNTERS - {"latenz_ms"}
    ):
        raise RuntimeError("release-candidate telemetry counters must be integers")
    if counters["errors"] != 0 or counters["io_buffer_bytes"] <= 0:
        raise RuntimeError("release-candidate telemetry reports errors or no I/O buffer")
    if counters["fragments_created"] > item_count:
        raise RuntimeError("release-candidate fragment count exceeds its item count")
    if item_count > 0 and not 0 < counters["largest_item_bytes"] <= counters["eingang_bytes"]:
        raise RuntimeError("release-candidate telemetry has invalid source item bounds")


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def thin_pool_capacity(pack_root: Path, maximum_growth_bytes: int | None,
                       *, require_estimate: bool) -> dict[str, object] | None:
    """Bound physical pool use for a pack root backed by an LVM thin volume."""
    try:
        mount = subprocess.run(
            ["findmnt", "--target", str(pack_root.resolve(strict=True)),
             "--noheadings", "--output", "SOURCE"],
            capture_output=True, text=True, check=False,
        )
    except OSError as error:
        raise RuntimeError("pack mount identity is unavailable") from error
    if mount.returncode or len(mount.stdout.splitlines()) != 1:
        raise RuntimeError("pack mount identity is unavailable")
    device = mount.stdout.strip()
    if not (device.startswith("/dev/mapper/") or device.startswith("/dev/dm-")
            or re.fullmatch(r"/dev/[^/]+/[^/]+", device)):
        return None

    prefix = [] if os.geteuid() == 0 else ["sudo", "-n"]

    def lvs(target: str) -> dict[str, str]:
        try:
            response = subprocess.run(
                [*prefix, "/usr/sbin/lvs", "--reportformat", "json", "--units", "b",
                 "--nosuffix", "-o",
                 "vg_name,lv_name,pool_lv,lv_size,data_percent,metadata_percent,seg_monitor",
                 target], capture_output=True, text=True, check=False,
            )
            rows = json.loads(response.stdout)["report"][0]["lv"]
        except (OSError, ValueError, KeyError, IndexError, TypeError) as error:
            raise RuntimeError("thin-pool identity is unavailable") from error
        if response.returncode or not isinstance(rows, list) or len(rows) != 1 \
                or not isinstance(rows[0], dict):
            raise RuntimeError("thin-pool identity is unavailable")
        return rows[0]

    volume = lvs(device)
    pool_name = volume.get("pool_lv")
    if not pool_name:
        return None
    vg_name = volume.get("vg_name")
    volume_name = volume.get("lv_name")
    if not isinstance(vg_name, str) or not re.fullmatch(r"[A-Za-z0-9_+.-]+", vg_name) \
            or not isinstance(pool_name, str) \
            or not re.fullmatch(r"[A-Za-z0-9_+.-]+", pool_name) \
            or not isinstance(volume_name, str) \
            or not re.fullmatch(r"[A-Za-z0-9_+.-]+", volume_name):
        raise RuntimeError("thin-pool identity is invalid")
    pool = lvs(f"{vg_name}/{pool_name}")
    if pool.get("lv_name") != pool_name or pool.get("vg_name") != vg_name \
            or pool.get("seg_monitor") != "monitored":
        raise RuntimeError("thin-pool monitoring or identity is invalid")
    try:
        pool_bytes = Decimal(pool["lv_size"])
        used_percent = Decimal(pool["data_percent"])
        metadata_percent = Decimal(pool["metadata_percent"])
    except (KeyError, InvalidOperation, TypeError) as error:
        raise RuntimeError("thin-pool usage is invalid") from error
    if not pool_bytes.is_finite() or pool_bytes <= 0 \
            or not used_percent.is_finite() or not 0 <= used_percent < 100 \
            or not metadata_percent.is_finite() or not 0 <= metadata_percent < 100:
        raise RuntimeError("thin-pool usage is invalid")
    try:
        policy = subprocess.run(
            [*prefix, "/usr/sbin/lvmconfig", "--type", "current",
             "activation/thin_pool_autoextend_threshold"],
            capture_output=True, text=True, check=False,
        )
    except OSError as error:
        raise RuntimeError("thin-pool autoextend policy is unavailable") from error
    match = re.fullmatch(r"thin_pool_autoextend_threshold=(\d+)", policy.stdout.strip())
    if policy.returncode or match is None or not 50 <= int(match.group(1)) <= 100:
        raise RuntimeError("thin-pool autoextend policy is invalid")
    threshold = Decimal(match.group(1))
    ceiling = min(THIN_POOL_MAX_DATA_PERCENT, threshold - 5)
    if require_estimate and (type(maximum_growth_bytes) is not int
                             or maximum_growth_bytes <= 0):
        raise RuntimeError("reviewed maximum thin-pool growth estimate is required")
    if maximum_growth_bytes is None:
        maximum_growth_bytes = 0
    if type(maximum_growth_bytes) is not int or maximum_growth_bytes < 0:
        raise RuntimeError("maximum thin-pool growth estimate is invalid")
    projected = used_percent + Decimal(maximum_growth_bytes) * 100 / pool_bytes
    if metadata_percent >= THIN_POOL_MAX_METADATA_PERCENT or projected > ceiling:
        raise RuntimeError("insufficient physical thin-pool headroom")
    return {"mount_source": device, "vg_name": vg_name,
            "thin_volume_name": volume_name, "pool_name": pool_name,
            "pool_size_bytes": int(pool_bytes),
            "data_percent_before_build": float(used_percent),
            "metadata_percent_before_build": float(metadata_percent),
            "maximum_pool_growth_bytes": maximum_growth_bytes,
            "projected_data_percent": float(projected),
            "maximum_data_percent": float(ceiling),
            "maximum_metadata_percent": float(THIN_POOL_MAX_METADATA_PERCENT),
            "autoextend_threshold_percent": int(threshold)}


def require_same_pool(previous: object, current: object) -> None:
    if previous is None and current is None:
        return
    identity = ("mount_source", "vg_name", "thin_volume_name", "pool_name")
    if not isinstance(previous, dict) or not isinstance(current, dict) \
            or any(not isinstance(previous.get(key), str) or not previous[key]
                   or not isinstance(current.get(key), str) or not current[key]
                   for key in identity) \
            or any(previous.get(key) != current.get(key) for key in identity):
        raise RuntimeError("pack thin-pool identity changed")


def prebuild_pack_capacity(arguments: argparse.Namespace) -> dict[str, object]:
    """Leave the approved pack reserve intact even at estimated peak build use."""
    if arguments.minimum_free_bytes < 0 or arguments.maximum_build_bytes <= 0:
        raise RuntimeError("pack reserve and maximum build estimate must be valid")
    if not arguments.pack_root.is_dir():
        raise RuntimeError("pack root is not an existing directory")
    free_bytes = shutil.disk_usage(arguments.pack_root).free
    required_bytes = arguments.minimum_free_bytes + arguments.maximum_build_bytes
    if free_bytes < required_bytes:
        raise RuntimeError("insufficient pack capacity before candidate build")
    result = {
        "free_bytes_before_build": free_bytes,
        "minimum_free_bytes": arguments.minimum_free_bytes,
        "maximum_build_bytes": arguments.maximum_build_bytes,
    }
    thin_pool = thin_pool_capacity(
        arguments.pack_root, getattr(arguments, "maximum_pool_growth_bytes", None),
        require_estimate=True,
    )
    if thin_pool is not None:
        if thin_pool["maximum_pool_growth_bytes"] < arguments.maximum_build_bytes:
            raise RuntimeError("thin-pool growth estimate omits pack build estimate")
        result["thin_pool"] = thin_pool
    return result


def build(arguments: argparse.Namespace, token: str) -> None:
    if arguments.checkpoint.exists() or arguments.checkpoint.is_symlink():
        raise RuntimeError("checkpoint already exists; preserve it and use verify")
    git_snapshot_commit_sha = getattr(arguments, "git_snapshot_commit_sha", None)
    expected_source_watermark_sha256 = getattr(
        arguments, "expected_source_watermark_sha256", None)
    source_review_sha256 = None
    gold_review_sha256 = None
    if git_snapshot_commit_sha is not None:
        review = read_snapshot_review(
            arguments.source_snapshot_review,
            arguments.source_snapshot_review_sha256,
            {"source_id": arguments.source_id,
             "source_watermark_sha256": expected_source_watermark_sha256},
            "mainrag.git-release-candidate.v1",
        )
        if review["source_type"] != "git" or review["git_head"] != git_snapshot_commit_sha:
            raise RuntimeError("pinned Git build differs from independently captured snapshot")
        require_live_snapshot(arguments.api_url, token, arguments.source_id,
                              review, git_snapshot_commit_sha)
        gold_path = arguments.source_snapshot_gold_review
        metadata = gold_path.lstat()
        if not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) & 0o077 \
                or metadata.st_size > 1024 * 1024:
            raise RuntimeError("prebuild gold review must be a bounded private regular file")
        raw = gold_path.read_bytes()
        gold = json.loads(raw)
        cases = gold.get("cases") if isinstance(gold, dict) else None
        if not isinstance(gold, dict) or gold.get("source_id") != arguments.source_id \
                or gold.get("source_type") != "git" \
                or not isinstance(gold.get("source_class"), str) \
                or gold.get("source_snapshot_review_sha256") != review["review_sha256"] \
                or not isinstance(cases, list) or len(cases) < 2 \
                or not any(isinstance(case, dict) and case.get("expects_match") is True
                           for case in cases) \
                or not any(isinstance(case, dict) and case.get("expects_match") is False
                           for case in cases) \
                or type(gold.get("reviewed_at_unix")) is not int \
                or gold["reviewed_at_unix"] < review["captured_at_unix"]:
            raise RuntimeError("prebuild gold review does not bind the frozen source")
        for case in cases:
            if not isinstance(case, dict) or set(case) != {
                    "id", "query", "expected_path_sha256", "expects_match"} \
                    or not isinstance(case["query"], str) \
                    or not 1 <= len(case["query"].encode()) <= 512 \
                    or type(case["expects_match"]) is not bool \
                    or not isinstance(case["expected_path_sha256"], str) \
                    or not re.fullmatch(r"[0-9a-f]{64}", case["expected_path_sha256"]) \
                    or (case["expects_match"] and review["paths"].get(
                        case["expected_path_sha256"], {}).get("status") != "same_bytes"):
                raise RuntimeError("prebuild gold case is not supported by frozen source bytes")
        source_review_sha256 = review["review_sha256"]
        gold_review_sha256 = hashlib.sha256(raw).hexdigest()
    pack_capacity = prebuild_pack_capacity(arguments)
    result, progress_attempt = monitored_build(arguments, token, request, atomic_private_json)
    if expected_source_watermark_sha256 is not None \
            and result["source_watermark_sha256"] != expected_source_watermark_sha256:
        raise RuntimeError("pinned Git build returned a different source watermark; reconcile writer")
    if result["active_generation_before"] != result["active_generation_after"]:
        raise RuntimeError("candidate construction changed the active pointer")
    validate_telemetry(result.get("telemetry"), int(result["item_count"]))
    state = source_state(arguments.api_url, token, arguments.source_id, int(result["generation_seq"]))
    postbuild_free_bytes = shutil.disk_usage(arguments.pack_root).free
    postbuild_resource_blocked = postbuild_free_bytes < arguments.minimum_free_bytes
    try:
        postbuild_thin_pool = thin_pool_capacity(
            arguments.pack_root, 0, require_estimate=False)
        require_same_pool(pack_capacity.get("thin_pool"), postbuild_thin_pool)
    except RuntimeError as error:
        postbuild_thin_pool = {"status": "BLOCKED", "reason": str(error)}
        postbuild_resource_blocked = True
    checkpoint = {
        "schema_version": 1,
        "build_progress_attempt_id": progress_attempt,
        # This reference may be published. A hash of a small numeric source ID
        # is enumerable, so use a random opaque value retained in the checkpoint.
        "source_ref": os.urandom(32).hex(),
        "source_id": arguments.source_id,
        "commit_sha": arguments.commit_sha,
        "generation_id": int(result["generation_id"]),
        "generation_seq": int(result["generation_seq"]),
        "source_watermark_sha256": result["source_watermark_sha256"],
        "item_count": int(result["item_count"]),
        "server_instance_id": state["server_instance_id"],
        "active_generation_id": state["active_generation_id"],
        "pack_capacity_before_build": pack_capacity,
        "pack_free_bytes_after_build": postbuild_free_bytes,
        "thin_pool_after_build": postbuild_thin_pool,
        "resource_gate_after_build": "BLOCKED" if postbuild_resource_blocked else "PASS",
        "build": result,
        "captured_at_unix": int(time.time()),
    }
    if git_snapshot_commit_sha is not None:
        checkpoint["git_snapshot_commit_sha"] = git_snapshot_commit_sha
        checkpoint["source_snapshot_review_sha256"] = source_review_sha256
        checkpoint["source_snapshot_gold_review_sha256"] = gold_review_sha256
    try:
        atomic_private_json(arguments.checkpoint, checkpoint, replace=False)
    except FileExistsError as error:
        raise RuntimeError("checkpoint appeared during build; inspect server state") from error
    if postbuild_resource_blocked:
        raise RuntimeError("post-build resource gate failed; protected checkpoint was preserved")
    publish_telemetry(result["telemetry"])
    print(json.dumps({
        "status": "VERIFIED",
        "source_ref": checkpoint["source_ref"],
        "generation_seq": checkpoint["generation_seq"],
        "item_count": checkpoint["item_count"],
        "reused_generation": bool(result["reused_generation"]),
    }, sort_keys=True))


def ranked(results: list[dict[str, Any]], mappings: dict[str, list[str]] | None = None) -> list[dict[str, Any]]:
    output = []
    mappings = mappings or {}
    for rank, result in enumerate(results, 1):
        hit_id = str(result.get("external_hit_id") or f"legacy:{int(result['chunk_id'])}")
        output.append({
            "hit_id": hit_id,
            "rank": rank,
            "score": float(result["score"]),
            "mapped_hit_ids": mappings.get(sha256_text(result["file_path"]), []),
            "authorized": True,
        })
    return output


def path_identity(results: list[dict[str, Any]]) -> list[str]:
    """Return the stable, de-duplicated path identity in result-rank order."""
    identity = []
    seen = set()
    for result in results:
        path_sha256 = sha256_text(result["file_path"])
        if path_sha256 not in seen:
            identity.append(path_sha256)
            seen.add(path_sha256)
    return identity


def read_snapshot_review(path: Path, expected_sha256: str, checkpoint: dict[str, Any],
                         adapter_profile_id: str) -> dict[str, Any]:
    """Require a bounded private source-drift review for the exact watermark."""
    if not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
        raise RuntimeError("exact source snapshot review digest is required")
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) & 0o077 \
        or metadata.st_size > 64 * 1024 * 1024:
        raise RuntimeError("source snapshot review must be a bounded private regular file")
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise RuntimeError("source snapshot review digest differs")
    review = json.loads(raw)
    if not isinstance(review, dict) \
            or review.get("schema_version") != "mainrag.storage-v2.source-snapshot-review.v1" \
            or review.get("source_id") != checkpoint["source_id"] \
            or review.get("source_type") not in {"fs", "git"} \
            or (review["source_type"] == "git") != adapter_profile_id.startswith("mainrag.git-") \
            or review.get("source_watermark_sha256") != checkpoint["source_watermark_sha256"] \
            or review.get("adapter_profile_id") != adapter_profile_id \
            or type(review.get("item_count")) is not int or review["item_count"] < 0 \
            or type(review.get("captured_at_unix")) is not int \
            or any(not isinstance(review.get(key), str)
                   or not re.fullmatch(r"[0-9a-f]{64}", review[key])
                   for key in ("source_root_sha256", "source_config_sha256")):
        raise RuntimeError("source snapshot review identity differs")
    if review["source_type"] == "git" and (
            not isinstance(review.get("git_head"), str) or not re.fullmatch(
                r"[0-9a-f]{40}|[0-9a-f]{64}", review["git_head"])):
        raise RuntimeError("source snapshot review Git commit identity differs")
    if checkpoint.get("git_snapshot_commit_sha") is not None \
            and review.get("git_head") != checkpoint["git_snapshot_commit_sha"]:
        raise RuntimeError("source snapshot review differs from pinned Git commit")
    scope_matcher(review.get("filesystem_scope"), adapter_profile_id)
    cut = review.get("filesystem_cut")
    if adapter_profile_id.startswith(cut_contract["CUT_PROFILE"]):
        cut_contract["require_manifest"](cut, review["source_root_sha256"],
                                          adapter_profile_id, review["item_count"])
        if not cut_contract["same_source_manifest"](cut, checkpoint.get("build", {}).get("filesystem_cut")):
            raise RuntimeError("source review differs from the original full build manifest")
    elif cut is not None:
        raise RuntimeError("source review has an unselected filesystem cut")
    paths = review.get("paths")
    if not isinstance(paths, dict):
        raise RuntimeError("source snapshot review path set is invalid")
    counts: dict[str, int] = {}
    for path_sha, row in paths.items():
        if not isinstance(path_sha, str) or not re.fullmatch(r"[0-9a-f]{64}", path_sha) \
                or not isinstance(row, dict) or row.get("status") not in {
                    "same_bytes", "changed_bytes", "source_file_missing", "outside_configured_scope"
                } or not isinstance(row.get("legacy_sha256"), str) \
                or not re.fullmatch(r"[0-9a-f]{64}", row["legacy_sha256"]):
            raise RuntimeError("source snapshot review path identity differs")
        observed = row.get("observed_sha256")
        if (row["status"] in {"source_file_missing", "outside_configured_scope"} and observed is not None) \
                or (row["status"] not in {"source_file_missing", "outside_configured_scope"}
                    and (not isinstance(observed, str)
                         or not re.fullmatch(r"[0-9a-f]{64}", observed)
                         or (observed == row["legacy_sha256"])
                         != (row["status"] == "same_bytes"))):
            raise RuntimeError("source snapshot review byte classification differs")
        if row["status"] == "outside_configured_scope" and review.get("filesystem_scope") is None:
            raise RuntimeError("source scope classification has no bound filter")
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    if review.get("status_counts") != counts:
        raise RuntimeError("source snapshot review counts differ")
    review["review_sha256"] = expected_sha256
    return review


def require_live_snapshot(api_url: str, token: str, source_id: int,
                          review: dict[str, Any], git_snapshot_commit_sha: str | None = None) -> None:
    suffix = ("?git_snapshot_commit_sha=" + git_snapshot_commit_sha
              if git_snapshot_commit_sha is not None else "")
    live = request(api_url, token, "GET",
                   f"/api/v1/admin/sources/{source_id}/storage-v2-release-watermark{suffix}",
                   timeout_seconds=600)
    if (live.get("source_id") != source_id
            or live.get("source_watermark_sha256") != review["source_watermark_sha256"]
            or live.get("adapter_profile_id") != review["adapter_profile_id"]
            or live.get("item_count") != review["item_count"]
            or live.get("filesystem_cut") != review.get("filesystem_cut")):
        raise RuntimeError("live source adapter differs from frozen snapshot review")


def query_set_sha256(comparisons: list[dict[str, Any]]) -> str:
    fixtures = sorted(
        json.dumps(item["fixture"], sort_keys=True, separators=(",", ":")).encode()
        for item in comparisons
    )
    digest = hashlib.sha256()
    for fixture in fixtures:
        digest.update(struct.pack(">Q", len(fixture)))
        digest.update(fixture)
    return digest.hexdigest()


def query_seed_summary(seeds: list[dict[str, Any]]) -> dict[str, Any]:
    """Describe suite diversity without changing cases or acceptance policy.

    Equality is exact query-text equality, not inferred semantic equivalence.
    Multiple expected paths for one query are cases, not independent queries.
    Counts alone do not prove representative gold coverage.
    """
    query_counts: dict[str, int] = {}
    for seed in seeds:
        query = seed["query"]
        query_counts[query] = query_counts.get(query, 0) + 1
    return {
        "schema_version": "mainrag.storage-v2.query-seed-summary.v1",
        "case_count": len(seeds),
        "distinct_query_count": len(query_counts),
        "repeated_query_case_count": len(seeds) - len(query_counts),
        "largest_query_group": max(query_counts.values(), default=0),
        "positive_case_count": sum(seed["expects_match"] is True for seed in seeds),
        "negative_case_count": sum(seed["expects_match"] is False for seed in seeds),
        "representative_gold_coverage": "NOT_ESTABLISHED",
    }


def load_gold_suite(arguments: argparse.Namespace, checkpoint: dict[str, Any],
                    verified: dict[str, Any],
                    source_review: dict[str, Any] | None = None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Bind a protected, reviewed source-class suite to this exact candidate."""
    path = arguments.gold_suite
    expected = arguments.expected_gold_suite_sha256
    if path is None or not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise RuntimeError("an expected protected gold-suite digest is required")
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) & 0o077:
        raise RuntimeError("gold suite must be a private regular file")
    raw = path.read_bytes()
    if len(raw) > 1024 * 1024 or hashlib.sha256(raw).hexdigest() != expected:
        raise RuntimeError("gold suite size or reviewed digest differs")
    def unique_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("duplicate JSON key")
            value[key] = item
        return value

    try:
        suite = json.loads(raw, object_pairs_hook=unique_keys)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise RuntimeError("gold suite is not valid JSON") from error
    if not isinstance(suite, dict) or suite.get("schema_version") != "mainrag.storage-v2.gold-suite.v1":
        raise RuntimeError("gold suite schema differs")
    bindings = {
        "source_id": checkpoint["source_id"],
        "generation_id": checkpoint["generation_id"],
        "commit_sha": checkpoint["commit_sha"],
        "source_watermark_sha256": checkpoint["source_watermark_sha256"],
        "adapter_profile_id": verified["adapter_profile_id"],
        "analysis_profile_id": verified["analysis_profile_id"],
        "search_profile_id": verified["search_profile_id"],
    }
    if any(type(suite.get(key)) is not type(value) or suite[key] != value
           for key, value in bindings.items()):
        raise RuntimeError("gold suite candidate identity differs")
    source_review_sha256 = (source_review["review_sha256"]
                            if source_review is not None else None)
    if suite.get("source_snapshot_review_sha256") != source_review_sha256:
        raise RuntimeError("gold suite source snapshot review binding differs")
    gold_review_sha256 = suite.get("source_snapshot_gold_review_sha256")
    if (source_review is not None and
        (not isinstance(gold_review_sha256, str)
         or not re.fullmatch(r"[0-9a-f]{64}", gold_review_sha256))) \
            or (source_review_sha256 is None and gold_review_sha256 is not None):
        raise RuntimeError("gold suite independent snapshot review binding differs")
    if source_review is not None:
        review_path = getattr(arguments, "source_snapshot_gold_review", None)
        if review_path is None:
            raise RuntimeError("independent source snapshot gold review is required")
        review_metadata = review_path.lstat()
        if (not stat.S_ISREG(review_metadata.st_mode)
                or stat.S_IMODE(review_metadata.st_mode) & 0o077
                or review_metadata.st_size > 1024 * 1024):
            raise RuntimeError("source snapshot gold review must be a bounded private file")
        review_raw = review_path.read_bytes()
        if hashlib.sha256(review_raw).hexdigest() != gold_review_sha256:
            raise RuntimeError("source snapshot gold review digest differs")
        gold_review = json.loads(review_raw, object_pairs_hook=unique_keys)
        if (not isinstance(gold_review, dict)
                or gold_review.get("source_id") != checkpoint["source_id"]
                or gold_review.get("source_type") != source_review["source_type"]
                or gold_review.get("source_class") != suite.get("source_class")
                or gold_review.get("source_snapshot_review_sha256")
                != source_review_sha256
                or gold_review.get("cases") != suite.get("cases")
                or type(gold_review.get("reviewed_at_unix")) is not int
                or gold_review["reviewed_at_unix"]
                < source_review["captured_at_unix"]):
            raise RuntimeError("source snapshot gold review differs from frozen source")
    source_class = suite.get("source_class")
    cases = suite.get("cases")
    if not isinstance(source_class, str) or not 1 <= len(source_class) <= 80:
        raise RuntimeError("gold suite has no source class")
    if not isinstance(cases, list) or not 2 <= len(cases) <= 256:
        raise RuntimeError("gold suite needs bounded positive and negative cases")
    seen_ids: set[str] = set()
    seen_cases: set[tuple[str, str, bool]] = set()
    positive = negative = 0
    for case in cases:
        if not isinstance(case, dict) or set(case) != {
            "id", "query", "expected_path_sha256", "expects_match",
        }:
            raise RuntimeError("gold suite case shape differs")
        if source_review is not None and case["expects_match"] is True \
                and source_review["paths"].get(case["expected_path_sha256"], {}).get(
                    "status") != "same_bytes":
            raise RuntimeError("positive gold expectation is not a same-byte source path")
        if not isinstance(case["id"], str) or not re.fullmatch(r"[0-9a-f]{64}", case["id"]):
            raise RuntimeError("gold suite case ID must be opaque SHA-256")
        if not isinstance(case["query"], str) or not 1 <= len(case["query"].encode()) <= 512:
            raise RuntimeError("gold suite query size differs")
        if not isinstance(case["expected_path_sha256"], str) or not re.fullmatch(
            r"[0-9a-f]{64}", case["expected_path_sha256"]
        ) or type(case["expects_match"]) is not bool:
            raise RuntimeError("gold suite expectation differs")
        identity = (case["query"], case["expected_path_sha256"], case["expects_match"])
        if case["id"] in seen_ids or identity in seen_cases:
            raise RuntimeError("gold suite contains duplicate cases")
        seen_ids.add(case["id"])
        seen_cases.add(identity)
        positive += case["expects_match"]
        negative += not case["expects_match"]
    if not positive or not negative or len({case["query"] for case in cases}) < 2:
        raise RuntimeError("gold suite lacks positive, negative, or distinct queries")
    return cases, {
        "schema_version": "mainrag.storage-v2.gold-suite-summary.v1",
        "suite_sha256": expected,
        "source_class": source_class,
        "case_count": len(cases),
        "positive_case_count": positive,
        "negative_case_count": negative,
        "distinct_query_count": len({case["query"] for case in cases}),
        "representative_coverage": "SUITE_DIGEST_BOUND_REVIEW_EXTERNAL",
        "source_snapshot_review_sha256": source_review_sha256,
        "source_snapshot_gold_review_sha256": gold_review_sha256,
    }


def query_difference_diagnostics(seed: dict[str, Any], current: dict[str, Any],
                                 storage: dict[str, Any]) -> dict[str, Any]:
    """Describe observed top-k differences, never infer corpus loss or relevance."""
    baseline, candidate = path_identity(current["results"]), path_identity(storage["results"])
    baseline_set, candidate_set = set(baseline), set(candidate)
    expected = seed["expected_path_sha256"]
    reasons = []
    if seed["expects_match"]:
        presence = (expected in baseline_set, expected in candidate_set)
        expected_location = {(True, True): "both", (True, False): "current_only",
                             (False, True): "storage_v2_only", (False, False): "neither"}[presence]
        if not presence[0]:
            reasons.append("expected_not_in_current_top_k")
        if not presence[1]:
            reasons.append("expected_not_in_storage_v2_top_k")
    else:
        expected_location = "not_applicable"
        if baseline or candidate:
            reasons.append("unexpected_negative_case_hits")
    missing = len(baseline_set - candidate_set)
    common_order_equal = (list(dict.fromkeys(path for path in baseline if path in candidate_set))
                          == list(dict.fromkeys(path for path in candidate if path in baseline_set)))
    if missing:
        reasons.append("baseline_paths_missing_from_top_k")
    if not common_order_equal:
        reasons.append("retained_baseline_order_changed")
    return {"schema_version": "mainrag.storage-v2.query-difference.v1",
            "expected_location": expected_location, "observations": reasons,
            "baseline_paths_missing": missing,
            "candidate_paths_added": len(candidate_set - baseline_set),
            "common_path_order_equal": common_order_equal,
            "current_repeated_path_hits": len(current["results"]) - len(baseline),
            "storage_v2_repeated_path_hits": len(storage["results"]) - len(candidate),
            "corpus_presence": "NOT_ESTABLISHED", "ranking_cause": "NOT_ESTABLISHED",
            "acceptance_effect": "NONE"}


def repeated_result_diagnostics(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    """Classify repeated reads of one path; never use across search engines.

    Timing/other envelope metadata is excluded. Every returned hit field and the
    total remain part of identity. A tie classification is not an acceptance.
    """
    def valid(response):
        if not isinstance(response, dict) or not isinstance(response.get("results"), list):
            return False
        if "total" in response and (type(response["total"]) is not int or response["total"] < 0):
            return False
        rows = response["results"]
        if any(not isinstance(row, dict) or type(row.get("chunk_id")) is not int
               or row["chunk_id"] <= 0 or type(row.get("score")) not in (int, float)
               or (type(row["score"]) is float and not math.isfinite(row["score"])) for row in rows):
            return False
        try:
            json.dumps(rows, sort_keys=True, allow_nan=False)
        except (TypeError, ValueError):
            return False
        return len({row["chunk_id"] for row in rows}) == len(rows)

    classification = "UNCLASSIFIED_VARIATION"
    if not valid(left) or not valid(right):
        classification = "INVALID_RESULT_IDENTITY"
    elif ("total" in left) == ("total" in right) and left.get("total") == right.get("total"):
        a, b = left["results"], right["results"]
        encoded_a = [json.dumps(row, sort_keys=True, allow_nan=False) for row in a]
        encoded_b = [json.dumps(row, sort_keys=True, allow_nan=False) for row in b]
        if encoded_a == encoded_b:
            classification = "ORDERED_RESULTS_IDENTICAL"
        elif (dict(zip((row["chunk_id"] for row in a), encoded_a))
              == dict(zip((row["chunk_id"] for row in b), encoded_b))
              and [row["score"] for row in a] == [row["score"] for row in b]):
            classification = "IDENTICAL_HITS_EQUAL_SCORE_TIE_PERMUTATION"
    return {"schema_version": "mainrag.storage-v2.repeated-result.v1",
            "classification": classification, "acceptance_effect": "NONE"}


def bind_automatic_expectation(seed: dict[str, Any], current: dict[str, Any],
                               source_review: dict[str, Any] | None
                               ) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Bind a stale automatic positive to unchanged legacy bytes before candidate reads.

    Keep every query and case ID. Reviewed gold expectations are never rebound.
    If no unchanged legacy result exists, retain the original failing expectation.
    """
    if source_review is None or not seed["expects_match"]:
        return seed, None
    original = seed["expected_path_sha256"]
    status = source_review["paths"].get(original, {}).get("status")
    if status not in {"changed_bytes", "source_file_missing", "outside_configured_scope"}:
        return seed, None
    replacement = next((path for path in path_identity(current["results"])
                        if source_review["paths"].get(path, {}).get("status") == "same_bytes"), None)
    if replacement is None:
        return seed, None
    return {**seed, "expected_path_sha256": replacement}, {
        "policy": "unchanged-legacy-positive-before-candidate-read-v1",
        "original_expected_path_sha256": original,
        "original_source_status": status,
        "expected_path_sha256": replacement,
        "source_snapshot_review_sha256": source_review["review_sha256"],
    }


def search_query_gates(seed: dict[str, Any], current: dict[str, Any],
                       storage: dict[str, Any], max_query_ms: int,
                       coverage: dict[str, Any] | None = None,
                       checkpoint: dict[str, Any] | None = None,
                       source_review: dict[str, Any] | None = None) -> dict[str, Any]:
    """Classify observable failures without accepting a plausible difference."""
    current_paths = path_identity(current["results"])
    storage_paths = path_identity(storage["results"])
    expected = seed["expected_path_sha256"]
    quality = (
        expected in current_paths and expected in storage_paths and current_paths == storage_paths
        if seed["expects_match"] else not current["results"] and not storage["results"]
    )
    coverage_check = None
    if coverage is not None:
        coverage_check = query_coverage_gates(
            seed, current, storage, coverage, checkpoint or {}, source_review)
        quality = coverage_check["passed"]
    took_ms = storage.get("took_ms")
    performance = (type(took_ms) is int and 0 <= took_ms <= max_query_ms)
    degradation = all(
        result.get("degradation", {}).get(stage) in {"available", "unavailable"}
        for result in storage["results"] for stage in ("graph", "semantic", "rerank")
    )
    return {
        "id": seed["id"],
        "quality_passed": quality,
        "performance_passed": performance,
        "degradation_passed": degradation,
        "expected_in_current": expected in current_paths,
        "expected_in_storage_v2": expected in storage_paths,
        "missing_current_paths": len(set(storage_paths) - set(current_paths)),
        "missing_storage_v2_paths": len(set(current_paths) - set(storage_paths)),
        "same_path_order": current_paths == storage_paths,
        "current_count": len(current["results"]),
        "storage_v2_count": len(storage["results"]),
        "current_took_ms": current.get("took_ms"),
        "storage_v2_took_ms": took_ms,
        "max_query_ms": max_query_ms,
        "current_identity_sha256": sha256_text(json.dumps(current_paths, separators=(",", ":"))),
        "storage_v2_identity_sha256": sha256_text(json.dumps(storage_paths, separators=(",", ":"))),
        "coverage": coverage_check,
        "diagnostics": query_difference_diagnostics(seed, current, storage),
    }


def query_coverage_gates(seed: dict[str, Any], current: dict[str, Any], storage: dict[str, Any],
                         evidence: dict[str, Any], checkpoint: dict[str, Any],
                         source_review: dict[str, Any] | None = None) -> dict[str, Any]:
    """Require complete legacy path recall and independent support for every new hit."""
    version = evidence.get("schema_version")
    policy = ("simple-conjunction-source-snapshot-v1" if source_review is not None else
              "simple-conjunction-non-inferiority-v2"
              if version == "mainrag.storage-v2.query-coverage.v4" else
              "simple-conjunction-non-inferiority-v1"
              if version == "mainrag.storage-v2.query-coverage.v3"
              else "literal-coverage-non-inferiority-v1")
    failed = {"passed": False, "policy": policy}
    if version not in {"mainrag.storage-v2.query-coverage.v1",
                       "mainrag.storage-v2.query-coverage.v2",
                       "mainrag.storage-v2.query-coverage.v3",
                       "mainrag.storage-v2.query-coverage.v4"} \
            or (source_review is not None
                and version != "mainrag.storage-v2.query-coverage.v4") \
            or evidence.get("query_sha256") != sha256_text(seed["query"]) \
            or any(type(evidence.get(key)) is not int or evidence[key] <= 0
                   for key in ("source_id", "generation_id", "generation_seq")) \
            or any(key not in checkpoint or evidence.get(key) != checkpoint[key]
                   for key in ("source_id", "generation_id", "generation_seq", "commit_sha")):
        return failed
    candidate = evidence.get("candidate")
    baseline = evidence.get("current")
    legacy = evidence.get("legacy_paths")
    if not all(isinstance(rows, list) for rows in (candidate, baseline, legacy)):
        return failed
    for rows, results, identity in ((candidate, storage["results"], "occurrence_id"),
                                    (baseline, current["results"], "chunk_id")):
        if len(rows) != len(results) or len(rows) > 10 or any(not isinstance(row, dict) for row in rows):
            return failed
        if any(type(row.get(identity)) is not int or row[identity] <= 0 for row in rows) \
                or any(type(hit.get("chunk_id")) is not int or hit["chunk_id"] <= 0
                       or not isinstance(hit.get("file_path"), str) for hit in results):
            return failed
        indexed = {row[identity]: row for row in rows}
        if len(indexed) != len(rows) or set(indexed) != {hit["chunk_id"] for hit in results}:
            return failed
        for hit in results:
            row = indexed[hit["chunk_id"]]
            if row.get("path_sha256") != sha256_text(hit["file_path"]):
                return failed
            if identity == "chunk_id":
                if row.get("indexed_match") is not True:
                    return failed
            elif row.get("external_hit_id") != hit.get("external_hit_id") \
                    or not isinstance(row.get("external_hit_id"), str) \
                    or row.get("body_text_matches") is not True \
                    or not isinstance(row.get("body_sha256"), str) \
                    or len(row["body_sha256"]) != 64 \
                    or any(c not in "0123456789abcdef" for c in row["body_sha256"]) \
                    or type(row.get("reference_frequency")) is not int \
                    or type(row.get("posting_frequency")) is not int \
                    or not (
                        (row["reference_frequency"] > 0
                         and row["posting_frequency"] == row["reference_frequency"])
                    or (version in {"mainrag.storage-v2.query-coverage.v2",
                                        "mainrag.storage-v2.query-coverage.v3",
                                        "mainrag.storage-v2.query-coverage.v4"}
                            and row.get("fts_body_matches") is True
                            and row.get("segment_matches") is True)
                    or (version == "mainrag.storage-v2.query-coverage.v4"
                            and row.get("legacy_segment_matches") is True
                            and row.get("segment_matches") is True)
                    ):
                return failed
            if identity == "occurrence_id" \
                    and version == "mainrag.storage-v2.query-coverage.v4" \
                    and type(row.get("legacy_segment_matches")) is not bool:
                return failed
    current_paths = path_identity(current["results"])
    storage_paths = path_identity(storage["results"])
    if any(not isinstance(row, dict) or not isinstance(row.get("path_sha256"), str) for row in legacy):
        return failed
    by_path = {row["path_sha256"]: row for row in legacy}
    if len(by_path) != len(legacy) or set(by_path) != set(current_paths) | set(storage_paths):
        return failed
    for row in legacy:
        if any(type(row.get(key)) is not int or row[key] < 0
               for key in ("chunk_count", "indexed_matches", "literal_matches")) \
                or row["indexed_matches"] > row["chunk_count"] \
                or row["literal_matches"] > row["chunk_count"]:
            return failed
    # One legacy source path can produce several chunk hits. Compare ordered
    # paths once each; every distinct baseline path still has to remain in the
    # candidate Top-10 and retain its relative order.
    baseline_paths = list(dict.fromkeys(current_paths))
    if source_review is not None:
        if (source_review.get("source_id") != checkpoint["source_id"]
                or source_review.get("source_watermark_sha256")
                != checkpoint["source_watermark_sha256"]
                or not isinstance(source_review.get("paths"), dict)
                or not isinstance(source_review.get("review_sha256"), str)
                or any(path not in source_review["paths"] for path in baseline_paths)):
            return failed
        if any(not isinstance(source_review["paths"][path], dict)
               for path in baseline_paths):
            return failed
        required_paths = [path for path in baseline_paths
                          if source_review["paths"][path].get("status") == "same_bytes"]
        stale_paths = [path for path in baseline_paths
                       if source_review["paths"][path].get("status") in {
                           "changed_bytes", "source_file_missing", "outside_configured_scope"}]
        if len(required_paths) + len(stale_paths) != len(baseline_paths):
            return failed
    else:
        required_paths = baseline_paths
        stale_paths = []
    retained = list(dict.fromkeys(path for path in storage_paths if path in set(required_paths)))
    expected_review = (source_review["paths"].get(seed["expected_path_sha256"])
                       if source_review is not None else None)
    # An automatic seed may name a changed file whose old query still matches
    # its current bytes. Do not rebind that path or accept a candidate hash as
    # its own source proof: require the independently frozen complete-file hash
    # and a body/segment match. Fragment matches require a separately verified,
    # complete ordered byte digest; context-only matches still fail.
    expected_source_body = None
    if isinstance(expected_review, dict) and expected_review.get("status") == "changed_bytes":
        observed = expected_review.get("observed_sha256")
        legacy_hash = expected_review.get("legacy_sha256")
        if (isinstance(observed, str) and re.fullmatch(r"[0-9a-f]{64}", observed)
                and isinstance(legacy_hash, str) and re.fullmatch(r"[0-9a-f]{64}", legacy_hash)
                and observed != legacy_hash
                and any(row["path_sha256"] == seed["expected_path_sha256"]
                        and row["body_sha256"] == observed
                        and row.get("fts_body_matches") is True
                        and row.get("segment_matches") is True for row in candidate)):
            expected_source_body = {
                "schema_version": "mainrag.storage-v2.expected-source-body.v1",
                "path_sha256": seed["expected_path_sha256"],
                "observed_sha256": observed, "legacy_sha256": legacy_hash,
                "body_sha256": observed, "source_status": "changed_bytes",
                "query_body_match": True,
                "source_snapshot_review_sha256": source_review["review_sha256"],
            }
        complete = evidence.get("complete_source_file")
        if (expected_source_body is None and complete_file_proof_valid(complete)
                and all(complete.get(key) == checkpoint[key] for key in (
                    "source_id", "generation_id", "generation_seq", "commit_sha"))
                and complete["path_sha256"] == seed["expected_path_sha256"]
                and complete["body_sha256"] == observed and observed != legacy_hash
                and any(row["path_sha256"] == seed["expected_path_sha256"]
                        and row.get("fts_body_matches") is True
                        and row.get("segment_matches") is True for row in candidate)):
            expected_source_body = {
                "schema_version": "mainrag.storage-v2.expected-source-body.v2",
                "path_sha256": seed["expected_path_sha256"],
                "observed_sha256": observed, "legacy_sha256": legacy_hash,
                "body_sha256": observed, "source_status": "changed_bytes",
                "query_body_match": True, "complete_source_file": complete,
                "source_snapshot_review_sha256": source_review["review_sha256"],
            }
    positive = (expected_review is None
                or (isinstance(expected_review, dict)
                    and expected_review.get("status") == "same_bytes")
                or expected_source_body is not None) \
               and (seed["expected_path_sha256"] in storage_paths
                and retained == required_paths)
    negative = not current["results"] and not storage["results"]
    classes: dict[str, int] = {}
    for path in set(storage_paths) - set(current_paths):
        row = by_path[path]
        if row["chunk_count"] == 0:
            reason = "legacy_not_indexed"
        elif row["indexed_matches"] == 0:
            reason = "legacy_lexical_projection_gap" if row["literal_matches"] else "legacy_content_gap"
        else:
            reason = "ranking_expansion"
        classes[reason] = classes.get(reason, 0) + 1
    return {"passed": positive if seed["expects_match"] else negative,
            "policy": policy,
            "source_snapshot_review_sha256": (
                source_review["review_sha256"] if source_review is not None else None),
            "same_byte_baseline_path_count": len(required_paths),
            "stale_baseline_path_count": len(stale_paths),
            "all_candidate_hits_supported": True,
            "all_current_hits_supported": True,
            "baseline_paths_retained_in_order": retained == required_paths,
            **({"expected_source_body": expected_source_body} if expected_source_body else {}),
            "additional_path_classes": classes}


def intelligence_layers_summary(response: Any, *, chunk_bytes: int = 65536,
                                maximum_item_chars: int = 16 * 1024**2) -> tuple[str, str]:
    """Hash the complete layers array with the existing canonical JSON grammar.

    Retain one decoded symbol at a time. The response must be a complete array
    of objects, including its closing delimiter and whitespace-only suffix.
    """
    decoder = json.JSONDecoder()
    utf8 = codecs.getincrementaldecoder("utf-8")()
    buffer = ""
    eof = False

    def more() -> None:
        nonlocal buffer, eof
        raw = response.read(chunk_bytes)
        eof = not raw
        buffer += utf8.decode(raw, final=eof)
        if len(buffer) > maximum_item_chars + chunk_bytes:
            raise RuntimeError("candidate intelligence symbol exceeds the streaming bound")

    def token() -> str:
        nonlocal buffer
        buffer = buffer.lstrip()
        while not buffer and not eof:
            more()
            buffer = buffer.lstrip()
        return buffer[:1]

    if token() != "[":
        raise RuntimeError("candidate intelligence layers is not an array")
    buffer = buffer[1:]
    digest = hashlib.sha256(b"[")
    name = None
    count = 0
    while token() != "]":
        if token() != "{":
            raise RuntimeError("candidate intelligence layers contains an invalid symbol")
        while True:
            try:
                item, end = decoder.raw_decode(buffer)
                break
            except json.JSONDecodeError as error:
                if eof:
                    raise RuntimeError("candidate intelligence layers is incomplete") from error
                more()
        if end > maximum_item_chars:
            raise RuntimeError("candidate intelligence symbol exceeds the streaming bound")
        buffer = buffer[end:]
        if count == 0:
            generic = item.get("generic_card", {})
            name = generic.get("name") or item.get("qualified_name")
            del generic
        else:
            digest.update(b", ")
        digest.update(json.dumps(item, sort_keys=True).encode())
        count += 1
        # Release the decoded object before reading the next symbol.
        del item
        separator = token()
        if separator == "]":
            break
        if separator != ",":
            raise RuntimeError("candidate intelligence layers has an invalid delimiter")
        buffer = buffer[1:]
        if token() != "{":
            raise RuntimeError("candidate intelligence layers has an invalid next symbol")
    buffer = buffer[1:]
    if token():
        raise RuntimeError("candidate intelligence layers has trailing data")
    if not count:
        raise RuntimeError("candidate intelligence layers returned no applicable symbol")
    if not name:
        raise RuntimeError("candidate intelligence symbol omitted its name")
    digest.update(b"]")
    return name, digest.hexdigest()


def request_intelligence_layers(api_url: str, token: str, path: str) -> tuple[str, str]:
    call = urllib.request.Request(api_url.rstrip("/") + path,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(call, timeout=24 * 3600) as response:
            return intelligence_layers_summary(response)
    except urllib.error.HTTPError as error:
        raise RuntimeError(f"API request failed with HTTP {error.code}") from error


def verify_intelligence(api_url: str, token: str, source_id: int, generation: int,
                        export: dict[str, Any], progress: dict[str, Any] | None = None) -> dict[str, Any]:
    hashes: dict[str, str] = {}
    if progress is not None:
        progress["intelligence_result_sha256"] = hashes
        progress["phase"] = "intelligence_layers"
    record_counts = export["payload"]["record_counts"]
    if int(record_counts["cards"]) == 0:
        return {"applicability": "unknown_not_applicable", "commands": []}
    # Named command reads use the same supported bound as normal API callers.
    # Full intelligence export/integrity remains part of server verification.
    common = {"source_id": source_id, "generation": generation,
              "include_test": "true", "limit": 200}
    if progress is not None:
        progress["intelligence_command_query_limit"] = common["limit"]
    layers_query = urllib.parse.urlencode({**common, "command": "layers"})
    name, hashes["layers"] = request_intelligence_layers(
        api_url, token, f"/api/v1/intelligence/shadow?{layers_query}")
    for command in ("card", "explain", "ownership"):
        if progress is not None:
            progress["phase"] = "intelligence_" + command
        query = urllib.parse.urlencode({**common, "command": command, "name": name})
        value = request(api_url, token, "GET", f"/api/v1/intelligence/shadow?{query}")
        hashes[command] = sha256_text(json.dumps(value, sort_keys=True))
    return {"applicability": "applicable", "commands": sorted(hashes),
            "command_query_limit": 200,
            "command_coverage": "complete bounded API responses; full intelligence export verified separately",
            "result_sha256": hashes}


def verify(arguments: argparse.Namespace, token: str) -> None:
    if arguments.output.exists():
        raise RuntimeError("verification output already exists; retain it and choose a new attempt")
    progress = VerificationProgress(arguments.output)
    try:
        verify_candidate(arguments, token, progress)
    except Exception as error:
        # Preserve completed proof even when a later HTTP request or local check
        # fails. Never copy exception messages, request headers, or response bodies.
        if not arguments.output.exists():
            failure = {"type": type(error).__name__}
            cause = error
            seen: set[int] = set()
            while cause is not None and id(cause) not in seen:
                seen.add(id(cause))
                if isinstance(cause, CandidateRequestFailure):
                    failure.update(cause.classification)
                if isinstance(cause, urllib.error.HTTPError):
                    failure["http_status"] = cause.code
                    break
                cause = cause.__cause__ or cause.__context__
            atomic_private_json(arguments.output, {
                **progress, "status": "FAIL", "failed_gate": progress["phase"],
                "error": failure,
            })
        progress.finish("FAILED")
        raise
    else:
        progress.finish("COMPLETED")


def read_private_receipt(path: Path, expected_sha256: str) -> dict[str, Any]:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        metadata = os.fstat(stream.fileno())
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid() \
                or stat.S_IMODE(metadata.st_mode) & 0o077:
            raise RuntimeError("receipt must be an owned private regular file")
        raw = stream.read(64 * 1024**2 + 1)
    if len(raw) > 64 * 1024**2 or not re.fullmatch(r"[0-9a-f]{64}", expected_sha256 or "") \
            or hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise RuntimeError("receipt differs from its reviewed digest or size bound")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise RuntimeError("receipt is not a JSON object")
    return value


def observe_reader_package(arguments: argparse.Namespace) -> dict[str, str] | None:
    path = getattr(arguments, "reader_package_receipt", None)
    if path is None:
        return None
    url = urllib.parse.urlparse(arguments.api_url)
    if url.scheme != "http" or url.hostname not in {"127.0.0.1", "localhost", "::1"} \
            or url.port != 3001 or url.path not in {"", "/"}:
        raise RuntimeError("local reader package binding requires the local production API")
    receipt = read_private_receipt(path, arguments.expected_reader_package_receipt_sha256)
    commit = receipt.get("commit")
    binary = receipt.get("binaries", {}).get("mainrag-api", {}).get("sha256")
    if receipt.get("status") != "PASS" or not isinstance(commit, str) \
            or not re.fullmatch(r"[0-9a-f]{40}", commit) or not isinstance(binary, str) \
            or not re.fullmatch(r"[0-9a-f]{64}", binary):
        raise RuntimeError("reader installation receipt identity is invalid")
    service = subprocess.run(["systemctl", "show", "mainrag-api.service", "-p", "ExecMainPID", "--value"],
                             capture_output=True, text=True, check=True)
    pid = service.stdout.strip()
    if not pid.isdigit() or int(pid) <= 0:
        raise RuntimeError("reader API service is inactive")
    measured = subprocess.run(["sudo", "-n", "sha256sum", f"/proc/{pid}/exe"],
                              capture_output=True, text=True, check=True).stdout.split()
    if not measured or measured[0] != binary:
        raise RuntimeError("running reader binary differs from the installation receipt")
    return {"commit_sha": commit, "binary_sha256": binary,
            "installation_receipt_sha256": arguments.expected_reader_package_receipt_sha256}


def reuse_completed_restart(path: Path, expected_sha256: str, checkpoint: dict[str, Any],
                           state: dict[str, Any]) -> dict[str, Any]:
    """Reuse a completed replay, never an interrupted or merely planned attempt.

    This reuses only restart/replay evidence. Integrity, live source review,
    frozen gold, search, intelligence, resource and qualification still run.
    A later reader failure does not invalidate a completed replay and proof.
    """
    prior = read_private_receipt(path, expected_sha256)
    qualification = prior.get("qualification", {})
    manifest = qualification.get("manifest", {})
    result = prior.get("result", {})
    failures, _ = candidate_proof(manifest)
    normal = not failures and result.get("status") == "release_candidate" \
        and result.get("evidence_id") == qualification.get("evidence_id")
    verified = prior.get("verification", {})
    restart = manifest.get("restart", {}) if normal else prior.get("restart_resume", {})
    completed_before_reader_failure = prior.get("status") == "FAIL" \
        and prior.get("failed_gate") in {
            "source_snapshot_review", "gold_suite", "intelligence", "search_current",
            "search_storage_v2", "query_coverage", "search_gates", "dual_read",
            "resource_budget", "reader_continuity",
        } and all(verified.get("checks", {}).get(name) == "PASS"
                  for name in integrity_resume["INTEGRITY_CHECKS"]) \
        and restart.get("server_instance_changed") is True \
        and restart.get("generation_reused") is True
    lexical = verified.get("lexical_segment_verification", {})
    completed_before_reader_failure = completed_before_reader_failure \
        and lexical.get("generation_id") == checkpoint.get("generation_id") \
        and lexical.get("occurrence_count") == checkpoint.get("item_count") \
        and lexical.get("missing_count") == 0 and lexical.get("invalid_count") == 0
    if not normal and not completed_before_reader_failure:
        raise RuntimeError("restart evidence is not a completed normal qualification")
    previous = prior.get("checkpoint", {})
    for name in ("source_id", "generation_id", "generation_seq", "commit_sha",
                 "source_watermark_sha256", "item_count", "active_generation_id"):
        if name not in checkpoint or previous.get(name) != checkpoint[name]:
            raise RuntimeError("restart evidence candidate identity differs")
    for name in ("git_snapshot_commit_sha", "source_snapshot_review_sha256",
                 "source_snapshot_gold_review_sha256"):
        if previous.get(name) != checkpoint.get(name):
            raise RuntimeError("restart evidence frozen input identity differs")
    if previous.get("build", {}).get("fixture_sha256") != checkpoint.get("build", {}).get("fixture_sha256") \
            or not checkpoint.get("build", {}).get("fixture_sha256"):
        raise RuntimeError("restart evidence build fixture differs")
    if normal and manifest.get("server_verification_sha256") != sha256_text(json.dumps(verified, sort_keys=True)):
        raise RuntimeError("restart evidence verification digest differs")
    for name in ("source_id", "generation_id", "generation_seq", "source_watermark_sha256",
                 "item_count", "active_generation_id", "verification_manifest_sha256"):
        if name not in state or verified.get(name) != state[name]:
            raise RuntimeError("restart evidence live candidate identity differs")
    if state.get("status") not in {"verified", "release_candidate"} \
            or not re.fullmatch(r"[0-9a-f]{64}", state.get("verification_manifest_sha256") or ""):
        raise RuntimeError("restart evidence live generation is not verified")
    for name in ("adapter_profile_id", "analysis_profile_id", "search_profile_id"):
        if not verified.get(name) or (normal and qualification.get(name) != verified[name]):
            raise RuntimeError("restart evidence producer profile differs")
    for name in ("generation_id", "commit_sha", "source_watermark_sha256"):
        if not normal and name == "commit_sha":
            # Verification binds the sealed generation and root, while the
            # immutable checkpoint above binds the original producer commit.
            continue
        producer = qualification if normal else verified
        if producer.get(name) != checkpoint[name]:
            raise RuntimeError("restart evidence producer identity differs")
    identity = {name: verified.get(name) for name in (
        "adapter_profile_id", "analysis_profile_id", "search_profile_id",
        "generation_root_sha256", "verification_manifest_sha256")}
    if not re.fullmatch(r"[0-9a-f]{64}", identity["generation_root_sha256"] or ""):
        raise RuntimeError("restart evidence generation root is invalid")
    return {**restart, "reused_completed_evidence_sha256": expected_sha256,
            "prior_qualification_completed": normal,
            "verified_producer_identity": identity}


def replay_completed_build(arguments: argparse.Namespace, token: str,
                           checkpoint: dict[str, Any]) -> dict[str, Any]:
    repeated_body = {"commit_sha": arguments.commit_sha}
    if checkpoint.get("git_snapshot_commit_sha") is not None:
        repeated_body["git_snapshot_commit_sha"] = checkpoint["git_snapshot_commit_sha"]
        repeated_body["expected_source_watermark_sha256"] = checkpoint["source_watermark_sha256"]
    repeated = request(
        arguments.api_url,
        token,
        "POST",
        f"/api/v1/admin/sources/{arguments.source_id}/storage-v2-release-candidate-build",
        repeated_body,
    )
    if (
        not repeated["reused_generation"]
        or int(repeated["generation_id"]) != checkpoint["generation_id"]
        or int(repeated["generation_seq"]) != checkpoint["generation_seq"]
        or repeated["source_watermark_sha256"] != checkpoint["source_watermark_sha256"]
        or repeated["active_generation_before"] != checkpoint["active_generation_id"]
        or repeated["active_generation_after"] != checkpoint["active_generation_id"]
    ):
        raise RuntimeError("restart/resume did not reproduce the completed candidate identity")
    validate_telemetry(repeated.get("telemetry"), int(repeated["item_count"]))
    return repeated


def verify_candidate(arguments: argparse.Namespace, token: str, progress: dict[str, Any]) -> None:
    checkpoint = json.loads(arguments.checkpoint.read_text(encoding="utf-8"))
    progress["checkpoint"] = checkpoint
    if checkpoint["source_id"] != arguments.source_id or checkpoint["commit_sha"] != arguments.commit_sha:
        raise RuntimeError("checkpoint source or commit identity differs")
    progress["phase"] = "reader_package"
    reader_package = observe_reader_package(arguments)
    progress["phase"] = "restart_state"
    if checkpoint.get("reconstructed_from_persisted_witness") is True:
        review_sha256 = checkpoint["reconstruction_evidence"].get("source_snapshot_review_sha256")
        if review_sha256 is not None and (
                arguments.source_snapshot_review is None
                or arguments.source_snapshot_review_sha256 != review_sha256):
            raise RuntimeError("reconstructed checkpoint requires exact source review recheck")
        state = reconstructed_source_state(arguments.api_url, token, checkpoint)
    else:
        state = restarted_source_state(
            arguments.api_url, token, arguments.source_id,
            checkpoint["generation_seq"], checkpoint["server_instance_id"],
        )
    if reader_package is not None:
        reader_package["server_instance_id"] = state["server_instance_id"]
        progress["reader_package"] = reader_package
    progress["phase"] = "resource_before_resume"
    free_before_resume = shutil.disk_usage(arguments.pack_root).free
    progress["free_bytes_before_resume"] = free_before_resume
    if free_before_resume < arguments.minimum_free_bytes:
        raise RuntimeError("resource reserve is below the approved minimum before resume")
    progress["thin_pool_before_resume"] = thin_pool_capacity(
        arguments.pack_root, 0, require_estimate=False)
    require_same_pool(checkpoint.get("pack_capacity_before_build", {}).get("thin_pool"),
                      progress["thin_pool_before_resume"])
    progress["phase"] = "restart_resume"
    restart_evidence = getattr(arguments, "completed_restart_evidence", None)
    repeated = None
    if restart_evidence is not None:
        progress["restart_resume"] = reuse_completed_restart(
            restart_evidence, arguments.expected_completed_restart_evidence_sha256, checkpoint, state)
    else:
        repeated = replay_completed_build(arguments, token, checkpoint)
        progress["restart_resume"] = {"server_instance_changed": True, "generation_reused": True}
    progress["phase"] = "integrity"
    completed_integrity = getattr(arguments, "completed_integrity_evidence", None)
    if completed_integrity is None:
        verified = request(
            arguments.api_url, token, "POST",
            f"/api/v1/admin/sources/{arguments.source_id}/storage-v2-release-candidate-verify",
            {"generation_id": checkpoint["generation_id"]},
        )
    else:
        if reader_package is None:
            raise RuntimeError("completed integrity reuse requires a bound local reader")
        prior = read_private_receipt(completed_integrity,
                                     arguments.expected_completed_integrity_evidence_sha256)
        previous_install = read_private_receipt(arguments.integrity_verifier_receipt,
                                               arguments.expected_integrity_verifier_receipt_sha256)
        observed = integrity_resume["observe_immutable_identity"](
            arguments.source_id, checkpoint["generation_id"])
        current_install = read_private_receipt(arguments.reader_package_receipt,
                                              arguments.expected_reader_package_receipt_sha256)
        current_functions = {name: digest for name, digest in current_install.get(
            "function_identities", {}).items() if name.startswith("storage_v2_")}
        if not current_functions or current_functions != observed["function_identities"]:
            raise RuntimeError("completed integrity current reader catalog differs from installation")
        verified = integrity_resume["validate_reuse"](
            prior, checkpoint, state, previous_install,
            arguments.expected_integrity_verifier_receipt_sha256, reader_package, observed)
        progress["integrity_reuse"] = {
            "policy": "immutable-generation-unchanged-verifier-exact-query-cache-v1",
            "completed_evidence_sha256": arguments.expected_completed_integrity_evidence_sha256,
            "verifier_installation_sha256": arguments.expected_integrity_verifier_receipt_sha256,
            "live_identity_sha256": sha256_text(json.dumps(observed, sort_keys=True)),
            "current_reader_installation_sha256": reader_package["installation_receipt_sha256"],
            "current_reader_checks_repeated": True,
        }
    progress["verification"] = verified
    if restart_evidence is not None:
        identity = progress["restart_resume"]["verified_producer_identity"]
        if any(verified.get(name) != value for name, value in identity.items()):
            raise RuntimeError("reused restart producer profile or generation integrity differs")
    progress["query_seed_summary"] = query_seed_summary(verified["query_seeds"])
    if (
        int(verified["source_id"]) != arguments.source_id
        or int(verified["generation_id"]) != checkpoint["generation_id"]
        or int(verified["generation_seq"]) != checkpoint["generation_seq"]
        or verified["source_watermark_sha256"] != checkpoint["source_watermark_sha256"]
        or verified["active_generation_id"] != checkpoint["active_generation_id"]
        or verified["status"] not in {"verified", "release_candidate"}
    ):
        raise RuntimeError("server verification returned a different candidate identity")
    source_review = None
    if getattr(arguments, "source_snapshot_review", None) is not None:
        progress["phase"] = "source_snapshot_review"
        source_review = read_snapshot_review(
            arguments.source_snapshot_review,
            arguments.source_snapshot_review_sha256,
            checkpoint, verified["adapter_profile_id"])
        require_live_snapshot(arguments.api_url, token, arguments.source_id,
                              source_review, checkpoint.get("git_snapshot_commit_sha"))
        progress["source_snapshot_review"] = {
            "review_sha256": source_review["review_sha256"],
            "source_watermark_sha256": source_review["source_watermark_sha256"],
            "status_counts": source_review["status_counts"],
            **({"filesystem_scope_sha256": source_review["filesystem_scope"]["sha256"],
                "adapter_profile_id": source_review["adapter_profile_id"]}
               if source_review.get("filesystem_scope") else {}),
            **({"filesystem_cut": source_review["filesystem_cut"],
                "build_filesystem_cut": checkpoint["build"]["filesystem_cut"],
                "source_root_sha256": source_review["source_root_sha256"],
                "adapter_profile_id": source_review["adapter_profile_id"],
                "item_count": source_review["item_count"]}
               if source_review.get("filesystem_cut") else {}),
        }
    progress["phase"] = "gold_suite"
    gold_cases, gold_summary = load_gold_suite(arguments, checkpoint, verified, source_review)
    progress["gold_suite_summary"] = gold_summary
    intelligence = verify_intelligence(
        arguments.api_url, token, arguments.source_id, checkpoint["generation_seq"],
        verified["intelligence_export"], progress,
    )
    progress["intelligence"] = intelligence

    comparisons = progress["comparisons"]
    query_results = progress["query_results"]
    query_coverage = progress["query_coverage"]
    quality_passed = True
    performance_passed = True
    degradation_passed = True
    for ordinal, (kind, seed) in enumerate(
        [("automatic", seed) for seed in verified["query_seeds"]]
        + [("gold", case) for case in gold_cases], 1
    ):
        pending = {"ordinal": ordinal, "kind": kind, "id": seed["id"],
                   "query_sha256": sha256_text(seed["query"])}
        progress["pending_query"] = pending
        common = {"query": seed["query"], "source_id": arguments.source_id, "limit": 10}
        progress["phase"] = "search_current"
        current = request(arguments.api_url, token, "POST", "/api/v1/search/keyword", common)
        expectation_binding = None
        if kind == "automatic":
            seed, expectation_binding = bind_automatic_expectation(seed, current, source_review)
        pending["current"] = ranked(current["results"])
        pending["current_path_sha256"] = path_identity(current["results"])
        pending["current_ms"] = current.get("took_ms")
        progress["phase"] = "search_storage_v2"
        storage = request(arguments.api_url, token, "POST", "/api/v1/search/keyword", {
            **common,
            "read_path": "storage_v2",
            "generation": str(checkpoint["generation_seq"]),
            "include_test": True,
            "graph_profile": "candidate-unavailable-v1",
            "semantic_profile": "candidate-unavailable-v1",
            "rerank_profile": "candidate-unavailable-v1",
        })
        pending["storage_v2"] = ranked(storage["results"])
        pending["storage_v2_path_sha256"] = path_identity(storage["results"])
        pending["storage_v2_ms"] = storage.get("took_ms")
        progress["phase"] = "query_coverage"
        current_by_path: dict[str, list[str]] = {}
        for result in current["results"]:
            current_by_path.setdefault(sha256_text(result["file_path"]), []).append(
                f"legacy:{int(result['chunk_id'])}"
            )
        # The source-backed proof supports bounded simple conjunctions.
        # Boolean, phrase and exact queries keep ordered-path comparison.
        simple = bool(re.fullmatch(r"\w+(?: \w+){0,7}", seed["query"])) \
            and len(seed["query"].encode()) <= 128 \
            and all(word.lower() not in {"and", "or", "not"}
                    for word in seed["query"].split(" "))
        coverage = None
        if kind == "automatic" or simple:
            file_cache = progress.setdefault("complete_source_files", {})
            path_sha = seed["expected_path_sha256"]
            path_review = source_review["paths"].get(path_sha) if source_review else None
            complete_required = (isinstance(path_review, dict) and path_review.get("status") == "changed_bytes"
                and path_sha in path_identity(storage["results"]) and path_sha not in file_cache)
            coverage = request(
                arguments.api_url, token, "POST",
                f"/api/v1/admin/sources/{arguments.source_id}/storage-v2-candidate-query-evidence",
                {"generation_id": checkpoint["generation_id"], "commit_sha": arguments.commit_sha,
                 "query": seed["query"],
                 "candidate_occurrence_ids": [hit["chunk_id"] for hit in storage["results"]],
                 "current_chunk_ids": [hit["chunk_id"] for hit in current["results"]],
                 **({"complete_source_path_sha256": path_sha} if complete_required else {})},
            )
            if complete_required:
                complete = coverage.get("complete_source_file")
                if not complete_file_proof_valid(complete) or any(
                        complete.get(key) != checkpoint[key] for key in (
                            "source_id", "generation_id", "generation_seq", "commit_sha")) \
                        or complete["path_sha256"] != path_sha:
                    raise RuntimeError("complete source file proof identity differs")
                file_cache[path_sha] = complete
            elif path_sha in file_cache:
                coverage["complete_source_file"] = file_cache[path_sha]
            query_coverage.append(coverage)
        gates = search_query_gates(seed, current, storage, arguments.max_query_ms,
                                   coverage, checkpoint, source_review)
        if expectation_binding is not None:
            gates["automatic_expectation_binding"] = expectation_binding
        case_run_id = sha256_text(f"{kind}:{seed['id']}")
        gates["id"] = case_run_id
        quality_passed &= gates["quality_passed"]
        performance_passed &= gates["performance_passed"]
        degradation_passed &= gates["degradation_passed"]
        # Gold cases may reuse automatic seed IDs. Both dual-read fixtures and
        # persisted query results need unique IDs across the two sets.
        fixture = {"id": case_run_id,
                   "kind": kind, "query": seed["query"],
                   "phrase": False, "k": 10}
        if coverage is not None:
            fixture["coverage_evidence_sha256"] = sha256_text(json.dumps(coverage, sort_keys=True))
        comparisons.append({
            "fixture": fixture,
            "normalized_query": seed["query"],
            "current": ranked(current["results"]),
            "storage_v2": ranked(storage["results"], current_by_path),
        })
        query_results.append(gates)
        progress.pop("pending_query")
    progress["phase"] = "candidate_search"
    if source_review is not None:
        require_live_snapshot(arguments.api_url, token, arguments.source_id,
                              source_review, checkpoint.get("git_snapshot_commit_sha"))
    if not query_results or not (quality_passed and performance_passed and degradation_passed):
        atomic_private_json(arguments.output, {
            "status": "FAIL", "failed_gate": "candidate_search",
            "checkpoint": checkpoint, "verification": verified,
            "intelligence": intelligence,
            "query_results": query_results, "comparisons": comparisons,
            "query_seed_summary": progress["query_seed_summary"],
            "gold_suite_summary": gold_summary,
            "source_snapshot_review": progress.get("source_snapshot_review"),
            "query_coverage": query_coverage,
            "checks": {"quality": quality_passed and bool(query_results),
                       "performance": performance_passed and bool(query_results),
                       "degradation": degradation_passed and bool(query_results)},
            "qualification_submitted": False,
        })
        raise RuntimeError("candidate search quality, latency, or degradation gate failed")
    dual_request = {
        "generation": checkpoint["generation_seq"],
        "commit_sha": arguments.commit_sha,
        "fixture_sha256": checkpoint["build"]["fixture_sha256"],
        "query_set_sha256": query_set_sha256(comparisons),
        "queries": comparisons,
        "exact_top10_passed": quality_passed,
        "performance_envelope_passed": performance_passed,
        "restart_passed": True,
        "optional_degradation_passed": degradation_passed,
    }
    progress["phase"] = "dual_read"
    dual = request(
        arguments.api_url,
        token,
        "POST",
        f"/api/v1/admin/sources/{arguments.source_id}/storage-v2-dual-read",
        dual_request,
    )
    if dual.get("status") != "PASS" or dual.get("artifact", {}).get("unexplained_count") != 0:
        raise RuntimeError("server rejected the dual-read evidence")
    progress["dual_read"] = dual
    progress["phase"] = "resource_budget"
    free_bytes = shutil.disk_usage(arguments.pack_root).free
    if free_bytes < arguments.minimum_free_bytes:
        raise RuntimeError("resource reserve is below the approved minimum")
    progress["thin_pool_after_verification"] = thin_pool_capacity(
        arguments.pack_root, 0, require_estimate=False)
    require_same_pool(checkpoint.get("pack_capacity_before_build", {}).get("thin_pool"),
                      progress["thin_pool_after_verification"])
    progress["phase"] = "server_checks"
    checks = {name: "PASS" for name in CHECKS}
    for name in (
        "artifact_root", "authorization", "body_pack_integrity", "intelligence",
        "intervals", "legacy_intelligence_export",
    ):
        if verified["checks"].get(name) != "PASS":
            raise RuntimeError(f"server verification did not pass {name}")
    # This identifier may leave the protected environment. Keep it independent
    # of enumerable source/generation IDs and retain it in failure evidence
    # before the qualification POST so an unknown outcome can be reconciled.
    evidence_id = str(uuid.uuid4())
    qualification = {
        "evidence_id": evidence_id,
        "generation_id": checkpoint["generation_id"],
        "commit_sha": arguments.commit_sha,
        "source_watermark_sha256": checkpoint["source_watermark_sha256"],
        "adapter_profile_id": verified["adapter_profile_id"],
        "analysis_profile_id": verified["analysis_profile_id"],
        "search_profile_id": verified["search_profile_id"],
        "manifest": {
            "status": "PASS",
            "checks": checks,
            "server_verification_sha256": sha256_text(json.dumps(verified, sort_keys=True)),
            **({"integrity_reuse": progress["integrity_reuse"]} if "integrity_reuse" in progress else {}),
            "dual_read_evidence_id": dual["evidence_id"],
            "dual_read_artifact_sha256": dual["artifact_sha256"],
            "query_results": query_results,
            "query_seed_summary": progress["query_seed_summary"],
            "gold_suite_summary": gold_summary,
            "source_snapshot_review": progress.get("source_snapshot_review"),
            "query_coverage_sha256": sha256_text(json.dumps(query_coverage, sort_keys=True)),
            "intelligence": intelligence,
            "resource": {"free_bytes": free_bytes, "minimum_free_bytes": arguments.minimum_free_bytes},
            "restart": progress["restart_resume"],
        },
    }
    progress["phase"] = "manifest_contract"
    if reader_package is not None:
        current_reader = observe_reader_package(arguments)
        current_state = source_state(arguments.api_url, token, arguments.source_id,
                                     checkpoint["generation_seq"])
        if current_reader != {key: value for key, value in reader_package.items()
                              if key != "server_instance_id"} \
                or current_state.get("server_instance_id") != reader_package["server_instance_id"]:
            raise RuntimeError("reader package or API instance changed during qualification")
        qualification["manifest"]["reader_package"] = reader_package
    progress["qualification"] = qualification
    failures, _ = candidate_proof(qualification["manifest"])
    if failures:
        raise RuntimeError("qualification manifest fails aggregate contract: " + ",".join(failures))
    progress["phase"] = "qualification"
    progress["qualification_attempted"] = True
    # A lost response does not prove the server rejected or never received a POST.
    progress["qualification_outcome"] = "UNKNOWN"
    result = request(
        arguments.api_url,
        token,
        "POST",
        f"/api/v1/admin/sources/{arguments.source_id}/storage-v2-release-candidate-qualify",
        qualification,
    )
    progress["qualification_outcome"] = "RESPONSE_RECEIVED"
    progress["result"] = result
    progress["phase"] = "evidence_write"
    artifact = {"checkpoint": checkpoint, "verification": verified, "dual_read": dual,
                "query_coverage": query_coverage,
                "qualification": qualification, "result": result}
    atomic_private_json(arguments.output, artifact)
    if repeated is not None:
        publish_telemetry(repeated["telemetry"])
    print(json.dumps({
        "status": result["status"], "source_ref": checkpoint["source_ref"],
        "generation_seq": result["generation_seq"], "evidence_id": result["evidence_id"],
        "active_generation_id": result["active_generation_id"],
    }, sort_keys=True))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("build", "verify"))
    parser.add_argument("--api-url", default="http://127.0.0.1:3001")
    parser.add_argument("--token-env", default="MAINRAG_TOKEN")
    parser.add_argument("--token-file", type=Path)
    parser.add_argument("--source-id", type=int, required=True)
    parser.add_argument("--commit-sha", required=True)
    parser.add_argument("--checkpoint", type=Path, required=True)
    parser.add_argument("--completed-restart-evidence", type=Path,
                        help="Reuse only a completed, same-generation normal restart/replay proof")
    parser.add_argument("--expected-completed-restart-evidence-sha256")
    parser.add_argument("--completed-integrity-evidence", type=Path,
                        help="Reuse completed immutable proof with an unchanged local verifier")
    parser.add_argument("--expected-completed-integrity-evidence-sha256")
    parser.add_argument("--integrity-verifier-receipt", type=Path)
    parser.add_argument("--expected-integrity-verifier-receipt-sha256")
    parser.add_argument("--reader-package-receipt", type=Path,
                        help="Bind current reader gates to a verified local installation receipt")
    parser.add_argument("--expected-reader-package-receipt-sha256")
    parser.add_argument("--resume-run-id", type=int, help="Require the exact persisted build run; use a new checkpoint path")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--pack-root", type=Path, default=Path("/data/mainrag/storage-v2-66/packs"))
    parser.add_argument("--minimum-free-bytes", type=int, default=40 * 1024**3)
    parser.add_argument("--maximum-build-bytes", type=int)
    parser.add_argument("--maximum-pool-growth-bytes", type=int)
    parser.add_argument("--max-query-ms", type=int, default=2000)
    parser.add_argument("--gold-suite", type=Path)
    parser.add_argument("--expected-gold-suite-sha256")
    parser.add_argument("--source-snapshot-review", type=Path)
    parser.add_argument("--source-snapshot-review-sha256")
    parser.add_argument("--source-snapshot-gold-review", type=Path)
    parser.add_argument("--git-snapshot-commit-sha")
    parser.add_argument("--expected-source-watermark-sha256")
    arguments = parser.parse_args()
    reuse_fields = (arguments.completed_integrity_evidence,
                    arguments.expected_completed_integrity_evidence_sha256,
                    arguments.integrity_verifier_receipt,
                    arguments.expected_integrity_verifier_receipt_sha256)
    if any(value is not None for value in reuse_fields) and (
            any(value is None for value in reuse_fields) or arguments.phase != "verify"
            or arguments.reader_package_receipt is None):
        parser.error("completed integrity reuse requires all four identities and a local verify reader")
    if ((arguments.reader_package_receipt is None)
            != (arguments.expected_reader_package_receipt_sha256 is None)):
        parser.error("reader package receipt requires its reviewed SHA-256")
    if arguments.reader_package_receipt is not None and arguments.phase != "verify":
        parser.error("reader package receipt applies only to verify")
    if len(arguments.commit_sha) != 40 or any(c not in "0123456789abcdef" for c in arguments.commit_sha):
        parser.error("--commit-sha must be a full lowercase Git SHA")
    if ((arguments.completed_restart_evidence is None)
            != (arguments.expected_completed_restart_evidence_sha256 is None)):
        parser.error("completed restart evidence requires its reviewed SHA-256")
    if arguments.completed_restart_evidence is not None and arguments.phase != "verify":
        parser.error("completed restart evidence applies only to verify")
    if arguments.phase == "verify" and arguments.output is None:
        parser.error("verify requires --output")
    if arguments.phase == "verify" and (arguments.gold_suite is None or
                                        arguments.expected_gold_suite_sha256 is None):
        parser.error("verify requires --gold-suite and --expected-gold-suite-sha256")
    if ((arguments.source_snapshot_review is None)
            != (arguments.source_snapshot_review_sha256 is None)):
        parser.error("source snapshot review requires its exact protected SHA-256")
    if ((arguments.source_snapshot_review is None)
            != (arguments.source_snapshot_gold_review is None)):
        parser.error("source snapshot review requires its frozen gold review")
    if ((arguments.git_snapshot_commit_sha is None)
            != (arguments.expected_source_watermark_sha256 is None)):
        parser.error("pinned Git build requires commit and exact watermark together")
    if arguments.git_snapshot_commit_sha is not None and (
            arguments.phase != "build"
            or not re.fullmatch(r"[0-9a-f]{40}", arguments.git_snapshot_commit_sha)
            or not re.fullmatch(r"[0-9a-f]{64}", arguments.expected_source_watermark_sha256)
            or arguments.source_snapshot_review is None):
        parser.error("pinned Git build requires an exact frozen source and gold review")
    if arguments.phase == "build" and arguments.git_snapshot_commit_sha is None \
            and arguments.source_snapshot_review is not None:
        parser.error("build source snapshot review requires an exact pinned Git commit")
    if arguments.phase == "build" and (arguments.maximum_build_bytes is None or
                                       arguments.maximum_build_bytes <= 0):
        parser.error("build requires a positive --maximum-build-bytes estimate")
    if arguments.minimum_free_bytes < 0:
        parser.error("--minimum-free-bytes must be non-negative")
    try:
        token = load_token(arguments.token_file, arguments.token_env)
    except RuntimeError as error:
        parser.error(str(error))
    (build if arguments.phase == "build" else verify)(arguments, token)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

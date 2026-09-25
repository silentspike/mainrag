#!/usr/bin/env python3
"""Couple a committed candidate-set activation to the API default read path."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import stat
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path


OPERATOR_PATH = Path(__file__).with_name("activation-set.py")
SPEC = importlib.util.spec_from_file_location("storage_v2_activation_operator", OPERATOR_PATH)
assert SPEC and SPEC.loader
OPERATOR = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(OPERATOR)

UNIT = "mainrag-api.service"
DROPIN = Path("/etc/systemd/system/mainrag-api.service.d/90-storage-v2-default-read.conf")
ENV_FILE = Path("/etc/mainrag/storage-v2-default-read.env")
ENV_NAME = "MAINRAG_STORAGE_V2_DEFAULT_READ_MANIFEST_SHA256"
ACTIVE_COMMIT_ENV_NAME = "MAINRAG_STORAGE_V2_ACTIVE_INGEST_COMMIT_SHA"
API_BINARY = Path("/opt/mainrag/api/mainrag-api")
DEFAULT_SWITCH_CONTRACT = {
    "unit": UNIT,
    "api_binary": str(API_BINARY),
    "dropin": str(DROPIN),
    "environment_file": str(ENV_FILE),
    "coupling_max_seconds": 300,
    "active_ingest_commit_env_name": ACTIVE_COMMIT_ENV_NAME,
}


def systemctl(*arguments: str) -> str:
    result = subprocess.run(["systemctl", *arguments], capture_output=True,
                            text=True, check=False)
    if result.returncode:
        raise RuntimeError("API service configuration or restart failed")
    return result.stdout.strip()


def read_service_state() -> tuple[int, str, str]:
    if systemctl("is-active", UNIT) != "active":
        raise RuntimeError("API service is not active")
    pid = systemctl("show", UNIT, "--property=MainPID", "--value")
    if not pid.isdecimal() or int(pid) <= 1:
        raise RuntimeError("API service PID is invalid")
    values = Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
    selected = [value.partition(b"=")[2].decode("ascii") for value in values
                if value.startswith((ENV_NAME + "=").encode())]
    commits = [value.partition(b"=")[2].decode("ascii") for value in values
               if value.startswith((ACTIVE_COMMIT_ENV_NAME + "=").encode())]
    if len(selected) > 1 or len(commits) > 1:
        raise RuntimeError("API default selector or active ingest commit is ambiguous")
    return int(pid), selected[0] if selected else "", commits[0] if commits else ""


def api_read_path(url: str, token_file: Path | None) -> str:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "http" or parsed.hostname not in ("127.0.0.1", "::1") \
            or parsed.username or parsed.password or parsed.path not in ("", "/") \
            or parsed.query or parsed.fragment:
        raise RuntimeError("API readback must use a local HTTP origin")
    headers = {}
    if token_file is not None:
        metadata = token_file.lstat()
        if not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) & 0o077:
            raise RuntimeError("API token file must be private")
        token = token_file.read_text().strip()
        if not token or "\n" in token or "\r" in token:
            raise RuntimeError("protected API token is invalid")
        headers["Authorization"] = "Bearer " + token
    request = urllib.request.Request(
        url.rstrip("/") + "/api/v1/intelligence/default-read-path", headers=headers)
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, request, fp, code, msg, headers, newurl):
            raise RuntimeError("API readback redirected")
    try:
        with urllib.request.build_opener(NoRedirect).open(request, timeout=10) as response:
            value = json.load(response)
    except (OSError, ValueError) as error:
        raise RuntimeError("API default read-path readback failed") from error
    if not isinstance(value, dict) or value.get("read_path") not in (
        "current", "storage_v2_active",
    ):
        raise RuntimeError("API default read-path response is invalid")
    return value["read_path"]


def switch_file_state(expected: str, commit: str) -> str:
    env_content = (ENV_NAME + "=" + expected + "\n"
                   + ACTIVE_COMMIT_ENV_NAME + "=" + commit + "\n").encode()
    dropin_content = ("[Service]\nEnvironmentFile=" + str(ENV_FILE) + "\n").encode()
    exists = (ENV_FILE.exists(), DROPIN.exists())
    if ENV_FILE.is_symlink() or DROPIN.is_symlink():
        raise RuntimeError("storage-v2 default selector has an unexpected path")
    if exists == (False, False):
        return "NEW"
    if exists == (True, True):
        for path in (ENV_FILE, DROPIN):
            metadata = path.stat()
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid() \
                    or stat.S_IMODE(metadata.st_mode) & 0o022:
                raise RuntimeError("existing selector file ownership or mode differs")
        if ENV_FILE.read_bytes() == env_content and DROPIN.read_bytes() == dropin_content:
            return "EXACT_EXISTING"
    raise RuntimeError("storage-v2 default selector differs; reconcile first")


def atomic_create(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def service_binary_sha256(pid: int) -> str:
    executable = Path(f"/proc/{pid}/exe")
    if executable.resolve() != API_BINARY.resolve():
        raise RuntimeError("API service executable differs from the approved path")
    return hashlib.sha256(executable.read_bytes()).hexdigest()


def verify_restarted_api(url: str, token_file: Path | None,
                         expected: str, commit: str) -> int:
    deadline = time.monotonic() + 20
    while True:
        try:
            pid, selected, active_commit = read_service_state()
            if selected == expected and active_commit == commit \
                    and api_read_path(url, token_file) == "storage_v2_active":
                return pid
        except (RuntimeError, OSError):
            pass
        if time.monotonic() >= deadline:
            raise RuntimeError("API default read-path readback differs")
        time.sleep(0.5)


def switch(args: argparse.Namespace) -> dict:
    now = int(time.time())
    plan = OPERATOR.read_private(args.plan, args.plan_sha256)
    approval = OPERATOR.read_private(args.approval, args.approval_sha256, 64 * 1024)
    OPERATOR.validate_approval(approval, plan, args.plan_sha256, now)
    attempt = OPERATOR.read_private(args.attempt, args.attempt_sha256, 64 * 1024)
    manifest = plan.get("manifest")
    if plan.get("schema_version") != "mainrag.storage-v2.activation-plan.v1" \
            or not isinstance(manifest, dict) \
            or plan.get("default_switch") != DEFAULT_SWITCH_CONTRACT \
            or attempt.get("schema_version") != "mainrag.storage-v2.activation-attempt.v1" \
            or attempt.get("status") != "DB_COMMITTED_DEFAULT_SWITCH_PENDING" \
            or attempt.get("plan_sha256") != args.plan_sha256 \
            or attempt.get("manifest_sha256") != plan.get("manifest_sha256") \
            or attempt.get("activation_id") != manifest.get("activation_id") \
            or type(attempt.get("committed_at_unix")) is not int \
            or not 0 <= now - attempt["committed_at_unix"] <= 300:
        raise RuntimeError("immediately coupled committed activation evidence is missing")
    OPERATOR.verify_committed(
        plan, OPERATOR.committed_readback(args.database, args.local_postgres,
                                          manifest["activation_id"]))
    if hashlib.sha256(API_BINARY.read_bytes()).hexdigest() != plan.get(
        "installed_binary_sha256"):
        raise RuntimeError("installed API binary differs from activation plan")
    intended = plan["manifest_sha256"]
    runtime_commit = manifest.get("code_commit_sha")
    if not isinstance(runtime_commit, str) or re.fullmatch(r"[0-9a-f]{40}", runtime_commit) is None:
        raise RuntimeError("active ingest runtime commit is invalid")
    file_state = switch_file_state(intended, runtime_commit)
    pid, current, current_commit = read_service_state()
    if service_binary_sha256(pid) != plan["installed_binary_sha256"]:
        raise RuntimeError("running API binary differs from activation plan")
    current_api = api_read_path(args.api_url, args.api_token_file)
    if file_state == "NEW" and (current or current_commit or current_api != "current"):
        raise RuntimeError("API default selector was already changed")
    if file_state == "EXACT_EXISTING" and not (
        (current == intended and current_commit == runtime_commit
         and current_api == "storage_v2_active")
        or (not current and not current_commit and current_api == "current")
    ):
        raise RuntimeError("existing API default selector is inconsistent")
    result = {"schema_version": "mainrag.storage-v2.default-switch.v1",
              "status": "SWITCH_PENDING", "plan_sha256": args.plan_sha256,
              "manifest_sha256": intended, "activation_id": manifest["activation_id"],
              "started_at_unix": now}
    OPERATOR.private_write(args.output, result)
    try:
        if file_state == "NEW":
            atomic_create(ENV_FILE, (ENV_NAME + "=" + intended + "\n"
                                     + ACTIVE_COMMIT_ENV_NAME + "="
                                     + runtime_commit + "\n").encode())
            atomic_create(DROPIN, ("[Service]\nEnvironmentFile=" + str(ENV_FILE)
                                   + "\n").encode())
        if current_api == "current":
            systemctl("daemon-reload")
            systemctl("restart", UNIT)
            pid = verify_restarted_api(args.api_url, args.api_token_file,
                                       intended, runtime_commit)
        if service_binary_sha256(pid) != plan["installed_binary_sha256"]:
            raise RuntimeError("restarted API binary differs from activation plan")
        OPERATOR.verify_committed(
            plan, OPERATOR.committed_readback(args.database, args.local_postgres,
                                              manifest["activation_id"]))
    except (RuntimeError, OSError) as error:
        OPERATOR.private_write(args.output, {**result, "status": "SWITCH_OUTCOME_UNKNOWN"},
                               replace=True)
        raise RuntimeError("default switch outcome is unverified; reconcile service and receipt") from error
    result.update(status="DEFAULT_SWITCHED_POST_INGEST_PENDING",
                  observed_at_unix=int(time.time()))
    OPERATOR.private_write(args.output, result, replace=True)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True)
    parser.add_argument("--local-postgres", action="store_true")
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--plan-sha256", required=True)
    parser.add_argument("--approval", type=Path, required=True)
    parser.add_argument("--approval-sha256", required=True)
    parser.add_argument("--attempt", type=Path, required=True)
    parser.add_argument("--attempt-sha256", required=True)
    parser.add_argument("--api-url", default="http://127.0.0.1:3001")
    parser.add_argument("--api-token-file", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error("default switch requires root service authority")
    if not OPERATOR.re.fullmatch(r"[A-Za-z0-9_-]+", args.database):
        parser.error("database must be a local database name")
    if args.output.exists() or args.output.is_symlink():
        parser.error("protected switch output already exists")
    try:
        value = switch(args)
    except (RuntimeError, OSError, ValueError) as error:
        parser.error(str(error))
    print(json.dumps({"status": value["status"],
                      "manifest_sha256": value["manifest_sha256"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())

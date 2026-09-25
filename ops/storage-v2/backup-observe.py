#!/usr/bin/env python3
"""Capture protected, read-only pgBackRest metadata for the backup preflight.

The result proves a completed backup is listed without a reported error. It
does not prove that the backup can be restored.
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
import time
from pathlib import Path


MAX_INFO_BYTES = 8 * 1024 * 1024


def unique_keys(pairs: list[tuple[str, object]]) -> dict:
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON key")
        value[key] = item
    return value


def latest_backup(raw: bytes, stanza: str, now: int) -> dict:
    if not raw or len(raw) > MAX_INFO_BYTES:
        raise RuntimeError("pgBackRest inventory exceeds its bound")
    try:
        inventory = json.loads(raw, object_pairs_hook=unique_keys)
    except (ValueError, UnicodeError) as error:
        raise RuntimeError("pgBackRest inventory is invalid JSON") from error
    if not isinstance(inventory, list) or len(inventory) != 1 \
            or not isinstance(inventory[0], dict):
        raise RuntimeError("pgBackRest inventory has no unique stanza")
    observed = inventory[0]
    if observed.get("name") != stanza or not isinstance(observed.get("status"), dict) \
            or type(observed["status"].get("code")) is not int \
            or observed["status"]["code"] != 0 \
            or not isinstance(observed.get("backup"), list) \
            or not observed["backup"]:
        raise RuntimeError("pgBackRest stanza or backup status is unavailable")
    repositories = observed.get("repo")
    locks = observed["status"].get("lock")
    if not isinstance(repositories, list) or not repositories \
            or any(not isinstance(repo, dict) or not isinstance(repo.get("status"), dict)
                   or type(repo["status"].get("code")) is not int
                   or repo["status"]["code"] != 0 for repo in repositories) \
            or not isinstance(locks, dict) \
            or any(not isinstance(locks.get(kind), dict)
                   or locks[kind].get("held") is not False
                   for kind in ("backup", "restore")):
        raise RuntimeError("pgBackRest repository or lock state is not ready")
    backups = observed["backup"]
    if any(not isinstance(item, dict) or not isinstance(item.get("timestamp"), dict)
           or type(item["timestamp"].get("stop")) is not int
           or item["timestamp"]["stop"] <= 0 for item in backups):
        raise RuntimeError("pgBackRest backup timestamps are invalid")
    latest = max(backups, key=lambda item: item["timestamp"]["stop"])
    completed = latest["timestamp"]["stop"]
    if completed > now + 300 or latest.get("error") is not False \
            or latest.get("type") not in ("full", "diff", "incr") \
            or not isinstance(latest.get("label"), str) or not latest["label"]:
        raise RuntimeError("pgBackRest latest backup is not a completed error-free backup")
    return {"completed_at_unix": completed, "backup_type": latest["type"],
            "backup_label_sha256": hashlib.sha256(latest["label"].encode()).hexdigest()}


def private_create(path: Path, raw: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if stat.S_IMODE(path.parent.stat().st_mode) & 0o077:
        raise RuntimeError("backup evidence directory is not private")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as output:
        output.write(raw)
        output.flush()
        os.fsync(output.fileno())


def capture(stanza: str, info_path: Path, evidence_path: Path) -> dict:
    if re.fullmatch(r"[A-Za-z0-9_-]+", stanza) is None:
        raise RuntimeError("pgBackRest stanza name is invalid")
    if info_path.parent != evidence_path.parent or info_path == evidence_path \
            or any(path.exists() or path.is_symlink() for path in (info_path, evidence_path)):
        raise RuntimeError("protected backup outputs must be new sibling files")
    command = (["sudo", "-n", "-u", "postgres"] if os.geteuid() != 0 else []) + [
        "pgbackrest", "--stanza=" + stanza, "--output=json", "info"
    ]
    try:
        response = subprocess.run(command, capture_output=True, check=False)
    except OSError as error:
        raise RuntimeError("pgBackRest read-only inventory is unavailable") from error
    if response.returncode:
        raise RuntimeError("pgBackRest read-only inventory failed")
    now = int(time.time())
    observed = latest_backup(response.stdout, stanza, now)
    evidence = {"schema_version": 2, "status": "PASS",
                "stanza": stanza,
                "completed_at_unix": observed["completed_at_unix"],
                "artifact_file": info_path.name,
                "artifact_sha256": hashlib.sha256(response.stdout).hexdigest(),
                "backup_type": observed["backup_type"],
                "backup_label_sha256": observed["backup_label_sha256"],
                "restore_tested": False, "observed_at_unix": now}
    private_create(info_path, response.stdout)
    private_create(evidence_path,
                   (json.dumps(evidence, sort_keys=True, separators=(",", ":")) + "\n").encode())
    return evidence


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stanza", required=True)
    parser.add_argument("--info-output", required=True, type=Path)
    parser.add_argument("--evidence-output", required=True, type=Path)
    args = parser.parse_args()
    try:
        evidence = capture(args.stanza, args.info_output, args.evidence_output)
    except (RuntimeError, OSError) as error:
        parser.error(str(error))
    print(json.dumps({"status": "PASS", "evidence_level": "backup-metadata-only",
                      "backup_type": evidence["backup_type"],
                      "restore_tested": False}, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())

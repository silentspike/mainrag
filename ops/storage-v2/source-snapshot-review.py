#!/usr/bin/env python3
"""Freeze independent legacy-file drift evidence against a live adapter watermark.

The protected output contains hashes and states, never source paths or content.
It must be captured before reviewing a gold suite or qualifying a candidate.
"""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import re
import runpy
import stat
import subprocess
import tempfile
import time
import urllib.request


load_token = runpy.run_path(str(Path(__file__).with_name("operator_token.py")))["load_token"]
registered_scope_matcher = runpy.run_path(str(Path(__file__).with_name("fs_scope.py")))["registered_scope_matcher"]
cut_read_root = runpy.run_path(str(Path(__file__).with_name("fs_cut.py")))["read_root"]
SHA256 = re.compile(r"[0-9a-f]{64}\Z")


def canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode()


def observation(api_url: str, token: str, source_id: int) -> dict:
    call = urllib.request.Request(
        api_url.rstrip("/")
        + f"/api/v1/admin/sources/{source_id}/storage-v2-release-watermark",
        headers={"Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(call, timeout=720) as response:
        value = json.load(response)
    if (value.get("source_id") != source_id
            or not isinstance(value.get("source_watermark_sha256"), str)
            or not SHA256.fullmatch(value["source_watermark_sha256"])
            or not isinstance(value.get("adapter_profile_id"), str)
            or type(value.get("item_count")) is not int):
        raise RuntimeError("release watermark observation is incomplete")
    return value


def legacy_registration(database: str, source_id: int) -> dict:
    statement = f"""
SELECT json_build_object(
    'source_id', source.id, 'source_type', source.type,
    'source_path', source.path, 'config', source.config,
    'files', COALESCE((
        SELECT json_agg(json_build_object(
            'path', file.path, 'hash', encode(file.hash, 'hex')) ORDER BY file.path)
          FROM files file WHERE file.source_id = source.id
    ), '[]'::json)
)
FROM sources source WHERE source.id = {source_id};
"""
    environment = os.environ.copy()
    environment["PGAPPNAME"] = "mainrag-storage-v2-source-snapshot-review"
    environment["PGOPTIONS"] = (
        environment.get("PGOPTIONS", "") +
        " -c default_transaction_read_only=on -c row_security=off"
    ).strip()
    command = ["sudo", "-n", "-u", "postgres", "psql", "-X", "--no-psqlrc",
               "-qAt", "--set=ON_ERROR_STOP=1", "--dbname", database,
               "--command", statement]
    result = subprocess.run(command, capture_output=True, text=True,
                            env=environment, check=False)
    if result.returncode != 0:
        raise RuntimeError("read-only legacy registration query failed")
    try:
        value = json.loads(result.stdout.strip())
    except (ValueError, TypeError) as error:
        raise RuntimeError("legacy registration query returned invalid JSON") from error
    if value.get("source_id") != source_id or value.get("source_type") not in {"fs", "git"} \
            or not isinstance(value.get("source_path"), str) \
            or not isinstance(value.get("files"), list):
        raise RuntimeError("registered source identity differs")
    return value


def git_checkout(registration: dict, cache_dir: Path) -> tuple[Path, str]:
    """Observe the exact clean checkout used by the Git adapter without fetching."""
    source = registration["source_path"]
    name = source.split("/")[-1].removesuffix(".git")
    if not name or name in {".", ".."} or "\\" in name:
        raise RuntimeError("git cache name is invalid")
    root = (cache_dir / name).resolve(strict=True)
    if not root.is_relative_to(cache_dir.resolve(strict=True)):
        raise RuntimeError("git cache root escapes its registered cache")
    cache_owner = cache_dir.stat().st_uid
    if root.stat().st_uid != cache_owner or root.stat().st_mode & 0o022:
        raise RuntimeError("git checkout ownership differs from its trusted cache")

    def git(*args: str) -> str:
        command = ["git", "--no-optional-locks", "-c", "core.fsmonitor=false", "-C", str(root), *args]
        if cache_owner != os.geteuid():
            # Read the service-owned checkout as its existing owner. Keep
            # Git's ownership guard; do not add a global safe-directory rule.
            command = ["sudo", "-n", "-u", f"#{cache_owner}", "--", *command]
        result = subprocess.run(command, capture_output=True,
                                text=True, check=False, timeout=30)
        if result.returncode:
            raise RuntimeError("git checkout identity cannot be verified")
        return result.stdout.rstrip("\n")

    if (git("rev-parse", "--show-toplevel") != str(root)
            or git("remote", "get-url", "origin") != source
            or git("symbolic-ref", "--short", "HEAD") not in {"main", "master"}
            or git("status", "--porcelain", "--untracked-files=all")):
        raise RuntimeError("git checkout is dirty or differs from registered source")
    head = git("rev-parse", "--verify", "HEAD^{commit}")
    if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", head):
        raise RuntimeError("git checkout commit identity is invalid")
    return root, head


def file_status(root: Path, relative: str, legacy_hash: str, includes=lambda relative: True) -> tuple[str, str | None]:
    if (not relative or relative.startswith("/") or "\\" in relative
            or any(part in {"", ".", ".."} for part in relative.split("/"))):
        raise RuntimeError("legacy file path is outside the registered source")
    if not includes(relative):
        return "outside_configured_scope", None
    path = root.joinpath(*relative.split("/"))
    if not path.parent.resolve(strict=False).is_relative_to(root):
        raise RuntimeError("legacy file parent escapes the registered source")
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        return "source_file_missing", None
    except OSError as error:
        raise RuntimeError("legacy file cannot be read safely") from error
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise RuntimeError("legacy file is no longer a regular file")
        digest = hashlib.sha256()
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    identity = lambda value: (value.st_dev, value.st_ino, value.st_size,
                              value.st_mtime_ns, value.st_ctime_ns)
    if identity(before) != identity(after):
        raise RuntimeError("source file changed during drift review")
    observed = digest.hexdigest()
    return ("same_bytes" if observed == legacy_hash else "changed_bytes"), observed


def capture(database: str, api_url: str, token: str, source_id: int,
            git_cache_dir: Path = Path("/data/mainrag/git-cache")) -> dict:
    before = observation(api_url, token, source_id)
    registration = legacy_registration(database, source_id)
    git_head = None
    includes = lambda relative: True
    if registration["source_type"] == "fs":
        includes = registered_scope_matcher(registration["config"], before)
    if registration["source_type"] == "git":
        root, git_head = git_checkout(registration, git_cache_dir)
    else:
        root = Path(registration["source_path"]).resolve(strict=True)
        if before.get("filesystem_cut") is not None:
            root = cut_read_root(Path(registration["source_path"]), before)
    if not root.is_dir():
        raise RuntimeError("registered source root is not a directory")
    paths = {}
    for file in registration["files"]:
        relative, legacy_hash = file.get("path"), file.get("hash")
        if not isinstance(relative, str) or not isinstance(legacy_hash, str) \
                or not SHA256.fullmatch(legacy_hash):
            raise RuntimeError("legacy file identity is invalid")
        path_hash = hashlib.sha256(relative.encode()).hexdigest()
        if path_hash in paths:
            raise RuntimeError("legacy file path identity is duplicated")
        status, observed_hash = file_status(root, relative, legacy_hash, includes)
        paths[path_hash] = {"status": status, "legacy_sha256": legacy_hash,
                            "observed_sha256": observed_hash}
    after = observation(api_url, token, source_id)
    if before != after:
        raise RuntimeError("source adapter watermark changed during drift review")
    if before.get("filesystem_cut") is not None and cut_read_root(
            Path(registration["source_path"]), after) != root:
        raise RuntimeError("source cut changed during drift review")
    if git_head is not None and git_checkout(registration, git_cache_dir) != (root, git_head):
        raise RuntimeError("git checkout changed during drift review")
    statuses = Counter(item["status"] for item in paths.values())
    return {
        "schema_version": "mainrag.storage-v2.source-snapshot-review.v1",
        "source_id": source_id,
        "source_type": registration["source_type"],
        **({"filesystem_scope": before["filesystem_scope"]} if before.get("filesystem_scope") is not None else {}),
        **({"filesystem_cut": before["filesystem_cut"]} if before.get("filesystem_cut") is not None else {}),
        **({"git_head": git_head} if git_head is not None else {}),
        "source_root_sha256": hashlib.sha256(registration["source_path"].encode()).hexdigest(),
        "source_config_sha256": hashlib.sha256(canonical(registration["config"])).hexdigest(),
        "source_watermark_sha256": before["source_watermark_sha256"],
        "adapter_profile_id": before["adapter_profile_id"],
        "item_count": before["item_count"],
        "captured_at_unix": int(time.time()),
        "status_counts": dict(statuses),
        "paths": paths,
    }


def private_create(path: Path, value: dict) -> None:
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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True)
    parser.add_argument("--api-url", default="http://127.0.0.1:3001")
    parser.add_argument("--token-env", default="MAINRAG_TOKEN")
    parser.add_argument("--token-file", type=Path)
    parser.add_argument("--source-id", type=int, required=True)
    parser.add_argument("--protected-output", type=Path, required=True)
    arguments = parser.parse_args()
    if arguments.source_id <= 0:
        parser.error("source identity must be positive")
    review = capture(arguments.database, arguments.api_url,
                     load_token(arguments.token_file, arguments.token_env),
                     arguments.source_id)
    private_create(arguments.protected_output, review)
    print(json.dumps({"status": "OBSERVED_ONLY", "path_count": len(review["paths"]),
                      "status_counts": review["status_counts"],
                      "review_sha256": hashlib.sha256(
                          arguments.protected_output.read_bytes()).hexdigest()}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

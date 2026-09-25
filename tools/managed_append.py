#!/usr/bin/env python3
"""Create an explicitly managed, immutable-segment JSONL append source.

Only this writer may change a managed source. Readers can trust unchanged
segment identities between scheduled full comparisons under that contract.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import stat
import uuid
from pathlib import Path

FORMAT = "mainrag.managed-append.v1"
MAX_SEGMENT_BYTES = 1024 * 1024
SEGMENT_NAME = re.compile(r"^[0-9]{8}-[0-9a-f]{32}\.jsonl$")


def canonical(value: dict[str, object]) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def initial_chain(epoch: str) -> str:
    return hashlib.sha256((FORMAT + ":" + epoch).encode("ascii")).hexdigest()


def next_chain(previous: str, segment: dict[str, object]) -> str:
    return hashlib.sha256(bytes.fromhex(previous) + canonical(segment)).hexdigest()


def check_root(root: Path) -> None:
    if not stat.S_ISDIR(root.lstat().st_mode) or root.is_symlink():
        raise ValueError("managed append root must be a real directory")
    if not stat.S_ISDIR((root / "segments").lstat().st_mode):
        raise ValueError("managed append segment directory is invalid")
    if (root / "segments").is_symlink():
        raise ValueError("managed append segment directory is a symlink")


def load_manifest(root: Path) -> dict[str, object]:
    check_root(root)
    path = root / "manifest.json"
    if not stat.S_ISREG(path.lstat().st_mode) or path.is_symlink():
        raise ValueError("managed append manifest is not a regular file")
    with path.open("rb") as source:
        raw = source.read(16 * 1024 * 1024 + 1)
    if len(raw) > 16 * 1024 * 1024:
        raise ValueError("managed append manifest exceeds its size limit")
    manifest = json.loads(raw)
    if not isinstance(manifest, dict) or set(manifest) != {"format", "epoch", "chain", "segments"}:
        raise ValueError("managed append manifest has unexpected fields")
    if manifest["format"] != FORMAT or not isinstance(manifest["epoch"], str):
        raise ValueError("managed append format or epoch is invalid")
    try:
        epoch = str(uuid.UUID(manifest["epoch"]))
    except (ValueError, AttributeError) as error:
        raise ValueError("managed append epoch is invalid") from error
    if epoch != manifest["epoch"] or not isinstance(manifest["segments"], list):
        raise ValueError("managed append epoch or segments are invalid")
    chain = initial_chain(epoch)
    for sequence, segment in enumerate(manifest["segments"], 1):
        if not isinstance(segment, dict) or set(segment) != {"sequence", "name", "bytes", "sha256"}:
            raise ValueError("managed append segment metadata is invalid")
        name, length, digest = segment["name"], segment["bytes"], segment["sha256"]
        if (type(segment["sequence"]) is not int or segment["sequence"] != sequence
                or not isinstance(name, str) or not SEGMENT_NAME.fullmatch(name)
                or not name.startswith(f"{sequence:08d}-")
                or type(length) is not int or not 0 < length <= MAX_SEGMENT_BYTES
                or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)):
            raise ValueError("managed append segment metadata is invalid")
        path = root / "segments" / name
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size != length:
            raise ValueError("managed append segment is missing or changed")
        chain = next_chain(chain, segment)
    if manifest["chain"] != chain:
        raise ValueError("managed append manifest chain does not match its segments")
    return manifest


def sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def write_new_file(path: Path, content: bytes, mode: int) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def publish_manifest(root: Path, manifest: dict[str, object]) -> None:
    temporary = root / f".manifest-{uuid.uuid4().hex}.tmp"
    try:
        write_new_file(temporary, canonical(manifest) + b"\n", 0o600)
        os.replace(temporary, root / "manifest.json")
        sync_directory(root)
    finally:
        temporary.unlink(missing_ok=True)


def initialize(root: Path) -> dict[str, object]:
    root.mkdir(mode=0o700)
    (root / "segments").mkdir(mode=0o700)
    epoch = str(uuid.uuid4())
    manifest: dict[str, object] = {
        "format": FORMAT, "epoch": epoch, "chain": initial_chain(epoch), "segments": []
    }
    publish_manifest(root, manifest)
    sync_directory(root.parent)
    return manifest


def append(root: Path, content: bytes) -> dict[str, object]:
    if not 0 < len(content) <= MAX_SEGMENT_BYTES or not content.endswith(b"\n"):
        raise ValueError("managed append segment must end in newline and fit the byte limit")
    try:
        for line in content.decode("utf-8").splitlines():
            if not line or not isinstance(json.loads(line), dict):
                raise ValueError("managed append requires JSON objects on complete lines")
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("managed append segment is not valid UTF-8 JSONL") from error

    check_root(root)
    lock = root / ".writer.lock"
    descriptor = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        manifest = load_manifest(root)
        segments = manifest["segments"]
        assert isinstance(segments, list)
        sequence = len(segments) + 1
        name = f"{sequence:08d}-{uuid.uuid4().hex}.jsonl"
        segment = {
            "sequence": sequence,
            "name": name,
            "bytes": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
        }
        segment_path = root / "segments" / name
        write_new_file(segment_path, content, 0o400)
        sync_directory(root / "segments")
        segments.append(segment)
        manifest["chain"] = next_chain(str(manifest["chain"]), segment)
        publish_manifest(root, manifest)
        return manifest
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("init", "append", "inspect"))
    parser.add_argument("root", type=Path)
    parser.add_argument("input", nargs="?", type=Path)
    args = parser.parse_args()
    if args.operation == "init":
        if args.input is not None:
            parser.error("init does not accept an input file")
        result = initialize(args.root)
    elif args.operation == "append":
        if args.input is None:
            parser.error("append requires an input file")
        with args.input.open("rb") as source:
            content = source.read(MAX_SEGMENT_BYTES + 1)
        result = append(args.root, content)
    else:
        if args.input is not None:
            parser.error("inspect does not accept an input file")
        result = load_manifest(args.root)
    print(json.dumps({"epoch": result["epoch"], "segments": len(result["segments"]), "chain": result["chain"]}))


if __name__ == "__main__":
    main()

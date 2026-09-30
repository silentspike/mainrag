"""Independently validate an immutable filesystem boundary and its full manifest."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import runpy
import stat
import subprocess
import uuid

PRODUCER = runpy.run_path(str(Path(__file__).with_name("source-cut-capture.py")))
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
CUT_PROFILE = "mainrag.fs-release-candidate.v4.btrfs-cut-v1."


def inspect_kernel(snapshot: Path, proof: dict, owner: int) -> None:
    if snapshot.stat().st_uid != owner:
        raise RuntimeError("snapshot property authority is unsafe")
    if owner != 0 or os.geteuid() == 0:
        binary = PRODUCER["inspector"]()
        identity = PRODUCER["identity"](binary, snapshot)
        immutable = PRODUCER["command"](binary, "property", "get", "-ts", snapshot, "ro") == "ro=true\n"
        if identity != {"uuid": proof["snapshot_uuid"], "parent_uuid": proof["origin_uuid"]} or not immutable:
            raise RuntimeError("cut kernel identity is not immutable")
        return
    helper = Path("/usr/libexec/mainrag/source-cut-capture")
    PRODUCER["trusted_directory"](helper.parent)
    meta = helper.lstat()
    if not stat.S_ISREG(meta.st_mode) or meta.st_uid != 0 or meta.st_mode & 0o022:
        raise RuntimeError("cut inspector authority is unsafe")
    result = subprocess.run(["/usr/bin/sudo", "-n", str(helper),
        "--source-root-sha256", proof["source_root_sha256"], "--inspect-cut-id", proof["cut_id"]],
        capture_output=True, check=True, timeout=120)
    if len(result.stdout) > PRODUCER["MAX_BYTES"]:
        raise RuntimeError("cut inspection exceeds its bound")
    observed = json.loads(result.stdout)
    expected = {key: proof[key] for key in (
        "source_root_sha256", "cut_id", "descriptor_sha256", "snapshot_uuid", "origin_uuid")}
    if observed != {"status": "PASS", "read_only": True, **expected}:
        raise RuntimeError("cut kernel identity is not immutable")


def cut_observation_valid(value: object) -> bool:
    if not isinstance(value, dict) or set(value) != {"cut", "fixture_sha256", "item_count", "input_bytes"}:
        return False
    proof = value["cut"]
    if (not isinstance(proof, dict) or set(proof) != {
            "format", "cut_id", "source_root_sha256", "descriptor_sha256",
            "snapshot_uuid", "origin_uuid", "captured_at_unix"}
            or proof["format"] != "mainrag.fs-read-cut.v1"
            or type(proof["captured_at_unix"]) is not int or proof["captured_at_unix"] <= 0
            or any(not isinstance(proof[k], str) or not SHA256.fullmatch(proof[k])
                   for k in ("source_root_sha256", "descriptor_sha256"))
            or not isinstance(value["fixture_sha256"], str) or not SHA256.fullmatch(value["fixture_sha256"])
            or any(type(value[k]) is not int or value[k] < 0 for k in ("item_count", "input_bytes"))):
        return False
    try:
        return all(isinstance(proof[k], str) and str(uuid.UUID(proof[k])) == proof[k]
                   and uuid.UUID(proof[k]).int != 0 for k in ("cut_id", "snapshot_uuid", "origin_uuid"))
    except (ValueError, AttributeError):
        return False


def require_manifest(value: object, root_sha256: str, profile: str,
                     item_count: int, source_watermark: str | None = None,
                     registered_root: str | None = None) -> None:
    if (not cut_observation_valid(value) or not isinstance(profile,str)
            or not re.fullmatch(re.escape(CUT_PROFILE)+r"scope-(?:unfiltered|[0-9a-f]{64})\.fragment-1048576-newline-65536(?:\.text-utf8-nul-space-v1)?",profile)
            or type(item_count) is not int or item_count < 0
            or value["cut"]["source_root_sha256"] != root_sha256
            or value["item_count"] != item_count):
        raise RuntimeError("filesystem cut full-manifest identity differs")
    if registered_root is not None:
        digest = hashlib.sha256(b"mainrag.storage-v2.source-watermark.v1\0")
        for component in ("fs", registered_root, profile, value["fixture_sha256"]):
            raw = component.encode()
            digest.update(len(raw).to_bytes(8, "big"))
            digest.update(raw)
        if source_watermark != digest.hexdigest():
            raise RuntimeError("filesystem cut source watermark differs")


def same_source_manifest(left: object, right: object) -> bool:
    """Different captures can witness the same complete registered source bytes."""
    return (cut_observation_valid(left) and cut_observation_valid(right)
            and all(left[key] == right[key] for key in ("fixture_sha256", "item_count", "input_bytes"))
            and left["cut"]["source_root_sha256"] == right["cut"]["source_root_sha256"])


def read_root(registered_root: Path, observation: dict,
              registry: Path | None = None, *, _owner: int = 0) -> Path:
    """Read pinned history, never follow a mutable current descriptor."""
    root = str(registered_root)
    root_sha = hashlib.sha256(root.encode()).hexdigest()
    value = observation.get("filesystem_cut")
    require_manifest(value, root_sha, observation["adapter_profile_id"],
                     observation["item_count"], observation["source_watermark_sha256"], root)
    if registered_root.resolve(strict=True) != registered_root:
        raise RuntimeError("registered cut source is not canonical")
    registry = registry or Path(os.environ.get(
        "MAINRAG_STORAGE_V2_SOURCE_CUT_ROOT", "/var/lib/mainrag-source-cuts"))
    PRODUCER["trusted_directory"](registry, _owner)
    proof = value["cut"]
    descriptor = registry / "history" / f"{proof['cut_id']}-{root_sha}.json"
    control, raw = PRODUCER["private_read"](descriptor, _owner, with_bytes=True)
    if hashlib.sha256(raw).hexdigest() != proof["descriptor_sha256"]:
        raise RuntimeError("pinned cut descriptor hash differs")
    keys = {"format", "cut_id", "source_root_sha256", "registered_root", "origin_subvolume",
            "snapshot_root", "read_root", "snapshot_uuid", "origin_uuid", "captured_at_unix"}
    if (set(control) != keys or any(control[k] != proof[k] for k in proof if k != "descriptor_sha256")
            or control["registered_root"] != root):
        raise RuntimeError("pinned cut descriptor identity differs")
    origin = Path(control["origin_subvolume"])
    published = registry / "views" / proof["cut_id"]
    snapshot = Path(control["snapshot_root"])
    if snapshot not in {published, published / "snapshot"}:
        raise RuntimeError("cut snapshot location differs")
    PRODUCER["trusted_directory"](registry / "views", _owner)
    PRODUCER["trusted_directory"](snapshot.parent, _owner)
    if not registered_root.is_relative_to(origin) or registered_root == origin:
        raise RuntimeError("cut origin does not contain the registered root")
    selected = snapshot / registered_root.relative_to(origin)
    if (control["snapshot_root"] != str(snapshot) or control["read_root"] != str(selected)
            or selected.resolve(strict=True) != selected or not selected.is_dir()):
        raise RuntimeError("cut read boundary is redirected")
    inspect_kernel(snapshot, proof, _owner)
    return selected

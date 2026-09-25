#!/usr/bin/env python3
"""Build a protected, non-applicable cleanup disposition draft from an inventory.

The draft enumerates observed objects for exact review. It cannot approve or
apply cleanup, and an omitted object never inherits a delete disposition.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import stat
import sys
from pathlib import Path


SPEC = importlib.util.spec_from_file_location(
    "storage_v2_cleanup_capture", Path(__file__).with_name("cleanup-plan.py")
)
assert SPEC and SPEC.loader
CAPTURE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CAPTURE)

CATALOG_LIMIT = 64 * 1024 * 1024
DECISION_LIMIT = 8 * 1024 * 1024
KINDS = (
    ("relation", "relations", ("oid",)),
    ("column", "columns", ("relation_oid", "number")),
    ("constraint", "constraints", ("oid",)),
    ("policy", "policies", ("oid",)),
    ("trigger", "triggers", ("oid",)),
    ("function", "functions", ("oid",)),
    ("index", "indexes", ("oid",)),
    ("generation", "generations", ("id",)),
    ("pack", "packs", ("id",)),
    ("outbox_class", "outbox_classes", ("action", "status")),
)


def private_read(path: Path, limit: int) -> tuple[dict, str]:
    parent = path.parent.resolve(strict=True)
    if stat.S_IMODE(parent.stat().st_mode) & 0o077:
        raise RuntimeError("protected input directory is accessible to others")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "rb") as source:
            before = os.fstat(source.fileno())
            if not stat.S_ISREG(before.st_mode) or stat.S_IMODE(before.st_mode) & 0o077 \
                    or before.st_size > limit:
                raise RuntimeError("protected input is not a private bounded regular file")
            raw = source.read(limit + 1)
            after = os.fstat(source.fileno())
    except OSError as error:
        raise RuntimeError("protected input is unavailable") from error
    if len(raw) > limit or before.st_ino != after.st_ino \
            or before.st_size != after.st_size or before.st_mtime_ns != after.st_mtime_ns:
        raise RuntimeError("protected input changed during read")
    try:
        value = json.loads(raw, object_pairs_hook=CAPTURE.unique_keys)
    except (ValueError, UnicodeError) as error:
        raise RuntimeError("protected input is invalid JSON") from error
    if not isinstance(value, dict):
        raise RuntimeError("protected input is not an object")
    return value, hashlib.sha256(raw).hexdigest()


def object_key(kind: str, identity: tuple[object, ...]) -> str:
    return json.dumps([kind, *identity], separators=(",", ":"), ensure_ascii=False)


def observed_objects(inventory: dict) -> list[dict]:
    if not isinstance(inventory.get("catalog"), dict):
        raise RuntimeError("cleanup catalog is missing")
    catalog = inventory["catalog"]
    objects = []
    for kind, field, identity_fields in KINDS:
        rows = catalog.get(field)
        if not isinstance(rows, list):
            raise RuntimeError("catalog object class is missing")
        for row in rows:
            if not isinstance(row, dict) or any(field not in row for field in identity_fields):
                raise RuntimeError("catalog object identity is incomplete")
            observed = dict(row)
            if kind == "relation":
                exact_rows = catalog.get("exact_rows", {})
                if not isinstance(exact_rows, dict):
                    raise RuntimeError("exact relation counts are invalid")
                exact_count = exact_rows.get(row.get("name"))
                if exact_count is not None:
                    if type(exact_count) is not int or exact_count < 0:
                        raise RuntimeError("exact relation count is invalid")
                    observed["exact_row_count"] = exact_count
            key = object_key(kind, tuple(row[field] for field in identity_fields))
            objects.append({"key": key, "kind": kind, "identity": {
                field: row[field] for field in identity_fields
            }, "observed_sha256": hashlib.sha256(CAPTURE.canonical(observed)).hexdigest(),
                "observed": observed})
    qdrant = inventory.get("qdrant")
    if qdrant is not None:
        if not isinstance(qdrant, dict) or not isinstance(qdrant.get("collections"), list) \
                or not isinstance(qdrant.get("aliases"), list):
            raise RuntimeError("Qdrant inventory is incomplete")
        for kind, field, name_field in (
            ("qdrant_collection", "collections", "name"),
            ("qdrant_alias", "aliases", "alias_name"),
        ):
            for row in qdrant[field]:
                if not isinstance(row, dict) or not isinstance(row.get(name_field), str):
                    raise RuntimeError("Qdrant identity is incomplete")
                objects.append({"key": object_key(kind, (row[name_field],)),
                                "kind": kind, "identity": {name_field: row[name_field]},
                                "observed_sha256": hashlib.sha256(CAPTURE.canonical(row)).hexdigest(),
                                "observed": row})
    runtime = inventory.get("runtime_search")
    if runtime is not None:
        if not isinstance(runtime, dict) or not isinstance(runtime.get("matches"), list):
            raise RuntimeError("runtime caller inventory is incomplete")
        for row in runtime["matches"]:
            if not isinstance(row, dict) or not isinstance(row.get("path"), str) \
                    or type(row.get("line")) is not int:
                raise RuntimeError("runtime caller identity is incomplete")
            objects.append({"key": object_key("runtime_match", (row["path"], row["line"])),
                            "kind": "runtime_match", "identity": {
                                "path": row["path"], "line": row["line"]
                            }, "observed_sha256": hashlib.sha256(CAPTURE.canonical(row)).hexdigest(),
                            "observed": row})
    exports = inventory.get("exports")
    if exports is not None:
        if not isinstance(exports, dict) or not isinstance(exports.get("files"), list):
            raise RuntimeError("export inventory is incomplete")
        for row in exports["files"]:
            if not isinstance(row, dict) or not isinstance(row.get("root"), str) \
                    or not isinstance(row.get("relative_path"), str):
                raise RuntimeError("export file identity is incomplete")
            objects.append({"key": object_key("export_file", (
                row["root"], row["relative_path"])),
                            "kind": "export_file", "identity": {
                                "root": row["root"], "relative_path": row["relative_path"]
                            }, "observed_sha256": hashlib.sha256(CAPTURE.canonical(row)).hexdigest(),
                            "observed": row})
    keys = [item["key"] for item in objects]
    if len(keys) != len(set(keys)) or len(keys) > 100000:
        raise RuntimeError("catalog has duplicated or excessive object identities")
    return sorted(objects, key=lambda item: item["key"])


def decisions_for(path: Path | None, catalog_sha256: str, known: set[str]) -> dict:
    if path is None:
        return {}
    decisions, _ = private_read(path, DECISION_LIMIT)
    if set(decisions) != {"schema_version", "catalog_sha256", "objects"} \
            or decisions["schema_version"] != "mainrag.storage-v2.cleanup-decisions.v1" \
            or decisions["catalog_sha256"] != catalog_sha256 \
            or not isinstance(decisions["objects"], list):
        raise RuntimeError("cleanup decisions are not bound to the exact catalog")
    resolved = {}
    for decision in decisions["objects"]:
        if not isinstance(decision, dict) or set(decision) != {
            "key", "disposition", "reason", "authority"
        } or not isinstance(decision["key"], str) \
                or decision["key"] not in known or decision["key"] in resolved \
                or decision["disposition"] not in ("KEEP", "DELETE") \
                or not isinstance(decision["reason"], str) or not decision["reason"].strip() \
                or not isinstance(decision["authority"], str) or not decision["authority"].strip():
            raise RuntimeError("cleanup decision is invalid or not in the observed object set")
        resolved[decision["key"]] = decision
    return resolved


def draft(inventory: dict, raw_sha256: str, decisions: dict) -> dict:
    catalog = inventory.get("catalog")
    if inventory.get("schema_version") != "mainrag.storage-v2.cleanup-catalog.v1" \
            or inventory.get("status") != "OBSERVED_ONLY" or not isinstance(catalog, dict) \
            or inventory.get("before_state_sha256") != hashlib.sha256(
                CAPTURE.canonical(catalog)).hexdigest():
        raise RuntimeError("cleanup catalog binding is invalid")
    objects = observed_objects(inventory)
    known = {item["key"] for item in objects}
    if not set(decisions) <= known:
        raise RuntimeError("cleanup decisions name unknown objects")
    for item in objects:
        decision = decisions.get(item["key"])
        item["disposition"] = decision["disposition"] if decision else "UNREVIEWED"
        item["reason"] = decision["reason"] if decision else None
        item["authority"] = decision["authority"] if decision else None
        if item["disposition"] == "DELETE" and item["kind"] == "relation" \
                and "exact_row_count" not in item["observed"]:
            raise RuntimeError("relation delete decision requires an exact row count")
        if item["disposition"] == "DELETE" and item["kind"] == "relation" \
                and item["observed"].get("name") == "legacy_hit_mapping":
            raise RuntimeError("durable legacy-hit mapping cannot be deleted")
    blockers = ["POST_ACTIVATION_ACCEPTANCE_UNVERIFIED",
                "OWNER_APPROVAL_FOR_EXACT_MANIFEST_MISSING",
                "RUNTIME_REMOVAL_UNVERIFIED", "EXPORT_RETENTION_UNVERIFIED",
                "BODY_PACK_INTEGRITY_UNVERIFIED", "DEPENDENCY_DISPOSITION_UNVERIFIED"]
    if inventory.get("qdrant") is None:
        blockers.append("QDRANT_NOT_INVENTORIED")
    if inventory.get("runtime_search") is None:
        blockers.append("RUNTIME_CALLERS_NOT_INVENTORIED")
    if catalog.get("reachability") is None:
        blockers.append("RETAINED_REACHABILITY_NOT_INVENTORIED")
    if inventory.get("exports") is None:
        blockers.append("EXPORT_FILES_NOT_INVENTORIED")
    if any(item["disposition"] == "UNREVIEWED" for item in objects):
        blockers.append("OBJECT_DISPOSITIONS_INCOMPLETE")
    return {"schema_version": "mainrag.storage-v2.cleanup-manifest-draft.v1",
            "status": "DRAFT_BLOCKED", "apply_allowed": False,
            "catalog_file_sha256": raw_sha256,
            "before_state_sha256": inventory["before_state_sha256"],
            "operator_sha256": inventory.get("operator_sha256"),
            "pointer_set_sha256": catalog.get("pointer_set_sha256"),
            "runtime_commit_sha": (inventory["runtime_search"].get("commit_sha")
                                   if isinstance(inventory.get("runtime_search"), dict)
                                   else None),
            "reachability_root_coverage": (catalog["reachability"].get("root_coverage")
                                           if isinstance(catalog.get("reachability"), dict)
                                           else None),
            "dependency_set_sha256": hashlib.sha256(CAPTURE.canonical(
                catalog.get("dependencies"))).hexdigest(),
            "objects": objects, "blockers": blockers}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", required=True, type=Path)
    parser.add_argument("--decisions", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    arguments = parser.parse_args()
    if arguments.output.exists() or arguments.output.is_symlink():
        parser.error("protected output already exists")
    try:
        inventory, raw_sha256 = private_read(arguments.catalog, CATALOG_LIMIT)
        objects = observed_objects(inventory)
        decisions = decisions_for(arguments.decisions, raw_sha256,
                                  {item["key"] for item in objects})
        manifest = draft(inventory, raw_sha256, decisions)
        output_sha256 = CAPTURE.private_create(arguments.output, manifest)
    except RuntimeError as error:
        parser.error(str(error))
    except OSError:
        parser.error("protected cleanup manifest output is unavailable")
    print(json.dumps({"status": "DRAFT_BLOCKED", "sha256": output_sha256,
                      "object_count": len(manifest["objects"]),
                      "unreviewed_count": sum(item["disposition"] == "UNREVIEWED"
                                              for item in manifest["objects"]),
                      "blocker_count": len(manifest["blockers"])}, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())

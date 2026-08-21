#!/usr/bin/env python3
"""Prepare and verify the versioned public fixture used by the shadow slice."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import struct
import tempfile
from pathlib import Path


HERE = Path(__file__).resolve().parent
BASE = HERE / "fixtures" / "corpus"
SYMBOL = HERE / "fixtures" / "shadow" / "fixture_symbol.rs"
MANIFEST = ".mainrag-shadow-fixture"
DELTA_SUFFIX = b"\npub fn telemetry_delta_probe() -> usize {\n    43\n}\n"


def sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def files_in(root: Path) -> list[Path]:
    return sorted(
        path.relative_to(root)
        for path in root.rglob("*")
        if path.is_file() and path.name != MANIFEST
    )


def fixture_sha256(root: Path, paths: list[Path]) -> str:
    digest = hashlib.sha256()
    for relative in paths:
        name = relative.as_posix().encode("utf-8")
        content = (root / relative).read_bytes()
        digest.update(struct.pack(">Q", len(name)))
        digest.update(name)
        digest.update(struct.pack(">Q", len(content)))
        digest.update(content)
    return digest.hexdigest()


def manifest_for(root: Path, variant: str) -> dict[str, object]:
    paths = files_in(root)
    return {
        "schema_version": 1,
        "variant": variant,
        "fixture_sha256": fixture_sha256(root, paths),
        "files": {path.as_posix(): sha256((root / path).read_bytes()) for path in paths},
    }


def write_json_atomic(path: Path, value: object) -> None:
    descriptor, temporary_name = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(value, output, indent=2, sort_keys=True)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary_name, path)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)


def verify(root: Path, expected_variant: str | None = None) -> dict[str, object]:
    manifest_path = root / MANIFEST
    if not manifest_path.is_file():
        raise RuntimeError("shadow fixture manifest is missing")
    recorded = json.loads(manifest_path.read_text(encoding="utf-8"))
    variant = recorded.get("variant")
    if variant not in {"base", "delta"} or (expected_variant and variant != expected_variant):
        raise RuntimeError("shadow fixture variant does not match")
    observed = manifest_for(root, str(variant))
    if observed != recorded:
        raise RuntimeError("shadow fixture files differ from their manifest")
    return observed


def prepare(output: Path) -> dict[str, object]:
    if output.exists():
        raise RuntimeError("shadow fixture output already exists")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=output.name + ".", dir=output.parent))
    try:
        for source in sorted(BASE.rglob("*")):
            if not source.is_file():
                continue
            destination = temporary / source.relative_to(BASE)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
        shutil.copy2(SYMBOL, temporary / SYMBOL.name)
        manifest = manifest_for(temporary, "base")
        write_json_atomic(temporary / MANIFEST, manifest)
        os.replace(temporary, output)
        return manifest
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def apply_delta(output: Path) -> dict[str, object]:
    verify(output, "base")
    symbol_path = output / SYMBOL.name
    content = symbol_path.read_bytes()
    descriptor, temporary_name = tempfile.mkstemp(prefix=symbol_path.name + ".", dir=output)
    try:
        with os.fdopen(descriptor, "wb") as changed:
            changed.write(content + DELTA_SUFFIX)
            changed.flush()
            os.fsync(changed.fileno())
        os.replace(temporary_name, symbol_path)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)
    manifest = manifest_for(output, "delta")
    write_json_atomic(output / MANIFEST, manifest)
    return manifest


def reset_to_base(output: Path) -> dict[str, object]:
    """Return the guarded one-file delta fixture to its exact base bytes."""
    verify(output, "delta")
    symbol_path = output / SYMBOL.name
    content = symbol_path.read_bytes()
    if not content.endswith(DELTA_SUFFIX):
        raise RuntimeError("shadow fixture delta suffix is missing")
    descriptor, temporary_name = tempfile.mkstemp(prefix=symbol_path.name + ".", dir=output)
    try:
        with os.fdopen(descriptor, "wb") as restored:
            restored.write(content[: -len(DELTA_SUFFIX)])
            restored.flush()
            os.fsync(restored.fileno())
        os.replace(temporary_name, symbol_path)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)
    manifest = manifest_for(output, "base")
    write_json_atomic(output / MANIFEST, manifest)
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("prepare", "delta", "reset", "verify"))
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    if arguments.action == "prepare":
        result = prepare(arguments.output)
    elif arguments.action == "delta":
        result = apply_delta(arguments.output)
    elif arguments.action == "reset":
        result = reset_to_base(arguments.output)
    else:
        result = verify(arguments.output)
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

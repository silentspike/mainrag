#!/usr/bin/env python3
"""Export or import a versioned storage-v2 intelligence bundle."""

from __future__ import annotations

import argparse
import json
import os
import stat
import sys
import tempfile
from datetime import date
from pathlib import Path

import psycopg2
from psycopg2.extras import Json


SCHEMA_VERSION = "mainrag.storage-v2-intelligence-export.v1"


def atomic_private_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, stat.S_IRUSR | stat.S_IWUSR)
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(value, output, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def database_url() -> str:
    value = os.environ.get("DATABASE_URL")
    if not value:
        raise SystemExit("DATABASE_URL is required")
    return value


def export_bundle(arguments: argparse.Namespace) -> None:
    if arguments.redaction == "protected":
        if arguments.output is None:
            raise SystemExit("protected export requires --output; stdout is forbidden")
        if not arguments.owner or not arguments.retention_until:
            raise SystemExit("protected export requires --owner and --retention-until")
        if date.fromisoformat(arguments.retention_until) < date.today():
            raise SystemExit("--retention-until must not be in the past")
    with psycopg2.connect(database_url()) as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT storage_v2_export_intelligence(%s,%s,%s)",
            (arguments.source_id, arguments.generation, arguments.redaction),
        )
        bundle = cursor.fetchone()[0]
    if bundle.get("schema_version") != SCHEMA_VERSION:
        raise SystemExit("database returned an unsupported export schema")
    bundle["transfer_metadata"] = {
        "owner": arguments.owner,
        "retention_until": arguments.retention_until,
        "cleanup": "delete the protected file at or before retention_until after verified import",
    }
    if arguments.output:
        atomic_private_json(arguments.output.resolve(), bundle)
        print(arguments.output.resolve())
    else:
        json.dump(bundle, sys.stdout, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        sys.stdout.write("\n")


def import_bundle(arguments: argparse.Namespace) -> None:
    bundle_path = arguments.input.resolve()
    file_mode = stat.S_IMODE(bundle_path.stat().st_mode)
    if file_mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise SystemExit("protected import file must not be accessible by group or other")
    bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
    if bundle.get("schema_version") != SCHEMA_VERSION:
        raise SystemExit("unsupported intelligence export schema")
    if bundle.get("redaction") != "protected":
        raise SystemExit("import requires a protected intelligence export")
    metadata = bundle.get("transfer_metadata") or {}
    if not metadata.get("owner") or not metadata.get("retention_until"):
        raise SystemExit("protected import requires owner and retention metadata")
    if date.fromisoformat(metadata["retention_until"]) < date.today():
        raise SystemExit("protected import retention date has expired")
    with psycopg2.connect(database_url()) as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT storage_v2_import_intelligence(%s,%s,%s)",
            (arguments.source_id, arguments.generation, Json(bundle)),
        )
        result = cursor.fetchone()[0]
    json.dump(result, sys.stdout, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    sys.stdout.write("\n")


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    commands = root.add_subparsers(dest="command", required=True)
    export = commands.add_parser("export")
    export.add_argument("--source-id", type=int, required=True)
    export.add_argument("--generation", required=True)
    export.add_argument("--redaction", choices=("public", "protected"), default="public")
    export.add_argument("--output", type=Path)
    export.add_argument("--owner")
    export.add_argument("--retention-until")
    export.set_defaults(handler=export_bundle)
    import_command = commands.add_parser("import")
    import_command.add_argument("--source-id", type=int, required=True)
    import_command.add_argument("--generation", required=True)
    import_command.add_argument("--input", type=Path, required=True)
    import_command.set_defaults(handler=import_bundle)
    return root


def main() -> int:
    arguments = parser().parse_args()
    arguments.handler(arguments)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

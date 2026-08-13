#!/usr/bin/env python3
"""Read-only inventory check for repository-managed MainRAG writers."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_POLICY = Path(__file__).with_name("writers.json")
ELIGIBLE_PREFIXES = ("api/src/", "cli/src/", "scripts/", "ops/migration/")
ELIGIBLE_SUFFIXES = {".rs", ".py", ".sh"}
DISCOVERY_PATTERNS = (
    re.compile(
        r"(?i)\b(?:INSERT\s+INTO|UPDATE|DELETE\s+FROM)\s+"
        r"(?:sources|files|chunks|chunk_embeddings(?:_gte)?|indexing_outbox|"
        r"symbols|call_graph)\b"
    ),
    re.compile(
        r"\.(?:upsert_chunks|delete_chunks|delete_by_source|set_payload|"
        r"create_payload_index)\s*\("
    ),
    re.compile(
        r"\b(?:index_source|sync_files|sync_source|delete_source|"
        r"backfill_orphaned)\s*\("
    ),
    re.compile(r"/collections/.+?/points"),
)


def tracked_files(root: Path = ROOT) -> list[str]:
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    return [item.decode() for item in result.stdout.split(b"\0") if item]


def discover_candidates(contents: dict[str, str]) -> set[str]:
    """Return tracked runtime/operator paths containing known write signals."""
    candidates: set[str] = set()
    for path, text in contents.items():
        candidate_path = Path(path)
        if not path.startswith(ELIGIBLE_PREFIXES):
            continue
        if candidate_path.suffix not in ELIGIBLE_SUFFIXES:
            continue
        if any(pattern.search(text) for pattern in DISCOVERY_PATTERNS):
            candidates.add(path)
    return candidates


def load_policy(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("schema_version") != 1 or not isinstance(data.get("writers"), list):
        raise ValueError("unsupported writer-policy schema")
    return data


def check(policy_path: Path = DEFAULT_POLICY, root: Path = ROOT) -> dict:
    policy = load_policy(policy_path)
    tracked = tracked_files(root)
    tracked_set = set(tracked)
    declared_entries = policy["writers"]
    declared = [entry.get("path", "") for entry in declared_entries]
    errors: list[str] = []

    if len(declared) != len(set(declared)):
        errors.append("writer policy contains duplicate paths")

    contents: dict[str, str] = {}
    for path in tracked:
        candidate = root / path
        try:
            contents[path] = candidate.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue

    discovered = discover_candidates(contents)
    missing = sorted(discovered - set(declared))
    if missing:
        errors.append(f"discovered undeclared writer paths: {missing}")

    checked: list[dict[str, str]] = []
    for entry in declared_entries:
        path = entry.get("path", "")
        if path not in tracked_set:
            errors.append(f"declared writer is not tracked: {path}")
            continue
        if not entry.get("class") or not entry.get("reason"):
            errors.append(f"declared writer lacks class or reason: {path}")
            continue
        data = (root / path).read_bytes()
        checked.append(
            {
                "path": path,
                "class": entry["class"],
                "sha256": hashlib.sha256(data).hexdigest(),
                "status": "PASS",
            }
        )

    return {
        "status": "PASS" if not errors else "FAIL",
        "mode": "read-only",
        "checked_count": len(checked),
        "checked": checked,
        "limitations": policy["limitations"],
        "required_operator_actions": [
            "Confirm the inventory against the target environment.",
            "Quiesce applicable writers through separately authorized operating procedures.",
            "Re-run this read-only gate after the code revision or inventory changes.",
        ],
        "errors": errors,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    result = check(args.policy)
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(f"Writer inventory: {result['status']} ({result['checked_count']} checked)")
        for item in result["checked"]:
            print(f"- {item['path']} [{item['class']}]: {item['status']}")
        print(f"Limitation: {result['limitations']}")
        for error in result["errors"]:
            print(f"ERROR: {error}", file=sys.stderr)
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())

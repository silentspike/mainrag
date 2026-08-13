#!/usr/bin/env python3
"""Fail closed on the repository's public GitHub Actions policy."""

from __future__ import annotations

import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_ROOT = ROOT / ".github" / "workflows"
FULL_SHA = re.compile(r"^[0-9a-f]{40}$")
REMOTE_USE = re.compile(r"^\s*-?\s*uses:\s*([^\s#]+)", re.MULTILINE)
JOB_KEY = re.compile(r"^  ([A-Za-z0-9_-]+):\s*$")


def job_blocks(text: str) -> dict[str, str]:
    lines = text.splitlines()
    try:
        jobs_line = next(index for index, line in enumerate(lines) if line == "jobs:")
    except StopIteration:
        return {}

    starts: list[tuple[str, int]] = []
    for index in range(jobs_line + 1, len(lines)):
        match = JOB_KEY.match(lines[index])
        if match:
            starts.append((match.group(1), index))

    blocks: dict[str, str] = {}
    for position, (name, start) in enumerate(starts):
        end = starts[position + 1][1] if position + 1 < len(starts) else len(lines)
        blocks[name] = "\n".join(lines[start:end])
    return blocks


def check_workflow(path: Path) -> list[str]:
    rel = path.relative_to(ROOT)
    text = path.read_text(encoding="utf-8")
    errors: list[str] = []

    if not re.search(r"^permissions:\s*(?:\{\s*\}|$)", text, re.MULTILINE):
        errors.append(f"{rel}: missing top-level permissions")
    if "self-hosted" in text:
        errors.append(f"{rel}: public workflows must not use self-hosted runners")
    if "ubuntu-latest" in text:
        errors.append(f"{rel}: use an explicit GitHub-hosted runner generation")

    for use in REMOTE_USE.findall(text):
        if use.startswith("./") or use.startswith("docker://"):
            continue
        if "@" not in use:
            errors.append(f"{rel}: action without ref: {use}")
            continue
        ref = use.rsplit("@", 1)[1]
        if not FULL_SHA.fullmatch(ref):
            errors.append(f"{rel}: action is not pinned to a full commit SHA: {use}")

    blocks = job_blocks(text)
    if not blocks:
        errors.append(f"{rel}: no jobs found")
    for name, block in blocks.items():
        if "timeout-minutes:" not in block:
            errors.append(f"{rel}: job {name} has no timeout-minutes")
        if "runs-on: ubuntu-24.04" not in block:
            errors.append(f"{rel}: job {name} must use ubuntu-24.04")

    if "pull_request_target:" in text:
        if "github.event.pull_request.head" in text or "github.head_ref" in text:
            errors.append(f"{rel}: privileged workflow references pull-request head data")
        if "actions/checkout" in text and "ref: ${{ github.event.pull_request.base.sha }}" not in text:
            errors.append(f"{rel}: privileged checkout is not pinned to the exact base SHA")

    return errors


def main() -> int:
    errors: list[str] = []
    workflows = sorted(WORKFLOW_ROOT.glob("*.yml")) + sorted(WORKFLOW_ROOT.glob("*.yaml"))
    if not workflows:
        errors.append("no active workflows found")

    nested = [
        path
        for path in ROOT.glob("**/.github/workflows/*")
        if path.is_file() and path.parent != WORKFLOW_ROOT
    ]
    for path in nested:
        errors.append(f"{path.relative_to(ROOT)}: workflow outside root .github/workflows is inactive")

    for workflow in workflows:
        errors.extend(check_workflow(workflow))

    if errors:
        print("Workflow policy violations:")
        for error in errors:
            print(f"- {error}")
        return 1

    print(f"Workflow policy passed for {len(workflows)} active workflows.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

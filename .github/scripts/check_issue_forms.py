#!/usr/bin/env python3
"""Validate required form files, rendered headings, and required inputs."""

from __future__ import annotations

import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
FORMS = ROOT / ".github" / "ISSUE_TEMPLATE"
REQUIRED = {
    "epic.yml": [
        "Product outcome",
        "Why",
        "Current baseline",
        "In scope",
        "Out of scope",
        "Non-goals and non-claims",
        "Child dependency graph",
        "Stories and disposition",
        "Architecture decisions",
        "Epic acceptance mapping",
        "Verification and release boundary",
        "Privacy, evidence, and cleanup",
        "Stop conditions",
        "Done when",
    ],
    "story.yml": [
        "Parent and dependencies",
        "User outcome",
        "Why",
        "Current baseline",
        "Desired outcome",
        "In scope",
        "Out of scope",
        "Non-goals and non-claims",
        "Implementation boundary and reusable paths",
        "Acceptance criteria",
        "Verification strategy",
        "Privacy, evidence, and cleanup",
        "Landing, closure, and approval boundary",
        "Stop conditions",
    ],
    "task.yml": [
        "Context and dependencies",
        "Why",
        "Goal",
        "Current baseline",
        "In scope",
        "Out of scope",
        "Non-goals and non-claims",
        "Implementation boundary and reusable paths",
        "Acceptance criteria",
        "Verification strategy",
        "Privacy, evidence, and cleanup",
        "Landing, closure, and approval boundary",
        "Stop conditions",
    ],
    "bug_report.yml": [
        "Current behavior",
        "Expected behavior",
        "Reproduction",
        "Environment and baseline",
        "Impact and safety boundary",
        "In scope",
        "Out of scope",
        "Non-goals and non-claims",
        "Affected path and reusable components",
        "Acceptance criteria",
        "Verification strategy",
        "Redacted evidence and cleanup",
        "Landing and rollback",
        "Stop conditions",
    ],
}


def input_blocks(text: str) -> list[str]:
    starts = [match.start() for match in re.finditer(r"^  - type: (?:textarea|input|dropdown|checkboxes)\s*$", text, re.MULTILINE)]
    return [text[start : starts[index + 1] if index + 1 < len(starts) else len(text)] for index, start in enumerate(starts)]


def main() -> int:
    errors: list[str] = []
    seen_ids: set[str] = set()

    for filename, labels in REQUIRED.items():
        path = FORMS / filename
        if not path.is_file():
            errors.append(f"missing form: {filename}")
            continue
        text = path.read_text(encoding="utf-8")
        if "Use public-safe information only" not in text and "Use public-safe information" not in text:
            errors.append(f"{filename}: missing privacy warning")

        blocks = input_blocks(text)
        label_to_block: dict[str, str] = {}
        for block in blocks:
            id_match = re.search(r"^    id:\s*([A-Za-z0-9_-]+)\s*$", block, re.MULTILINE)
            label_match = re.search(r"^      label:\s*(.+?)\s*$", block, re.MULTILINE)
            if not id_match or not label_match:
                errors.append(f"{filename}: every input needs a stable id and label")
                continue
            field_id = id_match.group(1)
            qualified_id = f"{filename}:{field_id}"
            if qualified_id in seen_ids:
                errors.append(f"{filename}: duplicate input id {field_id}")
            seen_ids.add(qualified_id)
            label_to_block[label_match.group(1).strip().strip('"')] = block

        for label in labels:
            block = label_to_block.get(label)
            if block is None:
                errors.append(f"{filename}: missing rendered heading {label}")
            elif not re.search(r"^      required:\s*true\s*$", block, re.MULTILINE):
                errors.append(f"{filename}: field {label} is not required")

    if errors:
        print("Issue form violations:")
        for error in errors:
            print(f"- {error}")
        return 1

    print(f"Issue form policy passed for {len(REQUIRED)} forms.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

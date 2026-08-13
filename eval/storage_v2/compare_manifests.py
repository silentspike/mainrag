#!/usr/bin/env python3
"""Compare two storage-v2 fixture manifests for reproducibility."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

EVAL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(EVAL_ROOT))

from storage_v2.harness import validate_manifest


EXACT_PATHS = (
    ("subject",),
    ("inputs",),
    ("configuration",),
    ("maintenance_gate", "checked"),
    ("ingest", "source_bytes_read"),
    ("ingest", "content_bytes_stored"),
    ("ingest", "parsed_items"),
    ("ingest", "unchanged_items_reused"),
    ("ingest", "errors"),
    ("ingest", "database_bytes_after_ingest"),
    ("search", "query_count"),
    ("search", "recall_at_10"),
    ("search", "mrr_at_10"),
    ("search", "result_identity_sha256"),
    ("search", "matched_documents_total"),
    ("search", "scored_channel_rows_total"),
    ("search", "returned_shortlist_total"),
)


def value_at(document: dict[str, Any], path: tuple[str, ...]) -> Any:
    value: Any = document
    for component in path:
        value = value[component]
    return value


def relative_delta(left: float, right: float) -> float:
    denominator = max(min(abs(left), abs(right)), 0.001)
    return abs(left - right) / denominator


def compare(left: dict[str, Any], right: dict[str, Any], timing_tolerance: float) -> list[str]:
    errors: list[str] = []
    if not 0 <= timing_tolerance <= 5:
        return ["timing tolerance must be between 0 and 5"]
    if left.get("status") != "PASS" or right.get("status") != "PASS":
        errors.append("both manifests must have aggregate PASS status")
    for path in EXACT_PATHS:
        if value_at(left, path) != value_at(right, path):
            errors.append(f"exact field differs: {'.'.join(path)}")
    stable_queries = []
    for manifest in (left, right):
        stable_queries.append(
            [
                {key: value for key, value in query.items() if key not in {"cold_first_ms", "warm_latency"}}
                for query in manifest["search"]["queries"]
            ]
        )
    if stable_queries[0] != stable_queries[1]:
        errors.append("exact field differs: search.queries excluding latency")
    for percentile_name in ("p50_ms", "p95_ms", "p99_ms"):
        left_value = float(left["search"]["warm_latency"][percentile_name])
        right_value = float(right["search"]["warm_latency"][percentile_name])
        delta = relative_delta(left_value, right_value)
        if delta > timing_tolerance:
            errors.append(
                f"warm {percentile_name} relative delta {delta:.3f} exceeds {timing_tolerance:.3f}"
            )
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("left", type=Path)
    parser.add_argument("right", type=Path)
    parser.add_argument("--timing-tolerance", type=float, default=0.50)
    args = parser.parse_args()
    left = json.loads(args.left.read_text(encoding="utf-8"))
    right = json.loads(args.right.read_text(encoding="utf-8"))
    validate_manifest(left)
    validate_manifest(right)
    errors = compare(left, right, args.timing_tolerance)
    if errors:
        print("FAIL: fixture manifests are not reproducible", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    print(
        "PASS: result identities, quality, work counts, and warm timing "
        f"agree within {args.timing_tolerance:.0%}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

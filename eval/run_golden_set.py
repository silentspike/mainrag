#!/usr/bin/env python3
"""
MAINRAG Golden Set Evaluation Script

Measures retrieval quality using curated test queries with known ground truth.
Outputs Hit@k metrics for acceptance testing (target: Hit@10 >= 90%).

Usage:
    python run_golden_set.py [--api-url URL] [--golden-set PATH] [--token TOKEN]

Example:
    python run_golden_set.py --token $JWT_TOKEN
"""

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from eval_common import path_matches

try:
    import requests
except ImportError:
    print("ERROR: requests library required. Install with: pip install requests")
    sys.exit(1)


@dataclass
class TestCase:
    """A single golden set test case."""
    id: str
    mode: str           # "hybrid", "keyword", "semantic"
    query: str
    source: str
    k: int
    expect_files: list[str]

    @property
    def is_negative(self) -> bool:
        """Negative cases expect no results."""
        return len(self.expect_files) == 0


@dataclass
class TestResult:
    """Result of running a single test case."""
    test_id: str
    hit: bool
    expected: list[str]
    actual: list[str]
    error: Optional[str] = None


def load_golden_set(path: Path) -> list[TestCase]:
    """Load test cases from JSONL file."""
    cases = []
    with open(path) as f:
        for line_num, line in enumerate(f, 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                data = json.loads(line)
                cases.append(TestCase(
                    id=data["id"],
                    mode=data.get("mode", "hybrid"),
                    query=data["query"],
                    source=data.get("source", ""),
                    k=data.get("k", 10),
                    expect_files=data.get("expect_files", [])
                ))
            except (json.JSONDecodeError, KeyError) as e:
                print(f"WARNING: Skipping invalid line {line_num}: {e}")
    return cases


def check_hit(result_files: list[str], expect_files: list[str]) -> bool:
    """
    Check if any expected file appears in results.

    Supports two match modes:
    - Directory patterns (ending with "/"): match if any result file contains the directory
    - File patterns: match if any result file ends with the expected path

    For negative cases (expect_files=[]), returns True only if no results.
    """
    if not expect_files:
        return len(result_files) == 0
    return any(
        path_matches(actual, expected)
        for expected in expect_files
        for actual in result_files
    )


def resolve_source_id(api_url: str, token: str, source_name: str, cache: dict) -> Optional[int]:
    """
    Resolve source name to source_id via API.
    Uses cache to avoid repeated lookups.
    """
    if source_name in cache:
        return cache[source_name]

    if not source_name:
        return None

    try:
        headers = {"Authorization": f"Bearer {token}"}
        resp = requests.get(f"{api_url}/sources", headers=headers, timeout=10)
        resp.raise_for_status()
        sources = resp.json().get("sources", [])
        for src in sources:
            cache[src.get("name", "")] = src.get("id")
        return cache.get(source_name)
    except Exception:
        return None


def run_search(api_url: str, token: str, case: TestCase, source_cache: dict) -> tuple[list[str], Optional[str]]:
    """
    Execute search query against MAINRAG API.
    Returns (list of result file paths, error message or None).

    Maps golden-set format to API format:
    - mode: "hybrid" → POST /search
    - mode: "keyword" → POST /search/keyword
    - source: "name" → source_id: int (via lookup)
    """
    headers = {"Authorization": f"Bearer {token}"}

    # Determine endpoint based on mode
    endpoint = "/search/keyword" if case.mode == "keyword" else "/search"

    # Build search request (API format)
    payload = {
        "query": case.query,
        "limit": case.k
    }

    # Resolve source name to source_id
    if case.source:
        source_id = resolve_source_id(api_url, token, case.source, source_cache)
        if source_id is not None:
            payload["source_id"] = source_id

    try:
        resp = requests.post(
            f"{api_url}{endpoint}",
            headers=headers,
            json=payload,
            timeout=30
        )
        resp.raise_for_status()

        data = resp.json()

        # Extract file paths from results
        # API returns: {"results": [{"file_path": "...", "chunk_id": ..., ...}, ...]}
        results = data.get("results", [])
        files = []
        for r in results:
            fp = r.get("file_path") or r.get("path") or ""
            if fp and fp not in files:
                files.append(fp)

        return files, None

    except requests.exceptions.Timeout:
        return [], "Timeout after 30s"
    except requests.exceptions.RequestException as e:
        return [], str(e)
    except (json.JSONDecodeError, KeyError) as e:
        return [], f"Invalid response: {e}"


def run_evaluation(
    api_url: str,
    token: str,
    cases: list[TestCase],
    verbose: bool = False
) -> list[TestResult]:
    """Run all test cases and collect results."""
    results = []
    source_cache = {}  # Cache for source_name -> source_id mapping

    for i, case in enumerate(cases, 1):
        if verbose:
            print(f"[{i}/{len(cases)}] Running: {case.id} ({case.mode})")

        actual_files, error = run_search(api_url, token, case, source_cache)

        if error:
            hit = False
            if verbose:
                print(f"  ERROR: {error}")
        else:
            hit = check_hit(actual_files, case.expect_files)
            if verbose:
                status = "HIT" if hit else "MISS"
                print(f"  {status}: expected={case.expect_files}, got={actual_files[:3]}...")

        results.append(TestResult(
            test_id=case.id,
            hit=hit,
            expected=case.expect_files,
            actual=actual_files[:case.k],
            error=error
        ))

    return results


def print_summary(results: list[TestResult], threshold: float = 0.90):
    """Print evaluation summary with pass/fail status."""
    total = len(results)
    hits = sum(1 for r in results if r.hit)
    errors = sum(1 for r in results if r.error)

    hit_rate = hits / total if total > 0 else 0.0
    passed = hit_rate >= threshold

    print("\n" + "=" * 60)
    print("GOLDEN SET EVALUATION SUMMARY")
    print("=" * 60)
    print(f"Total Test Cases:  {total}")
    print(f"Hits:              {hits}")
    print(f"Misses:            {total - hits - errors}")
    print(f"Errors:            {errors}")
    print(f"Hit@k Rate:        {hit_rate:.1%}")
    print(f"Threshold:         {threshold:.0%}")
    print(f"Status:            {'PASS' if passed else 'FAIL'}")
    print("=" * 60)

    # Show failures
    failures = [r for r in results if not r.hit]
    if failures:
        print("\nFailed Cases:")
        for r in failures:
            if r.error:
                print(f"  - {r.test_id}: ERROR - {r.error}")
            else:
                print(f"  - {r.test_id}: expected {r.expected}, got {r.actual[:3]}")

    return passed


def main():
    parser = argparse.ArgumentParser(
        description="Run MAINRAG Golden Set Evaluation",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__
    )
    parser.add_argument(
        "--api-url",
        default="http://localhost:3001/api/v1",
        help="MAINRAG API base URL (default: http://localhost:3001/api/v1)"
    )
    parser.add_argument(
        "--golden-set",
        type=Path,
        default=Path(__file__).parent / "golden-set.jsonl",
        help="Path to golden set JSONL file"
    )
    parser.add_argument(
        "--token",
        default="",
        help="JWT authentication token (or set MAINRAG_TOKEN env var)"
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=0.90,
        help="Hit@k threshold for pass/fail (default: 0.90)"
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Show progress for each test case"
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output results as JSON"
    )

    args = parser.parse_args()

    # Get token from args or environment
    import os
    token = args.token or os.environ.get("MAINRAG_TOKEN", "")
    if not token:
        print("ERROR: JWT token required. Use --token or set MAINRAG_TOKEN env var.")
        sys.exit(1)

    # Load golden set
    if not args.golden_set.exists():
        print(f"ERROR: Golden set file not found: {args.golden_set}")
        sys.exit(1)

    cases = load_golden_set(args.golden_set)
    if not cases:
        print("ERROR: No valid test cases found in golden set")
        sys.exit(1)

    print(f"Loaded {len(cases)} test cases from {args.golden_set}")
    print(f"API URL: {args.api_url}")
    print()

    # Run evaluation
    results = run_evaluation(args.api_url, token, cases, verbose=args.verbose)

    # Output
    if args.json:
        output = {
            "total": len(results),
            "hits": sum(1 for r in results if r.hit),
            "hit_rate": sum(1 for r in results if r.hit) / len(results),
            "threshold": args.threshold,
            "passed": sum(1 for r in results if r.hit) / len(results) >= args.threshold,
            "results": [
                {
                    "id": r.test_id,
                    "hit": r.hit,
                    "expected": r.expected,
                    "actual": r.actual,
                    "error": r.error
                }
                for r in results
            ]
        }
        print(json.dumps(output, indent=2))
    else:
        passed = print_summary(results, args.threshold)
        sys.exit(0 if passed else 1)


if __name__ == "__main__":
    main()

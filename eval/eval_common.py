"""Runtime-independent evaluation helpers shared by MainRAG harnesses."""

from __future__ import annotations

from collections.abc import Sequence


def normalize_path(path: str) -> str:
    """Normalize a repository-relative result path for comparison."""
    normalized = path.strip().rstrip("/")
    if normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized


def path_matches(result_path: str, expected_path: str) -> bool:
    """Match exact paths, suffixes, directory patterns, and bare stems."""
    raw_expected = expected_path.strip()
    directory_pattern = raw_expected.endswith("/")
    result = normalize_path(result_path)
    expected = normalize_path(expected_path)

    if result == expected:
        return True
    if result.endswith(f"/{expected}") or expected.endswith(f"/{result}"):
        return True
    if directory_pattern and f"/{expected}/" in f"/{result}/":
        return True
    if "." not in expected and "/" not in expected:
        return result.rsplit("/", 1)[-1].startswith(expected)
    return False


def recall_at_k(results: Sequence[str], expected: Sequence[str], k: int) -> float:
    """Return the fraction of expected identities found in the first *k*."""
    if k <= 0:
        raise ValueError("k must be positive")
    if not expected:
        return 1.0 if not results[:k] else 0.0
    found = sum(
        1
        for expected_path in expected
        if any(path_matches(result, expected_path) for result in results[:k])
    )
    return found / len(expected)


def reciprocal_rank(results: Sequence[str], expected: Sequence[str], k: int) -> float:
    """Return reciprocal rank of the first expected identity within *k*."""
    if k <= 0:
        raise ValueError("k must be positive")
    if not expected:
        return 1.0 if not results[:k] else 0.0
    for rank, result in enumerate(results[:k], 1):
        if any(path_matches(result, expected_path) for expected_path in expected):
            return 1.0 / rank
    return 0.0


def percentile(values: Sequence[float], percentile_value: float) -> float:
    """Return a linear-interpolated percentile for a non-empty sample."""
    if not values:
        raise ValueError("cannot calculate a percentile for an empty sample")
    if not 0 <= percentile_value <= 100:
        raise ValueError("percentile must be between 0 and 100")
    ordered = sorted(values)
    position = (len(ordered) - 1) * percentile_value / 100
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    if lower == upper:
        return float(ordered[lower])
    fraction = position - lower
    return float(ordered[lower] * (1 - fraction) + ordered[upper] * fraction)

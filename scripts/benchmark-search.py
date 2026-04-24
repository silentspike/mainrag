#!/usr/bin/env python3
"""MainRag Search Latency Benchmark.

Runs 10 representative queries from docs/search-baseline-gte-modernbert.md
via the `mainrag` CLI, measures wall-clock latency per query, and reports
p50/p95/p99 aggregated across all queries.

Output: data/benchmarks/search_latency_<timestamp>.json

Usage:
    python3 scripts/benchmark-search.py [--repeat 3] [--warmup 1]

Each query is repeated `--repeat` times (default 3) after `--warmup`
warmup invocations to eliminate cold-cache effects. Percentiles are
computed over the union of repeated measurements.
"""
import argparse
import json
import os
import statistics
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


QUERIES = [
    # Code-symbol (Java)
    "createClip delegation proxy",
    # Natural-language (conversational intent)
    "how to delete a clip from arranger",
    # Code-symbol (Rust)
    "fn hybrid_search",
    # Infrastructure (NL)
    "docker compose GPU nvidia TEI",
    # Ops troubleshooting (NL)
    "watcher permission denied token",
    # Broad domain (pathological — no strong match expected)
    "kubernetes pod scheduling affinity",
    # German text (cross-language)
    "Bewerbung Motivationsschreiben",
    # Security concept (NL + Code)
    "PostgreSQL RLS set_config security",
    # Systems (NL)
    "systemd service reboot dependency",
    # Media format (NL)
    "video render moov atom",
]


def run_query(query: str, limit: int = 10) -> float:
    """Run `mainrag search` once, return wall-clock seconds."""
    start = time.monotonic()
    result = subprocess.run(
        ["mainrag", "search", query, "--limit", str(limit)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    elapsed = time.monotonic() - start
    if result.returncode != 0:
        print(f"  WARN: rc={result.returncode} for '{query[:40]}...'", file=sys.stderr)
    return elapsed


def percentile(sorted_values, p):
    """Linear-interpolated percentile (equivalent to numpy's default)."""
    if not sorted_values:
        return None
    k = (len(sorted_values) - 1) * p / 100
    f = int(k)
    c = min(f + 1, len(sorted_values) - 1)
    if f == c:
        return sorted_values[int(k)]
    d0 = sorted_values[f] * (c - k)
    d1 = sorted_values[c] * (k - f)
    return d0 + d1


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--repeat", type=int, default=3, help="Measurements per query")
    parser.add_argument("--warmup", type=int, default=1, help="Warmup runs per query")
    parser.add_argument("--limit", type=int, default=10, help="mainrag --limit")
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parent.parent
    out_dir = repo_root / "data" / "benchmarks"
    out_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_file = out_dir / f"search_latency_{timestamp}.json"

    all_latencies_s = []
    per_query = []

    print(f"Benchmark: {len(QUERIES)} queries, warmup={args.warmup}, repeat={args.repeat}",
          file=sys.stderr, flush=True)

    for i, query in enumerate(QUERIES, 1):
        print(f"[{i}/{len(QUERIES)}] {query!r}", file=sys.stderr, flush=True)

        for w in range(args.warmup):
            run_query(query, args.limit)

        latencies = []
        for r in range(args.repeat):
            t = run_query(query, args.limit)
            latencies.append(t)
            print(f"    run {r+1}/{args.repeat}: {t*1000:.0f} ms", file=sys.stderr, flush=True)

        latencies.sort()
        q_p50 = percentile(latencies, 50)
        q_p95 = percentile(latencies, 95) if len(latencies) >= 2 else latencies[0]

        per_query.append({
            "query": query,
            "runs_ms": [round(l * 1000, 1) for l in latencies],
            "p50_ms": round(q_p50 * 1000, 1),
            "p95_ms": round(q_p95 * 1000, 1),
            "min_ms": round(min(latencies) * 1000, 1),
            "max_ms": round(max(latencies) * 1000, 1),
        })
        all_latencies_s.extend(latencies)

    all_latencies_s.sort()
    aggregate = {
        "p50_ms": round(percentile(all_latencies_s, 50) * 1000, 1),
        "p95_ms": round(percentile(all_latencies_s, 95) * 1000, 1),
        "p99_ms": round(percentile(all_latencies_s, 99) * 1000, 1),
        "min_ms": round(min(all_latencies_s) * 1000, 1),
        "max_ms": round(max(all_latencies_s) * 1000, 1),
        "mean_ms": round(statistics.mean(all_latencies_s) * 1000, 1),
        "stdev_ms": round(statistics.stdev(all_latencies_s) * 1000, 1) if len(all_latencies_s) >= 2 else 0,
        "sample_size": len(all_latencies_s),
    }

    report = {
        "timestamp_utc": timestamp,
        "environment": {
            "hostname": subprocess.run(["hostname"], capture_output=True, text=True).stdout.strip(),
            "cpu": "AMD Ryzen 9 5900HS (16 cores @ 4.68 GHz boost)",
            "gpu": "NVIDIA RTX 3050 Ti (4 GB VRAM, CUDA 12.8)",
            "ram_gb": 16,
            "embedding_model": os.environ.get("EMBEDDING_MODEL_ID", "Alibaba-NLP/gte-modernbert-base"),
            "reranker_model": "Alibaba-NLP/gte-reranker-modernbert-base",
            "vector_store": "Qdrant 1.16.3 (HNSW + Scalar Quantization INT8)",
            "fts": "PostgreSQL 18.1 GIN (UNION ALL simple+english)",
        },
        "parameters": {
            "queries": len(QUERIES),
            "warmup": args.warmup,
            "repeat": args.repeat,
            "limit": args.limit,
            "measurement": "wall-clock (subprocess invocation incl. CLI startup ~30-50ms)",
        },
        "aggregate": aggregate,
        "per_query": per_query,
    }

    with open(out_file, "w") as f:
        json.dump(report, f, indent=2)

    print(f"\nResults written to {out_file}", file=sys.stderr, flush=True)
    print(f"\nAggregate (n={aggregate['sample_size']}):", file=sys.stderr)
    print(f"  p50  = {aggregate['p50_ms']} ms", file=sys.stderr)
    print(f"  p95  = {aggregate['p95_ms']} ms", file=sys.stderr)
    print(f"  p99  = {aggregate['p99_ms']} ms", file=sys.stderr)
    print(f"  min  = {aggregate['min_ms']} ms", file=sys.stderr)
    print(f"  max  = {aggregate['max_ms']} ms", file=sys.stderr)
    print(f"  mean = {aggregate['mean_ms']} ±{aggregate['stdev_ms']} ms", file=sys.stderr)


if __name__ == "__main__":
    main()

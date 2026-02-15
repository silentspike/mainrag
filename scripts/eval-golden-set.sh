#!/usr/bin/env bash
# eval-golden-set.sh — Evaluate search quality against golden-set-expanded.jsonl
# Measures Recall@K, MRR@K, and latency per query.
#
# Usage: ./scripts/eval-golden-set.sh [--output docs/EVAL_BASELINE.md]
set -euo pipefail

API_URL="${API_URL:-http://localhost:3001}"
GOLDEN_SET="${GOLDEN_SET:-eval/golden-set-expanded.jsonl}"
OUTPUT_FILE="${1:-docs/EVAL_BASELINE.md}"

# --- Auth ---
TOKEN=$(curl -sf -X POST "$API_URL/api/v1/auth/login" \
    -H "Content-Type: application/json" \
    -d '{"username":"admin","password":"TestBaseline2025x"}' \
    | python3 -c "import sys,json; print(json.load(sys.stdin)['token'])")

if [ -z "$TOKEN" ]; then
    echo "ERROR: Could not get admin token"
    exit 1
fi

# Export vars for Python subprocess
export TOKEN API_URL GOLDEN_SET OUTPUT_FILE

echo "=== MAINRAG Golden-Set Evaluation ==="
echo "API: $API_URL"
echo "Golden Set: $GOLDEN_SET ($(wc -l < "$GOLDEN_SET") queries)"
echo ""

# --- Evaluation ---
python3 -u << 'PYEOF'
import json, sys, os, time
import urllib.request
import urllib.error

API_URL = os.environ.get("API_URL", "http://localhost:3001")
TOKEN = os.environ["TOKEN"]
GOLDEN_SET = os.environ.get("GOLDEN_SET", "eval/golden-set-expanded.jsonl")

def api_search(query, mode, limit=10, source_id=None):
    """Execute search via API, return (file_paths, took_ms)."""
    if mode == "keyword":
        url = f"{API_URL}/api/v1/search/keyword"
    else:
        url = f"{API_URL}/api/v1/search"

    body = {"query": query, "limit": limit}
    if source_id:
        body["source_id"] = source_id
    payload = json.dumps(body).encode()
    req = urllib.request.Request(url, data=payload, method="POST")
    req.add_header("Authorization", f"Bearer {TOKEN}")
    req.add_header("Content-Type", "application/json")

    start = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
    except (urllib.error.URLError, urllib.error.HTTPError, Exception) as e:
        return [], 0, str(e)
    elapsed_ms = (time.monotonic() - start) * 1000

    results = data.get("results", [])
    file_paths = [r.get("file_path", "") for r in results]
    took_ms = data.get("took_ms", elapsed_ms)
    return file_paths, took_ms, None

def normalize_path(p):
    """Normalize path for comparison (strip leading ./ and trailing /)."""
    p = p.strip().rstrip("/")
    if p.startswith("./"):
        p = p[2:]
    return p

def path_matches(result_path, expect_path):
    """Check if result path matches expected path (exact, suffix, or directory match).

    Directory patterns (ending with '/'): 'auth/' matches any file under an 'auth' directory.
    File patterns without extension: 'schema' matches files starting with 'schema' (e.g. schema.sql).
    """
    raw_expect = expect_path.strip()
    is_dir_pattern = raw_expect.endswith("/")
    rp = normalize_path(result_path)
    ep = normalize_path(expect_path)
    # Exact match
    if rp == ep:
        return True
    # Suffix match: result "api/src/services/search.rs" matches expect "services/search.rs"
    if rp.endswith("/" + ep) or ep.endswith("/" + rp):
        return True
    # Directory pattern: expect "auth/" matches result "api/src/auth/middleware.rs"
    if is_dir_pattern:
        dir_segment = "/" + ep + "/"
        if dir_segment in "/" + rp + "/":
            return True
    # Bare name without extension: expect "schema" matches "schema.sql", "schema_security.sql"
    if "." not in ep and "/" not in ep:
        basename = rp.rsplit("/", 1)[-1] if "/" in rp else rp
        if basename.startswith(ep):
            return True
    return False

def recall_at_k(result_files, expect_files, k):
    """Recall@K: fraction of expected files found in top-K results."""
    if not expect_files:
        return 1.0
    top_k = result_files[:k]
    found = sum(1 for ef in expect_files if any(path_matches(rf, ef) for rf in top_k))
    return found / len(expect_files)

def mrr(result_files, expect_files):
    """Mean Reciprocal Rank: 1/rank of first expected file found."""
    if not expect_files:
        return 1.0
    for i, rf in enumerate(result_files):
        if any(path_matches(rf, ef) for ef in expect_files):
            return 1.0 / (i + 1)
    return 0.0

# Resolve source names → source_ids
def resolve_source_id(source_name):
    """Resolve source name to ID via API."""
    url = f"{API_URL}/api/v1/admin/sources"
    req = urllib.request.Request(url)
    req.add_header("Authorization", f"Bearer {TOKEN}")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            sources = json.loads(resp.read())
            for s in sources:
                if s.get("name") == source_name:
                    return s.get("id")
    except Exception as e:
        print(f"WARNING: Source resolution failed: {e}", file=sys.stderr)
    return None

# Load golden set
queries = []
with open(GOLDEN_SET) as f:
    for line in f:
        line = line.strip()
        if line:
            queries.append(json.loads(line))

# Cache source name → ID mapping
source_id_cache = {}
for q in queries:
    src = q.get("source", "")
    if src and src not in source_id_cache:
        sid = resolve_source_id(src)
        source_id_cache[src] = sid
        if sid:
            print(f"  Source '{src}' → id={sid}")
        else:
            print(f"  WARNING: Source '{src}' not found!")

print(f"Evaluating {len(queries)} queries...")
print()

# Per-mode accumulators
stats = {
    "hybrid": {"recall": [], "mrr": [], "latency": [], "errors": 0, "zero_recall": []},
    "keyword": {"recall": [], "mrr": [], "latency": [], "errors": 0, "zero_recall": []},
}

for i, q in enumerate(queries):
    qid = q.get("id", f"q{i}")
    mode = q.get("mode", "hybrid")
    query = q["query"]
    k = q.get("k", 10)
    expect_files = q.get("expect_files", [])

    source_name = q.get("source", "")
    source_id = source_id_cache.get(source_name)
    result_files, took_ms, err = api_search(query, mode, limit=k, source_id=source_id)

    if err:
        stats[mode]["errors"] += 1
        print(f"  [{qid}] ERROR: {err}")
        continue

    r = recall_at_k(result_files, expect_files, k)
    m = mrr(result_files, expect_files)

    stats[mode]["recall"].append(r)
    stats[mode]["mrr"].append(m)
    stats[mode]["latency"].append(took_ms)

    if r == 0:
        stats[mode]["zero_recall"].append(qid)

    if (i + 1) % 20 == 0:
        print(f"  Progress: {i+1}/{len(queries)}")

print()
print("=" * 60)

# Generate report
report_lines = []
report_lines.append("# MAINRAG Golden-Set Evaluation Baseline")
report_lines.append("")
report_lines.append(f"**Datum:** {time.strftime('%Y-%m-%d %H:%M')}")
report_lines.append(f"**Queries:** {len(queries)}")
report_lines.append(f"**Tokenizer:** tiktoken cl100k (legacy)")
report_lines.append(f"**Chunker:** semantic-v1")
report_lines.append("")

for mode in ["hybrid", "keyword"]:
    s = stats[mode]
    n = len(s["recall"])
    if n == 0:
        continue

    avg_recall = sum(s["recall"]) / n
    avg_mrr = sum(s["mrr"]) / n
    perfect_recall = sum(1 for r in s["recall"] if r >= 1.0)
    zero_recall = len(s["zero_recall"])

    latencies = sorted(s["latency"])
    p50 = latencies[int(n * 0.5)] if n > 0 else 0
    p95 = latencies[int(n * 0.95)] if n > 0 else 0
    p99 = latencies[int(n * 0.99)] if n > 0 else 0
    avg_lat = sum(latencies) / n if n > 0 else 0

    header = f"## {mode.title()} Search (n={n})"
    report_lines.append(header)
    report_lines.append("")
    report_lines.append("| Metric | Value |")
    report_lines.append("|--------|-------|")
    report_lines.append(f"| **Recall@{10}** | **{avg_recall:.3f}** ({avg_recall*100:.1f}%) |")
    report_lines.append(f"| **MRR@{10}** | **{avg_mrr:.3f}** |")
    report_lines.append(f"| Perfect Recall (100%) | {perfect_recall}/{n} ({perfect_recall/n*100:.1f}%) |")
    report_lines.append(f"| Zero Recall (0%) | {zero_recall}/{n} ({zero_recall/n*100:.1f}%) |")
    report_lines.append(f"| Errors | {s['errors']} |")
    report_lines.append(f"| Latency p50 | {p50:.0f}ms |")
    report_lines.append(f"| Latency p95 | {p95:.0f}ms |")
    report_lines.append(f"| Latency p99 | {p99:.0f}ms |")
    report_lines.append(f"| Latency avg | {avg_lat:.0f}ms |")
    report_lines.append("")

    print(header)
    print(f"  Recall@10:  {avg_recall:.3f} ({avg_recall*100:.1f}%)")
    print(f"  MRR@10:     {avg_mrr:.3f}")
    print(f"  Perfect:    {perfect_recall}/{n} ({perfect_recall/n*100:.1f}%)")
    print(f"  Zero:       {zero_recall}/{n} ({zero_recall/n*100:.1f}%)")
    print(f"  Errors:     {s['errors']}")
    print(f"  Latency:    p50={p50:.0f}ms p95={p95:.0f}ms p99={p99:.0f}ms avg={avg_lat:.0f}ms")
    print()

    if s["zero_recall"]:
        report_lines.append(f"### Zero-Recall Queries ({mode})")
        report_lines.append("")
        for qid in s["zero_recall"][:20]:
            report_lines.append(f"- `{qid}`")
        if len(s["zero_recall"]) > 20:
            report_lines.append(f"- ... and {len(s['zero_recall'])-20} more")
        report_lines.append("")

# Overall
all_recall = stats["hybrid"]["recall"] + stats["keyword"]["recall"]
all_mrr = stats["hybrid"]["mrr"] + stats["keyword"]["mrr"]
if all_recall:
    overall_recall = sum(all_recall) / len(all_recall)
    overall_mrr = sum(all_mrr) / len(all_mrr)
    report_lines.append("## Overall")
    report_lines.append("")
    report_lines.append(f"| Metric | Value |")
    report_lines.append(f"|--------|-------|")
    report_lines.append(f"| **Recall@10** | **{overall_recall:.3f}** ({overall_recall*100:.1f}%) |")
    report_lines.append(f"| **MRR@10** | **{overall_mrr:.3f}** |")
    report_lines.append("")

    print("## Overall")
    print(f"  Recall@10:  {overall_recall:.3f} ({overall_recall*100:.1f}%)")
    print(f"  MRR@10:     {overall_mrr:.3f}")

# Write report
output_file = os.environ.get("OUTPUT_FILE", "docs/EVAL_BASELINE.md")
with open(output_file, "w") as f:
    f.write("\n".join(report_lines) + "\n")
print(f"\nReport written to: {output_file}")
PYEOF

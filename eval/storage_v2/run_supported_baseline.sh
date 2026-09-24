#!/usr/bin/env bash
# Run on hosted CI or a qualified Rust build-server checkout, never production.
set -euo pipefail

if [[ $# != 3 ]]; then
  echo 'usage: bash eval/storage_v2/run_supported_baseline.sh OUTPUT CODE_SHA EXECUTION_PROFILE' >&2
  exit 2
fi
baseline_output=$1
baseline_commit=$2
baseline_profile=$3
[[ "$(git rev-parse HEAD)" == "$baseline_commit" ]] || { echo 'checkout identity mismatch' >&2; exit 2; }
git diff --quiet HEAD -- || { echo 'tracked checkout differs from the named commit' >&2; exit 2; }
[[ ! -e "$baseline_output" && ! -L "$baseline_output" ]] || { echo 'output already exists' >&2; exit 2; }
[[ -n "${MAINRAG_INDEX_TEST_DATABASE_URL:-}" ]] || { echo 'explicit fixture connection required' >&2; exit 2; }
[[ -n "${TOKENIZER_ASSET_PATH:-}" ]] || { echo 'explicit pinned lexical asset required' >&2; exit 2; }
baseline_logs=$(mktemp -d)
cleanup_baseline_logs() {
  rm -f -- "$baseline_logs/run-1.log" "$baseline_logs/run-2.log"
  rmdir -- "$baseline_logs"
}
trap cleanup_baseline_logs EXIT

if python3 eval/storage_v2/check_writers.py; then
  for attempt in 1 2; do
    if ! cargo test -p mainrag-api --lib \
      services::index::baseline_tests::postgres_supported_frozen_corpus_baseline \
      -- --ignored --exact --nocapture | tee "$baseline_logs/run-$attempt.log"; then
      echo 'fixture_command_failed' | tee -a "$baseline_logs/run-$attempt.log"
      break
    fi
  done
fi

if ! git diff --quiet HEAD --; then
  echo 'fixture_command_failed' | tee -a "$baseline_logs/run-1.log"
fi

python3 eval/storage_v2/supported_baseline.py \
  --log "$baseline_logs/run-1.log" --log "$baseline_logs/run-2.log" \
  --code-sha "$baseline_commit" --execution-profile "$baseline_profile" \
  --output "$baseline_output"

"""Bounded canonical-hash comparison; preserve baseline statement timeouts."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import statistics
import subprocess
from pathlib import Path

from eval.storage_v2.schema import test_linear_hash_parts as hashes
from eval.storage_v2.shadow_slice import write_json_atomic


PART_COUNTS = (64, 1000, 10000, 130908)
TIMEOUT_MS = 30000
DOMAIN = 'mainrag.generation-root.v1'


def implementation_identity() -> dict[str, str]:
    if subprocess.check_output(['git', 'status', '--porcelain', '--untracked-files=all', '--',
                                'schema.sql', 'migrations', 'eval/storage_v2'],
                               cwd=hashes.graph.ROOT, text=True).strip():
        raise RuntimeError('commit the benchmark implementation before recording evidence')
    return {
        'commit_sha': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=hashes.graph.ROOT, text=True).strip(),
        'migration_sha256': hashlib.sha256(hashes.MIGRATION.read_bytes()).hexdigest(),
        'benchmark_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    }


def measure(case, count: int, expected: str) -> dict[str, object]:
    # The digest predicate must be evaluated on a stored nonconstant array.
    # A matching row proves the reference equality inside the timed query;
    # EXPLAIN alone would otherwise discard the computed digest value.
    query = f"SET statement_timeout='{TIMEOUT_MS}ms'; EXPLAIN (ANALYZE, BUFFERS, WAL, FORMAT JSON) " \
            f"SELECT 1 FROM fixture_hash_benchmark WHERE part_count={count} " \
            f"AND encode(storage_v2_hash_parts('{DOMAIN}',parts),'hex')='{expected}'"
    result = case.command('--set=VERBOSITY=verbose', '--command', query, check=False)
    if result.returncode:
        if '57014: canceling statement due to statement timeout' not in result.stderr:
            raise RuntimeError('unexpected hash benchmark statement failure')
        return {'status': 'TIMEOUT', 'timeout_ms': TIMEOUT_MS}
    plan = json.loads(result.stdout)[0]
    if plan['Plan']['Actual Rows'] != 1 or plan['Plan']['Actual Loops'] != 1:
        raise RuntimeError('hash benchmark did not match exactly one independently verified digest')
    if plan['Plan'].get('WAL Records', 0) != 0:
        raise RuntimeError('read-only hash benchmark unexpectedly generated WAL')
    return {
        'status': 'PASS', 'execution_ms': plan['Execution Time'], 'matched_rows': 1,
        'wal_records': 0, 'shared_hit_blocks': plan['Plan'].get('Shared Hit Blocks', 0),
        'shared_read_blocks': plan['Plan'].get('Shared Read Blocks', 0),
    }


def run_benchmark(repetitions: int = 3) -> dict[str, object]:
    if type(repetitions) is not int or not 3 <= repetitions <= 5:
        raise ValueError('use 3..5 bounded repetitions')
    if os.environ.get('STORAGE_V2_TEST_SOCKET'):
        raise RuntimeError('benchmark requires its own disposable PostgreSQL server')
    identity = implementation_identity()
    case = hashes.LinearHashPartsTests('test_large_generation_root_fits_unchanged_statement_budget')
    ready = False
    timings = []
    references = {count: hashes.canonical_digest(DOMAIN, [hashlib.sha256(str(n).encode()).digest()
                  for n in range(1, count + 1)]) for count in PART_COUNTS}
    try:
        case.setUpClass()
        ready = True
        case.sql('CREATE TABLE fixture_hash_benchmark(part_count INTEGER PRIMARY KEY, parts BYTEA[] NOT NULL); '
                 'INSERT INTO fixture_hash_benchmark SELECT n, ARRAY(SELECT digest(x::TEXT,\'sha256\') '
                 'FROM generate_series(1,n) x) FROM unnest(ARRAY[' + ','.join(map(str, PART_COUNTS)) + ']) n; '
                 'ANALYZE fixture_hash_benchmark;')
        baseline_identity = case.sql("SELECT md5(string_agg(md5(parts::TEXT),'' ORDER BY part_count)) "
                                     'FROM fixture_hash_benchmark')
        for repetition in range(1, repetitions + 1):
            variants = ('before', 'after') if repetition % 2 else ('after', 'before')
            for variant in variants:
                if variant == 'before':
                    case.sql(case.previous_definition)
                else:
                    case.file(hashes.MIGRATION)
                for count in PART_COUNTS:
                    measured = measure(case, count, references[count])
                    timings.append({'variant': variant, 'parts': count, 'repetition': repetition,
                                    'reference_sha256': references[count], **measured})
        fixture_unchanged = baseline_identity == case.sql(
            "SELECT md5(string_agg(md5(parts::TEXT),'' ORDER BY part_count)) FROM fixture_hash_benchmark")
        version = case.sql('SHOW server_version')
    finally:
        if ready:
            case.tearDownClass()
        elif hasattr(case, 'stack'):
            case.stack.close()
    if implementation_identity() != identity:
        raise RuntimeError('implementation changed during comparison')
    medians = {}
    for count in PART_COUNTS:
        values = {}
        for variant in ('before', 'after'):
            rows = [row for row in timings if row['parts'] == count and row['variant'] == variant]
            completed = [row['execution_ms'] for row in rows if row['status'] == 'PASS']
            values[variant + '_ms'] = statistics.median(completed) if completed else None
            values[variant + '_completed'] = len(completed)
            values[variant + '_timeouts'] = len(rows) - len(completed)
        medians[str(count)] = values
    passed = fixture_unchanged and all(row['status'] == 'PASS' for row in timings if row['variant'] == 'after')
    return {
        'status': 'PASS' if passed else 'FAIL', 'implementation': identity,
        'repetitions_per_variant': repetitions, 'statement_timeout_ms': TIMEOUT_MS,
        'fixture_unchanged': fixture_unchanged, 'postgresql_version': version,
        'part_counts': list(PART_COUNTS), 'timings': timings, 'medians': medians,
        'limitations': [
            'Synthetic SQL-only shared-host measurement, not whole-generation or production throughput proof.',
            'Before/after order alternates; fixture creation and function replacement are outside the timed query.',
            'Baseline timeouts remain explicit censored observations, never zero times or speedup ratios.',
            'Every completed query matches an independent reference digest; no production function is changed.',
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repetitions', type=int, default=3)
    parser.add_argument('--output', type=Path, required=True)
    arguments = parser.parse_args()
    metrics = os.environ.get('TM_KENNZAHLEN')
    if arguments.output.exists() or (metrics and Path(metrics).exists()):
        parser.error('output exists; retain prior evidence')
    if metrics and arguments.output.resolve() == Path(metrics).resolve():
        parser.error('evidence and telemetry require different output paths')
    result = run_benchmark(arguments.repetitions)
    write_json_atomic(arguments.output, result)
    if metrics:
        write_json_atomic(Path(metrics), {'hash_parts': {
            f'parts_{count}_{key}': value for count, row in result['medians'].items()
            for key, value in row.items() if value is not None
        }})
    print(json.dumps({'status': result['status'], 'medians': result['medians']}))
    if result['status'] != 'PASS':
        raise SystemExit(1)


if __name__ == '__main__':
    main()

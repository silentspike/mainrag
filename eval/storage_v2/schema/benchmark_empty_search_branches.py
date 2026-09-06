#!/usr/bin/env python3
"""Repeated full-result comparisons on an owned disposable search fixture."""
import argparse
import hashlib
import json
import os
import statistics
import subprocess
from pathlib import Path

from eval.storage_v2.schema import test_empty_search_branches as search
from eval.storage_v2.schema.benchmark_search_aggregates import measure
from eval.storage_v2.shadow_slice import write_json_atomic


def run_benchmark(repetitions=3, views=96):
    if type(repetitions) is not int or type(views) is not int \
            or not 3 <= repetitions <= 20 or not 24 <= views <= 512:
        raise ValueError('use 3..20 repetitions and 24..512 views')
    if os.environ.get('STORAGE_V2_TEST_SOCKET'):
        raise RuntimeError('benchmark requires its own disposable PostgreSQL server')
    if subprocess.check_output(['git', 'status', '--porcelain', '--',
                                'schema.sql', 'migrations', 'eval/storage_v2'], text=True).strip():
        raise RuntimeError('commit the benchmark implementation before recording evidence')
    identity = {'commit_sha': subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip(),
                'migration_sha256': hashlib.sha256(search.MIGRATION.read_bytes()).hexdigest(),
                'benchmark_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    case = search.EmptySearchBranchTests()
    initialized = False
    try:
        case.setUpClass()
        initialized = True
        case.make_search_fixture(views)
        after = case.definition()
        before = search.before_definition(after)
        queries = [{'type': 'term', 'value': 'alpha'},
                   {'type': 'phrase', 'value': 'alpha beta'},
                   {'type': 'exact', 'value': 'exact_key'}]
        samples, hashes = [], {}
        for repetition in range(1, repetitions + 1):
            for query in queries:
                variants = [('before', before), ('after', after)]
                for variant, definition in variants if repetition % 2 else reversed(variants):
                    result = measure(case, definition, query, views)
                    if result['result_sha256'] != hashes.setdefault(query['type'], result['result_sha256']):
                        raise RuntimeError('complete result differs across variants or repetitions')
                    samples.append({'query_class': query['type'], 'repetition': repetition,
                                    'variant': variant, **result})
        medians = {query['type']: {variant + '_execution_ms': statistics.median(
            row['execution_ms'] for row in samples if row['query_class'] == query['type'] and row['variant'] == variant)
            for variant in ('before', 'after')} for query in queries}
        return {'status': 'PASS', 'implementation': identity, 'samples': samples, 'medians': medians,
                'views': views, 'repetitions': repetitions,
                'limitations': 'Owned synthetic PostgreSQL fixture; both variants use custom plans and identical complete ordered results. EXPLAIN instrumentation and a shared host affect timing. Not production API latency, gold acceptance, or activation.'}
    finally:
        if initialized:
            case.tearDownClass()
        elif hasattr(case, 'stack'):
            case.stack.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repetitions', type=int, default=3)
    parser.add_argument('--views', type=int, default=96)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    metrics = os.environ.get('TM_KENNZAHLEN')
    if args.output.exists() or (metrics and Path(metrics).exists()):
        parser.error('preserve previous evidence and telemetry')
    if metrics and args.output.resolve() == Path(metrics).resolve():
        parser.error('evidence and telemetry require different output paths')
    result = run_benchmark(args.repetitions, args.views)
    write_json_atomic(args.output, result)
    if metrics:
        write_json_atomic(Path(metrics), {'empty_search_branches': {
            **{f'{kind}_{key}': value for kind, fields in result['medians'].items() for key, value in fields.items()},
            'complete_result_identity': 1, 'views': args.views, 'repetitions': args.repetitions}})
    print(json.dumps({'status': result['status'], 'medians': result['medians']}))


if __name__ == '__main__':
    main()

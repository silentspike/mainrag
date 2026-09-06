#!/usr/bin/env python3
"""Compare complete search projections with alternating generic-plan variants."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import statistics
import subprocess
import time
from pathlib import Path

from eval.storage_v2.schema import test_materialized_search_aggregates as search
from eval.storage_v2.shadow_slice import write_json_atomic


def implementation_identity():
    if subprocess.check_output(['git', 'status', '--porcelain', '--untracked-files=all', '--',
                                'schema.sql', 'migrations', 'eval/storage_v2'], text=True).strip():
        raise RuntimeError('commit the benchmark implementation before recording evidence')
    return {'commit_sha': subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip(),
            'migration_sha256': hashlib.sha256(search.MIGRATION.read_bytes()).hexdigest(),
            'benchmark_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}


def measure(case, definition, ast, expected_views):
    query = search.search_statement(definition)
    literal = json.dumps(ast).replace("'", "''")
    arguments = f"15,1,'{literal}','{{}}',10"
    statement = (
        "BEGIN READ ONLY; SET LOCAL statement_timeout='30s'; SET LOCAL jit=off; "
        "SET LOCAL plan_cache_mode=force_generic_plan; "
        f'PREPARE measured_search(BIGINT,BIGINT,JSONB,JSONB,BIGINT) AS {query}; '
        f'EXPLAIN (ANALYZE,BUFFERS,FORMAT JSON) EXECUTE measured_search({arguments}); '
        f'EXECUTE measured_search({arguments}); COMMIT;'
    )
    started = time.perf_counter()
    lines = case.sql(statement).splitlines()
    client_ms = (time.perf_counter() - started) * 1000
    result = json.loads(lines.pop())
    plan = json.loads('\n'.join(lines))[0]
    if result.get('fully_scored_views') != expected_views or result.get('total') != expected_views \
            or len(result.get('results', [])) != min(10, expected_views):
        raise RuntimeError('search projection omitted declared complete fixture work')
    for key in ('Planning Time', 'Execution Time'):
        if not isinstance(plan.get(key), (int, float)) or plan[key] < 0:
            raise RuntimeError('complete execution plan timing required')
    aggregates = {node['Subplan Name'].removeprefix('CTE '): node['Actual Loops']
                  for node in search.previous.nodes(plan['Plan'])
                  if node.get('Subplan Name', '').removeprefix('CTE ') in search.NAMES}
    if all(f'{name} AS MATERIALIZED (' in definition for name in search.NAMES):
        if set(aggregates) != set(search.NAMES) or any(value > 1 for value in aggregates.values()):
            raise RuntimeError('materialized search aggregates were not computed once')
    return {'client_ms': client_ms, 'execution_ms': plan['Execution Time'],
            'planning_ms': plan['Planning Time'], 'aggregate_loops': aggregates,
            'fully_scored_views': expected_views,
            'result_sha256': hashlib.sha256(json.dumps(result, sort_keys=True).encode()).hexdigest()}


def run_benchmark(repetitions=3, views=96):
    if type(repetitions) is not int or type(views) is not int \
            or not 3 <= repetitions <= 20 or not 24 <= views <= 512:
        raise ValueError('use 3..20 repetitions and 24..512 views')
    if os.environ.get('STORAGE_V2_TEST_SOCKET'):
        raise RuntimeError('benchmark requires its own disposable PostgreSQL server')
    identity = implementation_identity()
    case = search.MaterializedSearchAggregateTests()
    initialized = False
    try:
        case.setUpClass()
        initialized = True
        case.make_search_fixture(views)
        after = case.definition()
        before = search.before_definition(after)
        samples, hashes = [], {}
        queries = [{'type': 'term', 'value': 'alpha'}, {'type': 'phrase', 'value': 'alpha beta'},
                   {'type': 'exact', 'value': 'exact_key'}]
        for repetition in range(1, repetitions + 1):
            for query in queries:
                variants = (('before', before), ('after', after))
                for variant, definition in variants if repetition % 2 else reversed(variants):
                    value = measure(case, definition, query, views)
                    if value['result_sha256'] != hashes.setdefault(query['type'], value['result_sha256']):
                        raise RuntimeError('complete search results changed across variants or repetitions')
                    samples.append({'query_class': query['type'], 'repetition': repetition,
                                    'variant': variant, **value})
        medians = {query['type']: {variant + '_' + clock: statistics.median(
            row[clock] for row in samples if row['query_class'] == query['type'] and row['variant'] == variant)
            for variant in ('before', 'after') for clock in ('execution_ms', 'client_ms')}
            for query in queries}
        result = {'status': 'PASS', 'implementation': identity, 'views': views,
                  'repetitions': repetitions, 'samples': samples, 'medians': medians,
                  'postgresql_version': case.sql('SHOW server_version'),
                  'limitations': [
                      'Synthetic read-only SQL projection; not full authorized API or production qualification.',
                      'Execution time is one EXPLAIN ANALYZE execution; client time includes connection, plan, and a second complete result check.',
                      'Three query classes and alternating variant order; no candidate truncation or timeout increase.',
                      'Shared-host resource samples cannot be attributed exclusively to this fixture.',
                      'Materialized grouped output is not constant-memory; executor spill behavior remains workload-dependent.',
                  ]}
    finally:
        if initialized:
            case.tearDownClass()
        elif hasattr(case, 'stack'):
            case.stack.close()
    if implementation_identity() != identity:
        raise RuntimeError('implementation changed during comparison')
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repetitions', type=int, default=3)
    parser.add_argument('--views', type=int, default=96)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    metrics = os.environ.get('TM_KENNZAHLEN')
    if args.output.exists() or (metrics and Path(metrics).exists()):
        parser.error('output exists; retain prior evidence')
    if metrics and args.output.resolve() == Path(metrics).resolve():
        parser.error('evidence and telemetry require different output paths')
    result = run_benchmark(args.repetitions, args.views)
    write_json_atomic(args.output, result)
    if metrics:
        write_json_atomic(Path(metrics), {'search_aggregates': {
            **{f'{query}_{key}': value for query, fields in result['medians'].items()
               for key, value in fields.items()}, 'views': args.views, 'repetitions': args.repetitions}})
    print(json.dumps({'status': result['status'], 'medians': result['medians']}))


if __name__ == '__main__':
    main()

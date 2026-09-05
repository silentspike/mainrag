#!/usr/bin/env python3
"""Compare cold and complete card writes on an owned disposable SQL fixture."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import statistics
import subprocess
from pathlib import Path

from eval.storage_v2.schema import test_structural_card_reuse as reuse
from eval.storage_v2.schema.benchmark_search_document_reuse import nodes
from eval.storage_v2.shadow_slice import write_json_atomic


def implementation_identity() -> dict[str, str]:
    paths = ['schema.sql', 'migrations', 'eval/storage_v2']
    if subprocess.check_output(['git', 'status', '--porcelain', '--untracked-files=all', '--', *paths],
                               cwd=reuse.schema.ROOT, text=True).strip():
        raise RuntimeError('commit the benchmark implementation before recording evidence')
    return {
        'commit_sha': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=reuse.schema.ROOT, text=True).strip(),
        'migration_sha256': hashlib.sha256(reuse.MIGRATION.read_bytes()).hexdigest(),
        'base_reuse_migration_sha256': hashlib.sha256(reuse.BASE_REUSE.read_bytes()).hexdigest(),
        'benchmark_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    }


def measure(case, arguments: dict[str, str], calls: int, *, cold: bool) -> dict[str, object]:
    statement = ('BEGIN; SET LOCAL plan_cache_mode=force_generic_plan; '
                 'EXPLAIN (ANALYZE, BUFFERS, WAL, FORMAT JSON) ' +
                 case.bulk_query(arguments, 'fixture_card_benchmark') +
                 (' ROLLBACK;' if cold else ' COMMIT;'))
    plan = json.loads(case.sql(case.admin(statement)))[0]
    functions = [node for node in nodes(plan['Plan']) if node['Node Type'] == 'Function Scan']
    if len(functions) != 1 or functions[0]['Actual Loops'] != calls or functions[0]['Actual Rows'] != 1:
        raise RuntimeError('benchmark did not execute the declared nonempty card workload')
    return {
        'function_calls': calls, 'execution_ms': plan['Execution Time'],
        'shared_hit_blocks': plan['Plan'].get('Shared Hit Blocks', 0),
        'shared_read_blocks': plan['Plan'].get('Shared Read Blocks', 0),
        'wal_records': plan['Plan'].get('WAL Records', 0),
        'wal_bytes': plan['Plan'].get('WAL Bytes', 0),
    }


def run_benchmark(repetitions: int = 3, calls: int = 500) -> dict[str, object]:
    if type(repetitions) is not int or type(calls) is not int \
            or not 3 <= repetitions <= 20 or not 1 <= calls <= 5000:
        raise ValueError('use 3..20 repetitions and 1..5000 calls')
    if os.environ.get('STORAGE_V2_TEST_SOCKET'):
        raise RuntimeError('benchmark requires its own disposable PostgreSQL server')
    identity = implementation_identity()
    case = reuse.StructuralCardReuseTests('test_complete_reuse_performs_no_insert_and_preserves_sequences')
    initialized = False
    try:
        case.setUpClass()
        initialized = True
        arguments = case.bulk_fixture('fixture_card_benchmark', calls)
        timings = []
        for state in ('cold', 'warm'):
            if state == 'warm':
                case.file(reuse.PREVIOUS)
                if case.sql(case.admin(case.bulk_query(arguments, 'fixture_card_benchmark'))) != str(calls):
                    raise RuntimeError('warm fixture cardinality differs')
            before = case.data_identity()
            for repetition in range(1, repetitions + 1):
                variants = [('before', reuse.PREVIOUS), ('after', reuse.MIGRATION)]
                if repetition % 2 == 0:
                    variants.reverse()
                for variant, migration in variants:
                    if variant == 'after':
                        case.apply_current()
                    else:
                        case.file(migration)
                    measured = measure(case, arguments, calls, cold=state == 'cold')
                    if state == 'warm' and variant == 'after' and measured['wal_records'] != 0:
                        raise RuntimeError('complete reuse unexpectedly generated WAL records')
                    timings.append({'state': state, 'variant': variant, 'repetition': repetition, **measured})
                    if case.data_identity() != before:
                        raise RuntimeError('comparison changed the protected semantic fixture state')
            case.apply_current()
        medians = {}
        for state in ('cold', 'warm'):
            pair = {variant + '_ms': statistics.median(row['execution_ms'] for row in timings
                    if row['state'] == state and row['variant'] == variant) for variant in ('before', 'after')}
            pair['speedup_ratio'] = pair['before_ms'] / pair['after_ms']
            medians[state] = pair
        result = {
            'status': 'PASS', 'implementation': identity, 'postgresql_version': case.sql('SHOW server_version'),
            'calls_per_repetition': calls, 'repetitions_per_variant': repetitions,
            'semantic_rows_unchanged': True, 'complete_reuse_wal_records': 0,
            'timings': timings, 'medians': medians,
            'limitations': [
                'Synthetic SQL-only comparison on a shared host; not a production or API latency claim.',
                'Cold writes roll back identical fixture inputs; complete reuse retains the original rows.',
                'Sequence advances in the previous implementation are measured work, not semantic row changes.',
                'Variant order alternates; migration installation is outside the timed statement.',
            ],
        }
    finally:
        if initialized:
            case.tearDownClass()
        elif hasattr(case, 'stack'):
            case.stack.close()
    if implementation_identity() != identity:
        raise RuntimeError('implementation changed during comparison')
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repetitions', type=int, default=3)
    parser.add_argument('--calls', type=int, default=500)
    parser.add_argument('--output', type=Path, required=True)
    arguments = parser.parse_args()
    destination = os.environ.get('TM_KENNZAHLEN')
    if arguments.output.exists() or (destination and Path(destination).exists()):
        parser.error('output exists; retain prior evidence')
    if destination and arguments.output.resolve() == Path(destination).resolve():
        parser.error('evidence and telemetry require different output paths')
    result = run_benchmark(arguments.repetitions, arguments.calls)
    write_json_atomic(arguments.output, result)
    if destination:
        write_json_atomic(Path(destination), {'card_reuse': {
            **{f'{state}_{key}': value for state, values in result['medians'].items()
               for key, value in values.items() if key.endswith('_ms')},
            'calls': arguments.calls, 'repetitions': arguments.repetitions,
        }})
    print(json.dumps({'status': result['status'], 'medians': result['medians'],
                      'semantic_rows_unchanged': result['semantic_rows_unchanged']}))


if __name__ == '__main__':
    main()

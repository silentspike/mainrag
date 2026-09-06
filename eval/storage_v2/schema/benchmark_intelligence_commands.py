#!/usr/bin/env python3
"""Compare complete targeted commands against full-export command evaluation."""
import argparse
import hashlib
import json
import os
import statistics
import subprocess
import time
from pathlib import Path

from eval.storage_v2.schema import test_targeted_intelligence_commands as commands
from eval.storage_v2.shadow_slice import write_json_atomic


def implementation_identity():
    if subprocess.check_output(['git', 'status', '--porcelain', '--untracked-files=all', '--',
                                'schema.sql', 'migrations', 'eval/storage_v2'], text=True).strip():
        raise RuntimeError('commit the benchmark implementation before recording evidence')
    return {'commit_sha': subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip(),
            'migration_sha256': hashlib.sha256(commands.MIGRATION.read_bytes()).hexdigest(),
            'benchmark_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}


def fixture(case, count):
    args = commands.exports.IntelligenceExportTextTests.card_fixture(case, count)
    source = args['p_source_id']
    args.update(p_symbol_key="'fixture-target'", p_qualified_name="'crate::fixture_target'",
                p_generic_card="'{\"name\":\"fixture_target\"}'::JSONB")
    case.sql(case.admin(commands.cards.StructuralCardReuseTests.call(args)))
    case.sql(case.admin(f"""
SELECT storage_v2_record_call(o.id,s.id,'crate::fixture_target','call',
    '{{"resolution_kind":"parser_symbol_id"}}')
FROM storage_v2_symbol_occurrence o JOIN storage_v2_symbol s ON s.id=o.symbol_id
WHERE s.source_id={source} AND s.symbol_key='fixture-target';
SELECT storage_v2_record_call(o.id,NULL,'unknown_target','call','{{}}')
FROM storage_v2_symbol_occurrence o JOIN storage_v2_symbol s ON s.id=o.symbol_id
WHERE s.source_id={source} AND s.symbol_key='fixture-target';
SELECT storage_v2_put_intelligence_entity({source},'fixture-owner',NULL,'fixture_target','symbol','{{}}');
SELECT storage_v2_put_intelligence_entity({source},'fixture-owned',NULL,'fixture_other','symbol','{{}}');
SELECT storage_v2_put_intelligence_relation({source},a.id,b.id,'owns',
    '{{"resolution_kind":"user_asserted"}}')
FROM storage_v2_intelligence_entity a,storage_v2_intelligence_entity b
WHERE a.source_id={source} AND b.source_id={source}
  AND a.entity_key='fixture-owner' AND b.entity_key='fixture-owned';
"""))
    return source


def measure(case, source, command, query, before, expected_count):
    function = 'fixture_command_before' if before else 'storage_v2_intelligence_command'
    statement = (f"WITH result AS MATERIALIZED (SELECT {function}({source},'1','{command}',"
                 f"{commands.cards.json_literal(query)}) AS value) "
                 "SELECT jsonb_build_object('result_sha256',encode(sha256(convert_to(value::TEXT,'UTF8')),'hex'),"
                 "'record_count',CASE WHEN jsonb_typeof(value)='array' THEN jsonb_array_length(value) "
                 "ELSE jsonb_array_length(value->'proven')+jsonb_array_length(value->'unresolved') END) FROM result")
    sql = case.admin("BEGIN READ ONLY; SET LOCAL statement_timeout='30s'; "
                     f'EXPLAIN (ANALYZE,BUFFERS,FORMAT JSON) {statement}; {statement}; COMMIT;')
    started = time.perf_counter()
    lines = case.sql(sql).splitlines()
    client_ms = (time.perf_counter() - started) * 1000
    result = json.loads(lines.pop())
    plan = json.loads('\n'.join(lines))[0]
    digest = result.get('result_sha256')
    if type(result.get('record_count')) is not int or result['record_count'] != expected_count \
            or expected_count <= 0 or not isinstance(digest, str) or len(digest) != 64 \
            or any(c not in '0123456789abcdef' for c in digest):
        raise RuntimeError('complete nonempty command result and full hash required')
    if type(plan.get('Execution Time')) not in (int, float) or plan['Execution Time'] < 0:
        raise RuntimeError('complete server execution timing required')
    return {**result, 'execution_ms': plan['Execution Time'], 'client_ms': client_ms}


def run_benchmark(repetitions=3, cards=2500):
    if type(repetitions) is not int or type(cards) is not int \
            or not 3 <= repetitions <= 10 or not 32 <= cards <= 5000:
        raise ValueError('use 3..10 repetitions and 32..5000 background cards')
    if os.environ.get('STORAGE_V2_TEST_SOCKET'):
        raise RuntimeError('benchmark requires its own disposable PostgreSQL server')
    identity = implementation_identity()
    case = commands.TargetedIntelligenceCommandTests()
    initialized = False
    try:
        case.setUpClass()
        initialized = True
        source = fixture(case, cards)
        queries = [('card', {'name': 'fixture_target'}, 1), ('layers', {}, cards + 1),
                   ('explain', {'name': 'fixture_target'}, 2), ('ownership', {'name': 'fixture_target'}, 1)]
        samples, hashes = [], {}
        for repetition in range(1, repetitions + 1):
            for command, query, count in queries:
                for before in ((True, False) if repetition % 2 else (False, True)):
                    value = measure(case, source, command, query, before, count)
                    if value['result_sha256'] != hashes.setdefault(command, value['result_sha256']):
                        raise RuntimeError('full command result changed across variants or repetitions')
                    samples.append({'command': command, 'variant': 'before' if before else 'after',
                                    'repetition': repetition, **value})
        medians = {command: {variant + '_' + clock: statistics.median(
            row[clock] for row in samples if row['command'] == command and row['variant'] == variant)
            for variant in ('before', 'after') for clock in ('execution_ms', 'client_ms')}
            for command, _, _ in queries}
        result = {'status': 'PASS', 'implementation': identity, 'cards': cards + 1,
                  'repetitions': repetitions, 'samples': samples, 'medians': medians,
                  'postgresql_version': case.sql('SHOW server_version'),
                  'limitations': ['Synthetic SQL commands, not production/API qualification.',
                      'Server clock includes one complete command and full-result hash under EXPLAIN ANALYZE.',
                      'Client clock includes connection, instrumentation and a second complete result/hash check.',
                      'Unfiltered layers returns every fixture card; other commands return complete matching records.',
                      'Shared-host resource samples are context, not exclusively attributable fixture resources.']}
    finally:
        if initialized:
            case.tearDownClass()
        elif hasattr(case, 'stack'):
            case.stack.close()
    if implementation_identity() != identity:
        raise RuntimeError('implementation drifted during comparison')
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repetitions', type=int, default=3)
    parser.add_argument('--cards', type=int, default=2500)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    metrics = os.environ.get('TM_KENNZAHLEN')
    if args.output.exists() or (metrics and Path(metrics).exists()) \
            or (metrics and args.output.resolve() == Path(metrics).resolve()):
        parser.error('use distinct new evidence and telemetry destinations')
    result = run_benchmark(args.repetitions, args.cards)
    write_json_atomic(args.output, result)
    if metrics:
        write_json_atomic(Path(metrics), {'intelligence_commands': {
            **{f'{command}_{key}': value for command, fields in result['medians'].items()
               for key, value in fields.items()}, 'cards': result['cards'], 'repetitions': args.repetitions}})
    print(json.dumps({'status': result['status'], 'medians': result['medians']}))


if __name__ == '__main__':
    main()

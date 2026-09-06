#!/usr/bin/env python3
"""Repeated complete v1 export comparison on an owned disposable SQL fixture."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import statistics
import subprocess
import time
from pathlib import Path

from eval.storage_v2.schema import test_intelligence_export_text as export
from eval.storage_v2.shadow_slice import write_json_atomic


def implementation_identity():
    paths = ['schema.sql', 'migrations', 'eval/storage_v2']
    if subprocess.check_output(['git', 'status', '--porcelain', '--untracked-files=all', '--', *paths],
                               cwd=export.schema.ROOT, text=True).strip():
        raise RuntimeError('commit the benchmark implementation before recording evidence')
    return {
        'commit_sha': subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip(),
        'migration_sha256': hashlib.sha256(export.MIGRATION.read_bytes()).hexdigest(),
        'benchmark_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    }


def measure(case, source, redaction, before, expected_cards):
    function = 'fixture_export_before' if before else 'storage_v2_export_intelligence'
    statement = case.admin(
        "BEGIN READ ONLY; SET LOCAL statement_timeout='30s'; "
        f"WITH bundle AS MATERIALIZED (SELECT {function}({source},'1','{redaction}') AS value) "
        "SELECT jsonb_build_object('payload_sha256',value->>'payload_sha256',"
        "'protected_payload_sha256',CASE WHEN value->>'redaction'='public' "
        "THEN value->'payload'->>'protected_payload_sha256' ELSE value->>'payload_sha256' END,"
        "'cards',CASE WHEN value->>'redaction'='public' "
        "THEN (value->'payload'->'record_counts'->>'cards')::BIGINT "
        "ELSE jsonb_array_length(value->'payload'->'cards') END) FROM bundle; COMMIT;")
    started = time.perf_counter()
    result = json.loads(case.sql(statement))
    elapsed_ms = (time.perf_counter() - started) * 1000
    if result.get('cards') != expected_cards or not expected_cards:
        raise RuntimeError('export did not process the declared nonempty collection')
    if any(not isinstance(result.get(key), str) or len(result[key]) != 64
           or any(char not in '0123456789abcdef' for char in result[key])
           for key in ('payload_sha256', 'protected_payload_sha256')):
        raise RuntimeError('complete export hashes are required')
    return {**result, 'client_ms': elapsed_ms}


def run_benchmark(repetitions=3, cards=5000):
    if type(repetitions) is not int or type(cards) is not int \
            or not 3 <= repetitions <= 20 or not 1 <= cards <= 20000:
        raise ValueError('use 3..20 repetitions and 1..20000 cards')
    if os.environ.get('STORAGE_V2_TEST_SOCKET'):
        raise RuntimeError('benchmark requires its own disposable PostgreSQL server')
    identity = implementation_identity()
    case = export.IntelligenceExportTextTests()
    initialized = False
    try:
        case.setUpClass()
        initialized = True
        source = case.card_fixture(cards)['p_source_id']
        semantic_identity = export.cards.StructuralCardReuseTests.data_identity(case)
        timings, hashes = [], {}
        for redaction in ('public', 'protected'):
            for repetition in range(1, repetitions + 1):
                for before in ((True, False) if repetition % 2 else (False, True)):
                    measured = measure(case, source, redaction, before, cards)
                    expected = hashes.setdefault(redaction, measured['payload_sha256'])
                    if measured['payload_sha256'] != expected:
                        raise RuntimeError('export payload identity changed across variants or rounds')
                    if measured['protected_payload_sha256'] != hashes.setdefault(
                            'protected', measured['protected_payload_sha256']):
                        raise RuntimeError('public and protected export identities disagree')
                    timings.append({'redaction': redaction, 'variant': 'before' if before else 'after',
                                    'repetition': repetition, **measured})
        if export.cards.StructuralCardReuseTests.data_identity(case) != semantic_identity:
            raise RuntimeError('read-only export changed semantic fixture rows')
        medians = {redaction: {variant + '_client_ms': statistics.median(
            row['client_ms'] for row in timings if row['redaction'] == redaction and row['variant'] == variant)
            for variant in ('before', 'after')} for redaction in ('public', 'protected')}
        result = {
            'status': 'PASS', 'implementation': identity, 'cards': cards, 'repetitions': repetitions,
            'timings': timings, 'medians': medians, 'semantic_rows_unchanged': True,
            'postgresql_version': case.sql('SHOW server_version'),
            'limitations': [
                'Synthetic shared-host SQL export, not production source or API qualification.',
                'Client milliseconds include connection, transaction and complete export/hash validation.',
                'Variant order alternates; fixture setup is excluded from per-export timings.',
                'No timeout increase, source truncation, or partial hash is accepted.',
                'TEXT aggregation is not constant-memory or an unlimited-size streaming export.',
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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repetitions', type=int, default=3)
    parser.add_argument('--cards', type=int, default=5000)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    destination = os.environ.get('TM_KENNZAHLEN')
    if args.output.exists() or (destination and Path(destination).exists()):
        parser.error('output exists; retain prior evidence')
    if destination and args.output.resolve() == Path(destination).resolve():
        parser.error('evidence and telemetry require different output paths')
    result = run_benchmark(args.repetitions, args.cards)
    write_json_atomic(args.output, result)
    if destination:
        write_json_atomic(Path(destination), {'intelligence_export': {
            **{f'{redaction}_{key}': value for redaction, values in result['medians'].items()
               for key, value in values.items()},
            'cards': args.cards, 'repetitions': args.repetitions,
        }})
    print(json.dumps({'status': result['status'], 'medians': result['medians']}))


if __name__ == '__main__':
    main()

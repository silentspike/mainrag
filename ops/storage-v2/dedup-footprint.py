#!/usr/bin/env python3
"""Read-only storage accounting before/after dedup cleanup.

This measures storage, not content integrity, cleanup authority or filesystem
reclamation. Corpus descriptors come from the accepted inventory/review; equal
descriptors do not independently prove that either reader preserves the corpus.
Outputs are private because catalog names and local roots may be sensitive.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import stat
import subprocess
import time
from pathlib import Path


def module(name, filename):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(filename))
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


C = module('footprint_cleanup_catalog', 'cleanup-plan.py')
E = module('footprint_external_measurement', 'cleanup-external.py')
A = module('footprint_candidate_contract', 'candidate-aggregate-audit.py')
VERSION = 'mainrag.storage-v2.dedup-footprint.v1'
HEX = re.compile(r'[0-9a-f]{64}')

# pg_table_size includes TOAST, FSM, VM and init forks but excludes the parent
# indexes. TOAST indexes belong to the TOAST subtotal, not parent_indexes_bytes.
# Partition parents have no files; children occur once in this catalog scan.
SQL = """
BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY;
SET LOCAL row_security = off;
SET LOCAL lock_timeout = '3s';
SET LOCAL statement_timeout = '30s';
SELECT jsonb_build_object(
 'database_oid', (SELECT oid FROM pg_database WHERE datname=current_database()),
 'system_identifier', (SELECT system_identifier::text FROM pg_control_system()),
 'database_name_sha256', encode(digest(current_database(),'sha256'),'hex'),
 'database_bytes', pg_database_size(current_database()),
 'wal_bytes', (SELECT COALESCE(sum(size),0) FROM pg_ls_waldir()),
 'relations', (SELECT COALESCE(jsonb_agg(jsonb_build_object(
   'oid', c.oid, 'schema', n.nspname, 'name', c.relname, 'kind', c.relkind,
   'table_bytes', pg_table_size(c.oid),
   'toast_bytes', CASE WHEN c.reltoastrelid=0 THEN 0
      ELSE pg_total_relation_size(c.reltoastrelid) END,
   'parent_indexes_bytes', pg_indexes_size(c.oid),
   'total_bytes', pg_total_relation_size(c.oid)
 ) ORDER BY c.oid), '[]'::jsonb)
 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
 WHERE n.nspname !~ '^pg_' AND n.nspname<>'information_schema'
 AND c.relkind IN ('r','m','S')),
 'generations', (SELECT COALESCE(jsonb_agg(jsonb_build_object(
   'id', id, 'source_id', source_id, 'sequence', generation_seq,
   'status', status, 'item_count', item_count,
   'verification_manifest_sha256', verification_manifest_sha256
 ) ORDER BY id), '[]'::jsonb) FROM source_generation),
 'packs', (SELECT COALESCE(jsonb_agg(jsonb_build_object(
   'status', status, 'count', count, 'catalog_stored_bytes', stored_bytes,
   'catalog_live_bytes', live_bytes
 ) ORDER BY status), '[]'::jsonb)
 FROM (SELECT status, count(*) AS count, sum(stored_bytes) AS stored_bytes,
       sum(live_bytes) AS live_bytes FROM content_pack GROUP BY status) p),
 'active_pointer_sha256', (SELECT encode(digest(convert_to(
    COALESCE(jsonb_agg(jsonb_build_array(id, active_generation_id) ORDER BY id),
      '[]'::jsonb)::text, 'UTF8'),'sha256'),'hex') FROM logical_source),
 'building_run_count', (SELECT count(*) FROM storage_v2_ingest_run WHERE status='building'),
 'active_backend_count', (SELECT count(*) FROM pg_stat_activity
     WHERE datname=current_database() AND pid<>pg_backend_pid() AND state='active')
)::text;
COMMIT;
"""

CANONICAL = {'content_body', 'content_dictionary', 'content_pack', 'content_pack_entry'}
NATIVE = {'logical_source', 'source_generation', 'source_item', 'artifact_version',
          'generation_item_version', 'content_node', 'content_node_edge',
          'retrieval_view', 'view_component', 'occurrence', 'occurrence_edge',
          'occurrence_scope', 'content_reader_epoch', 'content_pack_reader'}
LEGACY = {'files', 'chunks', 'embeddings', 'chunk_embeddings', 'symbols',
          'call_graph', 'entities', 'entity_relations', 'indexing_outbox'}


def category(name, schema='public'):
    if schema != 'public':
        return 'other_database_objects'
    if name in CANONICAL:
        return 'canonical_body_pack_catalog'
    if name == 'legacy_hit_mapping' or name.startswith('storage_v2_legacy_hit_'):
        return 'durable_hit_compatibility'
    if name in LEGACY:
        return 'legacy_coexistence'
    if name.startswith(('storage_v2_search_', 'storage_v2_lexical_',
                        'storage_v2_compact_', 'storage_v2_derived_lexical_',
                        'storage_v2_byte_posting_', 'storage_v2_ordinary_first_',
                        'storage_v2_document_postings_', 'storage_v2_legacy_rank_',
                        'storage_v2_legacy_lexical_', 'storage_v2_reader_metadata_',
                        'storage_v2_occurrence_score_')):
        return 'search_projection_and_reader_metadata'
    if name in NATIVE or name.startswith('storage_v2_'):
        return 'native_graph_intelligence_and_operational_metadata'
    return 'other_database_objects'


def read_private(path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) & 0o077 \
            or info.st_size > 16*1024*1024:
        raise RuntimeError('input must be a bounded private regular file')
    raw = path.read_bytes()
    return json.loads(raw, object_pairs_hook=C.unique_keys), hashlib.sha256(raw).hexdigest()


def corpus_descriptor(value):
    """Preserve externally proven coverage; never infer it from byte totals."""
    fields = {
            'schema_version', 'retained_corpus_sha256', 'retained_item_count',
            'retained_logical_bytes', 'newly_preserved_logical_bytes', 'evidence_sha256'}
    v2 = isinstance(value, dict) and value.get('schema_version') == 'mainrag.storage-v2.footprint-corpus.v2'
    if v2:
        fields |= {'source_pins', 'coverage_vs_legacy', 'item_count_basis', 'logical_bytes_basis'}
    if not isinstance(value, dict) or set(value) != fields \
            or value['schema_version'] not in {'mainrag.storage-v2.footprint-corpus.v1',
                                               'mainrag.storage-v2.footprint-corpus.v2'} \
            or any(not isinstance(value[k], str) or not HEX.fullmatch(value[k])
                   for k in ('retained_corpus_sha256', 'evidence_sha256')) \
            or any(type(value[k]) is not int or value[k] < 0 for k in (
                'retained_item_count', 'retained_logical_bytes')):
        raise RuntimeError('invalid corpus descriptor')
    if v2:
        pins = value['source_pins']
        if value['newly_preserved_logical_bytes'] is not None \
                or value['coverage_vs_legacy'] != 'NOT_MEASURED' \
                or value['item_count_basis'] != 'PRODUCER_ITEMS_INCLUDING_FRAGMENTS' \
                or value['logical_bytes_basis'] != 'SUM_FROZEN_ADAPTER_INPUT_BYTES_NOT_UNIQUE_BODIES' \
                or not isinstance(pins, list) or not 1 <= len(pins) <= 4096:
            raise RuntimeError('invalid accepted corpus coverage')
        ids = []
        for pin in pins:
            if not isinstance(pin, dict) or set(pin) != set(INPUT_PIN_KEYS) | {
                    'generation_id', 'generation_seq', 'generation_root_sha256',
                    'qualification_file_sha256', 'qualification_manifest_sha256'} \
                    or any(type(pin[k]) is not int or pin[k] <= 0 for k in (
                        'source_id', 'generation_id', 'generation_seq')) \
                    or any(type(pin[k]) is not int or pin[k] < 0 for k in ('item_count', 'input_bytes')) \
                    or any(not isinstance(pin[k], str) or not HEX.fullmatch(pin[k]) for k in (
                        'fixture_sha256', 'source_watermark_sha256', 'generation_root_sha256',
                        'qualification_file_sha256', 'qualification_manifest_sha256')) \
                    or not isinstance(pin['adapter_profile_id'], str) or not pin['adapter_profile_id'] \
                    or (pin['source_snapshot_review_sha256'] is not None and (
                        not isinstance(pin['source_snapshot_review_sha256'], str)
                        or not HEX.fullmatch(pin['source_snapshot_review_sha256']))):
                raise RuntimeError('invalid accepted source coverage pin')
            ids.append(pin['source_id'])
        if ids != sorted(set(ids)) \
                or value['retained_item_count'] != sum(pin['item_count'] for pin in pins) \
                or value['retained_logical_bytes'] != sum(pin['input_bytes'] for pin in pins) \
                or value['retained_corpus_sha256'] != input_scope_digest(pins) \
                or value['evidence_sha256'] != hashlib.sha256(C.canonical(
                    [pin['qualification_file_sha256'] for pin in pins])).hexdigest():
            raise RuntimeError('accepted source scope or totals differ')
    elif type(value['newly_preserved_logical_bytes']) is not int \
            or value['newly_preserved_logical_bytes'] < 0:
        raise RuntimeError('invalid corpus descriptor')
    return value


INPUT_PIN_KEYS = ('source_id', 'fixture_sha256', 'source_watermark_sha256',
                  'adapter_profile_id', 'item_count', 'input_bytes', 'source_snapshot_review_sha256')


def input_scope_digest(pins):
    # A fresh timestamped source review can certify the same frozen input.
    # Its file digest is evidence provenance, not a different retained corpus.
    return hashlib.sha256(C.canonical([{key: pin[key] for key in INPUT_PIN_KEYS
                                       if key != 'source_snapshot_review_sha256'}
                                      for pin in pins])).hexdigest()


def qualifications_to_corpus(artifacts, expected_source_ids):
    """Adapt exact accepted operator artifacts without reading sources or SQL.

    Acceptance is historical, bound to the supplied file hashes. The regular
    current-watermark/package/cleanup admission must still be performed by the
    operator. Legacy-relative byte coverage is not available in these receipts.
    """
    if not 1 <= len(artifacts) <= 4096 or len(expected_source_ids) != len(artifacts) \
            or len(set(expected_source_ids)) != len(expected_source_ids) \
            or any(type(identifier) is not int or identifier <= 0 for identifier in expected_source_ids):
        raise RuntimeError('exact expected source set is required')
    pins = []
    for path, expected_sha in artifacts:
        if not isinstance(expected_sha, str) or not HEX.fullmatch(expected_sha):
            raise RuntimeError('exact qualification artifact SHA-256 is required')
        value, actual_sha = read_private(Path(path))
        if actual_sha != expected_sha:
            raise RuntimeError('qualification artifact digest differs')
        try:
            checkpoint, verified = value['checkpoint'], value['verification']
            qualification, result = value['qualification'], value['result']
            manifest, build = qualification['manifest'], checkpoint['build']
            failures, _ = A.candidate_proof(manifest)
            if failures or result['status'] != 'release_candidate' \
                    or qualification['evidence_id'] != result['evidence_id'] \
                    or any(checkpoint[key] != verified[key] for key in (
                        'source_id', 'generation_id', 'generation_seq', 'source_watermark_sha256', 'item_count')) \
                    or any(checkpoint[key] != result[key] for key in ('source_id', 'generation_id', 'generation_seq')) \
                    or qualification['generation_id'] != checkpoint['generation_id'] \
                    or qualification['source_watermark_sha256'] != checkpoint['source_watermark_sha256'] \
                    or qualification['adapter_profile_id'] != verified['adapter_profile_id'] \
                    or any(build[key] != checkpoint[key] for key in (
                        'source_id', 'generation_id', 'generation_seq', 'source_watermark_sha256', 'item_count')) \
                    or manifest['server_verification_sha256'] != hashlib.sha256(
                        json.dumps(verified, sort_keys=True).encode()).hexdigest():
                raise RuntimeError('qualification is not an accepted matching source proof')
            snapshot_review = manifest.get('source_snapshot_review')
            if snapshot_review is not None and snapshot_review.get('source_watermark_sha256') \
                    != checkpoint['source_watermark_sha256']:
                raise RuntimeError('source snapshot coverage differs')
            counters = build['telemetry']['ablauf']
            input_bytes = counters['eingang_bytes']
            if build.get('filesystem_cut') is not None and (
                    build['filesystem_cut']['input_bytes'] != input_bytes
                    or build['filesystem_cut']['fixture_sha256'] != build['fixture_sha256']
                    or build['filesystem_cut']['item_count'] != checkpoint['item_count']):
                raise RuntimeError('frozen input byte coverage differs')
            pins.append(dict(source_id=checkpoint['source_id'],
                fixture_sha256=build['fixture_sha256'],
                source_watermark_sha256=checkpoint['source_watermark_sha256'],
                adapter_profile_id=verified['adapter_profile_id'], item_count=checkpoint['item_count'],
                input_bytes=input_bytes,
                source_snapshot_review_sha256=snapshot_review.get('review_sha256') if snapshot_review else None,
                generation_id=checkpoint['generation_id'], generation_seq=checkpoint['generation_seq'],
                generation_root_sha256=verified['generation_root_sha256'],
                qualification_file_sha256=actual_sha, qualification_manifest_sha256=result['manifest_sha256']))
        except (KeyError, TypeError, AttributeError) as error:
            raise RuntimeError('qualification artifact is incomplete or not accepted') from error
    pins.sort(key=lambda pin: pin['source_id'])
    if [pin['source_id'] for pin in pins] != sorted(expected_source_ids):
        raise RuntimeError('accepted artifacts do not cover the exact expected source set')
    return corpus_descriptor(dict(schema_version='mainrag.storage-v2.footprint-corpus.v2',
        retained_corpus_sha256=input_scope_digest(pins),
        retained_item_count=sum(pin['item_count'] for pin in pins),
        retained_logical_bytes=sum(pin['input_bytes'] for pin in pins),
        newly_preserved_logical_bytes=None, coverage_vs_legacy='NOT_MEASURED',
        item_count_basis='PRODUCER_ITEMS_INCLUDING_FRAGMENTS',
        logical_bytes_basis='SUM_FROZEN_ADAPTER_INPUT_BYTES_NOT_UNIQUE_BODIES', source_pins=pins,
        evidence_sha256=hashlib.sha256(C.canonical([pin['qualification_file_sha256'] for pin in pins])).hexdigest()))


def observe_database(database, privileged):
    if not re.fullmatch(r'[A-Za-z0-9_-]+', database):
        raise RuntimeError('database must be a local database name')
    command = (['sudo', '-n', '-u', 'postgres'] if privileged else []) + [
        'psql', '-X', '--no-psqlrc', '-qAt', '--set=ON_ERROR_STOP=1', '--dbname', database]
    env = os.environ.copy()
    env['PGAPPNAME'] = 'mainrag-storage-v2-dedup-footprint'
    env['PGOPTIONS'] = (env.get('PGOPTIONS', '')+' -c default_transaction_read_only=on').strip()
    result = subprocess.run(command, input=SQL, text=True, capture_output=True,
                            timeout=40, env=env, check=False)
    if result.returncode:
        raise RuntimeError('read-only footprint observation failed; no absence is inferred')
    if len(result.stdout) > 16*1024*1024:
        raise RuntimeError('footprint observation exceeds its output bound')
    return json.loads(result.stdout, object_pairs_hook=C.unique_keys)


def summarize_database(observed):
    groups = {}
    seen = set()
    for row in observed['relations']:
        if row['oid'] in seen:
            raise RuntimeError('duplicate physical relation')
        seen.add(row['oid'])
        keys = ('table_bytes', 'toast_bytes', 'parent_indexes_bytes', 'total_bytes')
        if any(type(row[k]) is not int or row[k] < 0 for k in keys) \
                or row['toast_bytes'] > row['table_bytes'] \
                or row['table_bytes']+row['parent_indexes_bytes'] != row['total_bytes']:
            raise RuntimeError('relation size decomposition is inconsistent; recapture after writes stop')
        group = groups.setdefault(category(row['name'], row['schema']), dict(
            relation_count=0, heap_and_auxiliary_bytes=0, toast_including_indexes_bytes=0,
            parent_indexes_bytes=0, total_bytes=0))
        group['relation_count'] += 1
        group['heap_and_auxiliary_bytes'] += row['table_bytes']-row['toast_bytes']
        group['toast_including_indexes_bytes'] += row['toast_bytes']
        group['parent_indexes_bytes'] += row['parent_indexes_bytes']
        group['total_bytes'] += row['total_bytes']
    return groups


def validate_roots(roots):
    """Disjoint directory domains prevent counting a Qdrant snapshot twice."""
    paths = [Path(p).resolve(strict=True) for p in roots.values()]
    for i, first in enumerate(paths):
        for second in paths[i+1:]:
            if first == second or first in second.parents or second in first.parents:
                raise RuntimeError('storage measurement roots overlap')
    return roots


def snapshot(database, privileged, corpus, roots):
    started = time.time()
    corpus_descriptor(corpus)
    validate_roots(roots)
    observed = observe_database(database, privileged)
    groups = summarize_database(observed)
    # Reuse cleanup's bounded nonsymlink traversal and hardlink deduplication.
    disks = {role: E.vector_space(path, privileged) for role, path in validate_roots(roots).items()}
    return dict(schema_version=VERSION, status='OBSERVED_ONLY',
        observed_start_unix=started, observed_end_unix=time.time(),
        collector_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        measurement_contract_sha256=hashlib.sha256(C.canonical({name:
            hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
            for name in ('dedup-footprint.py', 'cleanup-external.py', 'cleanup-components.py',
                         'candidate-aggregate-audit.py', 'fs_cut.py')})).hexdigest(),
        corpus=corpus_descriptor(corpus), database=observed, relation_groups=groups,
        local_storage=disks,
        temporary_and_coexistence=dict(wal_bytes=observed['wal_bytes'],
            legacy_relation_bytes=groups.get('legacy_coexistence', {}).get('total_bytes', 0),
            qdrant_included_as_coexistence='qdrant' in disks),
        accounting=dict(database_file_length_bytes=observed['database_bytes'],
            catalog_relation_file_length_bytes=sum(g['total_bytes'] for g in groups.values()),
            database_minus_observed_relation_file_length_bytes=observed['database_bytes']-
                sum(g['total_bytes'] for g in groups.values()),
            external_allocated_bytes_by_role={role: row['allocated_bytes'] for role, row in disks.items()},
            total_is_complete=False, physical_reclaim_proven=False),
        limitations=[
            'Database file lengths and filesystem allocated blocks are distinct units; do not add them as physical allocated bytes.',
            'WAL is cluster-wide temporary storage, outside pg_database_size; it is not dedup payload.',
            'Physical sizes and directory scans are not atomic even inside a read-only SQL snapshot.',
            'Caller must freeze writers, supply complete local storage roots and bind cleanup/corpus proofs.',
            'External roots may share cross-directory hardlinks or reflinks; component allocations are not a unique extent total.',
            'Catalog stored/live pack bytes are not actual file allocation or evidence that retired files were removed.',
            'No Qdrant API, archive server, backup, content read, mutation or integrity verification is invoked.',
        ])


def compare(before, after):
    if before.get('schema_version') != VERSION or after.get('schema_version') != VERSION:
        raise RuntimeError('incompatible footprint snapshots')
    a, b = corpus_descriptor(before['corpus']), corpus_descriptor(after['corpus'])
    scope_keys = ('schema_version', 'retained_corpus_sha256', 'retained_item_count',
                  'retained_logical_bytes', 'newly_preserved_logical_bytes')
    comparable = all(a[k] == b[k] for k in scope_keys)
    if any(before['database'][k] != after['database'][k] for k in (
            'database_oid', 'system_identifier', 'database_name_sha256')):
        raise RuntimeError('database identity changed')
    if not before.get('measurement_contract_sha256') \
            or before['measurement_contract_sha256'] != after.get('measurement_contract_sha256'):
        raise RuntimeError('measurement contract changed')
    if set(before['local_storage']) != set(after['local_storage']):
        raise RuntimeError('measurement domains changed')
    disks = {}
    for role, old in before['local_storage'].items():
        new = after['local_storage'][role]
        if any(old[k] != new[k] for k in ('root', 'device', 'inode')):
            raise RuntimeError('storage root identity changed')
        disks[role] = dict(allocated_bytes_reduction=old['allocated_bytes']-new['allocated_bytes'],
            filesystem_available_bytes_increase=new['filesystem_available_bytes']-old['filesystem_available_bytes'])
    return dict(schema_version='mainrag.storage-v2.dedup-footprint-comparison.v1',
        status='OBSERVED_DELTA_SAME_DECLARED_CORPUS' if comparable else 'NOT_COMPARABLE_DIFFERENT_COVERAGE',
        same_declared_corpus=comparable, corpus_preservation_independently_verified=False,
        newly_preserved_logical_bytes_before=a['newly_preserved_logical_bytes'],
        newly_preserved_logical_bytes_after=b['newly_preserved_logical_bytes'],
        legacy_relative_coverage_bytes_supplied=a['newly_preserved_logical_bytes'] is not None
            and b['newly_preserved_logical_bytes'] is not None,
        compared_source_ids=[pin['source_id'] for pin in a.get('source_pins', [])] if comparable else [],
        observed_database_file_length_reduction=before['database']['database_bytes']-after['database']['database_bytes'],
        observed_local_storage_deltas=disks,
        observed_wal_bytes_change=after['database']['wal_bytes']-before['database']['wal_bytes'],
        dedup_savings_proven=False, physical_reclaim_proven=False,
        limitations=['Observed reductions require exact cleanup-manifest receipts, frozen-writer admission and preserved-corpus evidence before attribution.',
                     'Filesystem free-space change includes unrelated activity and is not added to relation/file reduction.'])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database')
    parser.add_argument('--local-postgres', action='store_true')
    parser.add_argument('--corpus', type=Path)
    parser.add_argument('--pack-root', type=Path)
    parser.add_argument('--qdrant-root', type=Path)
    parser.add_argument('--local-retained-root', type=Path)
    parser.add_argument('--before', type=Path)
    parser.add_argument('--after', type=Path)
    parser.add_argument('--qualification-artifact', nargs=2, action='append', default=[],
                        metavar=('PRIVATE_PATH', 'SHA256'))
    parser.add_argument('--expected-source-id', type=int, action='append', default=[])
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    if args.qualification_artifact:
        if any((args.before, args.after, args.database, args.corpus, args.pack_root,
                args.qdrant_root, args.local_retained_root, args.local_postgres)):
            parser.error('corpus adaptation accepts only qualification artifacts, expected sources and output')
        value = qualifications_to_corpus(args.qualification_artifact, args.expected_source_id)
    elif args.before or args.after:
        if not args.before or not args.after or any((args.database, args.corpus,
                args.pack_root, args.qdrant_root, args.local_retained_root, args.local_postgres,
                args.expected_source_id)):
            parser.error('comparison requires only --before, --after and --output')
        before, before_sha = read_private(args.before)
        after, after_sha = read_private(args.after)
        value = compare(before, after)
        value.update(before_file_sha256=before_sha, after_file_sha256=after_sha)
    else:
        if args.expected_source_id:
            parser.error('expected sources require exact qualification artifacts')
        if not args.database or not args.corpus or not args.pack_root:
            parser.error('observation requires --database, --corpus and --pack-root')
        corpus, corpus_sha = read_private(args.corpus)
        corpus_descriptor(corpus)
        roots = {'packs': args.pack_root}
        if args.qdrant_root:
            roots['qdrant'] = args.qdrant_root
        if args.local_retained_root:
            roots['retained_local_exports_and_evidence'] = args.local_retained_root
        value = snapshot(args.database, args.local_postgres, corpus, roots)
        value['corpus_file_sha256'] = corpus_sha
    digest = C.private_create(args.output, value)
    print(json.dumps(dict(status=value.get('status', 'CORPUS_FROM_HISTORICAL_ACCEPTED_ARTIFACTS'), evidence_sha256=digest,
                         dedup_savings_proven=False, physical_reclaim_proven=False)))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

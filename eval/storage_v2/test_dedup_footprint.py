"""Accounting and attribution boundaries for the dedup storage readback."""
import copy
import hashlib
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from eval.storage_v2.test_candidate_aggregate_audit import source as candidate_source


SPEC = importlib.util.spec_from_file_location('dedup_footprint_tests',
    Path(__file__).resolve().parents[2]/'ops/storage-v2/dedup-footprint.py')
F = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(F)


def corpus():
    return dict(schema_version='mainrag.storage-v2.footprint-corpus.v1',
        retained_corpus_sha256='a'*64, retained_item_count=2,
        retained_logical_bytes=200, newly_preserved_logical_bytes=100,
        evidence_sha256='b'*64)


def observed():
    return dict(database_oid=1, system_identifier='123', database_name_sha256='d'*64,
        database_bytes=1000, wal_bytes=400,
        relations=[dict(oid=1, schema='public', name='content_body', kind='r',
            table_bytes=150, toast_bytes=50, parent_indexes_bytes=20, total_bytes=170),
            dict(oid=2, schema='public', name='storage_v2_compact_lexical_block', kind='r',
            table_bytes=300, toast_bytes=200, parent_indexes_bytes=80, total_bytes=380),
            dict(oid=3, schema='public', name='chunks', kind='r',
            table_bytes=200, toast_bytes=0, parent_indexes_bytes=20, total_bytes=220)],
        generations=[], packs=[], active_pointer_sha256='c'*64,
        building_run_count=0, active_backend_count=0)


def snapshot():
    return dict(schema_version=F.VERSION, corpus=corpus(), database=observed(),
        measurement_contract_sha256='e'*64,
        local_storage={'packs': dict(root='/fixture/packs', device=1, inode=2,
            allocated_bytes=200, filesystem_available_bytes=1000)})


def accepted_artifact(source_id=3):
    manifest = copy.deepcopy(candidate_source(source_id)['generations'][0]['qualification_manifest'])
    identity = dict(source_id=source_id, generation_id=103, generation_seq=1,
                    source_watermark_sha256='c'*64, item_count=2)
    verified = dict(**identity, adapter_profile_id='fixture-adapter', generation_root_sha256='d'*64)
    manifest['server_verification_sha256'] = hashlib.sha256(json.dumps(verified, sort_keys=True).encode()).hexdigest()
    return dict(checkpoint=dict(**identity, build=dict(**identity, fixture_sha256='f'*64,
                    telemetry={'ablauf': {'eingang_bytes': 200}})),
        verification=verified,
        qualification=dict(manifest=manifest, generation_id=103, evidence_id='fixture-evidence',
                           source_watermark_sha256='c'*64, adapter_profile_id='fixture-adapter'),
        result=dict(source_id=source_id, generation_id=103, generation_seq=1,
                    status='release_candidate', evidence_id='fixture-evidence', manifest_sha256='e'*64))


class FootprintTests(unittest.TestCase):
    def test_accepted_artifact_adapter_retains_exact_sources_bytes_and_unknown_legacy_coverage(self):
        with patch.object(F, 'read_private', return_value=(accepted_artifact(), 'a'*64)):
            value = F.qualifications_to_corpus([('fixture.json', 'a'*64)], [3])
        self.assertEqual(value['retained_item_count'], 2)
        self.assertEqual(value['retained_logical_bytes'], 200)
        self.assertIsNone(value['newly_preserved_logical_bytes'])
        self.assertEqual(value['coverage_vs_legacy'], 'NOT_MEASURED')
        self.assertEqual(value['source_pins'][0]['source_id'], 3)
        changed = copy.deepcopy(value)
        changed['source_pins'][0]['input_bytes'] = 201
        with self.assertRaisesRegex(RuntimeError, 'scope or totals'):
            F.corpus_descriptor(changed)
        changed = copy.deepcopy(value)
        changed['source_pins'][0]['source_id'] = 4
        with self.assertRaisesRegex(RuntimeError, 'scope or totals'):
            F.corpus_descriptor(changed)

    def test_artifact_adapter_rejects_partial_acceptance_digest_or_source_set(self):
        artifact = accepted_artifact()
        for mutate in (
                lambda value: value['result'].update(status='verified'),
                lambda value: value['result'].update(generation_id=104),
                lambda value: value['qualification']['manifest']['checks'].update(search_quality='FAIL'),
                lambda value: value['checkpoint']['build'].update(source_watermark_sha256='b'*64),
                lambda value: value['verification'].update(generation_root_sha256='b'*64)):
            changed = copy.deepcopy(artifact)
            mutate(changed)
            with patch.object(F, 'read_private', return_value=(changed, 'a'*64)):
                with self.assertRaisesRegex(RuntimeError, 'matching source proof'):
                    F.qualifications_to_corpus([('fixture.json', 'a'*64)], [3])
        with patch.object(F, 'read_private', return_value=(artifact, 'a'*64)):
            with self.assertRaisesRegex(RuntimeError, 'digest differs'):
                F.qualifications_to_corpus([('fixture.json', 'b'*64)], [3])
            with self.assertRaisesRegex(RuntimeError, 'exact expected source set'):
                F.qualifications_to_corpus([('fixture.json', 'a'*64)], [4])
            with self.assertRaisesRegex(RuntimeError, 'exact expected source set'):
                F.qualifications_to_corpus([('fixture.json', 'a'*64)]*2, [3, 3])

    def test_frozen_cut_bytes_and_fixture_must_match_actual_producer_metrics(self):
        artifact = accepted_artifact()
        artifact['checkpoint']['build']['filesystem_cut'] = dict(input_bytes=201,
            fixture_sha256='f'*64, item_count=2)
        with patch.object(F, 'read_private', return_value=(artifact, 'a'*64)):
            with self.assertRaisesRegex(RuntimeError, 'frozen input byte coverage'):
                F.qualifications_to_corpus([('fixture.json', 'a'*64)], [3])

    def test_toast_indexes_are_counted_once_and_unknown_objects_not_omitted(self):
        groups = F.summarize_database(observed())
        canonical = groups['canonical_body_pack_catalog']
        self.assertEqual(canonical['heap_and_auxiliary_bytes'], 100)
        self.assertEqual(canonical['toast_including_indexes_bytes'], 50)
        self.assertEqual(canonical['parent_indexes_bytes'], 20)
        self.assertEqual(sum(row['total_bytes'] for row in groups.values()), 770)
        self.assertEqual(F.category('legacy_hit_mapping'), 'durable_hit_compatibility')
        self.assertEqual(F.category('storage_v2_legacy_rank_payload'),
                         'search_projection_and_reader_metadata')
        self.assertEqual(F.category('storage_v2_inverted_projection'),
                         'search_projection_and_reader_metadata')
        self.assertEqual(F.category('new_unknown_table'), 'other_database_objects')
        self.assertEqual(F.category('content_body', 'unrelated'), 'other_database_objects')

    def test_concurrent_growth_and_duplicate_relation_fail_accounting(self):
        value = observed()
        value['relations'][0]['total_bytes'] += 1
        with self.assertRaisesRegex(RuntimeError, 'inconsistent'):
            F.summarize_database(value)
        value = observed()
        value['relations'].append(value['relations'][0])
        with self.assertRaisesRegex(RuntimeError, 'duplicate'):
            F.summarize_database(value)

    def test_extra_content_is_not_reported_as_same_corpus_savings(self):
        before, after = snapshot(), snapshot()
        after['database']['database_bytes'] = 600
        after['corpus']['newly_preserved_logical_bytes'] += 50
        result = F.compare(before, after)
        self.assertFalse(result['same_declared_corpus'])
        self.assertEqual(result['status'], 'NOT_COMPARABLE_DIFFERENT_COVERAGE')
        self.assertFalse(result['dedup_savings_proven'])
        self.assertFalse(result['physical_reclaim_proven'])
        self.assertEqual(result['observed_database_file_length_reduction'], 400)
        for key in ('retained_corpus_sha256', 'retained_item_count', 'retained_logical_bytes'):
            changed = snapshot()
            changed['corpus'][key] = 'd'*64 if key.endswith('sha256') else 300
            self.assertFalse(F.compare(before, changed)['same_declared_corpus'])

    def test_comparison_keeps_wal_filesystem_activity_and_reduction_separate(self):
        before, after = snapshot(), snapshot()
        after['database']['database_bytes'] = 700
        after['database']['wal_bytes'] = 900
        after['local_storage']['packs'].update(allocated_bytes=150,
                                                filesystem_available_bytes=800)
        result = F.compare(before, after)
        self.assertTrue(result['same_declared_corpus'])
        self.assertEqual(result['observed_wal_bytes_change'], 500)
        self.assertEqual(result['observed_local_storage_deltas']['packs'],
                         dict(allocated_bytes_reduction=50, filesystem_available_bytes_increase=-200))
        self.assertFalse(result['corpus_preservation_independently_verified'])
        self.assertFalse(result['physical_reclaim_proven'])

    def test_changed_database_domain_or_root_identity_cannot_be_compared(self):
        for key, value in [('device', 4), ('inode', 5), ('root', '/other')]:
            changed = snapshot()
            changed['local_storage']['packs'][key] = value
            with self.assertRaisesRegex(RuntimeError, 'identity changed'):
                F.compare(snapshot(), changed)
        changed = snapshot()
        changed['database']['database_oid'] = 2
        with self.assertRaisesRegex(RuntimeError, 'identity changed'):
            F.compare(snapshot(), changed)
        changed = snapshot()
        changed['database']['system_identifier'] = '456'
        with self.assertRaisesRegex(RuntimeError, 'identity changed'):
            F.compare(snapshot(), changed)
        changed = snapshot()
        changed['measurement_contract_sha256'] = 'f'*64
        with self.assertRaisesRegex(RuntimeError, 'contract changed'):
            F.compare(snapshot(), changed)
        changed = snapshot()
        changed['local_storage']['qdrant'] = copy.deepcopy(changed['local_storage']['packs'])
        with self.assertRaisesRegex(RuntimeError, 'domains changed'):
            F.compare(snapshot(), changed)

    def test_nested_roots_reject_double_counting(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            child = root/'snapshots'
            child.mkdir()
            with self.assertRaisesRegex(RuntimeError, 'overlap'):
                F.validate_roots({'qdrant': root, 'snapshots': child})
            with self.assertRaisesRegex(RuntimeError, 'overlap'):
                F.validate_roots({'first': root, 'second': root})

    def test_database_observation_has_deadline_and_readonly_transaction(self):
        result = SimpleNamespace(returncode=0, stdout=json.dumps(observed()))
        with patch.object(F.subprocess, 'run', return_value=result) as invoke:
            self.assertEqual(F.observe_database('fixture', True), observed())
        command = invoke.call_args.args[0]
        kwargs = invoke.call_args.kwargs
        self.assertEqual(command[:4], ['sudo', '-n', '-u', 'postgres'])
        self.assertIn('BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY', kwargs['input'])
        self.assertIn('default_transaction_read_only=on', kwargs['env']['PGOPTIONS'])
        self.assertEqual(kwargs['timeout'], 40)
        with patch.object(F.subprocess, 'run', return_value=SimpleNamespace(returncode=1)):
            with self.assertRaisesRegex(RuntimeError, 'no absence'):
                F.observe_database('fixture', False)
        with self.assertRaisesRegex(RuntimeError, 'local database'):
            F.observe_database('fixture;DROP', False)

    def test_private_descriptor_rejects_boolean_count_and_unproven_identity(self):
        changed = corpus()
        changed['retained_item_count'] = True
        with self.assertRaisesRegex(RuntimeError, 'invalid corpus'):
            F.corpus_descriptor(changed)
        changed = corpus()
        changed['retained_corpus_sha256'] = None
        with self.assertRaisesRegex(RuntimeError, 'invalid corpus'):
            F.corpus_descriptor(changed)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'descriptor.json'
            path.write_text(json.dumps(corpus()))
            path.chmod(0o644)
            with self.assertRaisesRegex(RuntimeError, 'private'):
                F.read_private(path)


class DatabaseAllocationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.data = self.root/'pgdata'
        self.base = self.data/'base'/'7'
        self.base.mkdir(parents=True)
        (self.data/'global').mkdir()
        names = ['11','11.1','11_fsm','11_vm.2','12','13','14']
        for name in names:
            (self.base/name).write_bytes(b'x'*4096)
        (self.data/'global'/'99').write_bytes(b'x'*4096)
        relations = [dict(oid=n,kind=kind,filepath='base/7/'+str(n),
            owner_name='storage_v2_search_document',owner_schema='public') for n,kind in ((11,'r'),(12,'t'),(13,'i'),(14,'i'))]
        relations.append(dict(oid=99,kind='r',filepath='global/99',owner_name='pg_database',owner_schema='pg_catalog'))
        for relation in relations:
            relation['fork_lengths'] = (dict(main=8192, fsm=4096, vm=4096, init=0)
                if relation['oid'] == 11 else dict(main=4096, fsm=0, vm=0, init=0))
        self.observed = dict(database_oid=7,building_run_count=0,active_backend_count=0,
            physical_storage=dict(data_directory=str(self.data),server_major='18',tablespaces=[],relations=relations))

    def test_heap_toast_indexes_segments_forks_use_actual_blocks_once_without_body_reads(self):
        with patch.object(Path, 'read_bytes', side_effect=AssertionError('body read forbidden')):
            value = F.database_allocated_blocks(self.observed)
        self.assertTrue(value['complete'])
        self.assertFalse(value['exclusive_bytes_proven'])
        self.assertFalse(value['physical_reclaim_proven'])
        files = [*self.base.iterdir(),self.data/'global'/'99']
        self.assertEqual(value['allocated_file_bytes'],sum(path.stat().st_blocks*512 for path in files))
        self.assertEqual(value['file_length_bytes'],sum(path.stat().st_size for path in files))
        self.assertEqual(len(value['records']),8)
        forks = {(r['fork'],r['segment']) for r in value['records'] if r['relation_oid']==11}
        self.assertEqual(forks,{('main',0),('main',1),('fsm',0),('vm',2)})
        self.assertEqual(value['shared_cluster_catalog_allocated_bytes'],(self.data/'global'/'99').stat().st_blocks*512)
        self.assertEqual(value['canonical_content_bytes_read'],0)

    def test_unmapped_missing_toast_symlink_or_active_writer_never_complete(self):
        (self.base/'orphan').write_bytes(b'x'*4096)
        (self.base/'12').unlink()
        (self.base/'11.1').unlink()
        (self.base/'unreviewed_link').symlink_to(self.base/'11')
        self.observed['building_run_count']=1
        value=F.database_allocated_blocks(self.observed)
        self.assertFalse(value['complete'])
        reasons={item['reason'] for item in value['unknown']}
        self.assertIn('unmapped_database_file',reasons)
        self.assertIn('missing_relation_toast_or_index_main_file',reasons)
        self.assertIn('relation_fork_lengths_changed_or_incomplete',reasons)
        self.assertIn('symlink_directory_special_or_mounted_file',reasons)
        self.assertIn('database_writers_or_active_backends_not_frozen',reasons)

    def test_hardlinks_are_counted_once_and_external_alias_is_unknown(self):
        (self.base/'12').unlink()
        import os
        os.link(self.base/'11',self.base/'12')
        value=F.database_allocated_blocks(self.observed)
        self.assertFalse(value['complete'])
        self.assertEqual(len(value['records']),7)
        self.assertIn('hardlink_crosses_relation_or_category',{r['reason'] for r in value['unknown']})
        os.link(self.base/'13',self.root/'outside-census')
        value=F.database_allocated_blocks(self.observed)
        self.assertIn('hardlink_outside_census',{r['reason'] for r in value['unknown']})

    def test_tablespace_metadata_link_is_bound_and_drift_rejected(self):
        space=self.root/'tablespace';directory=space/'PG_18_202601001'/'7';directory.mkdir(parents=True)
        (directory/'55').write_bytes(b'x'*4096)
        links=self.data/'pg_tblspc';links.mkdir();link=links/'9999';link.symlink_to(space,target_is_directory=True)
        meta=self.observed['physical_storage'];meta['tablespaces']=[dict(oid=9999,location=str(space))]
        meta['relations'].append(dict(oid=55,kind='r',filepath='pg_tblspc/9999/PG_18_202601001/7/55',
            owner_name='content_body',owner_schema='public',
            fork_lengths=dict(main=4096, fsm=0, vm=0, init=0)))
        self.assertTrue(F.database_allocated_blocks(self.observed)['complete'])
        import os
        real_scandir=os.scandir
        replacement=self.root/'different';replacement.mkdir()
        def changed(path):
            iterator=real_scandir(path)
            if Path(path)==directory:
                link.unlink();link.symlink_to(replacement,target_is_directory=True)
            return iterator
        with patch.object(F.os,'scandir',side_effect=changed):
            value=F.database_allocated_blocks(self.observed)
        self.assertFalse(value['complete'])
        self.assertIn('tablespace_link_changed',{r['reason'] for r in value['unknown']})

    def test_opt_in_sql_is_metadata_only_and_missing_metadata_or_bound_stays_unknown(self):
        result=SimpleNamespace(returncode=0,stdout=json.dumps(self.observed))
        with patch.object(F.subprocess,'run',return_value=result) as invoke:
            F.observe_database('fixture',True,allocated_blocks=True)
        sql=invoke.call_args.kwargs['input']
        self.assertIn("'physical_storage'",sql)
        self.assertIn('pg_relation_filepath',sql)
        self.assertIn('pg_tablespace_location',sql)
        self.assertFalse(F.database_allocated_blocks({})['complete'])
        self.assertFalse(F.database_allocated_blocks(self.observed,maximum_entries=1)['complete'])


if __name__ == '__main__':
    unittest.main()

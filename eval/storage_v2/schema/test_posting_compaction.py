"""Lossless compaction, atomic visibility, scope and durable retry invariants."""
from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from eval.storage_v2.schema import test_compact_exact_postings as base
from eval.storage_v2.schema.presence_reader_fixture import (
    register_metadata_role_cleanup, register_presence_role_cleanup,
)

MIGRATION = base.schema.ROOT / 'migrations/157_storage_v2_lossless_posting_compaction.sql'
SPEC = importlib.util.spec_from_file_location('posting_compaction_fixture',
    base.schema.ROOT / 'ops/storage-v2/posting-compaction.py')
OP = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = OP
SPEC.loader.exec_module(OP)


class PostingCompactionTests(unittest.TestCase):
    schema = base.schema
    command = classmethod(base.CompactExactPostingTests.command.__func__)
    sql = classmethod(base.CompactExactPostingTests.sql.__func__)
    file = classmethod(base.CompactExactPostingTests.file.__func__)
    @staticmethod
    def actor(user_id, statement):
        return f"SET ROLE mainrag; SET app.user_id='{user_id}';" + statement

    @classmethod
    def admin(cls, statement):
        return cls.actor(cls.schema.ADMIN_ID, statement)
    quote = staticmethod(base.CompactExactPostingTests.quote)
    make_projection = base.CompactExactPostingTests.make_projection
    begin = base.CompactExactPostingTests.begin
    stage = base.CompactExactPostingTests.stage
    complete_analysis = base.CompactExactPostingTests.complete_analysis
    commit = base.CompactExactPostingTests.commit
    assert_sql_fails = base.CompactExactPostingTests.assert_sql_fails

    @classmethod
    def setUpClass(cls):
        try:
            base.CompactExactPostingTests.setUpClass.__func__(cls)
            for number in range(99, 157):
                if number == 123:
                    for signature in ('storage_v2_search_exact(bigint,text,jsonb,jsonb,bigint)',
                        'storage_v2_search_active_unchecked(text,jsonb,jsonb,bigint,bigint,boolean)'):
                        cls.sql(f'REVOKE ALL ON FUNCTION {signature} FROM storage_v2_shadow_worker')
                if number == 135:
                    cls.sql('REVOKE ALL ON FUNCTION storage_v2_require_complete_active_set(TEXT) FROM storage_v2_shadow_worker')
                if number == 145:
                    for signature in ('storage_v2_verify_generation(bigint,text)',
                                      'storage_v2_requalify_generation(bigint,text)'):
                        cls.sql(f'REVOKE ALL ON FUNCTION {signature} FROM storage_v2_shadow_worker')
                cls.file(next(MIGRATION.parent.glob(f'{number:03}_*.sql')))
                if number == 142:
                    register_presence_role_cleanup(cls.stack, cls.socket)
                if number == 145:
                    register_metadata_role_cleanup(cls.stack, cls.socket)
        except BaseException:
            if hasattr(cls, 'database'):
                base.CompactExactPostingTests.tearDownClass.__func__(cls)
            elif hasattr(cls, 'stack'):
                cls.stack.close()
            raise

    @classmethod
    def tearDownClass(cls):
        base.CompactExactPostingTests.tearDownClass.__func__(cls)

    def operator_sql(self, statement):
        return f"SET app.user_id='{self.schema.ADMIN_ID}';" + statement

    def readers(self, document, terms):
        array = 'ARRAY[' + ','.join(self.quote(v) for v in terms) + ']::TEXT[]'
        statements = {
            'point': f'SELECT term,term_frequency FROM unnest({array}) t(value) CROSS JOIN LATERAL storage_v2_document_posting({document},t.value)',
            'cached': f'SELECT term,term_frequency FROM unnest({array}) t(value) CROSS JOIN LATERAL storage_v2_cached_document_posting({document},t.value)',
            'scoped_term': f'SELECT term,term_frequency FROM unnest({array}) t(value) CROSS JOIN LATERAL storage_v2_scoped_term_posting(ARRAY[{document},{document},NULL]::BIGINT[],t.value)',
            'scoped_query': f'SELECT term,term_frequency FROM storage_v2_scoped_query_posting(array_fill({document}::BIGINT,ARRAY[1025]),{array})',
            'probe': f'SELECT term,term_frequency FROM unnest({array}) t(value) CROSS JOIN LATERAL storage_v2_posting_probe(t.value,4097) p WHERE p.document_id={document}',
        }
        result = {name: self.sql(self.admin("SELECT coalesce(jsonb_agg(to_jsonb(p) ORDER BY term,term_frequency),'[]'::JSONB) FROM (" + query + ') p'))
                  for name, query in statements.items()}
        result['all'] = self.sql(self.admin("SELECT coalesce(jsonb_agg(jsonb_build_array(term,frequency) ORDER BY term COLLATE \"C\"),'[]'::JSONB) FROM ("
            f'SELECT term,term_frequency AS frequency FROM storage_v2_search_posting WHERE document_id={document}) p'))
        result['identifiers'] = self.sql(self.admin(f'SELECT to_json(storage_v2_document_word_identifiers({document}))'))
        return result

    def test_complete_codec_visibility_resume_and_guard_block(self):
        fingerprints = {}
        for i in range(20000):
            value = 'collision_' + str(i)
            fingerprint = hashlib.sha256(value.encode()).digest()[:2]
            if fingerprint in fingerprints:
                collision = fingerprints[fingerprint], value
                break
            fingerprints[fingerprint] = value
        else:
            self.fail('public fingerprint collision is missing')
        text = collision[0] + ' ' + ' '.join(f'key_{i:04}' for i in range(600)) + " alpha alpha alpha api::call() quote's::value() 日本語9 " + 'long_' + 'é' * 8192
        node, view, digest = self.make_projection(text)
        document = int(self.sql(self.admin('SELECT id FROM storage_v2_put_search_document('
            f"'mainrag.lexical-simple.v1','node',{node},{self.quote(text)},ARRAY[]::TEXT[])")))
        self.sql(self.admin(f'SELECT storage_v2_bind_search_document({view},0,{document},1.0)'))
        run = self.begin(6, '75' * 32, '76' * 32, commit_sha='a' * 40)
        self.stage(run, 'posting.txt', text, node, view, digest)
        self.complete_analysis(digest)
        self.commit(run, 1)
        generation = int(self.sql(f'SELECT generation_id FROM storage_v2_ingest_run WHERE id={run}'))
        self.sql(self.admin(f"SELECT storage_v2_verify_generation({generation},repeat('c',64))"))
        # Only this disposable database prepares the retained historical layout;
        # all source pairs are copied exactly before controlled guards resume.
        self.sql(f"""BEGIN;
            ALTER TABLE storage_v2_search_posting DISABLE TRIGGER storage_v2_search_posting_sealed;
            ALTER TABLE storage_v2_compact_posting_block DISABLE TRIGGER storage_v2_compact_posting_immutable;
            INSERT INTO storage_v2_search_posting(document_id,term,term_frequency)
            SELECT {document},term,CASE WHEN term='alpha' THEN 1099511627776 ELSE frequency END
              FROM storage_v2_compact_posting_block b CROSS JOIN LATERAL
                   unnest(b.terms,b.term_frequencies) t(term,frequency) WHERE b.document_id={document};
            DELETE FROM storage_v2_compact_posting_block WHERE document_id={document};
            ALTER TABLE storage_v2_search_posting ENABLE TRIGGER storage_v2_search_posting_sealed;
            ALTER TABLE storage_v2_compact_posting_block ENABLE TRIGGER storage_v2_compact_posting_immutable;
            COMMIT;""")
        terms = [collision[0], collision[1], 'alpha', 'key_0042', 'api::call()', "quote's::value()", '日本語9', 'missing']
        expected = self.readers(document, terms)
        original_pairs = expected.pop('all')
        original_document = self.sql(f'SELECT to_jsonb(d) FROM storage_v2_search_document d WHERE id={document}')
        generations = self.sql('SELECT jsonb_agg(to_jsonb(g) ORDER BY id) FROM source_generation g')
        identity = self.sql(f"SELECT encode(materialization_sha256,'hex') FROM storage_v2_search_document WHERE id={document}")
        manifest = 'd7' * 32
        decoded_manifest = f"decode('{manifest}','hex')"
        body = MIGRATION.read_text().replace('BEGIN;', '', 1).rsplit('COMMIT;', 1)[0]
        self.assert_sql_fails('BEGIN; ALTER FUNCTION storage_v2_document_posting(BIGINT,TEXT) OWNER TO storage_v2_shadow_worker;' + body + 'ROLLBACK;', 'predecessor authority differs')
        self.assert_sql_fails('BEGIN; CREATE VIEW unsafe_posting_reader AS SELECT * FROM storage_v2_search_posting;' + body + 'ROLLBACK;', 'unreviewed physical view')
        self.file(MIGRATION)
        prepare = f"SELECT (storage_v2_prepare_posting_conversion({document},decode('{identity}','hex'),{decoded_manifest})).document_id"
        self.assert_sql_fails(self.admin(prepare), 'permission denied')
        self.assert_sql_fails(f"SET app.user_id='{self.schema.OTHER_ID}';" + prepare, 'administrative operator')
        self.sql(self.operator_sql(prepare))
        self.sql(self.operator_sql(prepare))  # Idempotent same-manifest prepare.
        self.assert_sql_fails(self.operator_sql(prepare.replace(manifest, 'e7' * 32)), 'different manifest')
        for view_name in ('storage_v2_visible_flat_posting', 'storage_v2_visible_compact_posting', 'storage_v2_posting_block_all'):
            self.assert_sql_fails('BEGIN; ALTER VIEW ' + view_name + ' SET (security_invoker=false);' + self.operator_sql(
                'SELECT storage_v2_posting_conversion_contract_sha256()') + ';ROLLBACK;', 'relation/view/trigger identity changed')
        self.assert_sql_fails('BEGIN; GRANT EXECUTE ON FUNCTION storage_v2_document_posting(BIGINT,TEXT) TO storage_v2_shadow_worker;' +
            self.operator_sql('SELECT storage_v2_posting_conversion_contract_sha256()') + ';ROLLBACK;', 'reader/guard identity changed')
        self.assert_sql_fails(self.operator_sql(f'SELECT * FROM storage_v2_copy_posting_conversion_batch({document},{decoded_manifest},0,NULL,1)'), 'single term exceeds')
        first = json.loads(self.sql(self.operator_sql(f'SELECT to_jsonb(r) FROM storage_v2_copy_posting_conversion_batch({document},{decoded_manifest},0,NULL,67108864) r')))
        self.assertEqual(first['term_count'], 256)
        self.assertEqual(json.loads(self.sql(self.operator_sql(f'SELECT to_jsonb(r) FROM storage_v2_copy_posting_conversion_batch({document},{decoded_manifest},0,NULL,67108864) r'))), first)
        current = self.readers(document, terms)
        current.pop('all')
        self.assertEqual(current, expected)
        self.assertEqual(self.sql(self.admin(f'SELECT count(*) FROM storage_v2_posting_block_all WHERE document_id={document}')), '0')
        self.assert_sql_fails(self.operator_sql(f"INSERT INTO storage_v2_compact_posting_block VALUES({document},999,ARRAY['forged'],ARRAY[1::BIGINT],DEFAULT)"), 'sealed search-document postings are immutable')
        self.assert_sql_fails(self.operator_sql(f"SELECT storage_v2_publish_posting_conversion({document},{decoded_manifest},256,decode(repeat('a',64),'hex'))"), 'publication proof differs')
        database = OP.Database(self.database, self.schema.ADMIN_ID, socket=self.socket)
        events = []
        operator = OP.PostingConversion(database, manifest, identity_gate=lambda *a: None,
            resource_gate=lambda *a: None, acceptance_gate=lambda *a: None,
            durable_event=events.append, max_batch_bytes=65536)
        pin = OP.DocumentPin(document, identity)
        state = operator.advance(pin)
        self.assertTrue(state['complete'])
        self.assertGreater(state['posting_count'], 600)
        self.assertEqual(events[-1]['status'], 'COMMITTED')
        self.assertIsNone(events[-1]['physical_reclaim_bytes'])
        self.assertEqual(operator.advance(pin), state)
        published = operator.publish(pin)
        self.assertEqual(published['phase'], 'COMPACT')
        current = self.readers(document, terms)
        current.pop('all')
        self.assertEqual(current, expected)
        target_pairs = self.sql(self.admin("SELECT jsonb_agg(jsonb_build_array(term,frequency) ORDER BY term COLLATE \"C\") FROM "
            f'storage_v2_complete_document_posting_blocks({document}) b CROSS JOIN LATERAL unnest(b.terms,b.term_frequencies) t(term,frequency)'))
        self.assertEqual(target_pairs, original_pairs)
        self.assertEqual(self.sql(self.admin(f"SELECT term_frequency FROM storage_v2_document_posting({document},'alpha')")), '1099511627776')
        named = f"SELECT storage_v2_search_exact(6,'1','{{\"type\":\"term\",\"value\":\"alpha\"}}','{{}}',10)"
        self.assert_sql_fails(self.actor(self.schema.OTHER_ID, named), 'authorized generation selector')
        self.assertEqual(operator.restore_flat_visibility(pin)['phase'], 'FLAT')
        current = self.readers(document, terms)
        current.pop('all')
        self.assertEqual(current, expected)
        self.assertEqual(operator.publish(pin)['phase'], 'COMPACT')
        self.assertEqual(original_document, self.sql(f'SELECT to_jsonb(d) FROM storage_v2_search_document d WHERE id={document}'))
        self.assertEqual(generations, self.sql('SELECT jsonb_agg(to_jsonb(g) ORDER BY id) FROM source_generation g'))
        retire = f"SELECT storage_v2_retire_converted_flat_postings({decoded_manifest},decode(repeat('b',64),'hex'),1,{state['posting_count']},"
        complete_set = self.sql("SELECT encode(sha256(convert_to(string_agg(document_id::text||':'||posting_count::text||':'||encode(pairs_sha256,'hex'),E'\\n' ORDER BY document_id),'UTF8')),'hex') FROM storage_v2_posting_conversion")
        retire += f"decode('{complete_set}','hex'))"
        operator.restore_flat_visibility(pin)
        self.assert_sql_fails(self.operator_sql(retire), 'unconverted owners')
        operator.publish(pin)
        result = json.loads(self.sql(self.operator_sql('SELECT to_jsonb(r) FROM (' + retire + ') x(r)')))
        self.assertEqual(result['posting_count'], state['posting_count'])
        self.assertEqual(self.sql('SELECT count(*) FROM storage_v2_search_posting'), '0')
        self.assert_sql_fails(self.operator_sql(f'SELECT storage_v2_restore_flat_posting_visibility({document},{decoded_manifest})'), 'no longer be restored')
        self.assertEqual(target_pairs, self.sql(self.admin("SELECT jsonb_agg(jsonb_build_array(term,frequency) ORDER BY term COLLATE \"C\") FROM "
            f'storage_v2_complete_document_posting_blocks({document}) b CROSS JOIN LATERAL unnest(b.terms,b.term_frequencies) t(term,frequency)')))
        # Exercise the real integrity observer SQL against this owned disposable
        # database. The production subprocess is replaced before it can run.
        integrity = OP.load_module('posting_integrity_observer_fixture',
                                   MIGRATION.parent.parent / 'ops/storage-v2/integrity_resume.py')
        real_run = subprocess.run
        def observe_fixture(command, **options):
            self.assertEqual(command[:4], ['sudo', '-n', '-u', 'postgres'])
            statement = command[command.index('-c') + 1]
            return real_run(['psql', '-X', '-qAt', '-v', 'ON_ERROR_STOP=1',
                '--host', str(self.socket), '--dbname', self.database, '-c', statement],
                capture_output=True, text=True, timeout=options['timeout'])
        with patch.object(integrity.subprocess, 'run', side_effect=observe_fixture):
            observed = integrity.observe_immutable_identity(6, generation)
        admission = observed['posting_compaction_admission']
        self.assertEqual(admission['validated_guard_sha256'],
                         observed['function_identities']['storage_v2_posting_conversion_require_operator()'])
        self.assertRegex(admission['contract_sha256'], r'^[0-9a-f]{64}$')
        self.assertEqual(observed['identity']['generation_id'], generation)
        print('PASS: bounded UTF8/BIGINT pair parity, staging invisibility, seven readers, committed resume, '
              'publication/rollback, RLS, catalog drift and exact retirement prerequisites', flush=True)


class PostingCompactionOperatorTests(unittest.TestCase):
    def test_status_creates_no_lock_or_event_and_queries_only_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = root / 'manifest.json'
            value = dict(schema_version='mainrag.storage-v2.posting-compaction.v1',
                authority='public fixture', preserve_all_generations=True,
                documents=[dict(document_id=1, materialization_sha256='a' * 64)],
                max_batch_bytes=1, maximum_batch_growth_bytes=1, minimum_free_bytes=OP.MINIMUM_RESERVE)
            for field in ('operator_sha256', 'runtime_binary_sha256', 'generation_state_sha256',
                          'pointer_state_sha256', 'catalog_contract_sha256'):
                value[field] = 'a' * 64
            manifest.write_text(json.dumps(value))
            manifest.chmod(0o600)
            before = list(root.iterdir())
            with patch.object(OP, 'Database') as database, patch.object(OP, 'operator_sha', return_value='a' * 64), \
                    patch.object(sys, 'argv', ['posting-compaction', 'status', '--manifest', str(manifest),
                        '--database', 'public_fixture', '--user-id', base.schema.ADMIN_ID]), redirect_stdout(io.StringIO()):
                database.return_value.query.return_value = []
                OP.main()
                self.assertEqual(database.return_value.query.call_count, 1)
                self.assertNotIn('readonly', database.return_value.query.call_args.kwargs)
                self.assertIn('FROM storage_v2_posting_conversion', database.return_value.query.call_args.args[0])
            self.assertEqual(list(root.iterdir()), before)

    def test_private_inputs_reject_symlinks_permissions_and_duplicate_keys(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / 'manifest.json'
            path.write_text('{"a":1}')
            path.chmod(0o600)
            self.assertEqual(OP.private_read(path)[0], {'a': 1})
            link = root / 'linked.json'
            link.symlink_to(path)
            with self.assertRaises(OSError):
                OP.private_read(link)
            path.chmod(0o644)
            with self.assertRaises(RuntimeError):
                OP.private_read(path)
            path.chmod(0o600)
            path.write_text('{"a":1,"a":2}')
            with self.assertRaises(ValueError):
                OP.private_read(path)

    def test_runtime_is_bound_to_the_actual_live_executable(self):
        binary = Path('/proc/self/exe').resolve()
        identity = OP.runtime_identity(os.getpid(), binary)
        self.assertEqual(identity[0], os.getpid())
        with self.assertRaises(RuntimeError):
            OP.runtime_identity(os.getpid(), Path(__file__))
        with self.assertRaises(ValueError):
            OP.DocumentPin(True, 'a' * 64)


if __name__ == '__main__':
    unittest.main()

"""One exact inert-run cache transition, net-cost refusal and logical parity.

The owned PostgreSQL fixture uses real constructors, restored original138
identifier arrays, unchanged161 guards and the actual current reader routes.
"""
from __future__ import annotations

import hashlib
import json
import string
import threading
import time
import unittest
import uuid

from eval.storage_v2.schema import test_retained_posting_tokenizer_parity as previous
from eval.storage_v2.schema.local_operator_fixture import command as local_command

ROOT = previous.ROOT
MIGRATION = ROOT / 'migrations/166_storage_v2_retained_document_cache_admission.sql'
OLD = 'storage_v2_convert_document_caches(uuid,bytea,bigint[],bytea[],bigint,integer)'
NEW = 'storage_v2_convert_retained_document_caches(uuid,bytea,bigint[],bytea[],bigint,integer,bigint,bytea)'


class RetainedDocumentCacheAdmissionTests(unittest.TestCase):
    base = previous.RetainedPostingTokenizerParityTests.base
    schema = base.schema
    command = classmethod(local_command)
    sql = classmethod(base.sql.__func__)
    file = classmethod(base.file.__func__)
    actor = staticmethod(base.actor)
    admin = classmethod(base.admin.__func__)
    quote = staticmethod(base.quote)
    make_projection = base.make_projection
    begin = base.begin
    stage = base.stage
    complete_analysis = base.complete_analysis
    commit = base.commit
    assert_sql_fails = base.assert_sql_fails
    retained = previous.RetainedPostingTokenizerParityTests.retained
    rank = previous.RetainedPostingTokenizerParityTests.rank
    envelope = previous.RetainedPostingTokenizerParityTests.envelope
    operator = previous.RetainedPostingTokenizerParityTests.operator
    authority = classmethod(previous.RetainedPostingTokenizerParityTests.authority.__func__)
    operation = '00000000-0000-0000-0000-000000000166'
    manifest = 'd6' * 32

    @classmethod
    def setUpClass(cls):
        previous.RetainedPostingTokenizerParityTests.setUpClass.__func__(cls)
        try:
            cls.old_definition = cls.sql(f"SELECT pg_get_functiondef('{OLD}'::REGPROCEDURE)")
            cls.sql(f"SET app.user_id='{cls.schema.ADMIN_ID}';" + MIGRATION.read_text())
        except BaseException:
            cls.base.tearDownClass.__func__(cls)
            raise

    @classmethod
    def tearDownClass(cls):
        cls.base.tearDownClass.__func__(cls)

    def fixture(self, content):
        # Pass the complete original word-ID grammar to the real constructor.
        # SQL138's supported restoration then stores its old equivalent array;
        # no fixture guard, document identity or seal is disabled or forged.
        token = uuid.uuid4().hex
        source = int(self.sql('INSERT INTO sources(id,name,type,path) '
            f"SELECT max(id)+1,'retained-cache-{token}','fixture','synthetic-cache' FROM sources RETURNING id"))
        node, view, digest = self.make_projection(content)
        run = self.begin(source, token * 2, uuid.uuid4().hex * 2)
        self.stage(run, 'cache.txt', content, node, view, digest)
        self.complete_analysis(digest)
        exact = ("ARRAY(SELECT DISTINCT token FROM regexp_split_to_table(lower(" + self.quote(content) +
                 "),'[^[:alnum:]_]+') token WHERE token<>'' AND token ~ '[_0-9]' ORDER BY token)")
        document = int(self.sql(self.admin('SELECT id FROM storage_v2_put_search_document('
            f"'mainrag.lexical-simple.v1','node',{node},{self.quote(content)},{exact})")))
        self.sql(self.admin(f'SELECT storage_v2_restore_document_identifiers({document-1},1)'))
        self.sql(self.admin(f'SELECT storage_v2_bind_search_document({view},0,{document},1.0)'))
        self.commit(run, 1)
        return tuple(map(int, self.sql('SELECT id::TEXT||\':\'||artifact_version_id::TEXT FROM occurrence '
                                     f'WHERE source_id={source}').split(':')))

    def call(self, rows, *, maximum=8388608, budget=10000, extra='', operation=None, identity=None):
        documents = ','.join(str(r['document']) for r in rows)
        materializations = ','.join("decode('" + (identity or r['identity']) + "','hex')" for r in rows)
        return ("SELECT coalesce(json_agg(r),'[]'::JSON) FROM storage_v2_convert_retained_document_caches("
                f"'{operation or self.operation}',decode('{self.manifest}','hex'),ARRAY[{documents}]::BIGINT[],"
                f'ARRAY[{materializations}]::BYTEA[],{maximum},{budget}{extra}) r')

    def apply(self, rows, **kwargs):
        return json.loads(self.sql(self.operator(self.call(rows, **kwargs))))

    def payload(self, doc):
        return self.sql(f"SET app.user_id='{self.schema.ADMIN_ID}';"
                        f"SELECT encode(storage_v2_retained_document_cache_payload_sha256({doc}),'hex')")

    def original_unsealed(self, doc):
        # Recreate only this owned historical pre138 input. The normal seal
        # guard is restored before166 or any reader/authority assertion runs.
        self.sql('BEGIN;ALTER TABLE storage_v2_document_postings_seal DISABLE TRIGGER document_postings_seal_immutable;'
                 f'DELETE FROM storage_v2_document_postings_seal WHERE document_id={doc};'
                 'ALTER TABLE storage_v2_document_postings_seal ENABLE TRIGGER document_postings_seal_immutable;COMMIT;')

    def invariant(self, doc, occurrence):
        return self.sql(f'''SELECT jsonb_build_object(
            'document',to_jsonb(d)-ARRAY['fts_simple','fts_simple_derived','fts_simple_fingerprints',
                                       'exact_identifiers','exact_identifiers_derived'],
            'occurrence',to_jsonb(o),'seal',(SELECT to_jsonb(s) FROM storage_v2_document_postings_seal s
                WHERE s.document_id=d.id),
            'generations',(SELECT jsonb_agg(to_jsonb(g) ORDER BY id) FROM source_generation g
                WHERE g.source_id=o.source_id),
            'runs',(SELECT jsonb_agg(to_jsonb(r) ORDER BY id) FROM storage_v2_ingest_run r
                WHERE r.source_id=o.source_id),
            'bindings',(SELECT jsonb_agg(to_jsonb(b) ORDER BY ordinal) FROM storage_v2_search_view_document b
                WHERE b.view_id=o.view_id))::TEXT
            FROM storage_v2_search_document d CROSS JOIN occurrence o WHERE d.id={doc} AND o.id={occurrence}''')

    def snapshots(self, row, occurrence):
        doc = row['document']
        queries = ('alpha', 'alpha beta', 'alpha -beta', 'alpha -missing', '"alpha beta"')
        ranks = {q: self.rank(occurrence, q) for q in queries}
        identifiers = self.sql(self.admin(f'SELECT to_json(storage_v2_document_exact_identifiers({doc}))'))
        key = json.loads(identifiers)[0]
        asts = ({'type': 'phrase', 'value': 'alpha beta'}, {'type': 'exact', 'value': key},
                {'type': 'and', 'children': [{'type': 'term', 'value': 'alpha'},
                                             {'type': 'exact', 'value': key}]})
        for ast in asts:
            statement = self.actor(self.schema.OTHER_ID,
                f"SELECT storage_v2_search_exact({row['source']},"
                f"(SELECT max(generation_seq)::TEXT FROM source_generation WHERE source_id={row['source']}),"
                f"{self.quote(json.dumps(ast))}::JSONB,'{{}}',10)")
            self.assert_sql_fails(statement, 'authorized generation selector required')
        return dict(ranks=ranks, identifiers=identifiers,
                    envelopes=[self.envelope(row['source'], ast) for ast in asts],
                    hidden=[self.rank(occurrence, q, actor=self.schema.OTHER_ID) for q in queries])

    def test_named_frozen_run_parity_cost_admission_replay_authority_writer_and_rollback(self):
        self.assertEqual(self.old_definition, self.sql(f"SELECT pg_get_functiondef('{OLD}'::REGPROCEDURE)"))
        words = ['alpha', 'beta', 'gamma'] + ['plain' + c for c in string.ascii_lowercase[:21]]
        paragraphs = [' '.join(sorted(words, key=lambda word: hashlib.sha256(f'{n}:{word}'.encode()).digest()))
                      for n in range(240)]
        keys = ['key_' + ''.join(hashlib.sha256(f'{n}:{piece}'.encode()).hexdigest() for piece in range(8))
                for n in range(10)]
        text = 'alpha beta 日本語 ' + '\n'.join(paragraphs) + '\n' + ' '.join(keys)
        row, occurrence = self.retained(text)
        doc = row['document']
        generation = int(self.sql('SELECT r.generation_id FROM storage_v2_ingest_run_item item '
            f'JOIN storage_v2_ingest_run r ON r.id=item.run_id WHERE item.occurrence_id={occurrence} LIMIT 1'))
        self.sql(self.admin(f"SELECT storage_v2_verify_generation({generation},'{ 'a6' * 32}')"))
        self.sql(self.admin(f'SELECT * FROM storage_v2_materialize_reader_metadata({generation})'))
        metadata = self.sql(f'SELECT to_jsonb(h)::TEXT FROM storage_v2_reader_metadata_header h WHERE generation_id={generation}')
        self.assertEqual(self.sql(self.admin(f'SELECT storage_v2_reader_metadata_ready(ARRAY[{generation}]::BIGINT[])')), 't')
        before = self.snapshots(row, occurrence)
        invariant = self.invariant(doc, occurrence)
        original = self.payload(doc)
        self.assertTrue(json.loads(before['ranks']['alpha']))
        self.assertGreater(json.loads(before['envelopes'][1])['total'], 0)
        self.assert_sql_fails(self.admin(self.call([row])), 'permission denied')
        self.assert_sql_fails("SET app.user_id='" + str(self.schema.OTHER_ID) + "';" + self.call([row]), 'administrative operator')
        self.assert_sql_fails(self.operator('SET LOCAL ROLE mainrag;' + self.call([row])), 'permission denied')
        self.assert_sql_fails(self.operator(self.call([row], maximum=1)), 'body-byte admission exceeded')
        self.assert_sql_fails(self.operator(self.call([row], identity='00' * 32)), 'materialization identity changed')
        self.assert_sql_fails(self.operator(self.call([row], budget=100), timeout='1s'), 'prearmed')
        result = self.apply([row])[0]
        self.assertEqual(result['disposition'], 'CONVERTED')
        self.assertEqual(result['original_cache_sha256'], '\\x' + original)
        self.assertLess(result['stored_cache_bytes'] + result['audit_bytes'] + result['fingerprint_index_bytes'],
                        result['original_stored_cache_bytes'])
        self.assertEqual(invariant, self.invariant(doc, occurrence))
        self.assertEqual(before, self.snapshots(row, occurrence))
        self.assertEqual(metadata, self.sql(f'SELECT to_jsonb(h)::TEXT FROM storage_v2_reader_metadata_header h WHERE generation_id={generation}'))
        self.assertEqual(self.sql(self.admin(f'SELECT storage_v2_reader_metadata_ready(ARRAY[{generation}]::BIGINT[])')), 't')
        self.assertEqual(self.sql(f'SELECT exact_identifiers_derived AND cardinality(exact_identifiers)=0 '
                                 f'FROM storage_v2_search_document WHERE id={doc}'), 't')
        self.assertEqual(self.sql('SELECT fts_changed AND identifiers_changed FROM '
                                 f'storage_v2_retained_document_cache_receipt WHERE document_id={doc}'), 't')
        self.assertEqual(self.apply([row])[0]['disposition'], 'REPLAY')
        self.assertEqual(self.payload(doc), result['cache_payload_sha256'][2:])
        self.assert_sql_fails(self.operator(self.call([row], operation='00000000-0000-0000-0000-000000000999')),
                              'exact manifest')
        self.assert_sql_fails(f'DELETE FROM storage_v2_retained_document_cache_receipt WHERE document_id={doc}', 'immutable')
        self.assertEqual(self.sql("SELECT count(*) FROM pg_constraint WHERE conrelid="
            "'storage_v2_retained_document_cache_receipt'::REGCLASS AND contype='f'"), '0')

        # Repeated short identifiers favor identifier-only removal: an added
        # fingerprint index must not hide the real original array saving.
        short_keys = ['key_' + hashlib.sha256(f'short-{n}'.encode()).hexdigest() for n in range(128)]
        fallback, _ = self.retained('alpha beta ' + ' '.join(short_keys))
        self.original_unsealed(fallback['document'])
        fallback_result = self.apply([fallback])[0]
        self.assertEqual(fallback_result['disposition'], 'CONVERTED')
        self.assertEqual(self.sql('SELECT identifiers_changed AND NOT fts_changed AND fingerprint_index_bytes=0 '
            'AND NOT original_seal_present '
            f'FROM storage_v2_retained_document_cache_receipt WHERE document_id={fallback["document"]}'), 't')
        self.assertEqual(self.sql('SELECT r.audit_bytes=pg_column_size(r)+pg_column_size(s)+256 '
            'FROM storage_v2_retained_document_cache_receipt r JOIN storage_v2_document_postings_seal s '
            f'ON s.document_id=r.document_id WHERE r.document_id={fallback["document"]}'), 't')

        tiny, tiny_occurrence = self.retained('alpha beta tiny_key')
        self.original_unsealed(tiny['document'])
        tiny_payload = self.payload(tiny['document'])
        tiny_invariant = self.invariant(tiny['document'], tiny_occurrence)
        for _ in range(2):
            skipped = self.apply([tiny])[0]
            self.assertEqual(skipped['disposition'], 'RETAIN_NO_SAVINGS')
            self.assertEqual(skipped['audit_bytes'], 0)
            self.assertEqual(skipped['fingerprint_index_bytes'], 0)
            self.assertEqual(skipped['stored_cache_bytes'], skipped['original_stored_cache_bytes'])
            self.assertEqual(self.payload(tiny['document']), tiny_payload)
            self.assertEqual(tiny_invariant, self.invariant(tiny['document'], tiny_occurrence))
            self.assertEqual(self.sql('SELECT count(*) FROM storage_v2_retained_document_cache_receipt '
                                     f'WHERE document_id={tiny["document"]}'), '0')
            self.assertEqual(self.sql('SELECT count(*) FROM storage_v2_document_cache_conversion_receipt '
                                     f'WHERE document_id={tiny["document"]}'), '0')
            self.assertEqual(self.sql('SELECT count(*) FROM storage_v2_document_postings_seal '
                                     f'WHERE document_id={tiny["document"]}'), '0')

        unsupported, _ = self.retained('alpha beta unsupported')
        old_arguments = (f"'{self.operation}',decode('{self.manifest}','hex'),ARRAY[{unsupported['document']}]::BIGINT[],"
                         f"ARRAY[decode('{unsupported['identity']}','hex')],8388608,10000")
        # This owned fixture uses the actual original161 path to establish an
        # already-derived empty-ID cache.166 must retain it without new audit.
        self.sql(self.operator(f'SELECT * FROM storage_v2_convert_document_caches({old_arguments})'))
        unsupported_payload = self.payload(unsupported['document'])
        self.assertEqual(self.apply([unsupported])[0]['disposition'], 'RETAIN_UNSUPPORTED')
        self.assertEqual(self.payload(unsupported['document']), unsupported_payload)
        self.assertEqual(self.sql('SELECT count(*) FROM storage_v2_retained_document_cache_receipt '
                                 f'WHERE document_id={unsupported["document"]}'), '0')

        corrupt, _ = self.retained('alpha beta corruption_key')
        self.sql('BEGIN;ALTER TABLE storage_v2_search_document DISABLE TRIGGER storage_v2_search_document_immutable;'
                 "UPDATE storage_v2_search_document SET fts_simple=to_tsvector('simple','wrong') "
                 f'WHERE id={corrupt["document"]};'
                 'ALTER TABLE storage_v2_search_document ENABLE TRIGGER storage_v2_search_document_immutable;COMMIT;')
        corrupt_payload = self.payload(corrupt['document'])
        results = self.apply([corrupt, tiny])
        self.assertEqual({r['document_id']: r['disposition'] for r in results},
                         {corrupt['document']: 'RETAIN_VECTOR_MISMATCH', tiny['document']: 'RETAIN_NO_SAVINGS'})
        self.assertEqual(self.payload(corrupt['document']), corrupt_payload)
        self.assertEqual(self.payload(tiny['document']), tiny_payload)

        rollback, _ = self.retained(text + ' rollback_key')
        rollback_original = self.payload(rollback['document'])
        self.assert_sql_fails(self.operator(self.call([rollback]) + ';SELECT 1/0'), 'division by zero')
        self.assertEqual(self.payload(rollback['document']), rollback_original)
        self.assertEqual(self.sql('SELECT count(*) FROM storage_v2_retained_document_cache_receipt '
                                 f'WHERE document_id={rollback["document"]}'), '0')
        self.assertEqual(self.sql('SELECT count(*) FROM storage_v2_document_cache_conversion_receipt '
                                 f'WHERE document_id={rollback["document"]}'), '0')

        # Hold a real source-writer advisory lock in a separate owned backend.
        marker = 'cache166-writer-' + uuid.uuid4().hex
        result_holder = []
        def hold_writer():
            result_holder.append(self.command('--command',
                f"SELECT set_config('application_name','{marker}',FALSE);"
                "SELECT pg_advisory_lock(hashtextextended('mainrag.storage-v2-ingest-source:"
                f"{rollback['source']}',0));SELECT pg_sleep(60)", check=False))
        thread = threading.Thread(target=hold_writer, daemon=True)
        thread.start()
        writer_pid = None
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                candidate = self.sql("SELECT a.pid FROM pg_stat_activity a WHERE a.datname=current_database() "
                    f"AND a.application_name='{marker}' AND EXISTS(SELECT 1 FROM pg_locks l WHERE l.pid=a.pid "
                    "AND l.locktype='advisory' AND l.granted) LIMIT 1")
                if candidate:
                    writer_pid = int(candidate)
                    break
                time.sleep(0.02)
            self.assertIsNotNone(writer_pid, 'owned writer backend never acquired its actual lease')
            self.assert_sql_fails(self.operator(self.call([rollback])), 'quiescent frozen-run maintenance')
        finally:
            self.sql("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname=current_database() "
                     f"AND application_name='{marker}' AND pid<>pg_backend_pid()")
            thread.join(timeout=5)
        self.assertFalse(thread.is_alive(), 'owned fixture writer backend did not stop')

        # The actual admitted building run, foreign run/hash and unchanged old
        # six-argument161 contract live in one deliberately rolled-back fixture
        # transaction. No invented terminal run status is used for cleanup.
        begin = (f"SELECT (storage_v2_begin_shadow_ingest({rollback['source']},repeat('ab',32),repeat('cd',32),"
                 "'cache166-pause','synthetic-snapshot','{}'::JSONB,FALSE)).id")
        parameters = (f"'{self.operation}',decode('{self.manifest}','hex'),ARRAY[{rollback['document']}]::BIGINT[],"
                      f"ARRAY[decode('{rollback['identity']}','hex')],8388608,10000")
        self.sql(self.operator(f"""DO $inert$
DECLARE run_id BIGINT; frozen BYTEA; converted RECORD; before_run BYTEA; rejected BOOLEAN; args TEXT; other_run BIGINT;
BEGIN
 run_id:=({begin});frozen:=storage_v2_retained_posting_run_sha256(run_id);before_run:=frozen;
 BEGIN
  PERFORM * FROM storage_v2_convert_document_caches({parameters});
  RAISE EXCEPTION 'old161 admitted an unfinished run';
 EXCEPTION WHEN raise_exception THEN
  IF SQLERRM NOT LIKE '%writer-free controlled maintenance%' THEN RAISE;END IF;
 END;
 FOR args IN SELECT value FROM unnest(ARRAY[
   ','||run_id::TEXT||',decode(repeat(''00'',32),''hex'')',
   ',999999999::BIGINT,decode('''||encode(frozen,'hex')||''',''hex'')',
   ',NULL::BIGINT,NULL::BYTEA'
 ]) value LOOP
  rejected:=FALSE;
  BEGIN
   EXECUTE 'SELECT * FROM storage_v2_convert_retained_document_caches('||
      {self.quote(parameters)}||args||')' INTO converted;
  EXCEPTION WHEN raise_exception THEN
   IF SQLERRM NOT LIKE '%quiescent frozen-run maintenance%' THEN RAISE;END IF;rejected:=TRUE;
  END;
  IF NOT rejected THEN RAISE EXCEPTION 'foreign/unbound frozen run admitted';END IF;
 END LOOP;
 BEGIN
  other_run:=(SELECT (storage_v2_begin_shadow_ingest({fallback['source']},repeat('de',32),repeat('ef',32),
       'cache166-other-building','synthetic-snapshot','{{}}'::JSONB,FALSE)).id);
  rejected:=FALSE;
  BEGIN
   PERFORM * FROM storage_v2_convert_retained_document_caches({parameters},run_id,frozen);
  EXCEPTION WHEN raise_exception THEN
   IF SQLERRM NOT LIKE '%quiescent frozen-run maintenance%' THEN RAISE;END IF;rejected:=TRUE;
  END;
  IF NOT rejected THEN RAISE EXCEPTION 'second actual building run admitted';END IF;
  RAISE EXCEPTION 'roll back owned second run' USING ERRCODE='P1661';
 EXCEPTION WHEN SQLSTATE 'P1661' THEN NULL;
 END;
 SELECT * INTO converted FROM storage_v2_convert_retained_document_caches({parameters},run_id,frozen);
 IF converted.disposition<>'CONVERTED' THEN RAISE EXCEPTION 'actual frozen run cache was not converted';END IF;
 IF storage_v2_retained_posting_run_sha256(run_id) IS DISTINCT FROM before_run THEN
  RAISE EXCEPTION 'frozen run/generation state changed';END IF;
 SELECT * INTO converted FROM storage_v2_convert_retained_document_caches({parameters},run_id,frozen);
 IF converted.disposition<>'REPLAY' THEN RAISE EXCEPTION 'exact frozen-run replay failed';END IF;
END $inert$""").removesuffix('COMMIT;') + 'ROLLBACK;')
        self.assertEqual(self.payload(rollback['document']), rollback_original)
        self.assertEqual(self.old_definition, self.sql(f"SELECT pg_get_functiondef('{OLD}'::REGPROCEDURE)"))


if __name__ == '__main__':
    unittest.main()
